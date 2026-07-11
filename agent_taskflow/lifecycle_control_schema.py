"""Additive PR-6 schema for lifecycle transitions and runtime controls."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from agent_taskflow.attempt_resources_schema import migrate_attempt_resources
from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import connect

LIFECYCLE_CONTROL_MIGRATION = "level2_lifecycle_control_v1"

# The graph is intentionally forward-only. Failure terminals are reachable from
# every active phase; review/completion terminals require at least preparation.
ATTEMPT_TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("created", "preparing"),
    ("created", "implementing"),
    ("created", "validating"),
    ("created", "waiting_approval"),
    ("created", "validation_failed"),
    ("created", "execution_timeout"),
    ("created", "execution_aborted"),
    ("created", "blocked"),
    ("created", "completed"),
    ("created", "failed"),
    ("created", "canceled"),
    ("preparing", "implementing"),
    ("preparing", "validating"),
    ("preparing", "waiting_approval"),
    ("preparing", "validation_failed"),
    ("preparing", "execution_timeout"),
    ("preparing", "execution_aborted"),
    ("preparing", "blocked"),
    ("preparing", "completed"),
    ("preparing", "failed"),
    ("preparing", "canceled"),
    ("implementing", "validating"),
    ("implementing", "waiting_approval"),
    ("implementing", "execution_timeout"),
    ("implementing", "execution_aborted"),
    ("implementing", "blocked"),
    ("implementing", "completed"),
    ("implementing", "failed"),
    ("implementing", "canceled"),
    ("validating", "waiting_approval"),
    ("validating", "validation_failed"),
    ("validating", "execution_timeout"),
    ("validating", "execution_aborted"),
    ("validating", "blocked"),
    ("validating", "completed"),
    ("validating", "failed"),
    ("validating", "canceled"),
)


def migrate_lifecycle_control(db_path: str | Path | None = None) -> None:
    """Install the forward-only Attempt graph and persisted control switches."""
    migrate_attempt_resources(db_path)
    with closing(connect(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lifecycle_allowed_transitions (
                entity_kind TEXT NOT NULL CHECK(entity_kind IN ('attempt')),
                from_status TEXT NOT NULL,
                to_status TEXT NOT NULL,
                PRIMARY KEY(entity_kind, from_status, to_status)
            )
            """
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO lifecycle_allowed_transitions(
                entity_kind, from_status, to_status
            ) VALUES ('attempt', ?, ?)
            """,
            ATTEMPT_TRANSITIONS,
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_controls (
                scope_kind TEXT NOT NULL CHECK(scope_kind IN ('global', 'task', 'attempt')),
                scope_id TEXT NOT NULL,
                mode TEXT NOT NULL CHECK(mode IN ('running', 'paused', 'kill_requested')),
                reason_code TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK(generation >= 1),
                metadata_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(scope_kind, scope_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_control_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_kind TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                from_mode TEXT,
                to_mode TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                actor TEXT NOT NULL,
                generation INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_runtime_control_events_scope
            ON runtime_control_events(scope_kind, scope_id, event_id)
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS runtime_control_events_no_update
            BEFORE UPDATE ON runtime_control_events
            BEGIN
                SELECT RAISE(ABORT, 'runtime control events are append-only');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS runtime_control_events_no_delete
            BEFORE DELETE ON runtime_control_events
            BEGIN
                SELECT RAISE(ABORT, 'runtime control events are append-only');
            END
            """
        )
        conn.execute("DROP TRIGGER IF EXISTS lifecycle_attempt_transition_guard")
        conn.execute(
            """
            CREATE TRIGGER lifecycle_attempt_transition_guard
            BEFORE UPDATE OF status ON attempts
            WHEN OLD.status <> NEW.status
             AND NOT EXISTS (
                SELECT 1 FROM lifecycle_allowed_transitions
                WHERE entity_kind = 'attempt'
                  AND from_status = OLD.status
                  AND to_status = NEW.status
             )
            BEGIN
                SELECT RAISE(ABORT, 'illegal attempt lifecycle transition');
            END
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations(name, applied_at)
            VALUES (?, ?)
            """,
            (LIFECYCLE_CONTROL_MIGRATION, utc_now_iso()),
        )


__all__ = [
    "ATTEMPT_TRANSITIONS",
    "LIFECYCLE_CONTROL_MIGRATION",
    "migrate_lifecycle_control",
]
