"""V1 Step 5, ruling 32: the dependency gate lives at runtime admission.

SPEC §44 "Dependency releases only after blocker completed" / §43.7 must hold
on every path that can start a Ticket, not only in the scheduler's selection.
These tests reproduce the independent review's path (PR #201 review, FAIL
item 1): a `blocked` Ticket whose `blocked_reason` is not a dependency wait,
with an unreleased blocker, is reset and then started directly, through the
API and through a scheduler tick.
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5_support import RecordingExecutor, make_fixture, worker_launcher  # noqa: E402

import agent_taskflow.canonical_runtime_path as canonical_path  # noqa: E402
from agent_taskflow.attempt_schema import migrate_task_attempt_lifecycle  # noqa: E402
from agent_taskflow.parallel_scheduler import run_scheduler_tick  # noqa: E402
from agent_taskflow.reset_lineage import ResetLineageStore  # noqa: E402
from agent_taskflow.runtime_admission import (  # noqa: E402
    RuntimeAdmissionStore,
    RuntimeDependencyUnreleasedError,
)
from agent_taskflow.store import TaskMirrorStore  # noqa: E402
from agent_taskflow.task_status_reset import (  # noqa: E402
    TaskStatusResetError,
    TaskStatusResetRequest,
    reset_task_status,
)
from agent_taskflow.ticket_dependencies import set_blocked_by  # noqa: E402


class DependencyAdmissionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        self.a = self.fx.create_ticket("Blocker A").task_key
        self.b = self.fx.create_ticket("Dependent B").task_key

    def reviewer_state(self) -> None:
        """Steps 1-3 of the review's reproduction."""
        # 1. B ran once and failed (SPEC §29.2: `failed`).
        self.fx.dispatch(self.b, RecordingExecutor("failed"))
        # 2. An operator held B through the /block route's store call.
        TaskMirrorStore(self.fx.db_path).update_task_status(
            self.b, "blocked", source="api", blocked_reason="operator hold"
        )
        # 3. set_blocked_by keeps B `blocked` and keeps that reason.
        set_blocked_by(self.fx.db_path, self.b, self.a, actor="operator")
        row = self.fx.task_row(self.b)
        self.assertEqual((row["status"], row["blocked_reason"], row["blocked_by"]),
                         ("blocked", "operator hold", self.a))

    def reserved_retry_bypassing_the_reset_guard(self) -> None:
        """The review's step 4 outcome: `queued` with a reserved retry Attempt."""
        ResetLineageStore(self.fx.db_path).reserve_retry(
            self.b, reason="lower-level reservation", actor="step5-test"
        )
        row = self.fx.task_row(self.b)
        self.assertEqual((row["status"], row["blocked_by"]), ("queued", self.a))
        self.assertIsNotNone(row["active_attempt_id"])

    def snapshot(self, key: str):
        migrate_task_attempt_lifecycle(self.fx.db_path)  # ruling 14 backfill first
        return (
            self.fx.task_row(key),
            self.fx.events(key),
            self.fx.attempts(key),
            self.fx.leases(key),
        )

    def lineage_states(self, key: str) -> list[str]:
        with closing(sqlite3.connect(self.fx.db_path)) as conn:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT state FROM reset_lineages WHERE task_key = ? ORDER BY created_at",
                    (key,),
                )
            ]


class ResetRefusesTests(DependencyAdmissionTestCase):
    """Ruling 32c."""

    def test_reset_of_the_reviewers_ticket_is_refused(self) -> None:
        self.reviewer_state()
        before = self.snapshot(self.b)
        with self.assertRaises(TaskStatusResetError) as ctx:
            reset_task_status(
                TaskStatusResetRequest(
                    task_key=self.b,
                    db_path=self.fx.db_path,
                    from_status="blocked",
                    reason="retry",
                    confirm_reset=True,
                )
            )
        self.assertIn(self.a, str(ctx.exception))
        self.assertIn("scripts/ticket_dependency.py", str(ctx.exception))
        self.assertEqual(self.snapshot(self.b), before)

    def test_reset_is_refused_for_an_unknown_blocker_too(self) -> None:
        self.reviewer_state()
        with closing(sqlite3.connect(self.fx.db_path)) as conn, conn:
            conn.execute("UPDATE tasks SET blocked_by = 'AT-9999' WHERE task_key = ?", (self.b,))
        with self.assertRaises(TaskStatusResetError):
            reset_task_status(
                TaskStatusResetRequest(
                    task_key=self.b,
                    db_path=self.fx.db_path,
                    from_status="blocked",
                    reason="retry",
                    confirm_reset=True,
                )
            )
        self.assertEqual(self.fx.status(self.b), "blocked")

    def test_reset_proceeds_once_the_blocker_completed(self) -> None:
        self.reviewer_state()
        self.fx.set_status(self.a, "cleaned")
        result = reset_task_status(
            TaskStatusResetRequest(
                task_key=self.b,
                db_path=self.fx.db_path,
                from_status="blocked",
                reason="retry",
                confirm_reset=True,
            )
        )
        self.assertEqual(result.to_status, "queued")


