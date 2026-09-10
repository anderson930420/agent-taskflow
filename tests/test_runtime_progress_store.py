"""Step 3 attempt-scoped runtime progress writes (SPEC §14, §14.0).

Runtime progress writes are the only writes Step 3 is allowed to make:
``current_phase``, ``current_activity``, and ObservedStep transitions. These
tests pin that the writes are attempt-scoped, that retry keeps earlier Attempt
records viewable, and that no lifecycle field is ever touched.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.models import TaskRecord
from agent_taskflow.runtime_progress import RuntimeProgressError
from agent_taskflow.runtime_progress_schema import (
    RUNTIME_PROGRESS_MIGRATION,
    migrate_runtime_progress,
)
from agent_taskflow.runtime_progress_store import RuntimeProgressStore
from agent_taskflow.store import TaskMirrorStore


class RuntimeProgressStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.repo_path.mkdir()

        self.tasks = TaskMirrorStore(self.db_path)
        self.tasks.init_db()
        self.tasks.upsert_task(
            TaskRecord(
                task_key="AT-101",
                project="forms",
                status="implementing",
                repo_path=self.repo_path,
                title="Separate ending-page image",
            )
        )
        self.attempts = AttemptStore(self.db_path)
        self.attempts.init_db()
        self.progress = RuntimeProgressStore(self.db_path)
        self.progress.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def new_attempt(self, task_key: str = "AT-101") -> str:
        return self.attempts.create_attempt(task_key, executor="manual").attempt_id


class MigrationTests(RuntimeProgressStoreTestCase):
    def test_migration_is_recorded_and_idempotent(self) -> None:
        migrate_runtime_progress(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            recorded = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name = ?",
                (RUNTIME_PROGRESS_MIGRATION,),
            ).fetchone()
        self.assertIsNotNone(recorded)

    def test_migration_creates_attempt_scoped_tables_only(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertIn("attempt_progress", names)
        self.assertIn("attempt_observed_steps", names)

    def test_migration_does_not_add_pr_columns_to_tasks(self) -> None:
        # §32.1 fields belong to Step 2. Step 3 must never create them.
        with sqlite3.connect(self.db_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        for column in (
            "pr_number",
            "pr_url",
            "pr_state",
            "pr_merged",
            "pr_head_sha",
            "merge_commit_sha",
            "review_decision",
            "ci_status",
            "integrated_base_sha",
            "reintegration_count",
            "reintegration_required",
            "pr_last_polled_at",
        ):
            with self.subTest(column=column):
                self.assertNotIn(column, columns)


class ObservedStepWriteTests(RuntimeProgressStoreTestCase):
    def test_record_step_is_attempt_scoped(self) -> None:
        attempt_id = self.new_attempt()
        self.progress.record_step(
            attempt_id=attempt_id,
            step="Prepare",
            status="passed",
            summary="worktree ready",
        )
        snapshot = self.progress.get_progress(attempt_id)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.attempt_id, attempt_id)
        self.assertEqual(snapshot.task_key, "AT-101")
        self.assertEqual(snapshot.step_status("Prepare"), "passed")

    def test_record_step_transitions_update_in_place(self) -> None:
        attempt_id = self.new_attempt()
        self.progress.record_step(
            attempt_id=attempt_id, step="Implementer", status="running"
        )
        self.progress.record_step(
            attempt_id=attempt_id, step="Implementer", status="passed"
        )
        steps = self.progress.list_steps(attempt_id)
        names = [step.name for step in steps]
        self.assertEqual(names.count("Implementer"), 1)
        self.assertEqual(steps[-1].status, "passed")

    def test_record_step_supports_every_spec_status(self) -> None:
        attempt_id = self.new_attempt()
        for status in ("pending", "running", "passed", "failed", "blocked"):
            with self.subTest(status=status):
                self.progress.record_step(
                    attempt_id=attempt_id, step="Validator", status=status
                )
                snapshot = self.progress.get_progress(attempt_id)
                assert snapshot is not None
                self.assertEqual(snapshot.step_status("Validator"), status)

    def test_record_step_rejects_unknown_attempt(self) -> None:
        with self.assertRaises(KeyError):
            self.progress.record_step(
                attempt_id="attempt-missing", step="Prepare", status="running"
            )

    def test_record_step_rejects_a_progress_estimate(self) -> None:
        attempt_id = self.new_attempt()
        with self.assertRaises(RuntimeProgressError):
            self.progress.record_step(
                attempt_id=attempt_id,
                step="Implementer",
                status="running",
                summary="73% complete",
            )

    def test_set_current_activity_records_phase_and_activity(self) -> None:
        attempt_id = self.new_attempt()
        self.progress.set_current_activity(
            attempt_id=attempt_id,
            phase="Implementer",
            activity="Adding frontend regression tests",
        )
        snapshot = self.progress.get_progress(attempt_id)
        assert snapshot is not None
        self.assertEqual(snapshot.current_phase, "Implementer")
        self.assertEqual(
            snapshot.current_activity, "Adding frontend regression tests"
        )

    def test_set_current_activity_rejects_a_progress_estimate(self) -> None:
        attempt_id = self.new_attempt()
        with self.assertRaises(RuntimeProgressError):
            self.progress.set_current_activity(
                attempt_id=attempt_id, phase="Implementer", activity="ETA 3 minutes"
            )

    def test_set_current_activity_rejects_unknown_phase(self) -> None:
        attempt_id = self.new_attempt()
        with self.assertRaises(RuntimeProgressError):
            self.progress.set_current_activity(attempt_id=attempt_id, phase="Deploy")

    def test_progress_is_absent_before_any_write(self) -> None:
        attempt_id = self.new_attempt()
        self.assertIsNone(self.progress.get_progress(attempt_id))
        self.assertEqual(self.progress.list_steps(attempt_id), ())


class RetryKeepsEarlierAttemptTests(RuntimeProgressStoreTestCase):
    """§14.0 — retry creates a new Attempt; the old ObservedSteps survive."""

    def test_new_attempt_does_not_erase_the_previous_attempt_steps(self) -> None:
        first = self.new_attempt()
        self.progress.record_step(
            attempt_id=first, step="Validator", status="failed", summary="pytest red"
        )
        self.attempts.close_attempt(
            first,
            status="validation_failed",
            reason_code="validators_failed",
            actor="test",
        )

        second = self.new_attempt()
        self.progress.record_step(
            attempt_id=second, step="Validator", status="passed"
        )

        first_snapshot = self.progress.get_progress(first)
        second_snapshot = self.progress.get_progress(second)
        assert first_snapshot is not None and second_snapshot is not None
        self.assertEqual(first_snapshot.step_status("Validator"), "failed")
        self.assertEqual(second_snapshot.step_status("Validator"), "passed")
        self.assertEqual(first_snapshot.attempt_number, 1)
        self.assertEqual(second_snapshot.attempt_number, 2)

    def test_latest_attempt_progress_selects_the_highest_attempt_number(self) -> None:
        first = self.new_attempt()
        self.progress.record_step(attempt_id=first, step="Prepare", status="passed")
        self.attempts.close_attempt(
            first, status="failed", reason_code="crash", actor="test"
        )
        second = self.new_attempt()
        self.progress.record_step(attempt_id=second, step="Prepare", status="running")

        latest = self.progress.get_latest_attempt_progress("AT-101")
        assert latest is not None
        self.assertEqual(latest.attempt_id, second)
        self.assertEqual(latest.step_status("Prepare"), "running")

    def test_list_attempt_progress_returns_every_attempt_oldest_first(self) -> None:
        first = self.new_attempt()
        self.attempts.close_attempt(
            first, status="failed", reason_code="crash", actor="test"
        )
        second = self.new_attempt()
        numbers = [
            item.attempt_number
            for item in self.progress.list_attempt_progress("AT-101")
        ]
        self.assertEqual(numbers, [1, 2])
        ids = [item.attempt_id for item in self.progress.list_attempt_progress("AT-101")]
        self.assertEqual(ids, [first, second])


class BatchSnapshotTests(RuntimeProgressStoreTestCase):
    """The board rebuilds on every SSE poll, so progress reads are batched."""

    def test_snapshots_for_attempts_returns_one_entry_per_known_attempt(
        self,
    ) -> None:
        self.tasks.upsert_task(
            TaskRecord(
                task_key="AT-102",
                project="forms",
                status="validating",
                repo_path=self.repo_path,
            )
        )
        first = self.new_attempt("AT-101")
        second = self.new_attempt("AT-102")
        self.progress.record_step(
            attempt_id=first, step="Implementer", status="running"
        )
        self.progress.set_current_activity(
            attempt_id=second, phase="Validator", activity="running pytest"
        )

        snapshots = self.progress.snapshots_for_attempts([first, second])
        self.assertEqual(sorted(snapshots), sorted([first, second]))
        self.assertEqual(snapshots[first].step_status("Implementer"), "running")
        self.assertEqual(snapshots[second].current_phase, "Validator")
        self.assertEqual(snapshots[second].task_key, "AT-102")

    def test_snapshots_for_attempts_skips_unknown_ids_without_raising(self) -> None:
        known = self.new_attempt()
        snapshots = self.progress.snapshots_for_attempts(
            [known, "attempt-missing", "", None]  # type: ignore[list-item]
        )
        self.assertEqual(list(snapshots), [known])

    def test_snapshots_for_attempts_returns_empty_for_no_ids(self) -> None:
        self.assertEqual(self.progress.snapshots_for_attempts([]), {})

    def test_snapshots_for_attempts_defaults_unrecorded_attempts(self) -> None:
        attempt_id = self.new_attempt()
        snapshot = self.progress.snapshots_for_attempts([attempt_id])[attempt_id]
        self.assertIsNone(snapshot.current_phase)
        self.assertEqual(snapshot.step_status("Prepare"), "pending")

    def test_snapshots_for_attempts_matches_single_attempt_reads(self) -> None:
        attempt_id = self.new_attempt()
        self.progress.record_step(
            attempt_id=attempt_id, step="Validator", status="failed"
        )
        self.progress.set_current_activity(
            attempt_id=attempt_id, phase="Validator", activity="pytest red"
        )
        batched = self.progress.snapshots_for_attempts([attempt_id])[attempt_id]
        single = self.progress.get_attempt_progress(attempt_id)
        self.assertEqual(batched, single)


class NoLifecycleWriteTests(RuntimeProgressStoreTestCase):
    """Forbidden layer — Step 3 performs no lifecycle state transition."""

    def _lifecycle_fingerprint(self) -> tuple[object, ...]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            task = conn.execute(
                "SELECT status, updated_at, active_attempt_id, final_outcome "
                "FROM tasks WHERE task_key = 'AT-101'"
            ).fetchone()
            attempts = conn.execute(
                "SELECT attempt_id, status, is_active, ended_at FROM attempts "
                "ORDER BY attempt_id"
            ).fetchall()
            lifecycle_events = conn.execute(
                "SELECT COUNT(*) FROM lifecycle_events"
            ).fetchone()[0]
            task_events = conn.execute(
                "SELECT COUNT(*) FROM task_events"
            ).fetchone()[0]
        return (
            tuple(task),
            tuple(tuple(row) for row in attempts),
            lifecycle_events,
            task_events,
        )

    def test_progress_writes_do_not_change_any_lifecycle_field(self) -> None:
        attempt_id = self.new_attempt()
        before = self._lifecycle_fingerprint()

        self.progress.record_step(
            attempt_id=attempt_id, step="Prepare", status="passed"
        )
        self.progress.record_step(
            attempt_id=attempt_id, step="Implementer", status="running"
        )
        self.progress.set_current_activity(
            attempt_id=attempt_id,
            phase="Implementer",
            activity="Adding frontend regression tests",
        )

        self.assertEqual(self._lifecycle_fingerprint(), before)

    def test_progress_writes_do_not_append_lifecycle_events(self) -> None:
        attempt_id = self.new_attempt()
        with sqlite3.connect(self.db_path) as conn:
            before = conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0]
        self.progress.record_step(
            attempt_id=attempt_id, step="Reviewer", status="running"
        )
        with sqlite3.connect(self.db_path) as conn:
            after = conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0]
        self.assertEqual(after, before)

    def test_store_exposes_no_status_mutation_helpers(self) -> None:
        forbidden = [
            name
            for name in dir(RuntimeProgressStore)
            if not name.startswith("_")
            and any(
                token in name
                for token in (
                    "status",
                    "approve",
                    "merge",
                    "push",
                    "cleanup",
                    "transition",
                )
            )
        ]
        self.assertEqual(forbidden, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
