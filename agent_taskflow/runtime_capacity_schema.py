"""Additive V1 Step 4 schema for the global ``max_concurrent_tasks`` control.

The control lives in the runtime-control database next to ``runtime_controls``
and follows the same shape: one current row per scope plus an append-only event
table. Only the ``global`` scope exists in V1.

The tables only store a value someone chose. They are not needed for the
limit to apply: a database without them is bounded by the default of 1 (ruling
15). Nothing runs this migration at startup. Writing a value installs it
(``scripts/runtime_control.py set-capacity``).

The table refuses, by ``CHECK``, any limit above 1 that is neither bound to the
SHA-256 of the rehearsal evidence it was approved with nor marked as a
disposable-fixture value (``runtime_capacity.set_disposable_fixture_capacity``).
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import connect

RUNTIME_CAPACITY_MIGRATION = "v1_step4_runtime_capacity_v1"
RUNTIME_CAPACITY_TABLE = "runtime_capacity_controls"
RUNTIME_CAPACITY_EVENTS_TABLE = "runtime_capacity_control_events"
DISPOSABLE_FIXTURE_CAPACITY_REASON = "disposable_fixture_capacity"

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS runtime_capacity_controls (
        scope_kind TEXT NOT NULL CHECK(scope_kind = 'global'),
        scope_id TEXT NOT NULL CHECK(scope_id = '*'),
        max_concurrent_tasks INTEGER NOT NULL CHECK(max_concurrent_tasks >= 1),
        evidence_path TEXT,
        evidence_sha256 TEXT,
        evidence_schema_version TEXT,
        evidence_repo_sha TEXT,
        reason_code TEXT NOT NULL,
        requested_by TEXT NOT NULL,
        requested_at TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK(generation >= 1),
        metadata_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY(scope_kind, scope_id),
        CHECK(
            max_concurrent_tasks = 1
            OR (evidence_sha256 IS NOT NULL AND length(evidence_sha256) = 64)
            OR reason_code = 'disposable_fixture_capacity'
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_capacity_control_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope_kind TEXT NOT NULL,
        scope_id TEXT NOT NULL,
        from_max_concurrent_tasks INTEGER,
        to_max_concurrent_tasks INTEGER NOT NULL,
        evidence_path TEXT,
        evidence_sha256 TEXT,
        reason_code TEXT NOT NULL,
        actor TEXT NOT NULL,
        generation INTEGER NOT NULL,
        timestamp TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS runtime_capacity_control_events_no_update
    BEFORE UPDATE ON runtime_capacity_control_events
    BEGIN
        SELECT RAISE(ABORT, 'runtime capacity control events are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS runtime_capacity_control_events_no_delete
    BEFORE DELETE ON runtime_capacity_control_events
    BEGIN
        SELECT RAISE(ABORT, 'runtime capacity control events are append-only');
    END
    """,
)


def runtime_capacity_deployed_in_connection(conn: sqlite3.Connection) -> bool:
    """Return whether the capacity control is installed, without changing it."""
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (RUNTIME_CAPACITY_TABLE,),
        ).fetchone()
        is not None
    )


def migrate_runtime_capacity(db_path: str | Path | None = None) -> None:
    """Install the global capacity control. Idempotent; never run at startup."""
    with closing(connect(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(name, applied_at) VALUES (?, ?)",
            (RUNTIME_CAPACITY_MIGRATION, utc_now_iso()),
        )


__all__ = [
    "DISPOSABLE_FIXTURE_CAPACITY_REASON",
    "RUNTIME_CAPACITY_EVENTS_TABLE",
    "RUNTIME_CAPACITY_MIGRATION",
    "RUNTIME_CAPACITY_TABLE",
    "migrate_runtime_capacity",
    "runtime_capacity_deployed_in_connection",
]
