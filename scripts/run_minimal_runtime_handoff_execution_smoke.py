#!/usr/bin/env python3
"""Run the Level 6A minimal runtime handoff execution hardening smoke.

This smoke seeds a queued task into an isolated workspace, walks the
scheduler proposal -> confirmation -> verifier report -> intake runner
handoff chain, then runs runtime preflight and invokes a fake
approved_task_runner under explicit operator confirmation. It records
runtime audit evidence and asserts that no approval, merge, cleanup,
GitHub mutation, scheduler loop, background worker, or automatic task
picking occurred.
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
    create_intake_runner_handoff_from_verifier_report,
)
from agent_taskflow.models import TaskRecord  # noqa: E402
from agent_taskflow.runtime_handoff_execution_from_handoff import (  # noqa: E402
    RUNTIME_EXECUTION_ARTIFACT_TYPE,
    RUNTIME_FINISHED_EVENT_TYPE,
    RUNTIME_PREFLIGHT_EVENT_TYPE,
    RUNTIME_STARTED_EVENT_TYPE,
    RuntimeHandoffExecutionRequest,
    check_runtime_handoff_preflight,
    run_runtime_handoff_execution_from_handoff,
)
from agent_taskflow.scheduler_candidate_proposals import (  # noqa: E402
    SchedulerCandidateProposalRequest,
    create_scheduler_proposal_from_candidate,
)
from agent_taskflow.scheduler_confirmation_from_proposal import (  # noqa: E402
    SchedulerConfirmationFromProposalRequest,
    create_scheduler_confirmation_from_proposal,
)
from agent_taskflow.scheduler_confirmation_verifier_report import (  # noqa: E402
    SchedulerConfirmationVerifierReportRequest,
    create_scheduler_confirmation_verifier_report,
)
from agent_taskflow.store import TaskMirrorStore  # noqa: E402
from agent_taskflow.tasks import normalize_task_key  # noqa: E402


DEFAULT_TASK_KEY = "AT-L6A-RUNTIME-SMOKE"
DEFAULT_PROJECT = "agent-taskflow"
EXPECTED_COMMAND_KIND = "create_task_execution_package"
SMOKE_OPERATOR = "level-6a-smoke"
SMOKE_OPERATOR_NOTE = "Level 6A minimal runtime handoff execution smoke"

FORBIDDEN_ARTIFACT_TYPES = (
    "approval_decision",
    "merge_recorded",
    "cleanup",
)

FORBIDDEN_EVENT_TYPES = (
    "approval_decision",
    "merge_recorded",
    "cleanup",
)

FORBIDDEN_PAYLOAD_MARKERS = (
    '"approved": true',
    '"merged": true',
    '"cleanup_performed": true',
    '"github_mutated": true',
    '"background_worker_started": true',
    '"scheduler_loop_started": true',
    '"automatic_task_picking_started": true',
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


def _forbidden_side_effect_counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        artifact_placeholders = ",".join("?" for _ in FORBIDDEN_ARTIFACT_TYPES)
        event_placeholders = ",".join("?" for _ in FORBIDDEN_EVENT_TYPES)
        artifacts = conn.execute(
            f"SELECT COUNT(*) FROM task_artifacts WHERE artifact_type IN ({artifact_placeholders})",
            FORBIDDEN_ARTIFACT_TYPES,
        ).fetchone()[0]
        events = conn.execute(
            f"SELECT COUNT(*) FROM task_events WHERE event_type IN ({event_placeholders})",
            FORBIDDEN_EVENT_TYPES,
        ).fetchone()[0]
        payload_rows = conn.execute(
            "SELECT payload_json FROM task_events WHERE payload_json IS NOT NULL"
        ).fetchall()
    markers = sum(
        sum(1 for marker in FORBIDDEN_PAYLOAD_MARKERS if marker in row[0])
        for row in payload_rows
    )
    return {"artifacts": artifacts, "events": events, "payload_markers": markers}


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
            title="Runtime handoff execution hardening smoke",
            status="queued",
            repo_path=repo_path,
            artifact_dir=artifact_dir,
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-01T00:00:00Z",
        )
    )


class _FakeApprovedTaskRunner:
    """Stable fake approved_task_runner used by the Level 6A smoke."""

    def __init__(self) -> None:
        self.call_count = 0
        self.last_kwargs: dict[str, Any] | None = None

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        self.last_kwargs = kwargs
        return {
            "ok": True,
            "status": "completed",
            "phase": "fake-approved-runner",
            "summary": "fake runner completed for Level 6A smoke",
            "artifacts": {},
            "safety": {
                "executor_started": False,
                "validators_started": False,
                "github_mutated": False,
                "branch_pushed": False,
                "pr_created": False,
                "merged": False,
                "approved": False,
                "cleanup_performed": False,
                "background_worker_started": False,
            },
        }


def run_smoke(
    *,
    workspace_root: Path,
    task_key: str = DEFAULT_TASK_KEY,
    project: str = DEFAULT_PROJECT,
) -> dict[str, Any]:
    """Run the smoke against an isolated workspace and return a summary."""

    normalized_task_key = normalize_task_key(task_key)
    workspace_root = _require_absolute_path(workspace_root, "workspace_root")
    db_path = workspace_root / "runtime-handoff-execution-smoke.db"
    repo_path = workspace_root / "repo"
    artifact_root = workspace_root / "artifacts"
    artifact_dir = artifact_root / normalized_task_key

    workspace_root.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        raise SmokeFailure(f"isolated smoke DB already exists: {db_path}")
    if artifact_root.exists() and any(artifact_root.iterdir()):
        raise SmokeFailure(f"isolated artifact root is not empty: {artifact_root}")
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
    _require(proposal_result.get("ok") is True, f"proposal creation not ok: {proposal_result!r}")
    proposal = proposal_result.get("proposal") or {}
    proposal_path = Path(str(proposal.get("proposal_artifact_path") or ""))

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
    _require(confirmation_result.get("ok") is True, "confirmation not ok")
    confirmation = confirmation_result.get("confirmation") or {}
    confirmation_path = Path(str(confirmation.get("artifact_path") or ""))

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
    _require(report_result.get("ok") is True, "verifier report not ok")
    verifier_report = report_result.get("verifier_report") or {}
    report_path = Path(str(verifier_report.get("artifact_path") or ""))

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
    _require(handoff_result.get("ok") is True, "handoff not ok")
    handoff = handoff_result.get("handoff") or {}
    handoff_path = Path(str(handoff.get("artifact_path") or ""))

    preflight = check_runtime_handoff_preflight(
        RuntimeHandoffExecutionRequest(
            db_path=db_path,
            artifact_root=artifact_root,
            task_key=normalized_task_key,
            handoff_id=handoff["handoff_id"],
            verifier_report_id=handoff["verifier_report_id"],
            confirmation_id=handoff["confirmation_id"],
            proposal_hash=handoff["proposal_hash"],
            proposal_item_id=handoff["proposal_item_id"],
            item_hash=handoff["item_hash"],
            recommended_command_kind=handoff["recommended_command_kind"],
            handoff_artifact_path=handoff_path,
        )
    )
    _require(
        preflight.get("preflight_passed") is True,
        f"runtime preflight failed: {preflight!r}",
    )
    _require(preflight.get("reasons") == [], "runtime preflight reasons not empty")

    fake_runner = _FakeApprovedTaskRunner()
    execution_result = run_runtime_handoff_execution_from_handoff(
        RuntimeHandoffExecutionRequest(
            db_path=db_path,
            artifact_root=artifact_root,
            task_key=normalized_task_key,
            handoff_id=handoff["handoff_id"],
            verifier_report_id=handoff["verifier_report_id"],
            confirmation_id=handoff["confirmation_id"],
            proposal_hash=handoff["proposal_hash"],
            proposal_item_id=handoff["proposal_item_id"],
            item_hash=handoff["item_hash"],
            recommended_command_kind=handoff["recommended_command_kind"],
            handoff_artifact_path=handoff_path,
            dry_run=False,
            confirm_run_approved_task_runner=True,
            operator=SMOKE_OPERATOR,
            operator_note=SMOKE_OPERATOR_NOTE,
        ),
        approved_task_runner_fn=fake_runner,
    )
    _require(execution_result.get("ok") is True, f"runtime execution failed: {execution_result!r}")
    _require(fake_runner.call_count == 1, f"fake runner called {fake_runner.call_count} times")

    runtime_execution = execution_result.get("runtime_execution") or {}
    runtime_artifact_path = Path(str(runtime_execution.get("artifact_path") or ""))
    _require(runtime_artifact_path.is_file(), f"runtime artifact missing: {runtime_artifact_path}")
    runtime_payload = json.loads(runtime_artifact_path.read_text(encoding="utf-8"))
    _require(runtime_payload.get("approved_task_runner_called") is True, "runner not flagged called")
    safety = runtime_payload.get("safety") or {}
    for flag in (
        "approved",
        "merged",
        "cleanup_performed",
        "github_mutated",
        "background_worker_started",
        "scheduler_loop_started",
        "automatic_task_picking_started",
    ):
        _require(safety.get(flag) is False, f"runtime safety.{flag} not false")
    _require(
        safety.get("requires_human_review_after_runtime") is True,
        "runtime safety requires_human_review_after_runtime is not true",
    )

    runtime_audit_events = store.list_runtime_audit_events(normalized_task_key)
    _require(
        len(runtime_audit_events) == 3,
        f"expected 3 runtime audit events, got {len(runtime_audit_events)}",
    )
    kinds = [event.get("kind") for event in runtime_audit_events]
    _require(
        kinds
        == [
            RUNTIME_PREFLIGHT_EVENT_TYPE,
            RUNTIME_STARTED_EVENT_TYPE,
            RUNTIME_FINISHED_EVENT_TYPE,
        ],
        f"unexpected runtime audit event order: {kinds}",
    )
    runtime_execution_artifacts = store.list_runtime_execution_artifacts(
        normalized_task_key
    )
    _require(
        len(runtime_execution_artifacts) == 1,
        "expected one runtime_handoff_execution artifact row",
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
        "runtime_execution": {
            "runtime_execution_id": runtime_payload.get("runtime_execution_id"),
            "handoff_id": runtime_payload.get("handoff_id"),
            "verifier_report_id": runtime_payload.get("verifier_report_id"),
            "confirmation_id": runtime_payload.get("confirmation_id"),
            "proposal_hash": runtime_payload.get("proposal_hash"),
            "proposal_item_id": runtime_payload.get("proposal_item_id"),
            "item_hash": runtime_payload.get("item_hash"),
            "recommended_command_kind": runtime_payload.get(
                "recommended_command_kind"
            ),
            "artifact_path": str(runtime_artifact_path),
            "approved_task_runner_called": runtime_payload.get(
                "approved_task_runner_called"
            ),
            "runner_ok": runtime_payload.get("runner_ok"),
        },
        "preflight": {
            "preflight_passed": preflight.get("preflight_passed"),
            "reasons": list(preflight.get("reasons") or []),
            "warning_count": len(preflight.get("warnings") or []),
        },
        "readbacks": {
            "runtime_audit_event_count": len(runtime_audit_events),
            "runtime_execution_artifact_count": len(runtime_execution_artifacts),
        },
        "safety": {
            "proposal_created": True,
            "confirmation_created": True,
            "verifier_report_created": True,
            "handoff_created": True,
            "runtime_started": safety.get("runtime_started"),
            "approved_task_runner_called": safety.get("approved_task_runner_called"),
            "executor_started": safety.get("executor_started"),
            "validators_started": safety.get("validators_started"),
            "github_mutated": safety.get("github_mutated"),
            "approved": safety.get("approved"),
            "merged": safety.get("merged"),
            "cleanup_performed": safety.get("cleanup_performed"),
            "background_worker_started": safety.get("background_worker_started"),
            "scheduler_loop_started": safety.get("scheduler_loop_started"),
            "automatic_task_picking_started": safety.get(
                "automatic_task_picking_started"
            ),
            "requires_human_review_after_runtime": safety.get(
                "requires_human_review_after_runtime"
            ),
        },
        "forbidden_side_effect_counts": forbidden_counts,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Level 6A minimal runtime handoff execution hardening smoke."
    )
    parser.add_argument("--task-key", default=DEFAULT_TASK_KEY)
    parser.add_argument(
        "--workspace-root",
        help=(
            "Absolute workspace root. By default a temporary directory "
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
            workspace_root = _require_absolute_path(args.workspace_root, "workspace_root")
        else:
            workspace_root = Path(
                tempfile.mkdtemp(prefix="agent-taskflow-l6a-runtime-", dir="/tmp")
            )
            cleanup_workspace = not args.keep_workspace

        summary = run_smoke(workspace_root=workspace_root, task_key=args.task_key)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Runtime handoff execution hardening smoke failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if cleanup_workspace and workspace_root is not None:
            shutil.rmtree(workspace_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
