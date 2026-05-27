#!/usr/bin/env python3
"""Run the Level 7A one-shot task pipeline hardening smoke.

This smoke seeds one queued task into an isolated workspace and then runs
the explicit operator-gated one-shot pipeline end-to-end with a fake
approved_task_runner injection. It asserts that every expected piece of
audit evidence is produced, that the fake runner is invoked exactly once,
that the final task status is ``waiting_approval``, and that no
forbidden side effect (approval, merge, cleanup, GitHub mutation,
scheduler loop, background worker, automatic task picking) occurred.
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
)
from agent_taskflow.models import TaskRecord  # noqa: E402
from agent_taskflow.one_shot_task_pipeline import (  # noqa: E402
    OneShotTaskPipelineRequest,
    run_one_shot_task_pipeline,
)
from agent_taskflow.runtime_handoff_execution_from_handoff import (  # noqa: E402
    RUNTIME_EXECUTION_ARTIFACT_TYPE,
    RUNTIME_FINISHED_EVENT_TYPE,
    RUNTIME_PREFLIGHT_EVENT_TYPE,
    RUNTIME_STARTED_EVENT_TYPE,
)
from agent_taskflow.scheduler_confirmation_from_proposal import (  # noqa: E402
    CONFIRMATION_ARTIFACT_TYPE,
)
from agent_taskflow.scheduler_confirmation_verifier_report import (  # noqa: E402
    VERIFIER_REPORT_ARTIFACT_TYPE,
)
from agent_taskflow.scheduler_proposals import PROPOSAL_ARTIFACT_TYPE  # noqa: E402
from agent_taskflow.store import TaskMirrorStore  # noqa: E402
from agent_taskflow.tasks import normalize_task_key  # noqa: E402


DEFAULT_TASK_KEY = "AT-L7A-ONE-SHOT-SMOKE"
DEFAULT_PROJECT = "agent-taskflow"
SMOKE_OPERATOR = "level-7a-smoke"
SMOKE_OPERATOR_NOTE = "Level 7A one-shot task pipeline smoke"

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
            title="One-shot task pipeline hardening smoke",
            status="queued",
            repo_path=repo_path,
            artifact_dir=artifact_dir,
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-01T00:00:00Z",
        )
    )


class _FakeOneShotApprovedTaskRunner:
    """Fake approved_task_runner used by the Level 7A one-shot smoke.

    Updates the task status to ``waiting_approval`` in the local mirror
    so the one-shot pipeline's final task readback matches the
    documented Level 7A goal. The fake never starts a real executor,
    never calls validators, and never mutates GitHub.
    """

    def __init__(self) -> None:
        self.call_count = 0
        self.last_kwargs: dict[str, Any] | None = None

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        self.last_kwargs = kwargs
        db_path = kwargs.get("db_path")
        task_key = kwargs.get("task_key")
        if db_path is not None and task_key is not None:
            store = TaskMirrorStore(Path(str(db_path)))
            store.update_task_status(
                str(task_key),
                "waiting_approval",
                source="fake-one-shot-runner",
                message="fake one-shot runner completed (audit evidence only)",
            )
        return {
            "ok": True,
            "status": "waiting_approval",
            "phase": "fake-one-shot-runner",
            "summary": "fake one-shot runner completed",
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


def _count_artifacts(store: TaskMirrorStore, task_key: str, artifact_type: str) -> int:
    return sum(
        1
        for artifact in store.list_task_artifacts(task_key)
        if artifact.artifact_type == artifact_type
    )


def _count_events(store: TaskMirrorStore, task_key: str, event_type: str) -> int:
    return sum(
        1 for event in store.list_task_events(task_key) if event.event_type == event_type
    )


def run_smoke(
    *,
    workspace_root: Path,
    task_key: str = DEFAULT_TASK_KEY,
    project: str = DEFAULT_PROJECT,
) -> dict[str, Any]:
    """Run the smoke against an isolated workspace and return a summary."""

    normalized_task_key = normalize_task_key(task_key)
    workspace_root = _require_absolute_path(workspace_root, "workspace_root")
    db_path = workspace_root / "one-shot-task-pipeline-smoke.db"
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

    fake_runner = _FakeOneShotApprovedTaskRunner()
    request = OneShotTaskPipelineRequest(
        db_path=db_path,
        artifact_root=artifact_root,
        task_key=normalized_task_key,
        dry_run=False,
        confirm_run_one_shot_pipeline=True,
        operator=SMOKE_OPERATOR,
        operator_note=SMOKE_OPERATOR_NOTE,
        proposal_max_items=1,
    )
    result = run_one_shot_task_pipeline(
        request,
        approved_task_runner_fn=fake_runner,
    )
    _require(result.get("ok") is True, f"one-shot pipeline not ok: {result!r}")
    _require(
        fake_runner.call_count == 1,
        f"fake runner called {fake_runner.call_count} times",
    )
    _require(
        result.get("final_task_status") == "waiting_approval",
        f"final task status not waiting_approval: {result.get('final_task_status')!r}",
    )

    stages = result.get("stages") or {}
    proposal_stage = stages.get("proposal") or {}
    confirmation_stage = stages.get("confirmation") or {}
    verifier_stage = stages.get("verifier_report") or {}
    handoff_stage = stages.get("handoff") or {}
    runtime_stage = stages.get("runtime_execution") or {}

    _require(proposal_stage.get("created") is True, "proposal stage not created")
    _require(
        confirmation_stage.get("created") is True, "confirmation stage not created"
    )
    _require(verifier_stage.get("created") is True, "verifier_report stage not created")
    _require(handoff_stage.get("created") is True, "handoff stage not created")
    _require(
        runtime_stage.get("created") is True, "runtime_execution stage not created"
    )
    _require(
        runtime_stage.get("approved_task_runner_called") is True,
        "runner stage approved_task_runner_called not true",
    )
    _require(
        runtime_stage.get("runner_status") == "waiting_approval",
        f"runner_status mismatch: {runtime_stage.get('runner_status')!r}",
    )

    evidence_counts = {
        "scheduler_proposal": _count_artifacts(
            store, normalized_task_key, PROPOSAL_ARTIFACT_TYPE
        ),
        "scheduler_confirmation": _count_artifacts(
            store, normalized_task_key, CONFIRMATION_ARTIFACT_TYPE
        ),
        "scheduler_confirmation_verifier_report": _count_artifacts(
            store, normalized_task_key, VERIFIER_REPORT_ARTIFACT_TYPE
        ),
        "intake_runner_handoff": _count_artifacts(
            store, normalized_task_key, HANDOFF_ARTIFACT_TYPE
        ),
        "runtime_handoff_execution": _count_artifacts(
            store, normalized_task_key, RUNTIME_EXECUTION_ARTIFACT_TYPE
        ),
        "runtime_audit_events": (
            _count_events(store, normalized_task_key, RUNTIME_PREFLIGHT_EVENT_TYPE)
            + _count_events(store, normalized_task_key, RUNTIME_STARTED_EVENT_TYPE)
            + _count_events(store, normalized_task_key, RUNTIME_FINISHED_EVENT_TYPE)
        ),
    }
    _require(
        evidence_counts == {
            "scheduler_proposal": 1,
            "scheduler_confirmation": 1,
            "scheduler_confirmation_verifier_report": 1,
            "intake_runner_handoff": 1,
            "runtime_handoff_execution": 1,
            "runtime_audit_events": 3,
        },
        f"evidence_counts mismatch: {evidence_counts}",
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

    forbidden_counts = _forbidden_side_effect_counts(db_path)
    _require(
        forbidden_counts == {"artifacts": 0, "events": 0, "payload_markers": 0},
        f"forbidden side effects found: {forbidden_counts}",
    )

    safety = result.get("safety") or {}
    return {
        "ok": True,
        "task_key": normalized_task_key,
        "db_path": str(db_path),
        "workspace_root": str(workspace_root),
        "artifact_root": str(artifact_root),
        "final_task_status": result.get("final_task_status"),
        "stages": {
            "proposal": proposal_stage,
            "confirmation": confirmation_stage,
            "verifier_report": verifier_stage,
            "handoff": handoff_stage,
            "runtime_execution": runtime_stage,
        },
        "runner": {
            "fake_runner_called": fake_runner.call_count >= 1,
            "call_count": fake_runner.call_count,
            "runner_status": runtime_stage.get("runner_status"),
        },
        "evidence_counts": evidence_counts,
        "safety": {
            "one_task_only": safety.get("one_task_only"),
            "operator_triggered": safety.get("operator_triggered"),
            "scheduler_loop_started": safety.get("scheduler_loop_started"),
            "background_worker_started": safety.get("background_worker_started"),
            "automatic_task_picking_started": safety.get(
                "automatic_task_picking_started"
            ),
            "approved": safety.get("approved"),
            "merged": safety.get("merged"),
            "cleanup_performed": safety.get("cleanup_performed"),
            "human_review_required": safety.get("human_review_required"),
        },
        "forbidden_side_effect_counts": forbidden_counts,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Level 7A one-shot task pipeline hardening smoke."
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    cleanup_workspace = False
    workspace_root: Path | None = None
    try:
        if args.workspace_root:
            workspace_root = _require_absolute_path(
                args.workspace_root, "workspace_root"
            )
        else:
            workspace_root = Path(
                tempfile.mkdtemp(prefix="agent-taskflow-l7a-one-shot-", dir="/tmp")
            )
            cleanup_workspace = not args.keep_workspace

        summary = run_smoke(workspace_root=workspace_root, task_key=args.task_key)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            f"One-shot task pipeline hardening smoke failed: {exc}",
            file=sys.stderr,
        )
        return 1
    finally:
        if cleanup_workspace and workspace_root is not None:
            shutil.rmtree(workspace_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
