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
                status="active",
            )
        )

        worktree = self.store.get_task_worktree("AT-0003")

        self.assertIsNotNone(worktree)
        assert worktree is not None
        self.assertEqual(worktree.task_key, "AT-0003")
        self.assertEqual(worktree.branch, "task/AT-0003")
        self.assertEqual(worktree.status, "active")
        self.assertEqual(
            worktree.worktree_path,
            Path("/home/ubuntu/agent-taskflow/.worktrees/AT-0003"),
        )

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


if __name__ == "__main__":
    unittest.main()
