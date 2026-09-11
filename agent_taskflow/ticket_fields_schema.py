"""V1 Step 1 Ticket columns on `tasks` — an explicit, operator-run migration.

`tasks` is the only canonical Ticket entity (PR #195 ruling). Step 1 keeps its
Python-derived Ticket metadata in ten nullable columns on `tasks`, and enforces
`One Ticket = One Worktree` (SPEC §44) with two partial unique indexes.

Nothing here runs at process startup (PR #195 ruling 4a). An operator installs
the migration on purpose with ``scripts/migrate_ticket_fields.py``. Startup
fails closed instead: :func:`require_ticket_fields` raises
:class:`TicketFieldsMigrationRequired`, naming that script, whenever a column
or index is missing.

The migration depends on the legacy task-mirror schema (the `tasks` table that
:func:`agent_taskflow.store.init_db` creates) but never installs it. When that
schema is missing, the migration raises :class:`TicketFieldsPreconditionError`
before writing anything.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from agent_taskflow.models import require_absolute_path, utc_now_iso
from agent_taskflow.store import connect


TICKET_FIELDS_MIGRATION = "tasks_ticket_fields"

#: The operator-run script that installs this migration.
TICKET_FIELDS_MIGRATION_SCRIPT = "scripts/migrate_ticket_fields.py"

#: Step 1's columns on `tasks`. All nullable; legacy rows keep NULL.
TASK_TICKET_COLUMNS: tuple[tuple[str, str], ...] = (
    ("prompt", "TEXT"),
    ("priority", "TEXT"),
    ("ai_title_status", "TEXT"),
    ("branch_slug_source", "TEXT"),
    ("blocked_by", "TEXT"),
    ("github_repo", "TEXT"),
    ("base_branch", "TEXT"),
    ("branch", "TEXT"),
    ("worktree_path", "TEXT"),
    ("commit_message_suggestion", "TEXT"),
)

#: `One Ticket = One Worktree`, enforced by storage. Partial, so legacy rows
#: with NULL derived values can never collide.
TASK_TICKET_INDEXES: tuple[tuple[str, str], ...] = (
    (
        "ux_tasks_worktree_path",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_worktree_path "
        "ON tasks(worktree_path) WHERE worktree_path IS NOT NULL",
    ),
    (
        "ux_tasks_repo_branch",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_repo_branch "
        "ON tasks(repo_path, branch) WHERE branch IS NOT NULL",
    ),
)

#: Legacy tables this migration needs and never creates.
REQUIRED_LEGACY_TABLES: tuple[str, ...] = ("tasks", "schema_migrations")


class TicketFieldsPreconditionError(RuntimeError):
    """Raised when the legacy task-mirror schema this migration needs is absent."""


class TicketFieldsMigrationRequired(RuntimeError):
    """Raised at startup when Step 1's `tasks` columns or indexes are absent."""


@dataclass(frozen=True)
class TicketFieldsMigrationResult:
    """What one run of the migration actually changed."""

    db_path: Path
    columns_added: tuple[str, ...]
    indexes_added: tuple[str, ...]
    migration_newly_recorded: bool

    @property
    def changed_schema(self) -> bool:
        return bool(self.columns_added or self.indexes_added)


def _path(db_path: str | Path) -> Path:
    return require_absolute_path(db_path, "db_path")


