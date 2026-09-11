"""Let a Ticket's Attempts share the Ticket's worktree (V1 Step 5, ruling 26d).

SPEC §9 / §44: One Ticket = One Worktree, and §33.3: a retry continues in the
same worktree. The Attempt-scoped resource table (PR-5,
:mod:`agent_taskflow.attempt_resources_schema`) declares ``branch_name`` and
``worktree_path`` ``UNIQUE``, so two Attempts can never share either. This
migration rebuilds ``attempt_resources`` with exactly those two ``UNIQUE``
keywords removed. Every other column, constraint, index and trigger is kept,
including ``UNIQUE(task_id, attempt_number)`` and the unique artifact root,
lock and PID paths, which stay per Attempt.

It is applied only by ``scripts/migrate_ticket_worktree_resources.py``. Nothing
applies it at startup: :func:`require_ticket_worktree_resources` fails closed
and names the script. Legacy tasks are unaffected by the rebuild; they still
get a fresh branch and worktree per Attempt because the allocator derives
unique paths for them.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3

from agent_taskflow.attempt_resources_schema import migrate_attempt_resources
from agent_taskflow.models import require_absolute_path, utc_now_iso
from agent_taskflow.store import connect

TICKET_WORKTREE_RESOURCES_MIGRATION = "v1_step5_ticket_worktree_attempt_resources"

#: The operator-run script that installs this migration.
TICKET_WORKTREE_MIGRATION_SCRIPT = "scripts/migrate_ticket_worktree_resources.py"

#: The only two column definitions the rebuild changes: old -> new.
RELAXED_COLUMN_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("branch_name TEXT NOT NULL UNIQUE,", "branch_name TEXT NOT NULL,"),
    ("worktree_path TEXT NOT NULL UNIQUE,", "worktree_path TEXT NOT NULL,"),
)

_TABLE = "attempt_resources"
_STAGING_TABLE = "attempt_resources_v1_step5_rebuild"


class TicketWorktreeMigrationRequired(RuntimeError):
    """Raised when a Ticket would run before this migration was applied."""


class TicketWorktreeMigrationError(RuntimeError):
    """Raised when the stored table does not have the shape this rebuild expects."""


class TicketWorktreePreconditionError(RuntimeError):
    """Raised before any write when the legacy task-mirror schema is absent."""


#: Legacy tables this migration needs and never creates.
REQUIRED_LEGACY_TABLES: tuple[str, ...] = ("tasks", "schema_migrations")


def check_ticket_worktree_preconditions(db_path: str | Path) -> None:
    """Refuse a missing database or one without the task-mirror schema."""
    path = _path(db_path)
    if not path.exists():
        missing = [f"database file {path}"]
    else:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        missing = [f"table {name}" for name in REQUIRED_LEGACY_TABLES if name not in tables]
    if missing:
        raise TicketWorktreePreconditionError(
            "The V1 Step 5 Ticket-worktree migration needs the legacy task-mirror "
            f"schema, which is missing from {path} ({', '.join(missing)}). It never "
            "creates that schema itself. Create it with agent_taskflow.store.init_db, "
            f"then run {TICKET_WORKTREE_MIGRATION_SCRIPT} again."
        )


@dataclass(frozen=True)
class TicketWorktreeMigrationResult:
    """What one run of the migration actually changed."""

    db_path: Path
    rebuilt: bool
    rows_copied: int
    migration_newly_recorded: bool

    @property
    def changed_schema(self) -> bool:
        return self.rebuilt


def _path(db_path: str | Path) -> Path:
    return require_absolute_path(db_path, "db_path")


def _migration_recorded(conn: sqlite3.Connection) -> bool:
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "schema_migrations" not in tables:
        return False
    row = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name = ?",
        (TICKET_WORKTREE_RESOURCES_MIGRATION,),
    ).fetchone()
    return row is not None


def _foreign_key_violations(conn: sqlite3.Connection) -> Counter:
    """Violations as a multiset of (parent table, fk id); rowids change on copy."""
    return Counter(
        (row[2], row[3]) for row in conn.execute(f"PRAGMA foreign_key_check({_TABLE})")
    )


def ticket_worktree_resources_applied(db_path: str | Path) -> bool:
    """Return True once the rebuild is recorded. Reads only; never creates the file."""
    path = _path(db_path)
    if not path.exists():
        return False
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        return _migration_recorded(conn)


def migration_required_message(db_path: str | Path) -> str:
    path = _path(db_path)
    return (
        "Refusing to run a Ticket: the V1 Step 5 Ticket-worktree migration is not "
        f"installed in {path}. Without it two Attempts of one Ticket cannot share "
        "the Ticket's worktree (SPEC §9). It is never applied at startup. Run the "
        "explicit migration first:\n"
        f"  python {TICKET_WORKTREE_MIGRATION_SCRIPT} --db-path {path}"
    )


def require_ticket_worktree_resources(db_path: str | Path) -> None:
    """Fail closed unless the rebuild is installed. Never applies it."""
    if not ticket_worktree_resources_applied(db_path):
        raise TicketWorktreeMigrationRequired(migration_required_message(db_path))


def relaxed_table_sql(table_sql: str) -> str:
    """Return ``table_sql`` with exactly the two ``UNIQUE`` keywords removed.

    Refuses any stored definition that does not contain each relaxed column
    definition exactly once, so an unexpected shape is never rebuilt.
    """
    relaxed = table_sql
    for old, new in RELAXED_COLUMN_DEFINITIONS:
        if relaxed.count(old) != 1:
            raise TicketWorktreeMigrationError(
                f"attempt_resources definition does not contain {old!r} exactly once; "
                "refusing to rebuild an unexpected schema"
            )
        relaxed = relaxed.replace(old, new)
    return relaxed


def migrate_ticket_worktree_resources(db_path: str | Path) -> TicketWorktreeMigrationResult:
    """Rebuild ``attempt_resources`` without the two ``UNIQUE`` keywords. Idempotent.

    Installs the Attempt-resource prerequisites first (the same chain
    ``scripts/migrate_attempt_resources.py`` applies), then, if not yet
    recorded, rebuilds the table in one ``BEGIN IMMEDIATE`` transaction with
    foreign keys off and ``legacy_alter_table`` on: it renames the old table
    aside, creates the new one from the exact relaxed definition (so the stored
    SQL differs by the two keywords only), copies every row, drops the old
    table, restores its index and triggers verbatim, refuses a rebuild that
    would add a foreign-key violation, and records the migration.
    ``legacy_alter_table`` keeps the rename from rewriting any other object's
    reference to the staging name (nothing references ``attempt_resources``
    today).
    """
    path = _path(db_path)
    check_ticket_worktree_preconditions(path)
    migrate_attempt_resources(path)
    with closing(connect(path)) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA legacy_alter_table = ON")
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                if _migration_recorded(conn):
                    return TicketWorktreeMigrationResult(
                        db_path=path,
                        rebuilt=False,
                        rows_copied=0,
                        migration_newly_recorded=False,
                    )
                table_sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (_TABLE,),
                ).fetchone()[0]
                companions = conn.execute(
                    """
                    SELECT type, name, sql FROM sqlite_master
                    WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL
                    ORDER BY type, name
                    """,
                    (_TABLE,),
                ).fetchall()
                relaxed = relaxed_table_sql(table_sql)
                columns = [row[1] for row in conn.execute(f"PRAGMA table_info({_TABLE})")]
                column_list = ", ".join(columns)
                violations_before = _foreign_key_violations(conn)
                # Move the old table aside, then create the new one from the
                # exact relaxed definition, so the stored SQL differs from the
                # original by the two keywords only.
                conn.execute(f"ALTER TABLE {_TABLE} RENAME TO {_STAGING_TABLE}")
                conn.execute(relaxed)
                copied = conn.execute(
                    f"INSERT INTO {_TABLE} ({column_list}) "
                    f"SELECT {column_list} FROM {_STAGING_TABLE}"
                ).rowcount
                conn.execute(f"DROP TABLE {_STAGING_TABLE}")
                for _kind, _name, sql in companions:
                    conn.execute(sql)
                introduced = _foreign_key_violations(conn) - violations_before
                if introduced:
                    raise TicketWorktreeMigrationError(
                        "attempt_resources rebuild would break foreign keys: "
                        f"{sorted(introduced.elements())!r}"
                    )
                conn.execute(
                    "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                    (TICKET_WORKTREE_RESOURCES_MIGRATION, utc_now_iso()),
                )
        finally:
            conn.execute("PRAGMA legacy_alter_table = OFF")
            conn.execute("PRAGMA foreign_keys = ON")
    return TicketWorktreeMigrationResult(
        db_path=path,
        rebuilt=True,
        rows_copied=int(copied),
        migration_newly_recorded=True,
    )


__all__ = [
    "REQUIRED_LEGACY_TABLES",
    "RELAXED_COLUMN_DEFINITIONS",
    "TicketWorktreePreconditionError",
    "check_ticket_worktree_preconditions",
    "TICKET_WORKTREE_MIGRATION_SCRIPT",
    "TICKET_WORKTREE_RESOURCES_MIGRATION",
    "TicketWorktreeMigrationError",
    "TicketWorktreeMigrationRequired",
    "TicketWorktreeMigrationResult",
    "migrate_ticket_worktree_resources",
    "migration_required_message",
    "relaxed_table_sql",
    "require_ticket_worktree_resources",
    "ticket_worktree_resources_applied",
]
