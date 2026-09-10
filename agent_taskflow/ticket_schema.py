"""Additive SQLite migration for V1 Ticket persistence (SPEC §12, §44).

Tickets live beside the existing task mirror rather than replacing it: the
legacy `tasks` table keeps mirroring Hermes/Kanban state, while `tickets`
holds the V1 prompt-first Ticket and its Python-derived metadata.

The `ux_tickets_worktree_path` index is the storage-level expression of the
`One Ticket = One Worktree` invariant.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import connect, init_db as init_task_db
from agent_taskflow.ticket_models import (
    TICKET_PRIORITY_SEQUENCE,
    TICKET_STATUS_SEQUENCE,
)


TICKET_CREATION_MIGRATION = "v1_ticket_creation_v1"


def _sql_enum(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _record_migration(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO schema_migrations (name, applied_at)
        VALUES (?, ?)
        """,
        (TICKET_CREATION_MIGRATION, utc_now_iso()),
    )


def _schema_statements() -> tuple[str, ...]:
    return (
        f"""
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id TEXT PRIMARY KEY,
            repository TEXT NOT NULL,
            ticket_prefix TEXT NOT NULL,
            ticket_sequence INTEGER NOT NULL CHECK(ticket_sequence >= 1),
            prompt TEXT NOT NULL,
            title TEXT NOT NULL,
            title_source TEXT NOT NULL CHECK(title_source IN ('ai', 'fallback')),
            priority TEXT NOT NULL
                CHECK(priority IN ({_sql_enum(TICKET_PRIORITY_SEQUENCE)})),
            status TEXT NOT NULL
                CHECK(status IN ({_sql_enum(TICKET_STATUS_SEQUENCE)})),
            blocked_by TEXT,
            repo_path TEXT NOT NULL,
            github_repo TEXT,
            base_branch TEXT NOT NULL,
            branch TEXT NOT NULL,
            branch_slug_source TEXT NOT NULL
                CHECK(branch_slug_source IN ('ai', 'fallback')),
            worktree_path TEXT NOT NULL,
            artifact_dir TEXT NOT NULL,
            commit_message_suggestion TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(ticket_prefix, ticket_sequence),
            CHECK(blocked_by IS NULL OR blocked_by <> ticket_id),
            FOREIGN KEY(blocked_by) REFERENCES tickets(ticket_id)
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_tickets_worktree_path
        ON tickets(worktree_path)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_tickets_repo_branch
        ON tickets(repo_path, branch)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_tickets_repository_status
        ON tickets(repository, status)
        """,
        """
        CREATE TABLE IF NOT EXISTS ticket_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            message TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(ticket_id) REFERENCES tickets(ticket_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_ticket_events_ticket
        ON ticket_events(ticket_id, event_id)
        """,
        """
        CREATE TRIGGER IF NOT EXISTS ticket_events_no_update
        BEFORE UPDATE ON ticket_events
        BEGIN
            SELECT RAISE(ABORT, 'ticket_events are append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS ticket_events_no_delete
        BEFORE DELETE ON ticket_events
        BEGIN
            SELECT RAISE(ABORT, 'ticket_events are append-only');
        END
        """,
    )


def migrate_tickets(db_path: str | Path | None = None) -> None:
    """Create the Ticket tables. Additive and safe to run repeatedly."""
    init_task_db(db_path)
    with closing(connect(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        for statement in _schema_statements():
            conn.execute(statement)
        _record_migration(conn)


__all__ = ["TICKET_CREATION_MIGRATION", "migrate_tickets"]
