"""Additive schema for managed executor and validator process groups."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from agent_taskflow.lifecycle_control_schema import migrate_lifecycle_control
from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import connect

EXECUTOR_PROCESS_MIGRATION = "level2_executor_process_lifecycle_v1"
ACTIVE_PROCESS_STATES = (
    "allocated",
    "running",
    "term_sent",
    "kill_sent",
)


def migrate_executor_process_lifecycle(db_path: str | Path | None = None) -> None:
    """Install managed process records, legal state transitions, and audit events."""
    migrate_lifecycle_control(db_path)
    with closing(connect(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS executor_processes (
                process_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                task_key TEXT NOT NULL,
                lease_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                executor_name TEXT NOT NULL,
                process_role TEXT NOT NULL DEFAULT 'executor'
                    CHECK(process_role IN ('executor', 'validator')),
                pid INTEGER,
                pgid INTEGER,
                session_id INTEGER,
                leader_start_ticks INTEGER,
                state TEXT NOT NULL CHECK(state IN (
                    'allocated', 'preflight_failed', 'start_failed', 'running',
                    'term_sent', 'kill_sent', 'exited', 'exit_unverified'
                )),
                started_at TEXT,
                term_sent_at TEXT,
                kill_sent_at TEXT,
                exited_at TEXT,
                exit_code INTEGER,
                termination_reason TEXT,
                verified_exit INTEGER NOT NULL DEFAULT 0 CHECK(verified_exit IN (0, 1)),
                launch_spec_path TEXT NOT NULL,
                pid_manifest_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(executor_processes)")
        }
        if "process_role" not in columns:
            conn.execute(
                """
                ALTER TABLE executor_processes
                ADD COLUMN process_role TEXT NOT NULL DEFAULT 'executor'
                CHECK(process_role IN ('executor', 'validator'))
                """
            )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_executor_process_one_active_per_attempt
            ON executor_processes(attempt_id)
            WHERE state IN ('allocated', 'running', 'term_sent', 'kill_sent')
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_executor_processes_task_state
            ON executor_processes(task_key, state, created_at)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS executor_process_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                process_id TEXT NOT NULL REFERENCES executor_processes(process_id),
                attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
                from_state TEXT,
                to_state TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                actor TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_executor_process_events_process
            ON executor_process_events(process_id, event_id)
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS executor_process_events_no_update
            BEFORE UPDATE ON executor_process_events
            BEGIN
                SELECT RAISE(ABORT, 'executor process events are append-only');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS executor_process_events_no_delete
            BEFORE DELETE ON executor_process_events
            BEGIN
                SELECT RAISE(ABORT, 'executor process events are append-only');
            END
            """
        )
        conn.execute("DROP TRIGGER IF EXISTS executor_process_state_guard")
        conn.execute(
            """
            CREATE TRIGGER executor_process_state_guard
            BEFORE UPDATE OF state ON executor_processes
            WHEN OLD.state <> NEW.state
             AND NOT (
                (OLD.state = 'allocated' AND NEW.state IN (
                    'preflight_failed', 'start_failed', 'running'
                )) OR
                (OLD.state = 'running' AND NEW.state IN (
                    'term_sent', 'exited', 'exit_unverified'
                )) OR
                (OLD.state = 'term_sent' AND NEW.state IN (
                    'kill_sent', 'exited', 'exit_unverified'
                )) OR
                (OLD.state = 'kill_sent' AND NEW.state IN (
                    'exited', 'exit_unverified'
                )) OR
                (OLD.state = 'exit_unverified' AND NEW.state = 'exited')
             )
            BEGIN
                SELECT RAISE(ABORT, 'illegal executor process state transition');
            END
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations(name, applied_at)
            VALUES (?, ?)
            """,
            (EXECUTOR_PROCESS_MIGRATION, utc_now_iso()),
        )


__all__ = [
    "ACTIVE_PROCESS_STATES",
    "EXECUTOR_PROCESS_MIGRATION",
    "migrate_executor_process_lifecycle",
]
