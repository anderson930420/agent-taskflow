"""V1 Step 5 acceptance: SPEC §29.1 / §29.2 failure vocabulary for Tickets (ruling 27)."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5_support import (  # noqa: E402
    REPO_ROOT,
    RecordingExecutor,
    RecordingValidator,
    make_fixture,
    worker_env,
)

from agent_taskflow.models import TaskWorktreeRecord  # noqa: E402
from agent_taskflow.runtime_admission import RuntimeAdmissionStore  # noqa: E402
from agent_taskflow.runtime_reaper import reap_stale_runtime  # noqa: E402
from agent_taskflow.store import TaskMirrorStore  # noqa: E402
from agent_taskflow.task_status_reset import (  # noqa: E402
    TaskStatusResetError,
    TaskStatusResetRequest,
    reset_task_status,
)
from agent_taskflow.ticket_dependencies import set_blocked_by  # noqa: E402


class FailureVocabularyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        self.ticket = self.fx.create_ticket("Failure vocabulary")
        self.key = self.ticket.task_key

    def assert_ends(self, result, status: str, reason_fragment: str) -> None:
        self.assertEqual(result.status, status, result.summary)
        self.assertEqual(self.fx.status(self.key), status)
        last = self.fx.status_events(self.key)[-1]
        self.assertEqual(last["payload"]["status"], status)
        self.assertIn(reason_fragment, last["message"] or "")
        # The runtime lease and the Attempt are released at once: nothing
        # holds a capacity slot after a failure.
        self.assertEqual(self.fx.leases(self.key, active_only=True), [])
        attempts = self.fx.attempts(self.key)
        if attempts:
            self.assertEqual(attempts[-1]["is_active"], 0)
        self.assertIsNone(self.fx.task_row(self.key)["active_attempt_id"])


class ValidatorFailureStopsForDecisionTests(FailureVocabularyTestCase):
    def test_validator_failed_ends_needs_decision(self) -> None:
        result = self.fx.dispatch(
            self.key, RecordingExecutor(), (RecordingValidator(status="failed"),)
        )
        self.assert_ends(result, "needs_decision", "fake validator failed")
        attempt = self.fx.attempts(self.key)[-1]
        self.assertEqual(attempt["status"], "validation_failed")
        self.assertEqual(attempt["validation_result"], "failed")

    def test_validator_blocked_ends_failed(self) -> None:
        result = self.fx.dispatch(
            self.key, RecordingExecutor(), (RecordingValidator(status="blocked"),)
        )
        self.assert_ends(result, "failed", "fake validator blocked")

    def test_validator_raising_ends_failed(self) -> None:
        result = self.fx.dispatch(
            self.key,
            RecordingExecutor(),
            (RecordingValidator(raise_exc=RuntimeError("validator crashed")),),
        )
        self.assert_ends(result, "failed", "validator crashed")

    def test_unavailable_validator_ends_failed(self) -> None:
        dispatcher = self.fx.dispatcher(RecordingExecutor(), ())
        dispatcher.validators = ("does-not-exist",)
        try:
            result = dispatcher.dispatch_task(self.key)
        finally:
            dispatcher.store.shutdown_runtime_supervisors()
        self.assert_ends(result, "failed", "does-not-exist")


class RuntimeFailureEndsFailedTests(FailureVocabularyTestCase):
    def test_executor_failed_ends_failed(self) -> None:
        result = self.fx.dispatch(self.key, RecordingExecutor("failed", summary="build broke"))
        self.assert_ends(result, "failed", "build broke")
        self.assertEqual(self.fx.attempts(self.key)[-1]["status"], "failed")

    def test_executor_blocked_ends_failed(self) -> None:
        result = self.fx.dispatch(self.key, RecordingExecutor("blocked", summary="gave up"))
        self.assert_ends(result, "failed", "gave up")

    def test_executor_crash_ends_failed(self) -> None:
        result = self.fx.dispatch(
            self.key, RecordingExecutor(raise_exc=RuntimeError("executor crashed"))
        )
        self.assert_ends(result, "failed", "executor crashed")

    def test_unavailable_executor_ends_failed(self) -> None:
        dispatcher = self.fx.dispatcher(RecordingExecutor())
        dispatcher.executor_registry = {}
        try:
            result = dispatcher.dispatch_task(self.key, executor_name="does-not-exist")
        finally:
            dispatcher.store.shutdown_runtime_supervisors()
        self.assert_ends(result, "failed", "does-not-exist is unavailable")

    def test_worktree_preparation_failure_ends_failed(self) -> None:
        self.ticket.worktree_path.mkdir(parents=True)
        result = self.fx.dispatch(self.key, RecordingExecutor())
        self.assert_ends(result, "failed", "is not a git worktree")

    def test_governance_refusal_ends_failed(self) -> None:
        # The Ticket's artifact directory cannot be created: the dispatcher's
        # governance check refuses before any claim.
        self.ticket.artifact_dir.parent.mkdir(parents=True, exist_ok=True)
        self.ticket.artifact_dir.write_text("not a directory\n", encoding="utf-8")
        executor = RecordingExecutor()
        result = self.fx.dispatch(self.key, executor)
        self.assert_ends(result, "failed", "File exists")
        self.assertEqual(executor.contexts, [])
        self.assertEqual(self.fx.attempts(self.key), [])

    def test_capacity_slot_is_free_right_after_a_failure(self) -> None:
        self.fx.dispatch(self.key, RecordingExecutor("failed"))
        other = self.fx.create_ticket("Runs next")
        result = self.fx.dispatch(other.task_key, RecordingExecutor())
        self.assertEqual(result.status, "waiting_approval", result.summary)


class LeaseExpiryTests(FailureVocabularyTestCase):
    def expire(self, task_key: str) -> list[str]:
        claim = RuntimeAdmissionStore(self.fx.db_path).claim(
            task_key, owner_id="crashed-runner", ttl_seconds=60
        )
        with closing(sqlite3.connect(self.fx.db_path)) as conn, conn:
            conn.execute(
                "UPDATE runtime_leases SET expires_at = '2000-01-01T00:00:00Z' WHERE lease_id = ?",
                (claim.lease_id,),
            )
        return [claim.attempt_id]

    def test_ticket_lease_expiry_ends_failed_and_is_audited(self) -> None:
        expected = self.expire(self.key)
        result = reap_stale_runtime(self.fx.db_path)
        self.assertEqual(list(result.expired_attempt_ids), expected)
        self.assertEqual(self.fx.status(self.key), "failed")
        last = self.fx.status_events(self.key)[-1]
        self.assertEqual(last["source"], "runtime_lease_reaper")
        self.assertEqual(last["payload"]["status"], "failed")
        self.assertIn("runtime_lease_expired", last["message"])
        attempt = self.fx.attempts(self.key)[-1]
        self.assertEqual(attempt["status"], "execution_aborted")
        self.assertEqual(attempt["execution_result"], "lease_expired")
        # Idempotent: a second reap changes nothing.
        events = self.fx.events(self.key)
        self.assertEqual(reap_stale_runtime(self.fx.db_path).expired_attempt_ids, ())
        self.assertEqual(self.fx.events(self.key), events)

    def test_legacy_lease_expiry_still_ends_blocked(self) -> None:
        self.fx.add_legacy_task("AT-LEGACY-LEASE")
        self.expire("AT-LEGACY-LEASE")
        reap_stale_runtime(self.fx.db_path)
        row = self.fx.task_row("AT-LEGACY-LEASE")
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["blocked_reason"], "runtime_lease_expired")


class StoppedTicketIsNotRedispatchedTests(FailureVocabularyTestCase):
    def test_failed_and_needs_decision_refuse_untouched(self) -> None:
        for status in ("failed", "needs_decision"):
            with self.subTest(status=status):
                self.fx.set_status(self.key, status)
                before = self.fx.task_row(self.key)
                events = self.fx.events(self.key)
                executor = RecordingExecutor()
                result = self.fx.dispatch(self.key, executor)
                self.assertNotIn(result.status, {"waiting_approval", "preparing"})
                self.assertEqual(executor.contexts, [])
                self.assertEqual(self.fx.task_row(self.key), before)
                self.assertEqual(self.fx.events(self.key), events)
                self.assertEqual(self.fx.attempts(self.key), [])


class LegacyTasksKeepBlockedTests(unittest.TestCase):
    """Ruling 27f: the remap is for Tickets only."""

    def setUp(self) -> None:
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        self.fx.add_legacy_task("AT-LEGACY-FAIL")
        TaskMirrorStore(self.fx.db_path).upsert_task_worktree(
            TaskWorktreeRecord(
                task_key="AT-LEGACY-FAIL",
                repo_path=self.fx.repo,
                worktree_path=self.fx.repo / ".worktrees" / "AT-LEGACY-FAIL",
                branch="task/AT-LEGACY-FAIL",
                base_branch="main",
                status="active",
            )
        )

    def test_legacy_executor_and_validator_failures_still_block(self) -> None:
        result = self.fx.dispatch("AT-LEGACY-FAIL", RecordingExecutor("failed", summary="nope"))
        self.assertEqual(result.status, "blocked")
        self.assertEqual(self.fx.task_row("AT-LEGACY-FAIL")["blocked_reason"], "nope")


class TicketRetryPathTests(FailureVocabularyTestCase):
    def reset(self, from_status: str, **kwargs):
        return reset_task_status(
            TaskStatusResetRequest(
                task_key=self.key,
                db_path=self.fx.db_path,
                from_status=from_status,
                reason="operator retry",
                actor="step5-test",
                confirm_reset=True,
                **kwargs,
            )
        )

    def test_failed_ticket_retries_to_created_with_a_new_attempt(self) -> None:
        self.fx.dispatch(self.key, RecordingExecutor("failed"))
        old_attempt = self.fx.attempts(self.key)[-1]["attempt_id"]

        result = self.reset("failed", expected_old_attempt_id=old_attempt)

        self.assertTrue(result.mutated)
        self.assertEqual((result.from_status, result.to_status), ("failed", "created"))
        self.assertEqual(result.old_attempt_id, old_attempt)
        self.assertEqual(self.fx.status(self.key), "created")
        self.assertEqual(self.fx.status_events(self.key)[-1]["payload"]["status"], "created")
        self.assertIn("ticket_retry_reset", self.fx.event_kinds(self.key))
        self.assertEqual(self.fx.dispatch(self.key, RecordingExecutor()).status, "waiting_approval")
        self.assertEqual([a["attempt_number"] for a in self.fx.attempts(self.key)], [1, 2])

    def test_needs_decision_ticket_retries_to_created(self) -> None:
        self.fx.dispatch(self.key, RecordingExecutor(), (RecordingValidator(status="failed"),))
        result = self.reset("needs_decision")
        self.assertEqual(result.to_status, "created")
        self.assertEqual(self.fx.status(self.key), "created")

    def test_dry_run_and_missing_confirmation_write_nothing(self) -> None:
        self.fx.dispatch(self.key, RecordingExecutor("failed"))
        before = self.fx.task_row(self.key)
        events = self.fx.events(self.key)
        preview = reset_task_status(
            TaskStatusResetRequest(
                task_key=self.key,
                db_path=self.fx.db_path,
                from_status="failed",
                reason="preview",
                dry_run=True,
            )
        )
        self.assertFalse(preview.mutated)
        self.assertEqual(preview.to_status, "created")
        with self.assertRaises(TaskStatusResetError):
            reset_task_status(
                TaskStatusResetRequest(
                    task_key=self.key,
                    db_path=self.fx.db_path,
                    from_status="failed",
                    reason="unconfirmed",
                )
            )
        self.assertEqual(self.fx.task_row(self.key), before)
        self.assertEqual(self.fx.events(self.key), events)

    def test_status_mismatch_is_refused(self) -> None:
        self.fx.dispatch(self.key, RecordingExecutor("failed"))
        with self.assertRaises(TaskStatusResetError):
            self.reset("needs_decision")
        self.assertEqual(self.fx.status(self.key), "failed")

    def test_retry_with_an_unreleased_dependency_is_refused(self) -> None:
        blocker = self.fx.create_ticket("Still running blocker")
        self.fx.dispatch(self.key, RecordingExecutor("failed"))
        set_blocked_by(self.fx.db_path, self.key, blocker.task_key, actor="step5-test")
        with self.assertRaises(TaskStatusResetError) as ctx:
            self.reset("failed")
        self.assertIn(blocker.task_key, str(ctx.exception))
        self.assertEqual(self.fx.status(self.key), "failed")

    def test_legacy_row_cannot_reset_from_failed(self) -> None:
        self.fx.add_legacy_task("AT-LEGACY-RESET", status="failed")
        with self.assertRaises(TaskStatusResetError):
            reset_task_status(
                TaskStatusResetRequest(
                    task_key="AT-LEGACY-RESET",
                    db_path=self.fx.db_path,
                    from_status="failed",
                    reason="not for legacy",
                    confirm_reset=True,
                )
            )
        self.assertEqual(self.fx.status("AT-LEGACY-RESET"), "failed")

    def test_cli_accepts_failed_for_a_ticket(self) -> None:
        self.fx.dispatch(self.key, RecordingExecutor("failed"))
        completed = subprocess.run(
            [
                sys.executable,
                "scripts/reset_task_status.py",
                "--task-key",
                self.key,
                "--db-path",
                str(self.fx.db_path),
                "--from-status",
                "failed",
                "--reason",
                "operator retry",
                "--confirm-reset",
            ],
            cwd=REPO_ROOT,
            env=worker_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('"to_status": "created"', completed.stdout)
        self.assertEqual(self.fx.status(self.key), "created")


class ReviewFixRetryGuardTests(FailureVocabularyTestCase):
    """B1: a retry never starts while a previous Attempt's process may be alive."""

    def test_retry_is_refused_while_attempt_resources_may_still_run(self) -> None:
        self.fx.dispatch(self.key, RecordingExecutor("failed"))
        with closing(sqlite3.connect(self.fx.db_path)) as conn, conn:
            conn.execute(
                "UPDATE attempt_resources SET status = 'reap_blocked_live_pid' WHERE task_key = ?",
                (self.key,),
            )
        request = TaskStatusResetRequest(
            task_key=self.key,
            db_path=self.fx.db_path,
            from_status="failed",
            reason="retry too early",
            confirm_reset=True,
        )
        with self.assertRaises(TaskStatusResetError) as ctx:
            reset_task_status(request)
        self.assertIn("may still be alive", str(ctx.exception))
        self.assertEqual(self.fx.status(self.key), "failed")
        with closing(sqlite3.connect(self.fx.db_path)) as conn, conn:
            conn.execute(
                "UPDATE attempt_resources SET status = 'reaped' WHERE task_key = ?", (self.key,)
            )
        self.assertEqual(reset_task_status(request).to_status, "created")


class ReviewFixRefusalTests(FailureVocabularyTestCase):
    """S2: refusing to start a Ticket never rewrites it."""

    def test_integration_and_other_statuses_are_refused_untouched(self) -> None:
        for status in ("ready_for_integration", "integrating", "unknown", "archived"):
            with self.subTest(status=status):
                self.fx.set_status(self.key, status)
                before = self.fx.task_row(self.key)
                events = self.fx.events(self.key)
                result = self.fx.dispatch(self.key, RecordingExecutor())
                self.assertIn("not runnable", result.summary)
                self.assertEqual(self.fx.task_row(self.key), before)
                self.assertEqual(self.fx.events(self.key), events)


if __name__ == "__main__":
    unittest.main()
