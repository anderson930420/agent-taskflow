"""Additive V1 Step 3 schema for attempt-scoped runtime progress (SPEC §14).

Two tables, both hanging off an Attempt (§14.0):

``attempt_progress``
    one row per Attempt holding ``current_phase`` and ``current_activity``.

``attempt_observed_steps``
    one row per (Attempt, §14.1 first-level step) holding the step status.

The migration is additive and touches nothing that already exists. It adds no
column to any existing table — ``tasks`` included — and in particular none of
the §32.1 PR fields, which belong to the Step 2 watcher, their sole writer.

It depends on the Level 2 Task/Attempt lifecycle schema (the ``attempts``
table and the ``tasks.task_id`` join column) but **never installs it**. That
schema is a lifecycle migration, owned by the control plane, and must be run by
an operator on purpose. When it is missing this migration fails closed: it
raises :class:`RuntimeProgressPreconditionError` before writing anything, and
the message names the exact migration to run manually. Nothing here runs at
process startup.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

# The migration *name* only. Importing the lifecycle migration function would
# let this module install lifecycle schema, which is exactly what it must not do.
from agent_taskflow.attempt_schema import TASK_ATTEMPT_LIFECYCLE_MIGRATION
from agent_taskflow.models import utc_now_iso
from agent_taskflow.runtime_progress import RUNTIME_STEPS, RUNTIME_STEP_STATUSES
from agent_taskflow.store import connect, default_db_path


RUNTIME_PROGRESS_MIGRATION = "v1_step3_runtime_progress_v1"

#: The operator-run script that installs the prerequisite lifecycle schema.
LIFECYCLE_MIGRATION_SCRIPT = "scripts/migrate_task_attempt_lifecycle.py"

#: Columns the Level 2 lifecycle migration adds to ``tasks``. Step 3 reads
#: ``task_id`` (the Attempt join key); the rest prove the migration completed.
REQUIRED_LIFECYCLE_TASK_COLUMNS: tuple[str, ...] = (
    "task_id",
    "task_class",
    "active_attempt_id",
    "final_outcome",
    "closed_at",
    "is_legacy",
)

#: Tables that must already exist. ``attempts`` is the foreign-key target of
#: both Step 3 tables and of their attempt-belongs-to-task triggers.
REQUIRED_LIFECYCLE_TABLES: tuple[str, ...] = (
    "attempts",
    "lifecycle_events",
    "schema_migrations",
)


class RuntimeProgressPreconditionError(RuntimeError):
    """Raised when the lifecycle schema Step 3 depends on is not installed."""


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


def _precondition_message(path: Path, missing: list[str]) -> str:
    return (
        f"Refusing to apply {RUNTIME_PROGRESS_MIGRATION} to {path}: the Level 2 "
        f"Task/Attempt lifecycle schema is not installed (missing: "
        f"{', '.join(missing)}). This migration never installs lifecycle "
        f"schema itself. Run the lifecycle migration "
        f"{TASK_ATTEMPT_LIFECYCLE_MIGRATION} manually first:\n"
        f"    python {LIFECYCLE_MIGRATION_SCRIPT} --db-path {path}\n"
        f"then re-run the runtime progress migration."
    )


def check_runtime_progress_preconditions(db_path: str | Path | None = None) -> None:
    """Fail closed unless the lifecycle schema is already installed.

    Opens the database **read-only** — the check itself can neither create the
    file nor change the schema.
    """

    path = Path(db_path) if db_path is not None else default_db_path()
    if not path.exists():
        raise RuntimeProgressPreconditionError(
            _precondition_message(path, ["the database file itself"])
        )

    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing: list[str] = []
        if "tasks" not in tables:
            missing.append("table tasks")
        else:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            missing.extend(
                f"tasks.{column}"
                for column in REQUIRED_LIFECYCLE_TASK_COLUMNS
                if column not in columns
            )
        missing.extend(
            f"table {table}"
            for table in REQUIRED_LIFECYCLE_TABLES
            if table not in tables
        )

    if missing:
        raise RuntimeProgressPreconditionError(_precondition_message(path, missing))


def migrate_runtime_progress(db_path: str | Path | None = None) -> None:
    """Install the attempt-scoped runtime progress tables. Idempotent.

    Raises :class:`RuntimeProgressPreconditionError` — before writing anything —
    if the lifecycle schema is missing. It never installs that schema.
    """

    check_runtime_progress_preconditions(db_path)
    with closing(connect(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)
        _record_migration(conn)


__all__ = [
    "LIFECYCLE_MIGRATION_SCRIPT",
    "REQUIRED_LIFECYCLE_TABLES",
    "REQUIRED_LIFECYCLE_TASK_COLUMNS",
    "RUNTIME_PROGRESS_MIGRATION",
    "RuntimeProgressPreconditionError",
    "check_runtime_progress_preconditions",
    "migrate_runtime_progress",
]
