#!/usr/bin/env python3
"""Run the Level 6C real approved_task_runner integration smoke.

This smoke seeds a queued task into an isolated workspace, walks the full
scheduler proposal -> confirmation -> verifier report -> intake runner
handoff chain, then runs runtime preflight and invokes the REAL
approved_task_runner via an isolated fixture repo and a safe shell executor.

The approved_task_runner is called only when --confirm-real-runner is
supplied. Without that flag the script exits with a nonzero status.

The task given to approved_task_runner is safe:
  - executor: shell
  - command: ("true",)   -- POSIX no-op, always exits 0
  - validators: ("smoke-noop",) -- trivially passing noop validator
  - preflight: False -- skipped (already done by runtime handoff path)
  - isolated fixture git repo -- no writes to the main repo
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
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
from agent_taskflow.validators.base import Validator, ValidatorContext, ValidatorResult  # noqa: E402


DEFAULT_TASK_KEY = "AT-L6C-REAL-RUNNER-SMOKE"
DEFAULT_PROJECT = "agent-taskflow"
EXPECTED_COMMAND_KIND = "create_task_execution_package"
SMOKE_OPERATOR = "level-6c-smoke"
SMOKE_OPERATOR_NOTE = "Level 6C real approved_task_runner integration smoke"

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
    """Raised when the smoke violates the expected safety contract."""


def _require_absolute_path(path: str | Path, field_name: str) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        raise ValueError(f"{field_name} must be absolute: {path}")
    return resolved


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _run_git(args: list[str]) -> None:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SmokeFailure(
            f"git {' '.join(str(a) for a in args[:4])} failed: {result.stderr.strip()}"
        )


def _init_fixture_repo(repo_path: Path) -> None:
    """Initialize a minimal isolated git repo for the real runner fixture."""
    repo_path.mkdir(parents=True, exist_ok=True)
    _run_git(["init", str(repo_path)])
    (repo_path / ".git" / "HEAD").write_text(
        "ref: refs/heads/main\n", encoding="utf-8"
    )
    _run_git(["-C", str(repo_path), "config", "user.email", "smoke@example.local"])
    _run_git(["-C", str(repo_path), "config", "user.name", "smoke"])
    smoke_marker = repo_path / ".smoke-l6c-fixture"
    smoke_marker.write_text("level-6c-real-runner-smoke-fixture\n", encoding="utf-8")
    _run_git(["-C", str(repo_path), "add", ".smoke-l6c-fixture"])
    _run_git(
        ["-C", str(repo_path), "commit", "-m", "fixture: init level 6c smoke repo"]
    )


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
            title="Real approved_task_runner integration smoke",
            status="queued",
            repo_path=repo_path,
            artifact_dir=artifact_dir,
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-01T00:00:00Z",
        )
    )


class _SmokeNoopValidator(Validator):
    """Trivially-passing validator for the Level 6C real runner fixture."""

    def run(self, context: ValidatorContext) -> ValidatorResult:
        return ValidatorResult(
            validator="smoke-noop",
            status="passed",
            summary="Level 6C smoke noop validator: trivially passed for fixture run",
            exit_code=0,
        )


class _RealApprovedTaskRunnerAdapter:
    """Wraps the real run_approved_task with an isolated fixture configuration.

    This adapter is injected as the approved_task_runner_fn in
    run_runtime_handoff_execution_from_handoff. It calls the actual
    run_approved_task interface (not a fake) with:
      - executor="shell", command=("true",)  -- safe POSIX no-op
      - validators=("smoke-noop",)           -- trivially passing
      - preflight=False                      -- already done by runtime handoff
      - isolated fixture repo_path           -- no writes to the main repo
    """

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path
        self.called = False
        self.last_result: Any = None

    def __call__(
        self,
        *,
        task_key: str,
        db_path: Path,
        artifact_root: Path,
        **_ignored: Any,
    ) -> Any:
        from agent_taskflow.approved_task_runner import (
            ApprovedTaskRunRequest,
            run_approved_task,
        )

        request = ApprovedTaskRunRequest(
            task_key=task_key,
            executor="shell",
            command=("true",),
            repo_path=self.repo_path,
            db_path=Path(str(db_path)),
            artifact_root=Path(str(artifact_root)),
            base_branch="main",
            validators=("smoke-noop",),
            confirm_approved_task=True,
            preflight=False,
        )
        result = run_approved_task(
            request,
            validator_registry={"smoke-noop": _SmokeNoopValidator()},
        )
        self.called = True
        self.last_result = result
        return result


def run_smoke(
    *,
    workspace_root: Path,
    task_key: str = DEFAULT_TASK_KEY,
    project: str = DEFAULT_PROJECT,
    confirm_real_runner: bool = False,
) -> dict[str, Any]:
    """Run the smoke against an isolated workspace and return a summary.

    Returns immediately with status="confirmation_required" when
    confirm_real_runner=False.
    """

    if not confirm_real_runner:
        return {
            "ok": False,
            "status": "confirmation_required",
            "real_runner_confirmed": False,
            "real_approved_task_runner_called": False,
            "message": (
                "--confirm-real-runner is required to call the real "
                "approved_task_runner interface"
            ),
        }

    normalized_task_key = normalize_task_key(task_key)
    workspace_root = _require_absolute_path(workspace_root, "workspace_root")
    db_path = workspace_root / "real-runner-integration-smoke.db"
    fixture_repo_path = workspace_root / "fixture-repo"
    artifact_root = workspace_root / "artifacts"
    artifact_dir = artifact_root / normalized_task_key

    workspace_root.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        raise SmokeFailure(f"isolated smoke DB already exists: {db_path}")
    if artifact_root.exists() and any(artifact_root.iterdir()):
        raise SmokeFailure(f"isolated artifact root is not empty: {artifact_root}")
    artifact_root.mkdir(parents=True, exist_ok=True)

    _init_fixture_repo(fixture_repo_path)

    store = TaskMirrorStore(db_path)
    store.init_db()
    _seed_queued_task(
        store=store,
        task_key=normalized_task_key,
        project=project,
        repo_path=fixture_repo_path,
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
        f"proposal creation not ok: {proposal_result!r}",
    )
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

    real_runner = _RealApprovedTaskRunnerAdapter(repo_path=fixture_repo_path)

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
        approved_task_runner_fn=real_runner,
    )
    _require(
        real_runner.called is True,
        "real approved_task_runner adapter was not called",
    )
    _require(
        execution_result.get("ok") is True,
        f"runtime execution failed: {execution_result!r}",
    )

    runtime_execution = execution_result.get("runtime_execution") or {}
    runtime_artifact_path = Path(str(runtime_execution.get("artifact_path") or ""))
    _require(
        runtime_artifact_path.is_file(),
        f"runtime execution artifact missing: {runtime_artifact_path}",
    )
    runtime_payload = json.loads(
        runtime_artifact_path.read_text(encoding="utf-8")
    )
    _require(
        runtime_payload.get("approved_task_runner_called") is True,
        "runtime artifact: approved_task_runner_called not true",
    )
    _require(
        runtime_payload.get("runner_returned") is True,
        "runtime artifact: runner_returned not true",
    )
    _require(
        runtime_payload.get("runner_ok") is True,
        "runtime artifact: runner_ok not true",
    )

    safety = runtime_payload.get("safety") or {}
    _require(
        safety.get("github_mutated") is False,
        "runtime safety.github_mutated unexpectedly true",
    )
    for flag in (
        "approved",
        "merged",
        "cleanup_performed",
        "background_worker_started",
        "scheduler_loop_started",
        "automatic_task_picking_started",
    ):
        _require(
            safety.get(flag) is False,
            f"runtime safety.{flag} is not false",
        )
    _require(
        safety.get("requires_human_review_after_runtime") is True,
        "runtime safety.requires_human_review_after_runtime is not true",
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
        "workspace_root": str(workspace_root),
        "db_path": str(db_path),
        "artifact_root": str(artifact_root),
        "real_runner_confirmed": True,
        "real_approved_task_runner_called": True,
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
            "runner_returned": runtime_payload.get("runner_returned"),
            "runner_ok": runtime_payload.get("runner_ok"),
            "runner_status": runtime_payload.get("runner_status"),
            "runner_phase": runtime_payload.get("runner_phase"),
        },
        "readbacks": {
            "runtime_audit_event_count": len(runtime_audit_events),
            "runtime_execution_artifact_count": len(runtime_execution_artifacts),
        },
        "safety": {
            "scheduler_loop_started": safety.get("scheduler_loop_started"),
            "background_worker_started": safety.get("background_worker_started"),
            "automatic_task_picking_started": safety.get(
                "automatic_task_picking_started"
            ),
            "approved": safety.get("approved"),
            "merged": safety.get("merged"),
            "cleanup_performed": safety.get("cleanup_performed"),
            "github_mutated": safety.get("github_mutated"),
        },
        "forbidden_side_effect_counts": forbidden_counts,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Level 6C real approved_task_runner integration smoke. "
            "Requires --confirm-real-runner to call the real runner interface."
        )
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
    parser.add_argument(
        "--confirm-real-runner",
        action="store_true",
        help=(
            "Required to call the real approved_task_runner interface. "
            "Without this flag the script exits with a nonzero status."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.confirm_real_runner:
        print(
            "Error: --confirm-real-runner is required to call the real "
            "approved_task_runner interface.\n"
            "Run with --confirm-real-runner to proceed.",
            file=sys.stderr,
        )
        return 1

    cleanup_workspace = False
    workspace_root: Path | None = None
    try:
        if args.workspace_root:
            workspace_root = _require_absolute_path(
                args.workspace_root, "workspace_root"
            )
        else:
            workspace_root = Path(
                tempfile.mkdtemp(
                    prefix="agent-taskflow-l6c-real-runner-", dir="/tmp"
                )
            )
            cleanup_workspace = not args.keep_workspace

        summary = run_smoke(
            workspace_root=workspace_root,
            task_key=args.task_key,
            confirm_real_runner=True,
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            f"Real approved_task_runner integration smoke failed: {exc}",
            file=sys.stderr,
        )
        return 1
    finally:
        if cleanup_workspace and workspace_root is not None:
            shutil.rmtree(workspace_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
