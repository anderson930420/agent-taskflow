"""Additive PR-8 schema for reset lineage and retry reservation."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from agent_taskflow.executor_process_schema import migrate_executor_process_lifecycle
from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import connect

RESET_LINEAGE_MIGRATION = "level2_reset_lineage_v1"


def _columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_sql: str,
) -> None:
    if column_name not in _columns(conn, table_name):
        conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"
        )


def migrate_reset_lineage(db_path: str | Path | None = None) -> None:
    """Install reset generation, old/new Attempt binding, and immutable audit."""
    migrate_executor_process_lifecycle(db_path)
    with closing(connect(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _add_column_if_missing(
            conn,
            "tasks",
            "reset_generation",
            "INTEGER NOT NULL DEFAULT 0",
        )
        conn.execute(
            "UPDATE tasks SET reset_generation = 0 WHERE reset_generation IS NULL"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reset_lineages (
                reset_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                task_key TEXT NOT NULL,
                old_attempt_id TEXT REFERENCES attempts(attempt_id),
                new_attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
                expected_generation INTEGER NOT NULL CHECK(expected_generation >= 0),
                committed_generation INTEGER NOT NULL CHECK(committed_generation >= 1),
                from_status TEXT NOT NULL CHECK(from_status = 'blocked'),
                to_status TEXT NOT NULL CHECK(to_status = 'queued'),
                reason TEXT NOT NULL,
                actor TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('reserved', 'claimed', 'canceled')),
                created_at TEXT NOT NULL,
                claimed_at TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(task_id, committed_generation)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_reset_lineages_task_generation
            ON reset_lineages(task_id, committed_generation DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reset_lineage_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                reset_id TEXT NOT NULL REFERENCES reset_lineages(reset_id),
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                old_attempt_id TEXT REFERENCES attempts(attempt_id),
                new_attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
                event_type TEXT NOT NULL CHECK(event_type IN (
                    'reserved', 'claimed', 'compare_and_set_rejected', 'artifact_failed'
                )),
                reason_code TEXT NOT NULL,
                actor TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_reset_lineage_events_reset
            ON reset_lineage_events(reset_id, event_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reset_lineage_suppressions (
                task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
                reset_id TEXT NOT NULL,
                new_attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS reset_lineage_events_no_update
            BEFORE UPDATE ON reset_lineage_events
            BEGIN
                SELECT RAISE(ABORT, 'reset lineage events are append-only');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS reset_lineage_events_no_delete
            BEFORE DELETE ON reset_lineage_events
            BEGIN
                SELECT RAISE(ABORT, 'reset lineage events are append-only');
            END
            """
        )
        conn.execute("DROP TRIGGER IF EXISTS reset_lineage_state_guard")
        conn.execute(
            """
            CREATE TRIGGER reset_lineage_state_guard
            BEFORE UPDATE OF state ON reset_lineages
            WHEN OLD.state <> NEW.state
             AND NOT (
                OLD.state = 'reserved' AND NEW.state IN ('claimed', 'canceled')
             )
            BEGIN
                SELECT RAISE(ABORT, 'illegal reset lineage state transition');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS reset_lineage_attempt_task_guard
            BEFORE INSERT ON reset_lineages
            WHEN NOT EXISTS (
                    SELECT 1 FROM attempts
                    WHERE attempt_id = NEW.new_attempt_id
                      AND task_id = NEW.task_id
                 )
              OR (
                    NEW.old_attempt_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM attempts
                    WHERE attempt_id = NEW.old_attempt_id
                      AND task_id = NEW.task_id
                )
              )
            BEGIN
                SELECT RAISE(ABORT, 'reset lineage attempts must belong to task');
            END
            """
        )
        conn.execute("DROP TRIGGER IF EXISTS reset_lineage_required_for_retry")
        conn.execute(
            """
            CREATE TRIGGER reset_lineage_required_for_retry
            BEFORE UPDATE OF status ON tasks
            WHEN OLD.status = 'blocked'
             AND NEW.status = 'queued'
             AND NOT EXISTS (
                SELECT 1
                FROM reset_lineage_suppressions
                WHERE task_id = NEW.task_id
                  AND new_attempt_id = NEW.active_attempt_id
             )
            BEGIN
                SELECT RAISE(ABORT, 'reset lineage reservation required');
            END
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations(name, applied_at)
            VALUES (?, ?)
            """,
            (RESET_LINEAGE_MIGRATION, utc_now_iso()),
        )


__all__ = ["RESET_LINEAGE_MIGRATION", "migrate_reset_lineage"]
