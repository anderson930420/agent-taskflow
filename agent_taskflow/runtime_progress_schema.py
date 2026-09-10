"""Additive V1 Step 3 schema for attempt-scoped runtime progress (SPEC §14).

Two tables, both hanging off an Attempt (§14.0):

``attempt_progress``
    one row per Attempt holding ``current_phase`` and ``current_activity``.

``attempt_observed_steps``
    one row per (Attempt, §14.1 first-level step) holding the step status.

The migration is additive and touches nothing that already exists. It creates
no Ticket column, no lifecycle column, and in particular none of the §32.1 PR
fields — those belong to the Step 2 watcher, which is their sole writer.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from agent_taskflow.attempt_schema import migrate_task_attempt_lifecycle
from agent_taskflow.models import utc_now_iso
from agent_taskflow.runtime_progress import RUNTIME_STEPS, RUNTIME_STEP_STATUSES
from agent_taskflow.store import connect


RUNTIME_PROGRESS_MIGRATION = "v1_step3_runtime_progress_v1"


def _sql_in_list(values: tuple[str, ...]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


_STEP_NAMES_SQL = _sql_in_list(RUNTIME_STEPS)
_STEP_STATUSES_SQL = _sql_in_list(RUNTIME_STEP_STATUSES)


_SCHEMA_STATEMENTS: tuple[str, ...] = (
    f"""
    CREATE TABLE IF NOT EXISTS attempt_progress (
        attempt_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        current_phase TEXT
            CHECK(current_phase IS NULL OR current_phase IN ({_STEP_NAMES_SQL})),
        current_activity TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id),
        FOREIGN KEY(task_id) REFERENCES tasks(task_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS attempt_observed_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        step_name TEXT NOT NULL CHECK(step_name IN ({_STEP_NAMES_SQL})),
        step_order INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ({_STEP_STATUSES_SQL})),
        summary TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{{}}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(attempt_id, step_name),
        FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id),
        FOREIGN KEY(task_id) REFERENCES tasks(task_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_attempt_observed_steps_attempt
    ON attempt_observed_steps(attempt_id, step_order)
    """,
    # Progress records may never drift onto an Attempt that belongs to another
    # Task. Mirrors the lifecycle_events guard installed by attempt_schema.
    """
    CREATE TRIGGER IF NOT EXISTS attempt_progress_task_guard
    BEFORE INSERT ON attempt_progress
    WHEN NOT EXISTS (
             SELECT 1
             FROM attempts
             WHERE attempt_id = NEW.attempt_id
               AND task_id = NEW.task_id
         )
    BEGIN
        SELECT RAISE(ABORT, 'progress attempt does not belong to task');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS attempt_observed_steps_task_guard
    BEFORE INSERT ON attempt_observed_steps
    WHEN NOT EXISTS (
             SELECT 1
             FROM attempts
             WHERE attempt_id = NEW.attempt_id
               AND task_id = NEW.task_id
         )
    BEGIN
        SELECT RAISE(ABORT, 'observed step attempt does not belong to task');
    END
    """,
)


def _record_migration(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO schema_migrations (name, applied_at)
        VALUES (?, ?)
        """,
        (RUNTIME_PROGRESS_MIGRATION, utc_now_iso()),
    )


def migrate_runtime_progress(db_path: str | Path | None = None) -> None:
    """Install the attempt-scoped runtime progress tables. Idempotent."""

    migrate_task_attempt_lifecycle(db_path)
    with closing(connect(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)
        _record_migration(conn)


__all__ = [
    "RUNTIME_PROGRESS_MIGRATION",
    "migrate_runtime_progress",
]
