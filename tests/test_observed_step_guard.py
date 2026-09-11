"""ObservedStep lost-write guard (V1 Step 4, §42 "ObservedStep lost-write test").

``RuntimeProgressStore.record_step`` must never regress a step for the same
Attempt — a late ``running`` can never overwrite ``passed`` — and concurrent
writers must not lose a step. Step 3's table schema is unchanged.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import tempfile
import threading
import unittest
from pathlib import Path

from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.concurrency_rehearsal import (
    add_rehearsal_task,
    create_rehearsal_fixture,
    run_worker_processes,
)
from agent_taskflow.models import TaskRecord
from agent_taskflow.runtime_progress import (
    RUNTIME_STEPS,
    ObservedStepRegressionError,
    RuntimeProgressError,
    is_step_status_regression,
)
from agent_taskflow.runtime_progress_schema import migrate_runtime_progress
from agent_taskflow.runtime_progress_store import RuntimeProgressStore
from agent_taskflow.store import TaskMirrorStore, connect


class GuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db_path = root / "state.db"
        repo = root / "repo"
        repo.mkdir()
        tasks = TaskMirrorStore(self.db_path)
        tasks.init_db()
        tasks.upsert_task(
            TaskRecord(
                task_key="AT-GUARD-1",
                project="forms",
                status="implementing",
                repo_path=repo,
                title="guard",
            )
        )
        self.attempts = AttemptStore(self.db_path)
        self.attempts.init_db()
        migrate_runtime_progress(self.db_path)
        self.progress = RuntimeProgressStore(self.db_path)
        self.attempt_id = self.attempts.create_attempt(
            "AT-GUARD-1", executor="manual"
        ).attempt_id

    def status_of(self, step: str) -> str | None:
        snapshot = self.progress.get_progress(self.attempt_id)
        return None if snapshot is None else snapshot.step_status(step)


class RegressionRuleTests(unittest.TestCase):
    def test_rank_order_is_pending_running_then_terminal(self) -> None:
        self.assertTrue(is_step_status_regression("running", "pending"))
        self.assertTrue(is_step_status_regression("passed", "running"))
        self.assertTrue(is_step_status_regression("failed", "running"))
        self.assertTrue(is_step_status_regression("blocked", "pending"))
        self.assertFalse(is_step_status_regression("pending", "running"))
        self.assertFalse(is_step_status_regression("running", "passed"))
        self.assertFalse(is_step_status_regression("running", "running"))

    def test_terminal_to_terminal_is_not_a_rank_regression(self) -> None:
        # Pinned by Step 3's test_record_step_supports_every_spec_status,
        # which writes every status in sequence on one step.
        self.assertFalse(is_step_status_regression("passed", "failed"))
        self.assertFalse(is_step_status_regression("failed", "blocked"))

    def test_no_current_status_is_never_a_regression(self) -> None:
        self.assertFalse(is_step_status_regression(None, "pending"))

    def test_regression_error_is_a_runtime_progress_error(self) -> None:
        self.assertTrue(issubclass(ObservedStepRegressionError, RuntimeProgressError))


class LateRunningNeverOverwritesPassedTests(GuardTestCase):
    def test_late_running_is_refused_and_passed_is_kept(self) -> None:
        self.progress.record_step(
            attempt_id=self.attempt_id, step="Implementer", status="running"
        )
        self.progress.record_step(
            attempt_id=self.attempt_id,
            step="Implementer",
            status="passed",
            summary="executor returned completed",
        )
        with self.assertRaises(ObservedStepRegressionError) as raised:
            self.progress.record_step(
                attempt_id=self.attempt_id,
                step="Implementer",
                status="running",
                summary="late heartbeat",
            )
        self.assertIn("passed", str(raised.exception))
        self.assertEqual(self.status_of("Implementer"), "passed")
        steps = {step.name: step for step in self.progress.list_steps(self.attempt_id)}
        self.assertEqual(steps["Implementer"].summary, "executor returned completed")

    def test_pending_after_running_is_refused(self) -> None:
        self.progress.record_step(
            attempt_id=self.attempt_id, step="Prepare", status="running"
        )
        with self.assertRaises(ObservedStepRegressionError):
            self.progress.record_step(
                attempt_id=self.attempt_id, step="Prepare", status="pending"
            )
        self.assertEqual(self.status_of("Prepare"), "running")

    def test_refused_write_changes_no_row(self) -> None:
        self.progress.record_step(
            attempt_id=self.attempt_id, step="Validator", status="failed"
        )
        with closing(connect(self.db_path)) as conn:
            before = [
                tuple(row)
                for row in conn.execute("SELECT * FROM attempt_observed_steps ORDER BY id")
            ]
        with self.assertRaises(ObservedStepRegressionError):
            self.progress.record_step(
                attempt_id=self.attempt_id, step="Validator", status="running"
            )
        with closing(connect(self.db_path)) as conn:
            after = [
                tuple(row)
                for row in conn.execute("SELECT * FROM attempt_observed_steps ORDER BY id")
            ]
        self.assertEqual(before, after)

    def test_the_guard_is_per_attempt(self) -> None:
        self.progress.record_step(
            attempt_id=self.attempt_id, step="Implementer", status="passed"
        )
        self.attempts.close_attempt(
            self.attempt_id, status="failed", reason_code="retry", actor="test"
        )
        retry = self.attempts.create_attempt("AT-GUARD-1", executor="manual").attempt_id
        # A retry is a new Attempt; its steps start over.
        self.progress.record_step(attempt_id=retry, step="Implementer", status="running")
        self.assertEqual(
            self.progress.get_progress(retry).step_status("Implementer"), "running"
        )
        self.assertEqual(self.status_of("Implementer"), "passed")

    def test_forward_and_same_rank_writes_still_apply(self) -> None:
        for status in ("pending", "running", "running", "passed", "failed"):
            self.progress.record_step(
                attempt_id=self.attempt_id, step="Reviewer", status=status
            )
        self.assertEqual(self.status_of("Reviewer"), "failed")


class ConcurrentRecordStepThreadTests(GuardTestCase):
    def test_concurrent_writers_on_distinct_steps_lose_nothing(self) -> None:
        barrier = threading.Barrier(len(RUNTIME_STEPS))

        def write(step: str) -> None:
            store = RuntimeProgressStore(self.db_path)
            barrier.wait()
            store.record_step(attempt_id=self.attempt_id, step=step, status="running")
            store.record_step(attempt_id=self.attempt_id, step=step, status="passed")

        with ThreadPoolExecutor(max_workers=len(RUNTIME_STEPS)) as pool:
            list(pool.map(write, RUNTIME_STEPS))

        steps = self.progress.list_steps(self.attempt_id)
        self.assertEqual([step.name for step in steps], list(RUNTIME_STEPS))
        self.assertTrue(all(step.status == "passed" for step in steps))

    def test_racing_running_and_passed_on_one_step_always_ends_passed(self) -> None:
        self.attempts.close_attempt(
            self.attempt_id, status="failed", reason_code="next", actor="test"
        )
        for _ in range(10):
            attempt_id = self.attempts.create_attempt(
                "AT-GUARD-1", executor="manual"
            ).attempt_id
            barrier = threading.Barrier(8)
            refused: list[str] = []
            lock = threading.Lock()

            def write(index: int) -> None:
                store = RuntimeProgressStore(self.db_path)
                status = "passed" if index == 0 else "running"
                barrier.wait()
                try:
                    store.record_step(
                        attempt_id=attempt_id, step="Implementer", status=status
                    )
                except ObservedStepRegressionError:
                    with lock:
                        refused.append(status)

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(write, range(8)))
            snapshot = self.progress.get_progress(attempt_id)
            self.assertEqual(snapshot.step_status("Implementer"), "passed")
            self.assertTrue(all(status == "running" for status in refused))
            self.attempts.close_attempt(
                attempt_id, status="failed", reason_code="next", actor="test"
            )


class ConcurrentRecordStepProcessTests(unittest.TestCase):
    def test_processes_writing_steps_of_one_attempt_lose_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = create_rehearsal_fixture(Path(tmp) / "fixture")
            add_rehearsal_task(fixture, "AT-GUARD-PROC")
            attempts = AttemptStore(fixture.db_path)
            attempt_id = attempts.create_attempt(
                "AT-GUARD-PROC", executor="manual"
            ).attempt_id
            requests = [
                {
                    "op": "record-steps",
                    "db_path": str(fixture.db_path),
                    "attempt_id": attempt_id,
                    "writes": [[step, "running"], [step, "passed"], [step, "running"]],
                }
                for step in RUNTIME_STEPS
            ]
            results = run_worker_processes(requests)

            self.assertEqual(len(results), len(RUNTIME_STEPS))
            for result in results:
                self.assertEqual(result["outcome"], "done", result)
                # The trailing late "running" is refused, never applied.
                self.assertEqual(result["refused"], 1, result)
            steps = RuntimeProgressStore(fixture.db_path).list_steps(attempt_id)
            self.assertEqual([step.name for step in steps], list(RUNTIME_STEPS))
            self.assertTrue(all(step.status == "passed" for step in steps))


if __name__ == "__main__":
    unittest.main()
