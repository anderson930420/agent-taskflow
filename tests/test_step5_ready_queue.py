"""V1 Step 5 acceptance: the ready queue (SPEC §7, §20; ruling 28 D3)."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5_support import make_fixture  # noqa: E402

from agent_taskflow.lifecycle_control import RuntimeControlStore  # noqa: E402
from agent_taskflow.ready_queue import PRIORITY_RANK, eligible_tickets  # noqa: E402
from agent_taskflow.runtime_admission import RuntimeAdmissionStore  # noqa: E402
from agent_taskflow.ticket_dependencies import set_blocked_by  # noqa: E402


class ReadyQueueTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)

    def keys(self) -> list[str]:
        return [ticket.task_key for ticket in eligible_tickets(self.fx.db_path)]

    def set_created_at(self, task_key: str, value: str) -> None:
        with closing(sqlite3.connect(self.fx.db_path)) as conn, conn:
            conn.execute("UPDATE tasks SET created_at = ? WHERE task_key = ?", (value, task_key))


class OrderingTests(ReadyQueueTestCase):
    def test_priority_then_fifo_then_task_key(self) -> None:
        low = self.fx.create_ticket("low", priority="low").task_key
        normal_late = self.fx.create_ticket("normal late", priority="normal").task_key
        critical = self.fx.create_ticket("critical", priority="critical").task_key
        normal_early = self.fx.create_ticket("normal early", priority="normal").task_key
        high = self.fx.create_ticket("high", priority="high").task_key
        tie_b = self.fx.create_ticket("tie b", priority="normal").task_key
        tie_a = self.fx.create_ticket("tie a", priority="normal").task_key
        self.set_created_at(normal_early, "2026-01-01T00:00:00Z")
        self.set_created_at(normal_late, "2026-01-03T00:00:00Z")
        self.set_created_at(tie_a, "2026-01-02T00:00:00Z")
        self.set_created_at(tie_b, "2026-01-02T00:00:00Z")

        expected = [critical, high, normal_early] + sorted([tie_a, tie_b]) + [normal_late, low]
        self.assertEqual(self.keys(), expected)
        # Deterministic: the same database always gives the same order.
        self.assertEqual(self.keys(), expected)

    def test_priority_rank_is_spec_order(self) -> None:
        self.assertEqual(
            sorted(PRIORITY_RANK, key=PRIORITY_RANK.__getitem__),
            ["critical", "high", "normal", "low"],
        )


class EligibilityTests(ReadyQueueTestCase):
    def test_only_claimable_unowned_unblocked_tickets_are_eligible(self) -> None:
        ready = self.fx.create_ticket("ready").task_key
        blocked = self.fx.create_ticket("blocked by dependency").task_key
        set_blocked_by(self.fx.db_path, blocked, ready, actor="step5-test")
        paused = self.fx.create_ticket("paused").task_key
        self.fx.set_status(paused, "paused")
        failed = self.fx.create_ticket("failed").task_key
        self.fx.set_status(failed, "failed")
        decision = self.fx.create_ticket("needs decision").task_key
        self.fx.set_status(decision, "needs_decision")
        owned = self.fx.create_ticket("owned").task_key
        RuntimeAdmissionStore(self.fx.db_path).claim(owned, owner_id="elsewhere", ttl_seconds=600)
        self.fx.add_legacy_task("AT-LEGACY-QUEUED", status="queued")

        self.assertEqual(self.keys(), [ready])

    def test_created_ticket_with_an_unreleased_blocker_is_not_eligible(self) -> None:
        blocker = self.fx.create_ticket("blocker").task_key
        dependent = self.fx.create_ticket("dependent").task_key
        with closing(sqlite3.connect(self.fx.db_path)) as conn, conn:
            conn.execute("UPDATE tasks SET blocked_by = ? WHERE task_key = ?", (blocker, dependent))
        self.assertEqual(self.keys(), [blocker])
        self.fx.set_status(blocker, "cleaned")
        self.assertEqual(self.keys(), [dependent])

    def test_paused_project_is_not_eligible(self) -> None:
        from agent_taskflow.project_class_control_schema import migrate_project_class_controls

        ticket = self.fx.create_ticket("project paused").task_key
        migrate_project_class_controls(self.fx.db_path)
        RuntimeControlStore(self.fx.db_path).pause(
            scope_kind="project",
            scope_id="step5",
            actor="step5-test",
        )
        self.assertNotIn(ticket, self.keys())

    def test_empty_queue(self) -> None:
        self.assertEqual(self.keys(), [])


if __name__ == "__main__":
    unittest.main()
