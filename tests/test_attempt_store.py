from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_taskflow.attempt_store import (
    ActiveAttemptExistsError,
    AttemptStore,
    TASK_ATTEMPT_LIFECYCLE_MIGRATION,
)
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore, connect


class AttemptStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "state.db"
        self.task_store = TaskMirrorStore(self.db_path)
        self.task_store.init_db()
        self.attempt_store = AttemptStore(self.db_path)

    def add_task(self, task_key: str = "AT-PR2-1") -> None:
        self.task_store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title=f"Task {task_key}",
                status="queued",
                repo_path="/tmp/agent-taskflow",
                artifact_dir=f"/tmp/agent-taskflow-artifacts/{task_key}",
            )
        )

    def test_migration_adds_task_attempt_and_lifecycle_schema(self) -> None:
        self.add_task()
        self.attempt_store.init_db()

        with closing(connect(self.db_path)) as conn, conn:
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            task_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
            indexes = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
            migration = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name = ?",
                (TASK_ATTEMPT_LIFECYCLE_MIGRATION,),
            ).fetchone()

        self.assertIn("attempts", tables)
        self.assertIn("lifecycle_events", tables)
        for column in (
            "task_id",
            "task_class",
            "active_attempt_id",
            "final_outcome",
            "closed_at",
            "is_legacy",
        ):
            self.assertIn(column, task_columns)
        self.assertIn("ux_tasks_task_id", indexes)
        self.assertIn("ux_attempts_one_active_per_task", indexes)
        self.assertIsNotNone(migration)

    def test_migration_marks_legacy_without_inventing_history(self) -> None:
        self.add_task()
        self.attempt_store.init_db()

        with closing(connect(self.db_path)) as conn, conn:
            task = conn.execute(
                """
                SELECT task_id, task_class, is_legacy, active_attempt_id
                FROM tasks
                WHERE task_key = 'AT-PR2-1'
                """
            ).fetchone()
            attempt_count = conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
            event_count = conn.execute(
                "SELECT COUNT(*) FROM lifecycle_events"
            ).fetchone()[0]

        self.assertEqual(task["task_id"], "task:AT-PR2-1")
        self.assertEqual(task["task_class"], "legacy")
        self.assertEqual(task["is_legacy"], 1)
        self.assertIsNone(task["active_attempt_id"])
        self.assertEqual(attempt_count, 0)
        self.assertEqual(event_count, 0)

    def test_migration_is_idempotent(self) -> None:
        self.add_task()
        self.attempt_store.init_db()
        self.attempt_store.init_db()

        with closing(connect(self.db_path)) as conn, conn:
            migration_count = conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE name = ?",
                (TASK_ATTEMPT_LIFECYCLE_MIGRATION,),
            ).fetchone()[0]

        self.assertEqual(migration_count, 1)

    def test_task_inserted_after_migration_gets_identity_lazily(self) -> None:
        self.attempt_store.init_db()
        self.add_task("AT-PR2-LATE")

        attempt = self.attempt_store.create_attempt("AT-PR2-LATE")

        self.assertEqual(attempt.task_id, "task:AT-PR2-LATE")
        with closing(connect(self.db_path)) as conn, conn:
            task = conn.execute(
                "SELECT task_id, is_legacy FROM tasks WHERE task_key = ?",
                ("AT-PR2-LATE",),
            ).fetchone()
        self.assertEqual(task["task_id"], "task:AT-PR2-LATE")
        self.assertEqual(task["is_legacy"], 1)

    def test_explicit_task_identity_can_promote_new_work_without_history_guessing(self) -> None:
        self.add_task("AT-PR2-CLASSIFIED")
        self.attempt_store.init_db()

        identity = self.attempt_store.register_task_identity(
            "AT-PR2-CLASSIFIED",
            task_class="docs-only-safe",
            is_legacy=False,
        )

        self.assertEqual(identity.task_id, "task:AT-PR2-CLASSIFIED")
        self.assertEqual(identity.task_class, "docs-only-safe")
        self.assertEqual(identity.current_status, "queued")
        self.assertFalse(identity.is_legacy)

    def test_three_sequential_attempts_have_stable_numbers_and_history(self) -> None:
        self.add_task()
        self.attempt_store.init_db()

        created_ids: list[str] = []
        for expected_number in (1, 2, 3):
            attempt = self.attempt_store.create_attempt(
                "AT-PR2-1",
                executor="noop",
                model="test-model",
                artifact_root=f"/tmp/artifacts/AT-PR2-1/{expected_number}",
            )
            created_ids.append(attempt.attempt_id)
            self.assertEqual(attempt.attempt_number, expected_number)
            self.assertTrue(attempt.is_active)
            closed = self.attempt_store.close_attempt(
                attempt.attempt_id,
                status="completed",
                reason_code="test_completed",
                actor="unit-test",
                execution_result="success",
                validation_result="passed",
            )
            self.assertFalse(closed.is_active)
            self.assertIsNotNone(closed.ended_at)

        attempts = self.attempt_store.list_attempts("AT-PR2-1")
        events = self.attempt_store.list_lifecycle_events("AT-PR2-1")

        self.assertEqual([item.attempt_id for item in attempts], created_ids)
        self.assertEqual([item.attempt_number for item in attempts], [1, 2, 3])
        self.assertEqual(len(events), 6)
        self.assertEqual(
            [(event.from_status, event.to_status) for event in events],
            [
                (None, "created"),
                ("created", "completed"),
                (None, "created"),
                ("created", "completed"),
                (None, "created"),
                ("created", "completed"),
            ],
        )

    def test_api_rejects_second_active_attempt(self) -> None:
        self.add_task()
        self.attempt_store.init_db()
        first = self.attempt_store.create_attempt("AT-PR2-1")

        with self.assertRaisesRegex(
            ActiveAttemptExistsError,
            first.attempt_id,
        ):
            self.attempt_store.create_attempt("AT-PR2-1")

    def test_partial_unique_index_rejects_direct_second_active_attempt(self) -> None:
        self.add_task()
        self.attempt_store.init_db()
        first = self.attempt_store.create_attempt("AT-PR2-1")
        now = first.created_at

        with self.assertRaises(sqlite3.IntegrityError):
            with closing(connect(self.db_path)) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO attempts (
                        attempt_id,
                        task_id,
                        attempt_number,
                        status,
                        is_active,
                        is_legacy,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, 2, 'created', 1, 0, ?, ?)
                    """,
                    ("attempt-direct-conflict", first.task_id, now, now),
                )

    def test_concurrent_creators_produce_only_one_active_attempt(self) -> None:
        self.add_task()
        self.attempt_store.init_db()

        def create(index: int) -> str:
            try:
                self.attempt_store.create_attempt(
                    "AT-PR2-1",
                    attempt_id=f"attempt-concurrent-{index}",
                )
                return "created"
            except ActiveAttemptExistsError:
                return "blocked"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(create, (1, 2)))

        self.assertCountEqual(outcomes, ["created", "blocked"])
        attempts = self.attempt_store.list_attempts("AT-PR2-1")
        self.assertEqual(len(attempts), 1)
        self.assertTrue(attempts[0].is_active)

    def test_task_active_attempt_pointer_must_match_same_task(self) -> None:
        self.add_task("AT-PR2-A")
        self.add_task("AT-PR2-B")
        self.attempt_store.init_db()
        attempt_a = self.attempt_store.create_attempt("AT-PR2-A")
        self.attempt_store.create_attempt("AT-PR2-B")

        with self.assertRaises(sqlite3.IntegrityError):
            with closing(connect(self.db_path)) as conn, conn:
                conn.execute(
                    """
                    UPDATE tasks
                    SET active_attempt_id = ?
                    WHERE task_key = 'AT-PR2-B'
                    """,
                    (attempt_a.attempt_id,),
                )

    def test_lifecycle_event_attempt_must_belong_to_task(self) -> None:
        self.add_task("AT-PR2-A")
        self.add_task("AT-PR2-B")
        self.attempt_store.init_db()
        attempt_a = self.attempt_store.create_attempt("AT-PR2-A")

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "does not belong to task",
        ):
            self.attempt_store.append_lifecycle_event(
                "AT-PR2-B",
                attempt_id=attempt_a.attempt_id,
                from_status="queued",
                to_status="preparing",
                reason_code="wrong_task",
                actor="unit-test",
            )

    def test_lifecycle_events_are_append_only(self) -> None:
        self.add_task()
        self.attempt_store.init_db()
        attempt = self.attempt_store.create_attempt(
            "AT-PR2-1",
            metadata={"source": "unit-test"},
        )
        event = self.attempt_store.list_lifecycle_events("AT-PR2-1")[0]
        self.assertEqual(json.loads(event.metadata_json), {"source": "unit-test"})

        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            with closing(connect(self.db_path)) as conn, conn:
                conn.execute(
                    "UPDATE lifecycle_events SET actor = 'changed' WHERE event_id = ?",
                    (event.event_id,),
                )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            with closing(connect(self.db_path)) as conn, conn:
                conn.execute(
                    "DELETE FROM lifecycle_events WHERE event_id = ?",
                    (event.event_id,),
                )

        self.assertEqual(
            self.attempt_store.get_active_attempt("AT-PR2-1").attempt_id,
            attempt.attempt_id,
        )

    def test_failure_timeout_and_abort_statuses_are_representable(self) -> None:
        for index, status in enumerate(
            ("validation_failed", "execution_timeout", "execution_aborted"),
            start=1,
        ):
            task_key = f"AT-PR2-STATUS-{index}"
            self.add_task(task_key)
        self.attempt_store.init_db()

        for index, status in enumerate(
            ("validation_failed", "execution_timeout", "execution_aborted"),
            start=1,
        ):
            task_key = f"AT-PR2-STATUS-{index}"
            attempt = self.attempt_store.create_attempt(task_key)
            closed = self.attempt_store.close_attempt(
                attempt.attempt_id,
                status=status,
                reason_code=status,
                actor="unit-test",
            )
            self.assertEqual(closed.status, status)


class TaskAttemptLifecycleDocsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        doc = (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "task-attempt-lifecycle-schema.md"
        )
        cls.text = doc.read_text(encoding="utf-8")
        cls.normalized = " ".join(cls.text.split())
        cls.normalized_lower = cls.normalized.lower()

    def test_records_implemented_schema_and_remaining_runtime_boundaries(self) -> None:
        for phrase in (
            "task_attempt_schema = implemented",
            "legacy_migration = implemented",
            "one_active_attempt_constraint = implemented",
            "append_only_lifecycle_events = implemented",
            "runtime_attempt_admission = not_implemented_in_this_pr",
            "attempt_scoped_worktree_lock_pid_artifact = not_implemented_in_this_pr",
            "milestone_0 = open_blocked",
            "level_2_eligible = false",
            "historical_attempts_synthesized = false",
            "ux_attempts_one_active_per_task",
            "BEGIN IMMEDIATE",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.normalized)

    def test_does_not_claim_runtime_or_level2_completion(self) -> None:
        for forbidden in (
            "runtime_attempt_admission = implemented",
            "milestone_0 = closed",
            "level_2_eligible = true",
            "all executor runs now use attempt_id",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.normalized_lower)


if __name__ == "__main__":
    unittest.main()
