"""Additive SQLite migration for Task, Attempt, and lifecycle persistence."""

from __future__ import annotations

from contextlib import closing
import sqlite3
from pathlib import Path

from agent_taskflow.attempt_models import default_task_id
from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import connect, init_db as init_task_db


TASK_ATTEMPT_LIFECYCLE_MIGRATION = "level2_task_attempt_lifecycle_v1"


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_sql: str,
) -> None:
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name not in columns:
        conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"
        )


def _record_migration(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO schema_migrations (name, applied_at)
        VALUES (?, ?)
        """,
        (TASK_ATTEMPT_LIFECYCLE_MIGRATION, utc_now_iso()),
    )


def migrate_task_attempt_lifecycle(db_path: str | Path | None = None) -> None:
    """Apply the migration without inventing historical Attempt records."""
    init_task_db(db_path)
    with closing(connect(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")

        _add_column_if_missing(conn, "tasks", "task_id", "TEXT")
        _add_column_if_missing(
            conn,
            "tasks",
            "task_class",
            "TEXT NOT NULL DEFAULT 'legacy'",
        )
        _add_column_if_missing(conn, "tasks", "active_attempt_id", "TEXT")
        _add_column_if_missing(conn, "tasks", "final_outcome", "TEXT")
        _add_column_if_missing(conn, "tasks", "closed_at", "TEXT")
        _add_column_if_missing(
            conn,
            "tasks",
            "is_legacy",
            "INTEGER NOT NULL DEFAULT 1",
        )

        rows = conn.execute(
            "SELECT task_key FROM tasks WHERE task_id IS NULL OR trim(task_id) = ''"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE tasks SET task_id = ? WHERE task_key = ?",
                (default_task_id(row["task_key"]), row["task_key"]),
            )

        conn.execute(
            """
            UPDATE tasks
            SET task_class = 'legacy'
            WHERE task_class IS NULL OR trim(task_class) = ''
            """
        )
        conn.execute("UPDATE tasks SET is_legacy = 1 WHERE is_legacy IS NULL")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_task_id ON tasks(task_id)"
        )

        schema_statements = (
            """
            CREATE TABLE IF NOT EXISTS attempts (
                attempt_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
                status TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
                is_legacy INTEGER NOT NULL DEFAULT 0 CHECK(is_legacy IN (0, 1)),
                executor TEXT,
                model TEXT,
                base_commit TEXT,
                policy_version TEXT,
                config_snapshot_hash TEXT,
                prompt_template_version TEXT,
                permission_profile TEXT,
                worktree_path TEXT,
                artifact_root TEXT,
                started_at TEXT,
                ended_at TEXT,
                execution_result TEXT,
                validation_result TEXT,
                merge_recommendation TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(task_id),
                UNIQUE(task_id, attempt_number),
                CHECK(
                    (is_active = 1 AND ended_at IS NULL)
                    OR (is_active = 0 AND ended_at IS NOT NULL)
                )
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_attempts_one_active_per_task
            ON attempts(task_id)
            WHERE is_active = 1
            """,
            """
            CREATE TABLE IF NOT EXISTS lifecycle_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                attempt_id TEXT,
                from_status TEXT,
                to_status TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                actor TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(task_id) REFERENCES tasks(task_id),
                FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS ix_lifecycle_events_task_time
            ON lifecycle_events(task_id, event_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS ix_lifecycle_events_attempt_time
            ON lifecycle_events(attempt_id, event_id)
            """,
            """
            CREATE TRIGGER IF NOT EXISTS lifecycle_events_attempt_task_guard
            BEFORE INSERT ON lifecycle_events
            WHEN NEW.attempt_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1
                     FROM attempts
                     WHERE attempt_id = NEW.attempt_id
                       AND task_id = NEW.task_id
                 )
            BEGIN
                SELECT RAISE(ABORT, 'lifecycle attempt does not belong to task');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS lifecycle_events_no_update
            BEFORE UPDATE ON lifecycle_events
            BEGIN
                SELECT RAISE(ABORT, 'lifecycle_events are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS lifecycle_events_no_delete
            BEFORE DELETE ON lifecycle_events
            BEGIN
                SELECT RAISE(ABORT, 'lifecycle_events are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS tasks_active_attempt_guard
            BEFORE UPDATE OF active_attempt_id ON tasks
            WHEN NEW.active_attempt_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1
                     FROM attempts
                     WHERE attempt_id = NEW.active_attempt_id
                       AND task_id = NEW.task_id
                       AND is_active = 1
                 )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'active_attempt_id must reference an active attempt for the same task'
                );
            END
            """,
        )
        for statement in schema_statements:
            conn.execute(statement)

        _record_migration(conn)
