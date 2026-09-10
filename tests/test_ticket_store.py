"""Ticket persistence over the canonical `tasks` table (PR #195 ruling).

`tasks` is the only canonical Ticket entity. There must be no `tickets` or
`ticket_events` table; Ticket columns live on `tasks` and the creation audit
event lives in `task_events`.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from agent_taskflow import store as store_module
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TASK_TICKET_COLUMNS, TaskMirrorStore, connect
from agent_taskflow.ticket_models import TicketRecord
from agent_taskflow.ticket_store import TicketStore, TicketStoreError


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

    def build(self, task_key: str, **overrides: object) -> TicketRecord:
        fields: dict[str, object] = {
            "task_key": task_key,
            "repository": "forms",
            "prompt": "Separate the ending page image",
            "title": "Separate the ending page image",
            "priority": "normal",
            "status": "created",
            "repo_path": self.repo_path,
            "base_branch": "main",
            "branch": f"task/{task_key}-separate",
            "worktree_path": self.repo_path / ".worktrees" / task_key,
            "artifact_dir": self.root / "artifacts" / task_key,
        }
        fields.update(overrides)
        return TicketRecord(**fields)  # type: ignore[arg-type]

    def create(self, **overrides: object) -> TicketRecord:
        return self.store.create_ticket(
            build=lambda task_key: self.build(task_key, **overrides),
            actor="test",
            payload={"kind": "ticket_created"},
            blocked_by=overrides.get("blocked_by"),  # type: ignore[arg-type]
        )

    def legacy_task(self, task_key: str, **overrides: object) -> None:
        fields: dict[str, object] = {
            "task_key": task_key,
            "project": "forms",
            "status": "queued",
            "repo_path": self.repo_path,
        }
        fields.update(overrides)
        TaskMirrorStore(self.db_path).upsert_task(TaskRecord(**fields))  # type: ignore[arg-type]

    def table_names(self) -> set[str]:
        with closing(connect(self.db_path)) as conn:
            return {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }


class CanonicalEntityTests(TicketStoreTestCase):
    """The ruling: `tasks` is the only Ticket entity."""

    def test_no_separate_ticket_table_exists(self) -> None:
        tables = self.table_names()
        self.assertIn("tasks", tables)
        self.assertIn("task_events", tables)
        self.assertNotIn("tickets", tables)
        self.assertNotIn("ticket_events", tables)

    def test_no_separate_ticket_table_exists_after_creating_a_ticket(self) -> None:
        self.create()
        self.assertNotIn("tickets", self.table_names())

    def test_ticket_columns_live_on_tasks(self) -> None:
        with closing(connect(self.db_path)) as conn:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
        for column, _sql in TASK_TICKET_COLUMNS:
            self.assertIn(column, columns)
        for column in ("priority", "title", "ai_title_status", "prompt"):
            self.assertIn(column, columns)

    def test_migration_is_registered_with_the_task_store(self) -> None:
        self.assertIn("tasks_ticket_fields", store_module.SCHEMA_MIGRATIONS)
        with closing(connect(self.db_path)) as conn:
            recorded = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name = 'tasks_ticket_fields'"
            ).fetchone()
        self.assertIsNotNone(recorded)

    def test_old_ticket_migration_is_gone(self) -> None:
        self.assertNotIn("v1_ticket_creation_v1", store_module.SCHEMA_MIGRATIONS)
        with closing(connect(self.db_path)) as conn:
            recorded = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name = 'v1_ticket_creation_v1'"
            ).fetchone()
        self.assertIsNone(recorded)

    def test_migration_is_idempotent(self) -> None:
        self.store.init_db()
        TaskMirrorStore(self.db_path).init_db()
        self.create()

    def test_ticket_is_a_task_mirror_row(self) -> None:
        ticket = self.create()
        mirrored = TaskMirrorStore(self.db_path).get_task(ticket.task_key)
        assert mirrored is not None
        self.assertEqual(mirrored.status, "created")
        self.assertEqual(mirrored.title, ticket.title)
        self.assertEqual(mirrored.project, "forms")
        self.assertEqual(mirrored.artifact_dir, ticket.artifact_dir)


class AllocationTests(TicketStoreTestCase):
    """One global counter, zero-padded to 4 digits: AT-0001."""

    def test_task_keys_come_from_one_global_counter(self) -> None:
        self.assertEqual(self.create().task_key, "AT-0001")
        self.assertEqual(self.create().task_key, "AT-0002")

    def test_counter_is_shared_across_repositories(self) -> None:
        first = self.create(repository="forms")
        second = self.create(repository="bullet_journal")
        self.assertEqual(first.task_key, "AT-0001")
        self.assertEqual(second.task_key, "AT-0002")

    def test_allocation_skips_legacy_mirror_task_keys(self) -> None:
        self.legacy_task("AT-0007")
        self.assertEqual(self.create().task_key, "AT-0008")

    def test_non_counter_task_keys_are_ignored(self) -> None:
        for key in ("AT-GH-188", "AT-MC-SMOKE", "BJ-0042"):
            self.legacy_task(key)
        self.assertEqual(self.create().task_key, "AT-0001")

    def test_worktree_paths_are_unique_at_the_storage_layer(self) -> None:
        first = self.create()
        with self.assertRaises(TicketStoreError):
            self.store.create_ticket(
                build=lambda task_key: self.build(
                    task_key,
                    worktree_path=first.worktree_path,
                ),
                actor="test",
            )

    def test_branches_are_unique_per_repository_at_the_storage_layer(self) -> None:
        first = self.create()
        with self.assertRaises(TicketStoreError):
            self.store.create_ticket(
                build=lambda task_key: self.build(task_key, branch=first.branch),
                actor="test",
            )

    def test_builder_must_honour_the_allocated_task_key(self) -> None:
        with self.assertRaises(TicketStoreError):
            self.store.create_ticket(
                build=lambda task_key: self.build("AT-9999"),
                actor="test",
            )


class AuditTests(TicketStoreTestCase):
    def test_creation_writes_one_created_task_event(self) -> None:
        ticket = self.create()
        events = self.store.list_ticket_events(ticket.task_key)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "created")
        self.assertEqual(events[0].source, "test")
        payload = json.loads(events[0].payload_json or "{}")
        self.assertEqual(payload["kind"], "ticket_created")

    def test_failed_insert_leaves_no_orphan_audit_event(self) -> None:
        first = self.create()
        with self.assertRaises(TicketStoreError):
            self.store.create_ticket(
                build=lambda task_key: self.build(
                    task_key,
                    worktree_path=first.worktree_path,
                ),
                actor="test",
            )
        with closing(connect(self.db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS total FROM task_events"
            ).fetchone()["total"]
            rows = conn.execute("SELECT COUNT(*) AS total FROM tasks").fetchone()["total"]
        self.assertEqual(count, 1)
        self.assertEqual(rows, 1)

    def test_blank_actor_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_ticket(build=self.build, actor="   ")


class BlockedByTests(TicketStoreTestCase):
    def test_unknown_blocker_is_refused(self) -> None:
        with self.assertRaises(TicketStoreError):
            self.create(blocked_by="AT-0404", status="blocked")

    def test_existing_ticket_blocker_is_accepted(self) -> None:
        blocker = self.create()
        dependent = self.create(blocked_by=blocker.task_key, status="blocked")
        self.assertEqual(dependent.blocked_by, blocker.task_key)
        self.assertEqual(dependent.status, "blocked")

    def test_legacy_task_can_be_a_blocker(self) -> None:
        # `tasks` is the one entity, so any task — not only prompt-first ones
        # — is a valid blocker.
        self.legacy_task("AT-GH-188")
        dependent = self.create(blocked_by="AT-GH-188", status="blocked")
        self.assertEqual(dependent.blocked_by, "AT-GH-188")


class ReadbackTests(TicketStoreTestCase):
    def test_get_and_list_round_trip(self) -> None:
        ticket = self.create()
        self.assertEqual(self.store.get_ticket(ticket.task_key), ticket)
        self.assertIsNone(self.store.get_ticket("AT-9999"))
        self.assertEqual(
            [item.task_key for item in self.store.list_tickets(repository="forms")],
            [ticket.task_key],
        )
        self.assertEqual(self.store.list_tickets(repository="other"), [])

    def test_legacy_rows_are_not_ticket_views(self) -> None:
        self.legacy_task("AT-0005")
        self.assertIsNone(self.store.get_ticket("AT-0005"))
        self.assertEqual(self.store.list_tickets(), [])
        self.assertIsNotNone(TaskMirrorStore(self.db_path).get_task("AT-0005"))

    def test_legacy_upsert_does_not_clobber_ticket_only_columns(self) -> None:
        ticket = self.create()
        # A later mirror re-sync of the same key through the legacy upsert.
        self.legacy_task(
            ticket.task_key,
            title="Renamed by mirror sync",
            artifact_dir=ticket.artifact_dir,
        )
        reread = self.store.get_ticket(ticket.task_key)
        assert reread is not None
        # Ticket-only columns are outside the legacy upsert's SET list.
        self.assertEqual(reread.prompt, ticket.prompt)
        self.assertEqual(reread.priority, ticket.priority)
        self.assertEqual(reread.branch, ticket.branch)
        self.assertEqual(reread.worktree_path, ticket.worktree_path)
        self.assertEqual(reread.ai_title_status, ticket.ai_title_status)
        # Shared legacy columns follow the existing upsert policy.
        self.assertEqual(reread.title, "Renamed by mirror sync")
        self.assertEqual(reread.status, "created")

    def test_list_filters_by_persisted_status(self) -> None:
        ticket = self.create()
        self.assertEqual(
            [item.task_key for item in self.store.list_tickets(statuses=["created"])],
            [ticket.task_key],
        )
        self.assertEqual(self.store.list_tickets(statuses=["blocked"]), [])
        self.assertEqual(self.store.list_tickets(statuses=[]), [])

    def test_list_rejects_unknown_status_filter(self) -> None:
        with self.assertRaises(ValueError):
            self.store.list_tickets(statuses=["not-a-status"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