class StartPathsRefuseTests(DependencyAdmissionTestCase):
    """Ruling 32e (i)-(iii), on the state a pre-fix reset would have produced."""

    def setUp(self) -> None:
        super().setUp()
        self.reviewer_state()
        self.reserved_retry_bypassing_the_reset_guard()

    def assert_untouched(self, before) -> None:
        self.assertEqual(self.snapshot(self.b), before)
        self.assertEqual(self.lineage_states(self.b), ["reserved"])
        self.assertEqual(self.fx.leases(self.b, active_only=True), [])

    def test_i_direct_dispatch_is_refused(self) -> None:
        before = self.snapshot(self.b)
        executor = RecordingExecutor()
        result = self.fx.dispatch(self.b, executor)
        self.assertEqual(executor.contexts, [])
        self.assertNotEqual(result.status, "ready_for_integration")
        self.assertIn(self.a, result.summary)
        self.assert_untouched(before)

    def test_ii_api_start_is_refused(self) -> None:
        from fastapi.testclient import TestClient

        from agent_taskflow.api.main import create_app

        before = self.snapshot(self.b)
        with TestClient(create_app(self.fx.db_path)) as client:
            response = client.post(f"/api/tasks/{self.b}/start", json={"validators": []})
        body = response.json()
        self.assertFalse(body["ok"], body)
        self.assertIn(self.a, json.dumps(body))
        self.assert_untouched(before)

    def test_iii_scheduler_tick_does_not_start_it(self) -> None:
        sync = self.fx.root / "sync"
        sync.mkdir()
        before = self.snapshot(self.b)
        result = run_scheduler_tick(self.fx.db_path, launcher=worker_launcher(sync), wait=True)
        # The blocker itself is eligible and may run; the dependent never starts.
        self.assertNotIn(self.b, result.candidates)
        self.assertNotIn(self.b, [s.task_key for s in result.started])
        self.assertNotEqual(self.fx.status(self.a), "cleaned")
        self.assert_untouched(before)

    def test_the_installed_adoption_claim_refuses(self) -> None:
        # The reset-reserved Attempt is adopted by its own claim transaction
        # (reset_runtime_path); the gate is there too.
        before = self.snapshot(self.b)
        admission = canonical_path.CanonicalRuntimeAdmissionStore(self.fx.db_path)
        with self.assertRaises(RuntimeDependencyUnreleasedError) as ctx:
            admission.claim(self.b, owner_id="step5-test")
        self.assertEqual(ctx.exception.blocker, self.a)
        self.assertEqual(ctx.exception.reason_code, "runtime_dependency_unreleased")
        self.assert_untouched(before)

    def test_completed_blocker_makes_the_reserved_retry_runnable(self) -> None:
        self.fx.set_status(self.a, "cleaned")
        result = self.fx.dispatch(self.b, RecordingExecutor())
        self.assertEqual(result.status, "ready_for_integration", result.summary)
        self.assertEqual(self.lineage_states(self.b), ["claimed"])


class ClaimRefusesTests(DependencyAdmissionTestCase):
    """Ruling 32a/b: RuntimeAdmissionStore.claim() itself refuses."""

    def force_ready_with_blocker(self, blocker: str) -> None:
        # Any route that bypassed set_blocked_by / the ready queue.
        with closing(sqlite3.connect(self.fx.db_path)) as conn, conn:
            conn.execute(
                "UPDATE tasks SET blocked_by = ?, status = 'created' WHERE task_key = ?",
                (blocker, self.b),
            )

    def test_claim_refuses_an_unreleased_blocker_and_writes_nothing(self) -> None:
        self.force_ready_with_blocker(self.a)
        for status in ("created", "queued", "waiting_approval", "blocked", "failed", "canceled"):
            with self.subTest(blocker_status=status):
                self.fx.set_status(self.a, status)
                before = self.snapshot(self.b)
                with self.assertRaises(RuntimeDependencyUnreleasedError) as ctx:
                    RuntimeAdmissionStore(self.fx.db_path).claim(self.b, owner_id="step5-test")
                self.assertEqual(ctx.exception.blocker_status, status)
                self.assertEqual(self.snapshot(self.b), before)

    def test_claim_refuses_an_unknown_blocker(self) -> None:
        self.force_ready_with_blocker("AT-9999")
        before = self.snapshot(self.b)
        with self.assertRaises(RuntimeDependencyUnreleasedError) as ctx:
            RuntimeAdmissionStore(self.fx.db_path).claim(self.b, owner_id="step5-test")
        self.assertIsNone(ctx.exception.blocker_status)
        self.assertIn("does not exist", str(ctx.exception))
        self.assertEqual(self.snapshot(self.b), before)

    def test_capacity_is_still_checked_first(self) -> None:
        from agent_taskflow.runtime_admission import RuntimeCapacityExceededError

        self.force_ready_with_blocker(self.a)
        RuntimeAdmissionStore(self.fx.db_path).claim(self.a, owner_id="holder", ttl_seconds=600)
        with self.assertRaises(RuntimeCapacityExceededError):
            RuntimeAdmissionStore(self.fx.db_path).claim(self.b, owner_id="step5-test")

    def test_completed_blocker_releases_the_claim(self) -> None:
        self.force_ready_with_blocker(self.a)
        for status in ("cleaned", "completed", "done"):
            with self.subTest(blocker_status=status):
                self.fx.set_status(self.a, status)
                claim = RuntimeAdmissionStore(self.fx.db_path).claim(self.b, owner_id="step5-test")
                self.assertEqual(self.fx.status(self.b), "preparing")
                RuntimeAdmissionStore(self.fx.db_path).release(
                    claim.attempt_id,
                    owner_id=claim.owner_id,
                    lease_token=claim.lease_token,
                    attempt_status="canceled",
                    task_status="created",
                    reason_code="runtime_canceled",
                )

    def test_a_ticket_without_blocked_by_is_unaffected(self) -> None:
        RuntimeAdmissionStore(self.fx.db_path).claim(self.b, owner_id="step5-test")
        self.assertEqual(self.fx.status(self.b), "preparing")


if __name__ == "__main__":
    unittest.main()
