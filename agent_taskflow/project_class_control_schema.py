"""Additive SQLite migration for project and task-class control scopes."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from agent_taskflow.lifecycle_control_schema import migrate_lifecycle_control
from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import connect


PROJECT_CLASS_CONTROLS_MIGRATION = "level2_project_class_controls_v1"
PROJECT_CLASS_CONTROL_SCOPES = (
    "global",
    "project",
    "task_class",
    "task",
    "attempt",
)


def _runtime_controls_support_required_scopes(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'runtime_controls'"
    ).fetchone()
    sql = "" if row is None or row["sql"] is None else str(row["sql"]).lower()
    return all(f"'{scope}'" in sql for scope in PROJECT_CLASS_CONTROL_SCOPES)


def _create_runtime_controls_table(
    conn: sqlite3.Connection,
    table_name: str,
) -> None:
    if table_name not in {"runtime_controls", "runtime_controls_m1d_new"}:
        raise ValueError(f"Unexpected runtime control table name: {table_name}")
    conn.execute(
        f"""
        CREATE TABLE {table_name} (
            scope_kind TEXT NOT NULL CHECK(
                scope_kind IN ('global', 'project', 'task_class', 'task', 'attempt')
            ),
            scope_id TEXT NOT NULL,
            mode TEXT NOT NULL CHECK(mode IN ('running', 'paused', 'kill_requested')),
            reason_code TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK(generation >= 1),
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            PRIMARY KEY(scope_kind, scope_id)
        )
        """
    )


def _rebuild_runtime_controls(conn: sqlite3.Connection) -> None:
    before = conn.execute("SELECT COUNT(*) FROM runtime_controls").fetchone()[0]
    _create_runtime_controls_table(conn, "runtime_controls_m1d_new")
    conn.execute(
        """
        INSERT INTO runtime_controls_m1d_new(
            scope_kind, scope_id, mode, reason_code, requested_by,
            requested_at, generation, metadata_json
        )
        SELECT scope_kind, scope_id, mode, reason_code, requested_by,
               requested_at, generation, metadata_json
        FROM runtime_controls
        """
    )
    copied = conn.execute(
        "SELECT COUNT(*) FROM runtime_controls_m1d_new"
    ).fetchone()[0]
    if copied != before:
        raise sqlite3.IntegrityError(
            f"runtime_controls row preservation failed: expected {before}, copied {copied}"
        )
    conn.execute("DROP TABLE runtime_controls")
    conn.execute("ALTER TABLE runtime_controls_m1d_new RENAME TO runtime_controls")


def _install_project_pause_guard(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS level2_project_pause_admission_guard
        BEFORE UPDATE OF status ON tasks
        WHEN NEW.status = 'preparing'
         AND OLD.status IS NOT 'preparing'
         AND COALESCE(NEW.is_legacy, 1) = 0
         AND EXISTS (
            SELECT 1
            FROM runtime_controls
            WHERE mode IN ('paused', 'kill_requested')
              AND (
                    (scope_kind = 'global' AND scope_id = '*')
                 OR (scope_kind = 'project' AND scope_id = NEW.project)
                 OR (scope_kind = 'task' AND scope_id = NEW.task_key)
              )
         )
        BEGIN
            SELECT RAISE(ABORT, 'Level 2 runtime admission denied by persisted control');
        END
        """
    )


def migrate_project_class_controls(db_path: str | Path | None = None) -> None:
    """Install project/class scopes without dropping control or event history."""
    migrate_lifecycle_control(db_path)
    with closing(connect(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _runtime_controls_support_required_scopes(conn):
            _rebuild_runtime_controls(conn)
        _install_project_pause_guard(conn)
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations(name, applied_at)
            VALUES (?, ?)
            """,
            (PROJECT_CLASS_CONTROLS_MIGRATION, utc_now_iso()),
        )


__all__ = [
    "PROJECT_CLASS_CONTROLS_MIGRATION",
    "PROJECT_CLASS_CONTROL_SCOPES",
    "migrate_project_class_controls",
]