def _schema_snapshot(conn: sqlite3.Connection) -> tuple[set[str], set[str], set[str]]:
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    indexes = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    columns = (
        {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "tasks" in tables
        else set()
    )
    return tables, indexes, columns


def missing_ticket_fields(db_path: str | Path) -> tuple[str, ...]:
    """Return every Step 1 column or index that is not installed.

    An empty tuple means the migration is fully installed. Reads only, and
    never creates the database file.
    """
    path = _path(db_path)
    wanted = tuple(f"tasks.{name}" for name, _sql in TASK_TICKET_COLUMNS) + tuple(
        f"index {name}" for name, _sql in TASK_TICKET_INDEXES
    )
    if not path.exists():
        return wanted
    with closing(connect(path)) as conn:
        _tables, indexes, columns = _schema_snapshot(conn)
    return tuple(
        f"tasks.{name}" for name, _sql in TASK_TICKET_COLUMNS if name not in columns
    ) + tuple(
        f"index {name}" for name, _sql in TASK_TICKET_INDEXES if name not in indexes
    )


def migration_required_message(db_path: str | Path, missing: tuple[str, ...]) -> str:
    path = _path(db_path)
    return (
        "Refusing to start: the V1 Step 1 Ticket columns are not installed in "
        f"{path} (missing: {', '.join(missing)}). They are no longer applied "
        "at startup. Run the explicit migration first:\n"
        f"  python {TICKET_FIELDS_MIGRATION_SCRIPT} --db-path {path}"
    )


def require_ticket_fields(db_path: str | Path) -> None:
    """Fail closed unless Step 1's columns and indexes are installed.

    Raises :class:`TicketFieldsMigrationRequired` naming the script to run.
    Never applies the migration.
    """
    missing = missing_ticket_fields(db_path)
    if missing:
        raise TicketFieldsMigrationRequired(migration_required_message(db_path, missing))


def check_ticket_fields_preconditions(db_path: str | Path) -> None:
    """Raise before any write if the legacy schema is missing."""
    path = _path(db_path)
    if not path.exists():
        missing = [f"database file {path}"]
    else:
        with closing(connect(path)) as conn:
            tables, _indexes, _columns = _schema_snapshot(conn)
        missing = [f"table {name}" for name in REQUIRED_LEGACY_TABLES if name not in tables]
    if missing:
        raise TicketFieldsPreconditionError(
            "The V1 Step 1 Ticket migration needs the legacy task-mirror schema, "
            f"which is missing from {path} ({', '.join(missing)}). It never installs "
            "that schema itself. Create it with agent_taskflow.store.init_db — "
            "Mission Control API startup does this before refusing to start — and "
            f"then run {TICKET_FIELDS_MIGRATION_SCRIPT} again."
        )


def migrate_ticket_fields(db_path: str | Path) -> TicketFieldsMigrationResult:
    """Install Step 1's `tasks` columns and indexes. Additive and idempotent.

    Adds exactly the columns in :data:`TASK_TICKET_COLUMNS` and the indexes in
    :data:`TASK_TICKET_INDEXES` that are not already present, and records
    :data:`TICKET_FIELDS_MIGRATION` in `schema_migrations`. Nothing else.
    """
    path = _path(db_path)
    check_ticket_fields_preconditions(path)
    with closing(connect(path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _tables, indexes_before, columns_before = _schema_snapshot(conn)

        columns_added: list[str] = []
        for name, sql_type in TASK_TICKET_COLUMNS:
            if name not in columns_before:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {sql_type}")
                columns_added.append(name)

        indexes_added: list[str] = []
        for name, statement in TASK_TICKET_INDEXES:
            if name not in indexes_before:
                conn.execute(statement)
                indexes_added.append(name)

        recorded = conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (TICKET_FIELDS_MIGRATION, utc_now_iso()),
        ).rowcount

    return TicketFieldsMigrationResult(
        db_path=path,
        columns_added=tuple(columns_added),
        indexes_added=tuple(indexes_added),
        migration_newly_recorded=bool(recorded),
    )


__all__ = [
    "REQUIRED_LEGACY_TABLES",
    "TASK_TICKET_COLUMNS",
    "TASK_TICKET_INDEXES",
    "TICKET_FIELDS_MIGRATION",
    "TICKET_FIELDS_MIGRATION_SCRIPT",
    "TicketFieldsMigrationRequired",
    "TicketFieldsMigrationResult",
    "TicketFieldsPreconditionError",
    "check_ticket_fields_preconditions",
    "migrate_ticket_fields",
    "migration_required_message",
    "missing_ticket_fields",
    "require_ticket_fields",
]
