from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore, init_db


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_task(
        self,
        task_key: str = "AT-0003",
        *,
        project: str = "agent-taskflow",
        status: str = "blocked",
    ) -> TaskRecord:
        return TaskRecord(
            task_key=task_key,
            project=project,
            board="agent-taskflow",
            hermes_task_id=f"t_{task_key.lower().replace('-', '_')}",
            title=f"Task {task_key}",
            status=status,
            repo_path="/home/ubuntu/agent-taskflow",
            artifact_dir=f"/home/ubuntu/.hermes/task-artifacts/{task_key}",
        )

    def test_init_db_creates_required_tables(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                """
            ).fetchall()

        table_names = {row[0] for row in rows}
        self.assertIn("tasks", table_names)
        self.assertIn("task_events", table_names)
        self.assertIn("task_artifacts", table_names)
        self.assertIn("task_worktrees", table_names)

    def test_init_db_is_idempotent(self) -> None:
        init_db(self.db_path)
        init_db(self.db_path)

        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]

        self.assertEqual(count, 0)

    def test_task_can_be_upserted_and_read_back(self) -> None:
        self.store.upsert_task(self.make_task())

        task = self.store.get_task("AT-0003")

        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.task_key, "AT-0003")
        self.assertEqual(task.project, "agent-taskflow")
        self.assertEqual(task.status, "blocked")
        self.assertIsNone(task.blocked_reason)
        self.assertEqual(task.repo_path, Path("/home/ubuntu/agent-taskflow"))

    def test_task_upsert_updates_existing_task(self) -> None:
        self.store.upsert_task(self.make_task(status="blocked"))
        self.store.upsert_task(self.make_task(status="waiting_approval"))

        task = self.store.get_task("AT-0003")

        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")

    def test_invalid_task_status_is_rejected_before_write(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid task status"):
            self.make_task(status="not-real")

    def test_tasks_can_be_filtered_by_project(self) -> None:
        self.store.upsert_task(self.make_task("AT-0003", project="agent-taskflow"))
        self.store.upsert_task(self.make_task("BJ-0001", project="bullet-journal"))

        tasks = self.store.list_tasks(project="agent-taskflow")

        self.assertEqual([task.task_key for task in tasks], ["AT-0003"])

    def test_tasks_can_be_filtered_by_status(self) -> None:
        self.store.upsert_task(self.make_task("AT-0003", status="blocked"))
        self.store.upsert_task(self.make_task("AT-0004", status="waiting_approval"))

        tasks = self.store.list_tasks(status="waiting_approval")

        self.assertEqual([task.task_key for task in tasks], ["AT-0004"])

    def test_update_task_status_records_event(self) -> None:
        self.store.upsert_task(self.make_task())

        self.store.update_task_status(
            "AT-0003",
            "waiting_approval",
            message="Worker finished implementation",
            source="kanban",
        )

        task = self.store.get_task("AT-0003")
        events = self.store.list_task_events("AT-0003")

        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "status_changed")
        self.assertEqual(events[0].source, "kanban")
        self.assertIn("waiting_approval", events[0].payload_json or "")

    def test_update_task_status_can_record_blocked_reason(self) -> None:
        self.store.upsert_task(self.make_task(status="queued"))

        self.store.update_task_status(
            "AT-0003",
            "blocked",
            message="Executor failed",
            source="dispatcher",
            blocked_reason="executor failed with status failed",
        )

        task = self.store.get_task("AT-0003")
        events = self.store.list_task_events("AT-0003")

        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "blocked")
        self.assertEqual(task.blocked_reason, "executor failed with status failed")
        self.assertIn("executor failed", events[-1].payload_json or "")

    def test_update_non_blocked_status_clears_blocked_reason(self) -> None:
        self.store.upsert_task(
            self.make_task(status="blocked").__class__(
                task_key="AT-0003",
                project="agent-taskflow",
                board="agent-taskflow",
                hermes_task_id="t_at_0003",
                title="Task AT-0003",
                status="blocked",
                repo_path="/home/ubuntu/agent-taskflow",
                artifact_dir="/home/ubuntu/.hermes/task-artifacts/AT-0003",
                blocked_reason="old reason",
            )
        )

        self.store.update_task_status("AT-0003", "queued", source="dispatcher")

        task = self.store.get_task("AT-0003")

        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "queued")
        self.assertIsNone(task.blocked_reason)

    def test_dispatcher_store_events_can_record_executor_run(self) -> None:
        self.store.upsert_task(self.make_task())

        run_id = self.store.create_executor_run(
            "AT-0003",
            "noop",
            model="fake-model",
            prompt_path="/home/ubuntu/.hermes/task-artifacts/AT-0003/implementation_prompt.md",
        )
        self.store.finish_executor_run(
            "AT-0003",
            run_id,
            executor="noop",
            status="completed",
            exit_code=0,
            summary="done",
            log_path="/home/ubuntu/.hermes/task-artifacts/AT-0003/noop.log",
            artifacts={"log": "/home/ubuntu/.hermes/task-artifacts/AT-0003/noop.log"},
        )

        events = self.store.list_task_events("AT-0003")
        payloads = [event.payload_json or "" for event in events]

        self.assertTrue(any("executor_run_started" in payload for payload in payloads))
        self.assertTrue(any("executor_run_finished" in payload for payload in payloads))
        self.assertTrue(any(run_id in payload for payload in payloads))

    def test_dispatcher_store_events_can_record_validation_result(self) -> None:
        self.store.upsert_task(self.make_task())

        self.store.record_validation_result(
            "AT-0003",
            "pytest",
            status="passed",
            exit_code=0,
            summary="tests passed",
            log_path="/home/ubuntu/.hermes/task-artifacts/AT-0003/pytest.log",
            artifacts={"log": "/home/ubuntu/.hermes/task-artifacts/AT-0003/pytest.log"},
        )

        events = self.store.list_task_events("AT-0003")

        self.assertEqual(events[-1].source, "dispatcher")
        self.assertIn("validation_result", events[-1].payload_json or "")
        self.assertIn("pytest", events[-1].payload_json or "")

    def test_update_missing_task_status_raises_key_error(self) -> None:
        with self.assertRaisesRegex(KeyError, "Task not found"):
            self.store.update_task_status("AT-404", "blocked")

    def test_record_task_event_and_list(self) -> None:
        self.store.upsert_task(self.make_task())

        self.store.record_task_event(
            "AT-0003",
            "note",
            "tester",
            message="manual note",
            payload={"x": 1},
        )

        events = self.store.list_task_events("AT-0003")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "note")
        self.assertEqual(events[0].source, "tester")
        self.assertEqual(events[0].message, "manual note")
        self.assertEqual(events[0].payload_json, '{"x": 1}')

    def test_task_artifact_can_be_recorded_and_listed(self) -> None:
        self.store.upsert_task(self.make_task())

        self.store.record_task_artifact(
            "AT-0003",
            "spec",
            "/home/ubuntu/.hermes/task-artifacts/AT-0003/spec.md",
        )

        artifacts = self.store.list_task_artifacts("AT-0003")

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].task_key, "AT-0003")
        self.assertEqual(artifacts[0].artifact_type, "spec")
        self.assertEqual(
            artifacts[0].path,
            Path("/home/ubuntu/.hermes/task-artifacts/AT-0003/spec.md"),
        )

    def test_task_artifact_workflow_policy_summary_is_accepted(self) -> None:
        self.store.upsert_task(self.make_task())

        self.store.record_task_artifact(
            "AT-0003",
            "workflow_policy_summary",
            "/tmp/artifacts/AT-0003/workflow_policy_summary.json",
        )

        artifacts = self.store.list_task_artifacts("AT-0003")

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].artifact_type, "workflow_policy_summary")

    def test_task_artifact_artifact_index_is_accepted(self) -> None:
        self.store.upsert_task(self.make_task())

        self.store.record_task_artifact(
            "AT-0003",
            "artifact_index",
            "/tmp/artifacts/AT-0003/artifact_index.json",
        )

        artifacts = self.store.list_task_artifacts("AT-0003")

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].artifact_type, "artifact_index")

    def test_task_artifact_other_still_works(self) -> None:
        self.store.upsert_task(self.make_task())

        self.store.record_task_artifact(
            "AT-0003",
            "other",
            "/tmp/artifacts/AT-0003/misc.json",
        )

        artifacts = self.store.list_task_artifacts("AT-0003")

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].artifact_type, "other")

    def test_task_artifact_invalid_type_is_rejected(self) -> None:
        self.store.upsert_task(self.make_task())

        with self.assertRaisesRegex(ValueError, "Invalid task artifact type"):
            self.store.record_task_artifact(
                "AT-0003",
                "not_a_real_type",
                "/tmp/artifacts/AT-0003/file.json",
            )

    def test_relative_task_artifact_path_is_rejected(self) -> None:
        self.store.upsert_task(self.make_task())

        with self.assertRaisesRegex(ValueError, "path must be absolute"):
            self.store.record_task_artifact("AT-0003", "spec", "relative/spec.md")

    def test_task_worktree_can_be_upserted_and_read_back(self) -> None:
        self.store.upsert_task(self.make_task())

        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key="AT-0003",
                repo_path="/home/ubuntu/agent-taskflow",
                worktree_path="/home/ubuntu/agent-taskflow/.worktrees/AT-0003",
                branch="task/AT-0003",
                base_branch="main",
                base_sha="abc123",
                status="active",
            )
        )

        worktree = self.store.get_task_worktree("AT-0003")

        self.assertIsNotNone(worktree)
        assert worktree is not None
        self.assertEqual(worktree.task_key, "AT-0003")
        self.assertEqual(worktree.branch, "task/AT-0003")
        self.assertEqual(worktree.base_sha, "abc123")
        self.assertEqual(worktree.status, "active")
        self.assertEqual(
            worktree.worktree_path,
            Path("/home/ubuntu/agent-taskflow/.worktrees/AT-0003"),
        )

    def test_task_worktree_base_sha_migration_is_idempotent(self) -> None:
        init_db(self.db_path)
        init_db(self.db_path)

        with sqlite3.connect(self.db_path) as conn:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(task_worktrees)").fetchall()
            }

        self.assertIn("base_sha", columns)

    def test_task_worktrees_can_be_filtered_by_project_and_status(self) -> None:
        self.store.upsert_task(self.make_task("AT-0003", project="agent-taskflow"))
        self.store.upsert_task(self.make_task("BJ-0001", project="bullet-journal"))

        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key="AT-0003",
                repo_path="/home/ubuntu/agent-taskflow",
                worktree_path="/home/ubuntu/agent-taskflow/.worktrees/AT-0003",
                branch="task/AT-0003",
                status="active",
            )
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key="BJ-0001",
                repo_path="/home/ubuntu/bullet_journal_app",
                worktree_path="/home/ubuntu/bullet_journal_app/.worktrees/BJ-0001",
                branch="task/BJ-0001",
                status="cleaned",
            )
        )

        worktrees = self.store.list_task_worktrees(
            project="agent-taskflow",
            status="active",
        )

        self.assertEqual([worktree.task_key for worktree in worktrees], ["AT-0003"])


class StoreExecutorFieldsTests(unittest.TestCase):
    """Phase 13: store persistence of TaskRecord executor selection fields."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_init_db_migration_adds_executor_fields(self) -> None:
        """init_db adds executor/model/provider/tools/pi_bin columns to existing DB."""
        # Prime the DB with a task before re-init with new migration
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-0001",
                project="agent-taskflow",
                status="queued",
                repo_path="/home/ubuntu/agent-taskflow",
            )
        )

        # Re-init should be idempotent and not raise
        self.store.init_db()

        # Verify columns exist
        with sqlite3.connect(self.db_path) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}

        for col in ("executor", "model", "provider", "tools", "pi_bin"):
            self.assertIn(col, cols, f"Column {col} should exist after migration")

    def test_init_db_migration_is_idempotent(self) -> None:
        """Multiple init_db calls do not fail on existing columns."""
        self.store.init_db()
        self.store.init_db()  # Should not raise
        self.store.init_db()  # Still should not raise

        task = self.store.get_task("AT-NO-SUCH")
        self.assertIsNone(task)

    def test_upsert_task_persists_executor_fields(self) -> None:
        """executor/model/provider/tools/pi_bin are persisted correctly."""
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-0013",
                project="agent-taskflow",
                status="queued",
                repo_path="/home/ubuntu/agent-taskflow",
                executor="pi",
                model="minimax-01",
                provider="minimax",
                tools=["Read", "Write"],
                pi_bin="pi",
            )
        )

        task = self.store.get_task("AT-0013")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.executor, "pi")
        self.assertEqual(task.model, "minimax-01")
        self.assertEqual(task.provider, "minimax")
        self.assertEqual(task.tools, ["Read", "Write"])
        self.assertEqual(task.pi_bin, "pi")

    def test_upsert_task_persists_tools_as_json(self) -> None:
        """tools list is stored as JSON text in SQLite."""
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-0013",
                project="agent-taskflow",
                status="queued",
                repo_path="/home/ubuntu/agent-taskflow",
                tools=["Read", "Write", "Bash"],
            )
        )

        # Read raw SQLite value
        with sqlite3.connect(self.db_path) as conn:
            raw = conn.execute(
                "SELECT tools FROM tasks WHERE task_key = ?",
                ("AT-0013",),
            ).fetchone()

        self.assertIsNotNone(raw)
        import json
        parsed = json.loads(raw[0])
        self.assertEqual(parsed, ["Read", "Write", "Bash"])

    def test_upsert_task_persists_null_when_fields_not_set(self) -> None:
        """executor/model/provider/tools/pi_bin are stored as NULL when not set."""
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-0013",
                project="agent-taskflow",
                status="queued",
                repo_path="/home/ubuntu/agent-taskflow",
            )
        )

        task = self.store.get_task("AT-0013")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertIsNone(task.executor)
        self.assertIsNone(task.model)
        self.assertIsNone(task.provider)
        self.assertIsNone(task.tools)
        self.assertIsNone(task.pi_bin)

    def test_upsert_task_updates_executor_fields(self) -> None:
        """Updating a task can change executor field values."""
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-0013",
                project="agent-taskflow",
                status="queued",
                repo_path="/home/ubuntu/agent-taskflow",
                executor="pi",
                model="minimax-01",
            )
        )
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-0013",
                project="agent-taskflow",
                status="implementing",
                repo_path="/home/ubuntu/agent-taskflow",
                executor="manual",
                model=None,
            )
        )

        task = self.store.get_task("AT-0013")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.executor, "manual")
        self.assertIsNone(task.model)

    def test_tools_field_with_single_tool(self) -> None:
        """tools list with single element is stored and read back correctly."""
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-0013",
                project="agent-taskflow",
                status="queued",
                repo_path="/home/ubuntu/agent-taskflow",
                tools=["Read"],
            )
        )

        task = self.store.get_task("AT-0013")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.tools, ["Read"])

    def test_tools_field_with_empty_list(self) -> None:
        """tools empty list is stored as '[]' JSON and read back as []."""
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-0013",
                project="agent-taskflow",
                status="queued",
                repo_path="/home/ubuntu/agent-taskflow",
                tools=[],
            )
        )

        task = self.store.get_task("AT-0013")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.tools, [])


if __name__ == "__main__":
    unittest.main()
