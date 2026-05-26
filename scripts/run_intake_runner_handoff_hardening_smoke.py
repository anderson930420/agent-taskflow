#!/usr/bin/env python3
"""Run the Level 5A intake runner handoff hardening smoke.

This smoke proves the local minimal handoff path:
scheduler proposal evidence, K1 eligibility, explicit scheduler
confirmation evidence, read-only verifier binding, explicit
scheduler_confirmation_verifier_report evidence, read-only handoff
binding, and explicit intake_runner_handoff evidence. It does not start
runtime execution, call the approved task runner, invoke executors or
validators, mutate GitHub, approve, merge, clean up, run a scheduler
loop, start a background worker, or automatically pick tasks.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.intake_runner_handoff_from_verifier_report import (  # noqa: E402
    HANDOFF_ARTIFACT_TYPE,
    HANDOFF_EVENT_TYPE,
    IntakeRunnerHandoffFromVerifierReportRequest,
    check_intake_runner_handoff_binding,
    create_intake_runner_handoff_from_verifier_report,
)
from agent_taskflow.models import TaskRecord  # noqa: E402
from agent_taskflow.scheduler_candidate_proposals import (  # noqa: E402
    SchedulerCandidateProposalRequest,
    create_scheduler_proposal_from_candidate,
)
from agent_taskflow.scheduler_confirmation_eligibility import (  # noqa: E402
    SchedulerConfirmationEligibilityRequest,
    check_scheduler_confirmation_eligibility,
)
from agent_taskflow.scheduler_confirmation_from_proposal import (  # noqa: E402
    CONFIRMATION_ARTIFACT_TYPE,
    CONFIRMATION_EVENT_TYPE,
    SchedulerConfirmationFromProposalRequest,
    create_scheduler_confirmation_from_proposal,
)
from agent_taskflow.scheduler_confirmation_verifier_report import (  # noqa: E402
    VERIFIER_REPORT_ARTIFACT_TYPE,
    VERIFIER_REPORT_EVENT_TYPE,
    SchedulerConfirmationVerifierReportRequest,
    check_scheduler_confirmation_verifier_binding,
    create_scheduler_confirmation_verifier_report,
)
from agent_taskflow.scheduler_proposals import (  # noqa: E402
    PROPOSAL_ARTIFACT_TYPE,
    PROPOSAL_EVENT_TYPE,
)
from agent_taskflow.store import TaskMirrorStore  # noqa: E402
from agent_taskflow.tasks import normalize_task_key  # noqa: E402


DEFAULT_TASK_KEY = "AT-L5A-HANDOFF-SMOKE"
DEFAULT_PROJECT = "agent-taskflow"
EXPECTED_COMMAND_KIND = "create_task_execution_package"
SMOKE_OPERATOR = "level-5a-smoke"
SMOKE_OPERATOR_NOTE = "Level 5A intake runner handoff hardening smoke"

FORBIDDEN_ARTIFACT_TYPES = (
    "runtime_handoff_execution",
    "validation_result",
    "approval_decision",
    "merge_recorded",
    "cleanup",
)

FORBIDDEN_EVENT_TYPES = (
    "runtime_preflight_finished",
    "runtime_execution_started",
    "runtime_execution_finished",
    "executor_run_started",
    "executor_run_finished",
    "validation_result",
    "approval_decision",
    "merge_recorded",
    "cleanup",
)

FORBIDDEN_PAYLOAD_MARKERS = (
    '"approved_task_runner_called": true',
    "executor_run_started",
    "executor_run_finished",
    "validation_result",
    "runtime_execution_started",
    "runtime_execution_finished",
)


class SmokeFailure(RuntimeError):
    """Raised when the smoke path violates the expected safety contract."""


def _require_absolute_path(path: str | Path, field_name: str) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        raise ValueError(f"{field_name} must be absolute: {path}")
    return resolved


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _db_counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        return {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "artifacts": conn.execute(
                "SELECT COUNT(*) FROM task_artifacts"
            ).fetchone()[0],
            "worktrees": conn.execute(
                "SELECT COUNT(*) FROM task_worktrees"
            ).fetchone()[0],
        }


def _forbidden_side_effect_counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        artifact_placeholders = ",".join("?" for _ in FORBIDDEN_ARTIFACT_TYPES)
        event_placeholders = ",".join("?" for _ in FORBIDDEN_EVENT_TYPES)
        artifacts = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM task_artifacts
            WHERE artifact_type IN ({artifact_placeholders})
            """,
            FORBIDDEN_ARTIFACT_TYPES,
        ).fetchone()[0]
        events = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM task_events
            WHERE event_type IN ({event_placeholders})
            """,
            FORBIDDEN_EVENT_TYPES,
        ).fetchone()[0]
        payload_rows = conn.execute(
            """
            SELECT payload_json
            FROM task_events
            WHERE payload_json IS NOT NULL
            """
        ).fetchall()

    return {
        "artifacts": artifacts,
        "events": events,
        "payload_markers": sum(
            _payload_marker_hit_count(row[0]) for row in payload_rows
        ),
    }


def _payload_marker_hit_count(payload_json: str) -> int:
    return sum(1 for marker in FORBIDDEN_PAYLOAD_MARKERS if marker in payload_json)


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    _require(path.is_file(), f"{label} does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SmokeFailure(f"{label} is not valid JSON: {path}") from exc
    _require(isinstance(payload, dict), f"{label} JSON is not an object: {path}")
    return payload


def _seed_queued_task(
    *,
    store: TaskMirrorStore,
    task_key: str,
    project: str,
    repo_path: Path,
    artifact_dir: Path,
) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    store.upsert_task(
        TaskRecord(
            task_key=task_key,
            project=project,
            board=project,
            title="Intake runner handoff hardening smoke",
            status="queued",
            repo_path=repo_path,
            artifact_dir=artifact_dir,
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-01T00:00:00Z",
        )
    )


def _task_artifacts(
    store: TaskMirrorStore,
    task_key: str,
    artifact_type: str,
) -> list[Any]:
    return [
        artifact
        for artifact in store.list_task_artifacts(task_key)
        if artifact.artifact_type == artifact_type
    ]


def _task_events(
    store: TaskMirrorStore,
    task_key: str,
    event_type: str,
) -> list[Any]:
    return [
        event
        for event in store.list_task_events(task_key)
        if event.event_type == event_type
    ]


def _assert_proposal_artifact(
    *,
    artifact_path: Path,
    proposal: dict[str, Any],
    task_key: str,
) -> None:
    payload = _read_json_object(artifact_path, "scheduler proposal artifact")
    items = payload.get("items")
    _require(isinstance(items, list), "proposal artifact items is not a list")
    _require(len(items) == 1, "proposal artifact does not contain one item")
    item = items[0]
    _require(isinstance(item, dict), "proposal artifact item is not an object")
    _require(
        payload.get("proposal_hash") == proposal.get("proposal_hash"),
        "proposal hash mismatch",
    )
    _require(item.get("task_key") == task_key, "proposal task_key mismatch")
    _require(
        item.get("proposal_item_id") == proposal.get("proposal_item_id"),
        "proposal item id mismatch",
    )
    _require(
        item.get("item_hash") == proposal.get("item_hash"),
        "proposal item hash mismatch",
    )
    _require(
        item.get("recommended_command_kind") == EXPECTED_COMMAND_KIND,
        "proposal recommended command kind mismatch",
    )


def _assert_confirmation_artifact(
    *,
    artifact_path: Path,
    confirmation: dict[str, Any],
    proposal: dict[str, Any],
) -> None:
    payload = _read_json_object(artifact_path, "scheduler confirmation artifact")
    for key in (
        "confirmation_id",
        "proposal_hash",
        "proposal_item_id",
        "item_hash",
        "recommended_command_kind",
        "proposal_artifact_path",
    ):
        _require(
            payload.get(key) == confirmation.get(key),
            f"confirmation {key} mismatch",
        )
    _require(
        payload.get("proposal_hash") == proposal.get("proposal_hash"),
        "confirmation does not bind proposal hash",
    )
    for flag in (
        "not_execution_permission",
        "not_verifier_report",
        "not_handoff",
        "not_runtime",
        "requires_next_gate",
    ):
        _require(payload.get(flag) is True, f"confirmation {flag} is not true")


def _assert_verifier_report_artifact(
    *,
    artifact_path: Path,
    verifier_report: dict[str, Any],
    confirmation: dict[str, Any],
) -> None:
    payload = _read_json_object(
        artifact_path,
        "scheduler confirmation verifier report artifact",
    )
    for key in (
        "verifier_report_id",
        "confirmation_id",
        "proposal_hash",
        "proposal_item_id",
        "item_hash",
        "recommended_command_kind",
        "confirmation_artifact_path",
        "proposal_artifact_path",
        "artifact_path",
        "verification_passed",
    ):
        _require(key in payload, f"verifier report missing {key}")
    for key in (
        "verifier_report_id",
        "confirmation_id",
        "proposal_hash",
        "proposal_item_id",
        "item_hash",
        "recommended_command_kind",
    ):
        _require(
            payload.get(key) == verifier_report.get(key),
            f"verifier report {key} mismatch",
        )
    for key in (
        "confirmation_id",
        "proposal_hash",
        "proposal_item_id",
        "item_hash",
        "recommended_command_kind",
    ):
        _require(
            payload.get(key) == confirmation.get(key),
            f"verifier report does not bind confirmation {key}",
        )
    _require(payload.get("verification_passed") is True, "report did not pass")
    for flag in (
        "not_execution_permission",
        "not_handoff",
        "not_runtime",
        "requires_next_gate",
    ):
        _require(payload.get(flag) is True, f"verifier report {flag} is not true")
        _require(
            payload.get("safety", {}).get(flag) is True,
            f"verifier report safety.{flag} is not true",
        )


def _assert_handoff_artifact(
    *,
    artifact_path: Path,
    handoff: dict[str, Any],
    verifier_report: dict[str, Any],
) -> dict[str, Any]:
    payload = _read_json_object(artifact_path, "intake runner handoff artifact")
    for key in (
        "handoff_id",
        "verifier_report_id",
        "confirmation_id",
        "proposal_hash",
        "proposal_item_id",
        "item_hash",
        "recommended_command_kind",
        "verifier_report_artifact_path",
        "confirmation_artifact_path",
        "proposal_artifact_path",
        "artifact_path",
    ):
        _require(key in payload, f"handoff missing {key}")
        _require(payload.get(key) == handoff.get(key), f"handoff {key} mismatch")
    for key in (
        "verifier_report_id",
        "confirmation_id",
        "proposal_hash",
        "proposal_item_id",
        "item_hash",
        "recommended_command_kind",
    ):
        _require(
            payload.get(key) == verifier_report.get(key),
            f"handoff does not bind verifier report {key}",
        )
    for flag in (
        "not_execution_permission",
        "not_runtime",
        "requires_runtime_preflight",
        "requires_next_gate",
    ):
        _require(payload.get(flag) is True, f"handoff {flag} is not true")
        _require(
            payload.get("safety", {}).get(flag) is True,
            f"handoff safety.{flag} is not true",
        )
    for flag in (
        "runtime_started",
        "approved_task_runner_called",
        "executor_started",
        "validators_started",
        "github_mutated",
        "approved",
        "merged",
        "cleanup_performed",
        "background_worker_started",
    ):
        _require(
            payload.get("safety", {}).get(flag) is False,
            f"handoff safety.{flag} is not false",
        )
    _require(
        payload.get("approved_task_runner_called") is False,
        "handoff top-level approved_task_runner_called is not false",
    )
    _require(
        payload.get("safety", {}).get("handoff_created") is True,
        "handoff safety did not mark creation",
    )
    return payload


def _require_clean_workspace(db_path: Path, artifact_root: Path) -> None:
    if db_path.exists():
        raise SmokeFailure(f"isolated smoke DB already exists: {db_path}")
    if artifact_root.exists() and any(artifact_root.iterdir()):
        raise SmokeFailure(f"isolated artifact root is not empty: {artifact_root}")


def run_smoke(
    *,
    workspace_root: Path,
    task_key: str = DEFAULT_TASK_KEY,
    project: str = DEFAULT_PROJECT,
) -> dict[str, Any]:
    """Run the smoke against an isolated workspace root and return a summary."""

    normalized_task_key = normalize_task_key(task_key)
    workspace_root = _require_absolute_path(workspace_root, "workspace_root")
    db_path = workspace_root / "intake-runner-handoff-smoke.db"
    repo_path = workspace_root / "repo"
    artifact_root = workspace_root / "artifacts"
    artifact_dir = artifact_root / normalized_task_key

    workspace_root.mkdir(parents=True, exist_ok=True)
    _require_clean_workspace(db_path, artifact_root)
    artifact_root.mkdir(parents=True, exist_ok=True)

    store = TaskMirrorStore(db_path)
    store.init_db()
    _seed_queued_task(
        store=store,
        task_key=normalized_task_key,
        project=project,
        repo_path=repo_path,
        artifact_dir=artifact_dir,
    )

    proposal_result = create_scheduler_proposal_from_candidate(
        SchedulerCandidateProposalRequest(
            task_key=normalized_task_key,
            db_path=db_path,
            artifact_root=artifact_root,
            dry_run=False,
            confirm_create_proposal=True,
            expected_status="queued",
            expected_recommended_command_kind=EXPECTED_COMMAND_KIND,
        )
    )
    _require(
        proposal_result.get("ok") is True,
        f"proposal creation was not ok: {proposal_result!r}",
    )
    _require(proposal_result.get("status") == "created", "proposal was not created")
    proposal_safety = proposal_result.get("safety")
    _require(isinstance(proposal_safety, dict), "proposal safety missing")
    _require(
        proposal_safety.get("proposal_created") is True,
        "proposal not marked created",
    )
    proposal = proposal_result.get("proposal")
    _require(isinstance(proposal, dict), "proposal summary missing")
    proposal_path = Path(str(proposal.get("proposal_artifact_path") or ""))
    _assert_proposal_artifact(
        artifact_path=proposal_path,
        proposal=proposal,
        task_key=normalized_task_key,
    )
    _require(
        len(_task_artifacts(store, normalized_task_key, PROPOSAL_ARTIFACT_TYPE)) == 1,
        "expected one scheduler_proposal artifact",
    )
    _require(
        len(_task_events(store, normalized_task_key, PROPOSAL_EVENT_TYPE)) == 1,
        "expected one scheduler_proposal_created event",
    )

    eligibility_before = _db_counts(db_path)
    eligibility = check_scheduler_confirmation_eligibility(
        SchedulerConfirmationEligibilityRequest(
            db_path=db_path,
            task_key=normalized_task_key,
            proposal_item_id=proposal["proposal_item_id"],
            proposal_hash=proposal["proposal_hash"],
            proposal_id=proposal["proposal_id"],
            item_hash=proposal["item_hash"],
            recommended_command_kind=proposal["recommended_command_kind"],
            expected_status="queued",
            proposal_artifact_path=proposal_path,
        )
    )
    _require(_db_counts(db_path) == eligibility_before, "K1 eligibility mutated DB")
    _require(
        eligibility.get("eligible") is True,
        f"proposal item not eligible: {eligibility!r}",
    )
    _require(eligibility.get("reasons") == [], "eligibility reasons not empty")

    confirmation_result = create_scheduler_confirmation_from_proposal(
        SchedulerConfirmationFromProposalRequest(
            db_path=db_path,
            artifact_root=artifact_root,
            task_key=normalized_task_key,
            proposal_item_id=proposal["proposal_item_id"],
            proposal_hash=proposal["proposal_hash"],
            proposal_id=proposal["proposal_id"],
            item_hash=proposal["item_hash"],
            recommended_command_kind=proposal["recommended_command_kind"],
            expected_status="queued",
            proposal_artifact_path=proposal_path,
            dry_run=False,
            confirm_create_confirmation=True,
            operator=SMOKE_OPERATOR,
            operator_note=SMOKE_OPERATOR_NOTE,
        )
    )
    _require(
        confirmation_result.get("ok") is True,
        f"confirmation creation was not ok: {confirmation_result!r}",
    )
    _require(
        confirmation_result.get("status") == "created",
        "confirmation was not created",
    )
    confirmation_safety = confirmation_result.get("safety")
    _require(isinstance(confirmation_safety, dict), "confirmation safety missing")
    _require(
        confirmation_safety.get("confirmation_created") is True,
        "confirmation not marked created",
    )
    confirmation = confirmation_result.get("confirmation")
    _require(isinstance(confirmation, dict), "confirmation summary missing")
    confirmation_path = Path(str(confirmation.get("artifact_path") or ""))
    _assert_confirmation_artifact(
        artifact_path=confirmation_path,
        confirmation=confirmation,
        proposal=proposal,
    )
    _require(
        len(_task_artifacts(store, normalized_task_key, CONFIRMATION_ARTIFACT_TYPE))
        == 1,
        "expected one scheduler_confirmation artifact",
    )
    _require(
        len(_task_events(store, normalized_task_key, CONFIRMATION_EVENT_TYPE)) == 1,
        "expected one scheduler_confirmation_created event",
    )

    verifier_binding_before = _db_counts(db_path)
    verifier_binding = check_scheduler_confirmation_verifier_binding(
        SchedulerConfirmationVerifierReportRequest(
            db_path=db_path,
            artifact_root=artifact_root,
            task_key=normalized_task_key,
            confirmation_id=confirmation["confirmation_id"],
            proposal_hash=confirmation["proposal_hash"],
            proposal_item_id=confirmation["proposal_item_id"],
            item_hash=confirmation["item_hash"],
            recommended_command_kind=confirmation["recommended_command_kind"],
            confirmation_artifact_path=confirmation_path,
        )
    )
    _require(
        _db_counts(db_path) == verifier_binding_before,
        "verifier binding mutated DB",
    )
    _require(
        verifier_binding.get("verification_passed") is True,
        f"verifier binding failed: {verifier_binding!r}",
    )
    _require(verifier_binding.get("reasons") == [], "verifier reasons not empty")

    report_result = create_scheduler_confirmation_verifier_report(
        SchedulerConfirmationVerifierReportRequest(
            db_path=db_path,
            artifact_root=artifact_root,
            task_key=normalized_task_key,
            confirmation_id=confirmation["confirmation_id"],
            proposal_hash=confirmation["proposal_hash"],
            proposal_item_id=confirmation["proposal_item_id"],
            item_hash=confirmation["item_hash"],
            recommended_command_kind=confirmation["recommended_command_kind"],
            confirmation_artifact_path=confirmation_path,
            dry_run=False,
            confirm_create_verifier_report=True,
            operator=SMOKE_OPERATOR,
            operator_note=SMOKE_OPERATOR_NOTE,
        )
    )
    _require(
        report_result.get("ok") is True,
        f"verifier report creation was not ok: {report_result!r}",
    )
    _require(
        report_result.get("status") == "created",
        "verifier report was not created",
    )
    report_safety = report_result.get("safety")
    _require(isinstance(report_safety, dict), "verifier report safety missing")
    _require(
        report_safety.get("verifier_report_created") is True,
        "verifier report not marked created",
    )
    verifier_report = report_result.get("verifier_report")
    _require(isinstance(verifier_report, dict), "verifier report summary missing")
    report_path = Path(str(verifier_report.get("artifact_path") or ""))
    _assert_verifier_report_artifact(
        artifact_path=report_path,
        verifier_report=verifier_report,
        confirmation=confirmation,
    )
    _require(
        len(_task_artifacts(store, normalized_task_key, VERIFIER_REPORT_ARTIFACT_TYPE))
        == 1,
        "expected one scheduler_confirmation_verifier_report artifact",
    )
    _require(
        len(_task_events(store, normalized_task_key, VERIFIER_REPORT_EVENT_TYPE)) == 1,
        "expected one scheduler_confirmation_verifier_report_created event",
    )

    handoff_binding_before = _db_counts(db_path)
    handoff_binding = check_intake_runner_handoff_binding(
        IntakeRunnerHandoffFromVerifierReportRequest(
            db_path=db_path,
            artifact_root=artifact_root,
            task_key=normalized_task_key,
            verifier_report_id=verifier_report["verifier_report_id"],
            confirmation_id=verifier_report["confirmation_id"],
            proposal_hash=verifier_report["proposal_hash"],
            proposal_item_id=verifier_report["proposal_item_id"],
            item_hash=verifier_report["item_hash"],
            recommended_command_kind=verifier_report["recommended_command_kind"],
            verifier_report_artifact_path=report_path,
        )
    )
    _require(
        _db_counts(db_path) == handoff_binding_before,
        "handoff binding mutated DB",
    )
    _require(
        handoff_binding.get("handoff_allowed") is True,
        f"handoff binding failed: {handoff_binding!r}",
    )
    _require(handoff_binding.get("reasons") == [], "handoff reasons not empty")
    handoff_warnings = handoff_binding.get("warnings") or []
    _require(isinstance(handoff_warnings, list), "handoff warnings is not a list")
    _require(
        len(handoff_warnings) == 0,
        f"handoff warnings not empty: {handoff_warnings!r}",
    )

    handoff_result = create_intake_runner_handoff_from_verifier_report(
        IntakeRunnerHandoffFromVerifierReportRequest(
            db_path=db_path,
            artifact_root=artifact_root,
            task_key=normalized_task_key,
            verifier_report_id=verifier_report["verifier_report_id"],
            confirmation_id=verifier_report["confirmation_id"],
            proposal_hash=verifier_report["proposal_hash"],
            proposal_item_id=verifier_report["proposal_item_id"],
            item_hash=verifier_report["item_hash"],
            recommended_command_kind=verifier_report["recommended_command_kind"],
            verifier_report_artifact_path=report_path,
            dry_run=False,
            confirm_create_handoff=True,
            operator=SMOKE_OPERATOR,
            operator_note=SMOKE_OPERATOR_NOTE,
        )
    )
    _require(
        handoff_result.get("ok") is True,
        f"handoff creation was not ok: {handoff_result!r}",
    )
    _require(handoff_result.get("status") == "created", "handoff was not created")
    handoff_safety = handoff_result.get("safety")
    _require(isinstance(handoff_safety, dict), "handoff safety missing")
    _require(
        handoff_safety.get("handoff_created") is True,
        "handoff not marked created",
    )
    handoff = handoff_result.get("handoff")
    _require(isinstance(handoff, dict), "handoff summary missing")
    handoff_path = Path(str(handoff.get("artifact_path") or ""))
    _assert_handoff_artifact(
        artifact_path=handoff_path,
        handoff=handoff,
        verifier_report=verifier_report,
    )
    _require(
        len(_task_artifacts(store, normalized_task_key, HANDOFF_ARTIFACT_TYPE)) == 1,
        "expected one intake_runner_handoff artifact",
    )
    _require(
        len(_task_events(store, normalized_task_key, HANDOFF_EVENT_TYPE)) == 1,
        "expected one intake_runner_handoff_created event",
    )

    forbidden_counts = _forbidden_side_effect_counts(db_path)
    _require(
        forbidden_counts == {"artifacts": 0, "events": 0, "payload_markers": 0},
        f"forbidden side effects found: {forbidden_counts}",
    )

    return {
        "ok": True,
        "task_key": normalized_task_key,
        "db_path": str(db_path),
        "workspace_root": str(workspace_root),
        "artifact_root": str(artifact_root),
        "proposal": {
            "proposal_id": proposal.get("proposal_id"),
            "proposal_hash": proposal.get("proposal_hash"),
            "proposal_item_id": proposal.get("proposal_item_id"),
            "item_hash": proposal.get("item_hash"),
            "recommended_command_kind": proposal.get("recommended_command_kind"),
            "artifact_path": str(proposal_path),
        },
        "confirmation": {
            "confirmation_id": confirmation.get("confirmation_id"),
            "proposal_hash": confirmation.get("proposal_hash"),
            "proposal_item_id": confirmation.get("proposal_item_id"),
            "item_hash": confirmation.get("item_hash"),
            "recommended_command_kind": confirmation.get("recommended_command_kind"),
            "artifact_path": str(confirmation_path),
        },
        "verifier_report": {
            "verifier_report_id": verifier_report.get("verifier_report_id"),
            "confirmation_id": verifier_report.get("confirmation_id"),
            "proposal_hash": verifier_report.get("proposal_hash"),
            "proposal_item_id": verifier_report.get("proposal_item_id"),
            "item_hash": verifier_report.get("item_hash"),
            "recommended_command_kind": verifier_report.get(
                "recommended_command_kind"
            ),
            "artifact_path": str(report_path),
        },
        "handoff": {
            "handoff_id": handoff.get("handoff_id"),
            "verifier_report_id": handoff.get("verifier_report_id"),
            "confirmation_id": handoff.get("confirmation_id"),
            "proposal_hash": handoff.get("proposal_hash"),
            "proposal_item_id": handoff.get("proposal_item_id"),
            "item_hash": handoff.get("item_hash"),
            "recommended_command_kind": handoff.get("recommended_command_kind"),
            "artifact_path": str(handoff_path),
        },
        "binding": {
            "handoff_allowed": handoff_binding.get("handoff_allowed"),
            "reasons": list(handoff_binding.get("reasons") or []),
            "warning_count": len(handoff_warnings),
        },
        "safety": {
            "proposal_created": proposal_safety.get("proposal_created"),
            "confirmation_created": confirmation_safety.get("confirmation_created"),
            "verifier_report_created": report_safety.get("verifier_report_created"),
            "handoff_created": handoff_safety.get("handoff_created"),
            "runtime_started": handoff_safety.get("runtime_started"),
            "approved_task_runner_called": handoff_safety.get(
                "approved_task_runner_called"
            ),
            "executor_started": handoff_safety.get("executor_started"),
            "validators_started": handoff_safety.get("validators_started"),
            "github_mutated": handoff_safety.get("github_mutated"),
            "approved": handoff_safety.get("approved"),
            "merged": handoff_safety.get("merged"),
            "cleanup_performed": handoff_safety.get("cleanup_performed"),
            "background_worker_started": handoff_safety.get(
                "background_worker_started"
            ),
            "not_execution_permission": handoff_safety.get(
                "not_execution_permission"
            ),
            "requires_runtime_preflight": handoff_safety.get(
                "requires_runtime_preflight"
            ),
            "requires_next_gate": handoff_safety.get("requires_next_gate"),
        },
        "forbidden_side_effect_counts": forbidden_counts,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Level 5A intake runner handoff hardening smoke.",
    )
    parser.add_argument(
        "--task-key",
        default=DEFAULT_TASK_KEY,
        help=f"Task key to use. Default: {DEFAULT_TASK_KEY}",
    )
    parser.add_argument(
        "--workspace-root",
        help=(
            "Absolute workspace root to use. By default a temporary directory "
            "under /tmp is created and removed after the run."
        ),
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Keep the auto-created temporary workspace after the run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    cleanup_workspace = False
    workspace_root: Path | None = None
    try:
        if args.workspace_root:
            workspace_root = _require_absolute_path(
                args.workspace_root,
                "workspace_root",
            )
        else:
            workspace_root = Path(
                tempfile.mkdtemp(
                    prefix="agent-taskflow-l5a-handoff-",
                    dir="/tmp",
                )
            )
            cleanup_workspace = not args.keep_workspace

        summary = run_smoke(
            workspace_root=workspace_root,
            task_key=args.task_key,
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Intake runner handoff hardening smoke failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if cleanup_workspace and workspace_root is not None:
            shutil.rmtree(workspace_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
