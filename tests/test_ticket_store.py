"""Ticket persistence, Task ID allocation and audit-log tests (SPEC §12, §44)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore, connect
from agent_taskflow.ticket_metadata import format_ticket_id
from agent_taskflow.ticket_models import TicketRecord
from agent_taskflow.ticket_schema import TICKET_CREATION_MIGRATION
from agent_taskflow.ticket_store import (
    TicketAllocation,
    TicketStore,
    TicketStoreError,
)


class TicketStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "forms"
        self.store = TicketStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def build(self, allocation: TicketAllocation, **overrides: object) -> TicketRecord:
        fields: dict[str, object] = {
            "ticket_id": allocation.ticket_id,
            "repository": "forms",
            "prompt": "Separate the ending page image",
            "title": "Separate the ending page image",
            "priority": "normal",
            "status": "ready",
            "repo_path": self.repo_path,
            "base_branch": "main",
            "branch": f"task/{allocation.ticket_id}-separate",
            "worktree_path": self.repo_path / ".worktrees" / allocation.ticket_id,
            "artifact_dir": self.root / "artifacts" / allocation.ticket_id,
            "ticket_prefix": allocation.ticket_prefix,
            "ticket_sequence": allocation.ticket_sequence,
        }
        fields.update(overrides)
        return TicketRecord(**fields)  # type: ignore[arg-type]

    def create(self, prefix: str = "AT", **overrides: object) -> TicketRecord:
        return self.store.create_ticket(
            ticket_prefix=prefix,
            build=lambda allocation: self.build(allocation, **overrides),
            actor="test",
            payload={"kind": "ticket_created"},
            blocked_by=overrides.get("blocked_by"),  # type: ignore[arg-type]
        )


class MigrationTests(TicketStoreTestCase):
    def test_migration_is_recorded_and_idempotent(self) -> None:
        self.store.init_db()
        with closing(connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT name FROM schema_migrations WHERE name = ?",
                (TICKET_CREATION_MIGRATION,),
            ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_legacy_task_mirror_tables_still_exist(self) -> None:
        with closing(connect(self.db_path)) as conn:
            names = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        self.assertIn("tasks", names)
        self.assertIn("tickets", names)
        self.assertIn("ticket_events", names)


class AllocationTests(TicketStoreTestCase):
    def test_task_ids_increment_per_prefix(self) -> None:
        first = self.create()
        second = self.create()
        self.assertEqual(first.ticket_id, format_ticket_id("AT", 1))
        self.assertEqual(second.ticket_id, format_ticket_id("AT", 2))

    def test_prefixes_have_independent_counters(self) -> None:
        self.create(prefix="AT")
        other = self.create(prefix="BJ")
        self.assertEqual(other.ticket_id, format_ticket_id("BJ", 1))

    def test_allocation_skips_legacy_mirror_task_keys(self) -> None:
        mirror = TaskMirrorStore(self.db_path)
        mirror.init_db()
        mirror.upsert_task(
            TaskRecord(
                task_key="AT-007",
                project="forms",
                status="queued",
                repo_path=self.repo_path,
            )
        )
        ticket = self.create()
        self.assertEqual(ticket.ticket_id, format_ticket_id("AT", 8))

    def test_worktree_paths_are_unique_at_the_storage_layer(self) -> None:
        first = self.create()
        with self.assertRaises(TicketStoreError):
            self.store.create_ticket(
                ticket_prefix="AT",
                build=lambda allocation: self.build(
                    allocation,
                    worktree_path=first.worktree_path,
                ),
                actor="test",
            )

    def test_builder_must_honour_the_allocated_task_id(self) -> None:
        with self.assertRaises(TicketStoreError):
            self.store.create_ticket(
                ticket_prefix="AT",
                build=lambda allocation: self.build(
                    TicketAllocation("AT-999", "AT", 999)
                ),
                actor="test",
            )


class AuditTests(TicketStoreTestCase):
    def test_creation_writes_one_audit_event(self) -> None:
        ticket = self.create()
        events = self.store.list_ticket_events(ticket.ticket_id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "ticket_created")
        self.assertEqual(events[0].actor, "test")

    def test_ticket_events_are_append_only(self) -> None:
        ticket = self.create()
        with closing(connect(self.db_path)) as conn, conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE ticket_events SET actor = 'tamper' WHERE ticket_id = ?",
                    (ticket.ticket_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM ticket_events WHERE ticket_id = ?",
                    (ticket.ticket_id,),
                )

    def test_failed_insert_leaves_no_orphan_audit_event(self) -> None:
        first = self.create()
        with self.assertRaises(TicketStoreError):
            self.store.create_ticket(
                ticket_prefix="AT",
                build=lambda allocation: self.build(
                    allocation,
                    worktree_path=first.worktree_path,
                ),
                actor="test",
            )
        with closing(connect(self.db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS total FROM ticket_events"
            ).fetchone()["total"]
        self.assertEqual(count, 1)


class BlockedByTests(TicketStoreTestCase):
    def test_unknown_blocker_is_refused(self) -> None:
        with self.assertRaises(TicketStoreError):
            self.create(blocked_by="AT-404", status="blocked")

    def test_existing_blocker_is_accepted(self) -> None:
        blocker = self.create()
        dependent = self.create(blocked_by=blocker.ticket_id, status="blocked")
        self.assertEqual(dependent.blocked_by, blocker.ticket_id)
        self.assertEqual(dependent.status, "blocked")


class ReadbackTests(TicketStoreTestCase):
    def test_get_and_list_round_trip(self) -> None:
        ticket = self.create()
        self.assertEqual(self.store.get_ticket(ticket.ticket_id), ticket)
        self.assertIsNone(self.store.get_ticket("AT-999"))
        self.assertEqual(
            [item.ticket_id for item in self.store.list_tickets(repository="forms")],
            [ticket.ticket_id],
        )
        self.assertEqual(self.store.list_tickets(repository="other"), [])

    def test_list_rejects_unknown_status_filter(self) -> None:
        with self.assertRaises(ValueError):
            self.store.list_tickets(status="not-a-status")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
