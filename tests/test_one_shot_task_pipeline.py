"""Tests for the Level 7A one-shot task pipeline core module."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.intake_runner_handoff_from_verifier_report import (
    HANDOFF_ARTIFACT_TYPE,
)
from agent_taskflow.models import TaskRecord
from agent_taskflow.one_shot_task_pipeline import (
    ONE_SHOT_PIPELINE_SAFETY_FLAGS,
    ONE_SHOT_PIPELINE_SCHEMA_VERSION,
    ONE_SHOT_PIPELINE_SOURCE,
    OneShotTaskPipelineError,
    OneShotTaskPipelineRequest,
    run_one_shot_task_pipeline,
)
from agent_taskflow.runtime_handoff_execution_from_handoff import (
    RUNTIME_EXECUTION_ARTIFACT_TYPE,
    RUNTIME_FINISHED_EVENT_TYPE,
    RUNTIME_PREFLIGHT_EVENT_TYPE,
    RUNTIME_STARTED_EVENT_TYPE,
)
from agent_taskflow.scheduler_confirmation_from_proposal import (
    CONFIRMATION_ARTIFACT_TYPE,
)
from agent_taskflow.scheduler_confirmation_verifier_report import (
    VERIFIER_REPORT_ARTIFACT_TYPE,
)
from agent_taskflow.scheduler_proposals import PROPOSAL_ARTIFACT_TYPE
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "agent_taskflow" / "one_shot_task_pipeline.py"


def _seed_queued_task(
    *,
    workspace_root: Path,
    task_key: str = "AT-L7A-CORE-TEST",
    status: str = "queued",
) -> tuple[Path, Path, TaskMirrorStore]:
    db_path = workspace_root / "state.db"
    repo_path = workspace_root / "repo"
    artifact_root = workspace_root / "artifacts"
    repo_path.mkdir(parents=True, exist_ok=True)
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_dir = artifact_root / task_key
    artifact_dir.mkdir(parents=True, exist_ok=True)

    store = TaskMirrorStore(db_path)
    store.init_db()
    store.upsert_task(
        TaskRecord(
            task_key=task_key,
            project="agent-taskflow",
            board="agent-taskflow",
            title="L7A core test task",
            status=status,
            repo_path=repo_path,
            artifact_dir=artifact_dir,
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-01T00:00:00Z",
        )
    )
    return db_path, artifact_root, store


class _FakeWaitingApprovalRunner:
    """Fake approved_task_runner that flips status to waiting_approval."""

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
                source="fake-one-shot-runner-test",
                message="fake runner completed in test",
            )
        return {
            "ok": True,
            "status": "waiting_approval",
            "phase": "fake-one-shot-runner-test",
            "summary": "fake runner completed in test",
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


class OneShotTaskPipelineCoreTests(unittest.TestCase):
    def test_dry_run_writes_nothing_and_does_not_call_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            db_path, artifact_root, store = _seed_queued_task(workspace_root=workspace)

            fake_runner = _FakeWaitingApprovalRunner()
            request = OneShotTaskPipelineRequest(
                db_path=db_path,
                artifact_root=artifact_root,
                task_key="AT-L7A-CORE-TEST",
                dry_run=True,
                confirm_run_one_shot_pipeline=False,
            )
            result = run_one_shot_task_pipeline(
                request, approved_task_runner_fn=fake_runner
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(result["mode"], "dry_run")
            self.assertEqual(result["task_key"], "AT-L7A-CORE-TEST")
            self.assertTrue(result["would_run_pipeline"])
            self.assertTrue(result["safety"]["dry_run"])
            self.assertFalse(result["safety"]["approved_task_runner_called"])
            self.assertEqual(fake_runner.call_count, 0)

            with sqlite3.connect(db_path) as conn:
                artifact_count = conn.execute(
                    "SELECT COUNT(*) FROM task_artifacts"
                ).fetchone()[0]
                event_count = conn.execute(
                    "SELECT COUNT(*) FROM task_events"
                ).fetchone()[0]
            self.assertEqual(artifact_count, 0)
            self.assertEqual(event_count, 0)

    def test_confirmed_pipeline_runs_all_stages_with_fake_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            db_path, artifact_root, store = _seed_queued_task(workspace_root=workspace)

            fake_runner = _FakeWaitingApprovalRunner()
            request = OneShotTaskPipelineRequest(
                db_path=db_path,
                artifact_root=artifact_root,
                task_key="AT-L7A-CORE-TEST",
                dry_run=False,
                confirm_run_one_shot_pipeline=True,
                operator="test-operator",
                operator_note="test note",
            )
            result = run_one_shot_task_pipeline(
                request, approved_task_runner_fn=fake_runner
            )

            self.assertTrue(result["ok"], msg=f"result: {result!r}")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["mode"], "confirmed")
            self.assertEqual(result["final_task_status"], "waiting_approval")
            self.assertEqual(fake_runner.call_count, 1)

            stages = result["stages"]
            self.assertTrue(stages["proposal"]["created"])
            self.assertIsNotNone(stages["proposal"]["proposal_id"])
            self.assertIsNotNone(stages["proposal"]["proposal_hash"])
            self.assertIsNotNone(stages["proposal"]["proposal_item_id"])
            self.assertIsNotNone(stages["proposal"]["item_hash"])
            self.assertTrue(stages["confirmation"]["created"])
            self.assertIsNotNone(stages["confirmation"]["confirmation_id"])
            self.assertTrue(stages["verifier_report"]["created"])
            self.assertIsNotNone(stages["verifier_report"]["verifier_report_id"])
            self.assertTrue(stages["handoff"]["created"])
            self.assertIsNotNone(stages["handoff"]["handoff_id"])
            self.assertTrue(stages["runtime_execution"]["created"])
            self.assertIsNotNone(stages["runtime_execution"]["runtime_execution_id"])
            self.assertTrue(
                stages["runtime_execution"]["approved_task_runner_called"]
            )
            self.assertEqual(
                stages["runtime_execution"]["runner_status"], "waiting_approval"
            )

            runtime_artifacts = [
                a
                for a in store.list_task_artifacts("AT-L7A-CORE-TEST")
                if a.artifact_type == RUNTIME_EXECUTION_ARTIFACT_TYPE
            ]
            self.assertEqual(len(runtime_artifacts), 1)
            self.assertTrue(Path(runtime_artifacts[0].path).is_file())

            audit_events = store.list_runtime_audit_events("AT-L7A-CORE-TEST")
            self.assertEqual(len(audit_events), 3)
            kinds = [event.get("kind") for event in audit_events]
            self.assertEqual(
                kinds,
                [
                    RUNTIME_PREFLIGHT_EVENT_TYPE,
                    RUNTIME_STARTED_EVENT_TYPE,
                    RUNTIME_FINISHED_EVENT_TYPE,
                ],
            )

    def test_confirmed_mode_requires_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            db_path, artifact_root, _ = _seed_queued_task(workspace_root=workspace)
            request = OneShotTaskPipelineRequest(
                db_path=db_path,
                artifact_root=artifact_root,
                task_key="AT-L7A-CORE-TEST",
                dry_run=False,
                confirm_run_one_shot_pipeline=False,
            )
            with self.assertRaises(OneShotTaskPipelineError):
                run_one_shot_task_pipeline(request)

    def test_missing_task_fails_before_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            db_path, artifact_root, _ = _seed_queued_task(workspace_root=workspace)
            fake_runner = _FakeWaitingApprovalRunner()
            request = OneShotTaskPipelineRequest(
                db_path=db_path,
                artifact_root=artifact_root,
                task_key="AT-DOES-NOT-EXIST",
                dry_run=False,
                confirm_run_one_shot_pipeline=True,
            )
            result = run_one_shot_task_pipeline(
                request, approved_task_runner_fn=fake_runner
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["failed_stage"], "proposal")
            self.assertIn("task_missing", result["reasons"])
            self.assertEqual(fake_runner.call_count, 0)

            with sqlite3.connect(db_path) as conn:
                artifact_count = conn.execute(
                    "SELECT COUNT(*) FROM task_artifacts"
                ).fetchone()[0]
            self.assertEqual(artifact_count, 0)

    def test_pipeline_stops_on_stage_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            db_path, artifact_root, store = _seed_queued_task(workspace_root=workspace)
            fake_runner = _FakeWaitingApprovalRunner()
            request = OneShotTaskPipelineRequest(
                db_path=db_path,
                artifact_root=artifact_root,
                task_key="AT-L7A-CORE-TEST",
                dry_run=False,
                confirm_run_one_shot_pipeline=True,
                recommended_command_kind="cleanup_continue",
            )
            result = run_one_shot_task_pipeline(
                request, approved_task_runner_fn=fake_runner
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["failed_stage"], "proposal")
            self.assertEqual(fake_runner.call_count, 0)

            stages = result.get("stages") or {}
            self.assertNotIn("runtime_execution", stages)
            self.assertNotIn("handoff", stages)

    def test_forbidden_side_effect_counts_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            db_path, artifact_root, store = _seed_queued_task(workspace_root=workspace)
            fake_runner = _FakeWaitingApprovalRunner()
            request = OneShotTaskPipelineRequest(
                db_path=db_path,
                artifact_root=artifact_root,
                task_key="AT-L7A-CORE-TEST",
                dry_run=False,
                confirm_run_one_shot_pipeline=True,
            )
            result = run_one_shot_task_pipeline(
                request, approved_task_runner_fn=fake_runner
            )
            self.assertTrue(result["ok"])

            forbidden_artifact_types = (
                "approval_decision",
                "merge_recorded",
                "cleanup",
            )
            forbidden_event_types = (
                "approval_decision",
                "merge_recorded",
                "cleanup",
            )
            forbidden_markers = (
                '"approved": true',
                '"merged": true',
                '"cleanup_performed": true',
                '"background_worker_started": true',
                '"scheduler_loop_started": true',
                '"automatic_task_picking_started": true',
            )
            with sqlite3.connect(db_path) as conn:
                a_count = conn.execute(
                    "SELECT COUNT(*) FROM task_artifacts WHERE artifact_type IN (?, ?, ?)",
                    forbidden_artifact_types,
                ).fetchone()[0]
                e_count = conn.execute(
                    "SELECT COUNT(*) FROM task_events WHERE event_type IN (?, ?, ?)",
                    forbidden_event_types,
                ).fetchone()[0]
                payload_rows = conn.execute(
                    "SELECT payload_json FROM task_events WHERE payload_json IS NOT NULL"
                ).fetchall()
            marker_count = sum(
                sum(1 for marker in forbidden_markers if marker in row[0])
                for row in payload_rows
            )
            self.assertEqual(a_count, 0)
            self.assertEqual(e_count, 0)
            self.assertEqual(marker_count, 0)

    def test_constants_and_safety_defaults(self) -> None:
        self.assertEqual(
            ONE_SHOT_PIPELINE_SCHEMA_VERSION, "one_shot_task_pipeline.v1"
        )
        self.assertEqual(ONE_SHOT_PIPELINE_SOURCE, "one_shot_task_pipeline")
        self.assertTrue(ONE_SHOT_PIPELINE_SAFETY_FLAGS["one_task_only"])
        self.assertTrue(ONE_SHOT_PIPELINE_SAFETY_FLAGS["operator_triggered"])
        self.assertFalse(ONE_SHOT_PIPELINE_SAFETY_FLAGS["scheduler_loop_started"])
        self.assertFalse(
            ONE_SHOT_PIPELINE_SAFETY_FLAGS["background_worker_started"]
        )
        self.assertFalse(
            ONE_SHOT_PIPELINE_SAFETY_FLAGS["automatic_task_picking_started"]
        )
        self.assertFalse(ONE_SHOT_PIPELINE_SAFETY_FLAGS["approved"])
        self.assertFalse(ONE_SHOT_PIPELINE_SAFETY_FLAGS["merged"])
        self.assertFalse(ONE_SHOT_PIPELINE_SAFETY_FLAGS["cleanup_performed"])
        self.assertTrue(ONE_SHOT_PIPELINE_SAFETY_FLAGS["human_review_required"])

    def test_request_validates_paths_and_max_items(self) -> None:
        with self.assertRaises(ValueError):
            OneShotTaskPipelineRequest(
                db_path=Path("relative.db"),
                artifact_root=Path("/tmp"),
                task_key="AT-X",
            )
        with self.assertRaises(ValueError):
            OneShotTaskPipelineRequest(
                db_path=Path("/tmp/x.db"),
                artifact_root=Path("relative"),
                task_key="AT-X",
            )
        with self.assertRaises(ValueError):
            OneShotTaskPipelineRequest(
                db_path=Path("/tmp/x.db"),
                artifact_root=Path("/tmp"),
                task_key="AT-X",
                proposal_max_items=0,
            )

    def test_source_has_no_loop_or_auto_pick_or_pr_merge_cleanup(self) -> None:
        source_text = MODULE_PATH.read_text(encoding="utf-8")
        forbidden_substrings = (
            "while True",
            "schedule.every",
            "asyncio.sleep",
            "threading.Thread",
            "Thread(",
            "subprocess.run",
            "subprocess.Popen",
            "git push",
            "gh pr create",
            "gh pr merge",
            "from agent_taskflow.branch_push",
            "from agent_taskflow.draft_pr",
            "from agent_taskflow.local_cleanup_confirm",
            "from agent_taskflow.remote_branch_cleanup_confirm",
            "from agent_taskflow.task_closeout_confirm",
            "from agent_taskflow.github_issue",
            "from agent_taskflow.post_merge_cleanup",
            "from agent_taskflow.api",
            "from agent_taskflow.dispatcher",
            "from agent_taskflow.executors",
            "from agent_taskflow.validators",
        )
        for needle in forbidden_substrings:
            self.assertNotIn(
                needle,
                source_text,
                msg=f"forbidden substring {needle!r} found in {MODULE_PATH}",
            )


if __name__ == "__main__":
    unittest.main()
