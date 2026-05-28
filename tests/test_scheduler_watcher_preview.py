from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord
from agent_taskflow.scheduler_watcher_preview import (
    SchedulerWatcherPreviewRequest,
    WATCHER_PREVIEW_SAFETY_FLAGS,
    WATCHER_PREVIEW_SCHEMA_VERSION,
    WATCHER_PREVIEW_SOURCE,
    build_scheduler_watcher_preview,
)
from agent_taskflow.store import TaskMirrorStore


class SchedulerWatcherPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def seed_task(
        self,
        task_key: str,
        *,
        status: str,
        title: str = "Watcher preview task",
        blocked_reason: str | None = None,
    ) -> Path:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title=title,
                status=status,
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                blocked_reason=blocked_reason,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )
        return artifact_dir

    def record_artifact(
        self,
        task_key: str,
        artifact_type: str,
        filename: str,
    ) -> None:
        path = self.artifact_root / task_key / filename
        path.write_text("{}\n", encoding="utf-8")
        self.store.record_task_artifact(task_key, artifact_type, path)

    def record_executor_and_validators(self, task_key: str) -> None:
        run_id = self.store.create_executor_run(task_key, "manual")
        self.store.finish_executor_run(
            task_key,
            run_id,
            executor="manual",
            status="completed",
            exit_code=0,
            summary="done",
        )
        self.store.record_validation_result(
            task_key,
            "pytest",
            status="passed",
            exit_code=0,
            summary="passed",
        )

    def preview(self, **kwargs: object) -> dict[str, object]:
        return build_scheduler_watcher_preview(
            SchedulerWatcherPreviewRequest(db_path=self.db_path, **kwargs)
        )

    def db_counts(self) -> dict[str, int]:
        with sqlite3.connect(self.db_path) as conn:
            return {
                "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                "events": conn.execute(
                    "SELECT COUNT(*) FROM task_events"
                ).fetchone()[0],
                "artifacts": conn.execute(
                    "SELECT COUNT(*) FROM task_artifacts"
                ).fetchone()[0],
                "worktrees": conn.execute(
                    "SELECT COUNT(*) FROM task_worktrees"
                ).fetchone()[0],
            }

    def test_preview_lists_eligible_queued_task(self) -> None:
        self.seed_task("AT-L8A-001", status="queued")

        payload = self.preview()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["schema_version"], WATCHER_PREVIEW_SCHEMA_VERSION)
        self.assertEqual(payload["source"], WATCHER_PREVIEW_SOURCE)
        self.assertEqual(payload["mode"], "dry_run_preview")
        self.assertEqual(payload["candidate_count"], 1)
        candidate = payload["candidates"][0]
        self.assertEqual(candidate["task_key"], "AT-L8A-001")
        self.assertTrue(candidate["would_run"])
        self.assertEqual(candidate["would_run_pipeline"], "task_to_draft_pr")
        self.assertFalse(candidate["suggested_command_executed"])
        self.assertIn("--confirm-draft-pr", candidate["required_operator_flags"])

    def test_preview_skips_blocked_waiting_completed_by_default(self) -> None:
        self.seed_task("AT-L8A-BLOCKED", status="blocked", blocked_reason="blocked")
        self.seed_task("AT-L8A-WAITING", status="waiting_approval")
        self.record_executor_and_validators("AT-L8A-WAITING")
        self.seed_task("AT-L8A-ACCEPTED", status="accepted")

        payload = self.preview()

        skipped_by_key = {item["task_key"]: item for item in payload["skipped"]}
        self.assertEqual(skipped_by_key["AT-L8A-BLOCKED"]["reason"], "blocked")
        self.assertEqual(
            skipped_by_key["AT-L8A-WAITING"]["reason"], "waiting_approval"
        )
        self.assertEqual(skipped_by_key["AT-L8A-ACCEPTED"]["reason"], "completed")
        self.assertEqual(payload["summary"]["blocked_count"], 1)
        self.assertEqual(payload["summary"]["waiting_approval_count"], 1)
        self.assertEqual(payload["summary"]["completed_count"], 1)

    def test_preview_exposes_blocked_backlog_without_execution(self) -> None:
        self.seed_task(
            "AT-L8A-BLOCKED-BACKLOG",
            status="blocked",
            title="Blocked backlog task",
            blocked_reason="validator failed",
        )

        payload = self.preview()

        self.assertEqual(payload["candidate_count"], 0)
        self.assertEqual(payload["blocked_backlog_count"], 1)
        self.assertEqual(payload["summary"]["blocked_count"], 1)
        self.assertEqual(payload["summary"]["blocked_backlog_count"], 1)
        self.assertEqual(len(payload["blocked_backlog"]), 1)
        blocked = payload["blocked_backlog"][0]
        self.assertEqual(blocked["task_key"], "AT-L8A-BLOCKED-BACKLOG")
        self.assertEqual(blocked["title"], "Blocked backlog task")
        self.assertEqual(blocked["blocked_reason"], "validator failed")
        self.assertEqual(blocked["required_operator_action"], "inspect_manually")
        self.assertIn("repair the underlying issue", blocked["recovery_hint"])
        self.assertFalse(blocked["would_run"])
        self.assertFalse(blocked["safety"]["executed_now"])
        self.assertFalse(blocked["safety"]["status_changed_now"])
        self.assertFalse(blocked["safety"]["github_mutated_now"])

        skipped_by_key = {item["task_key"]: item for item in payload["skipped"]}
        skipped = skipped_by_key["AT-L8A-BLOCKED-BACKLOG"]
        self.assertEqual(skipped["reason"], "blocked")
        self.assertEqual(skipped["blocked_reason"], "validator failed")

    def test_blocked_tasks_are_never_candidates_even_when_included(self) -> None:
        self.seed_task(
            "AT-L8A-BLOCKED-INCLUDED",
            status="blocked",
            blocked_reason="manual review required",
        )

        payload = self.preview(include_blocked=True)

        self.assertEqual(payload["candidate_count"], 0)
        self.assertEqual(payload["blocked_backlog_count"], 1)
        blocked = payload["blocked_backlog"][0]
        self.assertEqual(blocked["task_key"], "AT-L8A-BLOCKED-INCLUDED")
        self.assertFalse(blocked["would_run"])
        skipped_by_key = {item["task_key"]: item for item in payload["skipped"]}
        self.assertEqual(
            skipped_by_key["AT-L8A-BLOCKED-INCLUDED"]["reason"],
            "unsupported_command_kind",
        )
        self.assertIn(
            "blocked tasks are never executable preview items",
            skipped_by_key["AT-L8A-BLOCKED-INCLUDED"]["warnings"],
        )

    def test_preview_include_flags(self) -> None:
        self.seed_task("AT-L8A-WAITING-FLAG", status="waiting_approval")
        self.record_executor_and_validators("AT-L8A-WAITING-FLAG")
        self.seed_task("AT-L8A-DONE-FLAG", status="completed")

        default_payload = self.preview()
        include_waiting = self.preview(include_waiting_approval=True)
        include_completed = self.preview(
            include_completed=True,
            include_no_action=True,
        )

        self.assertEqual(default_payload["candidate_count"], 0)
        waiting_keys = {item["task_key"] for item in include_waiting["candidates"]}
        self.assertIn("AT-L8A-WAITING-FLAG", waiting_keys)
        completed_reasons = {
            item["task_key"]: item["reason"] for item in include_completed["skipped"]
        }
        self.assertNotEqual(
            completed_reasons["AT-L8A-DONE-FLAG"],
            "completed",
        )

    def test_preview_limit_zero(self) -> None:
        self.seed_task("AT-L8A-LIMIT", status="queued")

        payload = self.preview(limit=0)

        self.assertEqual(payload["candidate_count"], 0)
        self.assertEqual(payload["summary"]["would_run_count"], 0)

    def test_preview_is_read_only(self) -> None:
        self.seed_task("AT-L8A-READONLY", status="queued")
        before = self.db_counts()
        before_status = self.store.get_task("AT-L8A-READONLY").status

        self.preview()

        self.assertEqual(self.db_counts(), before)
        self.assertEqual(
            self.store.get_task("AT-L8A-READONLY").status,
            before_status,
        )

    def test_preview_safety_flags(self) -> None:
        self.seed_task("AT-L8A-SAFE", status="queued")

        payload = self.preview()

        self.assertEqual(payload["safety"], WATCHER_PREVIEW_SAFETY_FLAGS)
        self.assertTrue(payload["safety"]["dry_run_preview"])
        self.assertTrue(payload["safety"]["read_only"])
        for key, value in payload["safety"].items():
            if key not in {"dry_run_preview", "read_only"}:
                self.assertFalse(value, key)

    def test_request_normalizes_strings_and_requires_absolute_db_path(self) -> None:
        request = SchedulerWatcherPreviewRequest(
            db_path=self.db_path,
            project="  ",
            status=" queued ",
            recommended_command_kind=" queued_task_handoff ",
            operator=" op ",
            operator_note=" ",
        )

        self.assertIsNone(request.project)
        self.assertEqual(request.status, "queued")
        self.assertEqual(request.recommended_command_kind, "queued_task_handoff")
        self.assertEqual(request.operator, "op")
        self.assertIsNone(request.operator_note)
        with self.assertRaises(ValueError):
            SchedulerWatcherPreviewRequest(db_path=Path("relative.db"))
        with self.assertRaises(ValueError):
            SchedulerWatcherPreviewRequest(db_path=self.db_path, limit=-1)

    def test_source_does_not_import_or_call_forbidden_execution_paths(self) -> None:
        source = Path("agent_taskflow/scheduler_watcher_preview.py").read_text(
            encoding="utf-8"
        )
        forbidden = (
            "from agent_taskflow.one_shot_task_pipeline",
            "from agent_taskflow.task_to_draft_pr_pipeline",
            "from agent_taskflow.approved_task_runner",
            "from agent_taskflow.executors",
            "from agent_taskflow.validators",
            "from agent_taskflow.branch_push",
            "from agent_taskflow.draft_pr",
            "run_one_shot_task_pipeline(",
            "run_task_to_draft_pr_pipeline(",
            "approved_task_runner(",
            "approved_task_runner_fn",
            "branch_push_fn",
            "draft_pr_fn",
            "while True",
            "threading.Thread",
            "asyncio.sleep",
            "schedule.every",
            "cron",
            "webhook",
            "polling",
            "git push",
            "gh pr create",
        )
        for needle in forbidden:
            self.assertNotIn(needle, source, needle)


if __name__ == "__main__":
    unittest.main()
