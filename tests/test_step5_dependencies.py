"""V1 Step 5 acceptance: dependencies (SPEC §5, §43.6-§43.8; ruling 28 D4-D6)."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5_support import REPO_ROOT, RecordingExecutor, make_fixture, worker_env  # noqa: E402

from agent_taskflow.ticket_dependencies import (  # noqa: E402
    DEPENDENCY_SOURCE,
    TicketDependencyError,
    dependency_blocked_reason,
    is_dependency_blocked_reason,
    maintain_dependencies,
    remove_blocked_by,
    set_blocked_by,
)

CLI = "scripts/ticket_dependency.py"


class DependencyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        self.a = self.fx.create_ticket("Blocker A").task_key
        self.b = self.fx.create_ticket("Dependent B").task_key

    def snapshot(self, *keys: str):
        return [(self.fx.task_row(k), self.fx.events(k)) for k in keys]


class InvalidDependencyTests(DependencyTestCase):
    """§43.6: rejected at set time, nothing persisted."""

    def assert_rejected(self, task_key: str, blocker: str, fragment: str) -> None:
        keys = sorted({self.a, self.b, task_key})
        before = self.snapshot(*keys)
        with self.assertRaises(TicketDependencyError) as ctx:
            set_blocked_by(self.fx.db_path, task_key, blocker, actor="step5-test")
        self.assertIn(fragment, str(ctx.exception))
        self.assertEqual(self.snapshot(*keys), before)

    def test_self_dependency_is_rejected(self) -> None:
        self.assert_rejected(self.b, self.b, "cannot block itself")

    def test_unknown_blocker_is_rejected(self) -> None:
        self.assert_rejected(self.b, "AT-9999", "does not exist")

    def test_unknown_dependent_is_rejected(self) -> None:
        with self.assertRaises(TicketDependencyError):
            set_blocked_by(self.fx.db_path, "AT-9998", self.a, actor="step5-test")

    def test_two_ticket_cycle_is_rejected(self) -> None:
        set_blocked_by(self.fx.db_path, self.b, self.a, actor="step5-test")
        self.assert_rejected(self.a, self.b, "cycle")

    def test_longer_cycle_is_rejected(self) -> None:
        c = self.fx.create_ticket("C").task_key
        set_blocked_by(self.fx.db_path, self.b, self.a, actor="step5-test")
        set_blocked_by(self.fx.db_path, c, self.b, actor="step5-test")
        self.assert_rejected(self.a, c, "cycle")

    def test_creation_time_cycle_cannot_exist(self) -> None:
        # Step 1 checks the blocker exists; a new key cannot already be a blocker.
        created = self.fx.create_ticket("Created blocked", blocked_by=self.a)
        self.assertEqual(created.status, "blocked")
        self.assert_rejected(self.a, created.task_key, "cycle")


class SetDependencyTests(DependencyTestCase):
    def test_setting_blocked_by_on_a_ready_ticket_blocks_it_and_is_audited(self) -> None:
        change = set_blocked_by(self.fx.db_path, self.b, self.a, actor="operator")
        row = self.fx.task_row(self.b)
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["blocked_by"], self.a)
        self.assertTrue(is_dependency_blocked_reason(row["blocked_reason"]))
        self.assertEqual(row["blocked_reason"], dependency_blocked_reason(self.a))
        self.assertEqual((change.from_status, change.to_status), ("created", "blocked"))
        last = self.fx.status_events(self.b)[-1]
        self.assertEqual(last["source"], DEPENDENCY_SOURCE)
        self.assertEqual(last["payload"]["status"], "blocked")
        self.assertIn("ticket_dependency_set", self.fx.event_kinds(self.b))

    def test_replacing_the_blocker_keeps_it_blocked_on_the_new_one(self) -> None:
        c = self.fx.create_ticket("C").task_key
        set_blocked_by(self.fx.db_path, self.b, self.a, actor="operator")
        set_blocked_by(self.fx.db_path, self.b, c, actor="operator")
        row = self.fx.task_row(self.b)
        self.assertEqual((row["status"], row["blocked_by"]), ("blocked", c))
        self.assertEqual(row["blocked_reason"], dependency_blocked_reason(c))

    def test_removing_the_dependency_makes_it_ready_again(self) -> None:
        set_blocked_by(self.fx.db_path, self.b, self.a, actor="operator")
        change = remove_blocked_by(self.fx.db_path, self.b, actor="operator")
        row = self.fx.task_row(self.b)
        self.assertEqual((row["status"], row["blocked_by"]), ("created", None))
        self.assertEqual((change.from_status, change.to_status), ("blocked", "created"))
        self.assertIn("ticket_dependency_removed", self.fx.event_kinds(self.b))


class ReleaseOnlyAfterCompletedTests(DependencyTestCase):
    """§43.7 / §43.34 / §5.3."""

    def setUp(self) -> None:
        super().setUp()
        set_blocked_by(self.fx.db_path, self.b, self.a, actor="operator")

    def test_no_status_short_of_completed_releases(self) -> None:
        for status in (
            "created",
            "queued",
            # Running statuses cannot be written without a real claim
            # (runtime triggers); test_a_running_blocker_does_not_release
            # claims the blocker instead.
            "waiting_approval",
            "waiting_for_review",
            "accepted",
            "ready_for_integration",
            "integrating",
            "blocked",
            "paused",
            "needs_decision",
            "rejected",
            "unknown",
        ):
            with self.subTest(blocker_status=status):
                self.fx.set_status(self.a, status)
                result = maintain_dependencies(self.fx.db_path, actor="scheduler")
                self.assertEqual(result.released, ())
                self.assertEqual(self.fx.status(self.b), "blocked")

    def test_a_running_blocker_does_not_release(self) -> None:
        from agent_taskflow.runtime_admission import RuntimeAdmissionStore

        RuntimeAdmissionStore(self.fx.db_path).claim(self.a, owner_id="runner", ttl_seconds=600)
        self.assertEqual(self.fx.status(self.a), "preparing")
        result = maintain_dependencies(self.fx.db_path, actor="scheduler")
        self.assertEqual((result.released, result.needs_decision), ((), ()))
        self.assertEqual(self.fx.status(self.b), "blocked")

    def test_completed_releases_and_is_audited(self) -> None:
        for status in ("cleaned", "completed", "done"):
            with self.subTest(blocker_status=status):
                set_blocked_by(self.fx.db_path, self.b, self.a, actor="operator")
                self.fx.set_status(self.a, status)
                result = maintain_dependencies(self.fx.db_path, actor="scheduler")
                self.assertEqual(result.released, (self.b,))
                row = self.fx.task_row(self.b)
                self.assertEqual((row["status"], row["blocked_by"]), ("created", None))
                self.assertIn("ticket_dependency_released", self.fx.event_kinds(self.b))
                self.fx.set_status(self.a, "created")

    def test_a_failure_blocked_row_is_never_released(self) -> None:
        self.fx.set_status(self.b, "blocked", blocked_reason="Executor fake returned failed")
        self.fx.set_status(self.a, "cleaned")
        result = maintain_dependencies(self.fx.db_path, actor="scheduler")
        self.assertEqual(result.released, ())
        self.assertEqual(self.fx.status(self.b), "blocked")

    def test_created_with_blocked_by_is_released_by_the_same_rule(self) -> None:
        c = self.fx.create_ticket("Created with blocker", blocked_by=self.a).task_key
        self.assertIsNone(self.fx.task_row(c)["blocked_reason"])
        self.fx.set_status(self.a, "cleaned")
        result = maintain_dependencies(self.fx.db_path, actor="scheduler")
        self.assertIn(c, result.released)
        self.assertEqual(self.fx.status(c), "created")

    def test_maintenance_is_idempotent(self) -> None:
        self.fx.set_status(self.a, "cleaned")
        maintain_dependencies(self.fx.db_path, actor="scheduler")
        events = self.fx.events(self.b)
        again = maintain_dependencies(self.fx.db_path, actor="scheduler")
        self.assertEqual((again.released, again.needs_decision), ((), ()))
        self.assertEqual(self.fx.events(self.b), events)


class FailedOrCancelledBlockerTests(DependencyTestCase):
    """§43.8 / §5.4: never a silent release."""

    def test_failed_or_cancelled_blocker_sends_dependent_to_needs_decision(self) -> None:
        for status in ("failed", "canceled", "archived"):
            with self.subTest(blocker_status=status):
                set_blocked_by(self.fx.db_path, self.b, self.a, actor="operator")
                self.fx.set_status(self.a, status)
                result = maintain_dependencies(self.fx.db_path, actor="scheduler")
                self.assertEqual(result.needs_decision, (self.b,))
                row = self.fx.task_row(self.b)
                self.assertEqual(row["status"], "needs_decision")
                self.assertEqual(row["blocked_by"], self.a)
                last = self.fx.status_events(self.b)[-1]
                self.assertEqual(last["source"], DEPENDENCY_SOURCE)
                self.assertEqual(last["payload"]["status"], "needs_decision")
                self.assertIn(self.a, last["message"])
                self.assertIn("ticket_dependency_blocker_stopped", self.fx.event_kinds(self.b))
                self.assertEqual(self.fx.attempts(self.b), [])
                remove_blocked_by(self.fx.db_path, self.b, actor="operator")
                self.assertEqual(self.fx.status(self.b), "created")
                self.fx.set_status(self.a, "created")

    def test_running_dependent_is_not_interrupted_then_stops_for_decision(self) -> None:
        # D4: a runtime-discovered dependency on a Ticket that is already running.
        from agent_taskflow.runtime_admission import RuntimeAdmissionStore

        admission = RuntimeAdmissionStore(self.fx.db_path)
        claim = admission.claim(self.b, owner_id="running-executor", ttl_seconds=600)
        self.assertEqual(self.fx.status(self.b), "preparing")
        set_blocked_by(self.fx.db_path, self.b, self.a, actor="executor-report")
        self.assertEqual(self.fx.status(self.b), "preparing")
        self.fx.set_status(self.a, "failed")
        result = maintain_dependencies(self.fx.db_path, actor="scheduler")
        self.assertEqual(result.needs_decision, ())
        self.assertEqual(result.deferred_running, (self.b,))
        self.assertEqual(self.fx.status(self.b), "preparing")
        self.assertEqual(len(self.fx.leases(self.b, active_only=True)), 1)
        # Its Attempt ends; the next maintenance stops it for a decision.
        admission.release(
            claim.attempt_id,
            owner_id=claim.owner_id,
            lease_token=claim.lease_token,
            attempt_status="failed",
            task_status="failed",
            reason_code="executor_failed",
        )
        result = maintain_dependencies(self.fx.db_path, actor="scheduler")
        self.assertEqual(result.needs_decision, (self.b,))
        self.assertEqual(self.fx.status(self.b), "needs_decision")

    def test_dependent_of_a_stopped_blocker_never_runs(self) -> None:
        from agent_taskflow.ready_queue import eligible_tickets

        set_blocked_by(self.fx.db_path, self.b, self.a, actor="operator")
        self.fx.set_status(self.a, "failed")
        maintain_dependencies(self.fx.db_path, actor="scheduler")
        self.assertNotIn(self.b, [t.task_key for t in eligible_tickets(self.fx.db_path)])
        executor = RecordingExecutor()
        self.fx.dispatch(self.b, executor)
        self.assertEqual(executor.contexts, [])
        self.assertEqual(self.fx.status(self.b), "needs_decision")


class DependencyCliTests(DependencyTestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, CLI, "--db-path", str(self.fx.db_path), *args],
            cwd=REPO_ROOT,
            env=worker_env(),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_set_previews_without_confirmation_and_writes_with_it(self) -> None:
        before = self.snapshot(self.b)
        preview = self.run_cli("set", "--task-key", self.b, "--blocked-by", self.a, "--actor", "op")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertFalse(json.loads(preview.stdout)["mutated"])
        self.assertEqual(self.snapshot(self.b), before)

        done = self.run_cli(
            "set", "--task-key", self.b, "--blocked-by", self.a, "--actor", "op", "--confirm"
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        payload = json.loads(done.stdout)
        self.assertTrue(payload["mutated"])
        self.assertEqual(payload["to_status"], "blocked")
        self.assertEqual(self.fx.task_row(self.b)["blocked_by"], self.a)

    def test_cycle_is_refused_with_exit_2(self) -> None:
        set_blocked_by(self.fx.db_path, self.b, self.a, actor="op")
        before = self.snapshot(self.a, self.b)
        done = self.run_cli(
            "set", "--task-key", self.a, "--blocked-by", self.b, "--actor", "op", "--confirm"
        )
        self.assertEqual(done.returncode, 2)
        self.assertIn("cycle", done.stderr)
        self.assertEqual(self.snapshot(self.a, self.b), before)

    def test_remove_and_retry(self) -> None:
        set_blocked_by(self.fx.db_path, self.b, self.a, actor="op")
        self.fx.set_status(self.a, "failed")
        maintain_dependencies(self.fx.db_path, actor="scheduler")
        self.assertEqual(self.fx.status(self.b), "needs_decision")

        removed = self.run_cli("remove", "--task-key", self.b, "--actor", "op", "--confirm")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(self.fx.status(self.b), "created")

        self.fx.dispatch(self.a, RecordingExecutor("failed"))
        self.assertEqual(self.fx.status(self.a), "failed")
        retried = self.run_cli(
            "retry", "--task-key", self.a, "--actor", "op", "--reason", "retry blocker", "--confirm"
        )
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(json.loads(retried.stdout)["to_status"], "created")
        self.assertEqual(self.fx.status(self.a), "created")


class ReviewFixDependencyTests(DependencyTestCase):
    """B2 and S1 from the independent review."""

    def test_a_failure_blocked_dependent_keeps_its_reason_when_the_blocker_fails(self) -> None:
        set_blocked_by(self.fx.db_path, self.b, self.a, actor="op")
        self.fx.set_status(self.b, "blocked", blocked_reason="Executor fake returned failed")
        self.fx.set_status(self.a, "failed")
        result = maintain_dependencies(self.fx.db_path, actor="scheduler")
        self.assertEqual(result.needs_decision, ())
        row = self.fx.task_row(self.b)
        self.assertEqual((row["status"], row["blocked_reason"]), ("blocked", "Executor fake returned failed"))

    def test_a_failed_dependent_needs_the_audited_retry_not_just_a_removal(self) -> None:
        set_blocked_by(self.fx.db_path, self.b, self.a, actor="op")
        self.fx.set_status(self.b, "failed")
        self.fx.set_status(self.a, "failed")
        self.assertEqual(
            maintain_dependencies(self.fx.db_path, actor="scheduler").needs_decision, (self.b,)
        )
        change = remove_blocked_by(self.fx.db_path, self.b, actor="op")
        self.assertEqual((change.from_status, change.to_status), ("needs_decision", "needs_decision"))
        self.assertEqual(self.fx.status(self.b), "needs_decision")
        c = self.fx.create_ticket("New blocker").task_key
        change = set_blocked_by(self.fx.db_path, self.b, c, actor="op")
        self.assertEqual(change.to_status, "needs_decision")

    def test_a_dependency_wait_returns_to_ready_when_removed(self) -> None:
        set_blocked_by(self.fx.db_path, self.b, self.a, actor="op")
        self.fx.set_status(self.a, "canceled")
        maintain_dependencies(self.fx.db_path, actor="scheduler")
        self.assertEqual(self.fx.status(self.b), "needs_decision")
        self.assertEqual(remove_blocked_by(self.fx.db_path, self.b, actor="op").to_status, "created")

    def test_a_reserved_retry_attempt_blocks_setting_a_dependency(self) -> None:
        from agent_taskflow.task_status_reset import TaskStatusResetRequest, reset_task_status

        # A pre-Step-5 Ticket that failed as `blocked` is reset the legacy way,
        # which reserves a retry Attempt and moves it to `queued`.
        self.fx.dispatch(self.b, RecordingExecutor("failed"))
        self.fx.set_status(self.b, "blocked", blocked_reason="old failure")
        reset_task_status(
            TaskStatusResetRequest(
                task_key=self.b,
                db_path=self.fx.db_path,
                from_status="blocked",
                reason="legacy retry",
                confirm_reset=True,
            )
        )
        row = self.fx.task_row(self.b)
        self.assertEqual(row["status"], "queued")
        self.assertIsNotNone(row["active_attempt_id"])
        before = self.snapshot(self.b)
        with self.assertRaises(TicketDependencyError) as ctx:
            set_blocked_by(self.fx.db_path, self.b, self.a, actor="op")
        self.assertIn("reserved retry Attempt", str(ctx.exception))
        self.assertEqual(self.snapshot(self.b), before)


if __name__ == "__main__":
    unittest.main()
