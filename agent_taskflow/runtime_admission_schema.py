"""Additive SQLite migration for atomic runtime admission and leases."""

from __future__ import annotations

from contextlib import closing
import sqlite3
from pathlib import Path

from agent_taskflow.attempt_schema import migrate_task_attempt_lifecycle
from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import connect

RUNTIME_ADMISSION_MIGRATION = "level2_runtime_admission_v1"
DEFAULT_LEASE_TTL_SECONDS = 3600


def _schema_statements() -> tuple[str, ...]:
    ttl = DEFAULT_LEASE_TTL_SECONDS
    return (
        """
        CREATE TABLE IF NOT EXISTS runtime_claim_suppressions (
            task_id TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(task_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS runtime_leases (
            lease_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            auth_mode TEXT NOT NULL CHECK(auth_mode IN ('token', 'implicit_status')),
            ttl_seconds INTEGER NOT NULL CHECK(ttl_seconds >= 1),
            acquired_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            released_at TEXT,
            release_reason TEXT,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
            FOREIGN KEY(task_id) REFERENCES tasks(task_id),
            FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id),
            UNIQUE(token_hash),
            CHECK(
                (is_active = 1 AND released_at IS NULL)
                OR (is_active = 0 AND released_at IS NOT NULL)
            )
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_leases_one_active_per_task
        ON runtime_leases(task_id)
        WHERE is_active = 1
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_leases_one_active_per_attempt
        ON runtime_leases(attempt_id)
        WHERE is_active = 1
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_runtime_leases_expiry
        ON runtime_leases(is_active, expires_at)
        """,
        """
        CREATE TRIGGER IF NOT EXISTS runtime_duplicate_pickup_guard
        BEFORE UPDATE OF status ON tasks
        WHEN NEW.status = 'preparing'
             AND OLD.status = 'preparing'
             AND NOT EXISTS (
                 SELECT 1 FROM runtime_claim_suppressions
                 WHERE task_id = COALESCE(NEW.task_id, 'task:' || NEW.task_key)
             )
        BEGIN
            SELECT RAISE(ABORT, 'runtime pickup already claimed');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS runtime_pickup_claim_after_preparing
        AFTER UPDATE OF status ON tasks
        WHEN NEW.status = 'preparing'
             AND OLD.status IS NOT 'preparing'
             AND NOT EXISTS (
                 SELECT 1 FROM runtime_claim_suppressions
                 WHERE task_id = COALESCE(NEW.task_id, 'task:' || NEW.task_key)
             )
        BEGIN
            UPDATE tasks
            SET task_id = COALESCE(NULLIF(task_id, ''), 'task:' || task_key),
                task_class = COALESCE(NULLIF(task_class, ''), 'legacy'),
                is_legacy = COALESCE(is_legacy, 1)
            WHERE task_key = NEW.task_key;

            SELECT RAISE(ABORT, 'runtime pickup already has active attempt')
            WHERE EXISTS (
                SELECT 1 FROM attempts
                WHERE task_id = (SELECT task_id FROM tasks WHERE task_key = NEW.task_key)
                  AND is_active = 1
            ) OR EXISTS (
                SELECT 1 FROM runtime_leases
                WHERE task_id = (SELECT task_id FROM tasks WHERE task_key = NEW.task_key)
                  AND is_active = 1
            );

            INSERT INTO attempts (
                attempt_id, task_id, attempt_number, status, is_active, is_legacy,
                executor, model, base_commit, policy_version,
                config_snapshot_hash, prompt_template_version, permission_profile,
                worktree_path, artifact_root, started_at, ended_at,
                execution_result, validation_result, merge_recommendation,
                created_at, updated_at
            )
            SELECT
                'attempt-' || lower(hex(randomblob(16))),
                task_id,
                COALESCE(
                    (SELECT MAX(attempt_number)
                     FROM attempts
                     WHERE attempts.task_id = tasks.task_id),
                    0
                ) + 1,
                'preparing', 1, 0,
                executor, model, NULL, NULL, NULL, NULL, NULL,
                (SELECT worktree_path
                 FROM task_worktrees
                 WHERE task_worktrees.task_key = tasks.task_key),
                artifact_dir,
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), NULL,
                NULL, NULL, NULL,
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            FROM tasks
            WHERE task_key = NEW.task_key;

            UPDATE tasks
            SET active_attempt_id = (
                SELECT attempt_id FROM attempts
                WHERE task_id = tasks.task_id AND is_active = 1
            )
            WHERE task_key = NEW.task_key;

            INSERT INTO runtime_leases (
                lease_id, task_id, attempt_id, owner_id, token_hash, auth_mode,
                ttl_seconds, acquired_at, heartbeat_at, expires_at,
                released_at, release_reason, is_active
            )
            SELECT
                'lease-' || lower(hex(randomblob(16))),
                tasks.task_id,
                tasks.active_attempt_id,
                'pending:' || tasks.task_key,
                lower(hex(randomblob(32))),
                'implicit_status',
                {ttl},
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+{ttl} seconds'),
                NULL, NULL, 1
            FROM tasks
            WHERE task_key = NEW.task_key;

            INSERT INTO lifecycle_events (
                task_id, attempt_id, from_status, to_status,
                reason_code, actor, timestamp, metadata_json
            )
            SELECT
                task_id, active_attempt_id, OLD.status, 'preparing',
                'runtime_pickup_claimed_implicit',
                'runtime_admission_trigger',
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                '{{"auth_mode":"implicit_status"}}'
            FROM tasks
            WHERE task_key = NEW.task_key;
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS runtime_transition_requires_live_lease
        BEFORE UPDATE OF status ON tasks
        WHEN NEW.status IN ('implementing', 'validating')
             AND OLD.status IS NOT NEW.status
             AND NOT EXISTS (
                 SELECT 1
                 FROM runtime_leases
                 WHERE runtime_leases.task_id = NEW.task_id
                   AND runtime_leases.attempt_id = NEW.active_attempt_id
                   AND runtime_leases.is_active = 1
                   AND julianday(runtime_leases.expires_at) > julianday('now')
             )
        BEGIN
            SELECT RAISE(ABORT, 'runtime transition requires active unexpired lease');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS runtime_executor_start_requires_live_lease
        BEFORE INSERT ON task_events
        WHEN NEW.event_type = 'note'
             AND instr(COALESCE(NEW.payload_json, ''), '"executor_run_started"') > 0
             AND NOT EXISTS (
                 SELECT 1
                 FROM tasks
                 JOIN runtime_leases
                   ON runtime_leases.task_id = tasks.task_id
                  AND runtime_leases.attempt_id = tasks.active_attempt_id
                 WHERE tasks.task_key = NEW.task_key
                   AND runtime_leases.is_active = 1
                   AND julianday(runtime_leases.expires_at) >
                       julianday(COALESCE(NEW.created_at, 'now'))
             )
        BEGIN
            SELECT RAISE(ABORT, 'executor start requires active unexpired runtime lease');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS runtime_task_event_heartbeat
        AFTER INSERT ON task_events
        WHEN EXISTS (
            SELECT 1
            FROM tasks
            JOIN runtime_leases
              ON runtime_leases.task_id = tasks.task_id
             AND runtime_leases.attempt_id = tasks.active_attempt_id
            WHERE tasks.task_key = NEW.task_key
              AND runtime_leases.is_active = 1
              AND julianday(runtime_leases.expires_at) >
                  julianday(COALESCE(NEW.created_at, 'now'))
        )
        BEGIN
            UPDATE runtime_leases
            SET owner_id = CASE
                    WHEN auth_mode = 'implicit_status'
                         AND owner_id LIKE 'pending:%'
                         AND NEW.event_type = 'status_changed'
                         AND instr(
                             COALESCE(NEW.payload_json, ''),
                             '"status": "preparing"'
                         ) > 0
                    THEN NEW.source || ':event-' || NEW.id
                    ELSE owner_id
                END,
                heartbeat_at = NEW.created_at,
                expires_at = strftime(
                    '%Y-%m-%dT%H:%M:%fZ',
                    NEW.created_at,
                    '+' || ttl_seconds || ' seconds'
                )
            WHERE task_id = (
                    SELECT task_id FROM tasks WHERE task_key = NEW.task_key
                )
              AND attempt_id = (
                    SELECT active_attempt_id FROM tasks WHERE task_key = NEW.task_key
                )
              AND is_active = 1;

            UPDATE attempts
            SET updated_at = NEW.created_at
            WHERE attempt_id = (
                    SELECT active_attempt_id FROM tasks WHERE task_key = NEW.task_key
                )
              AND is_active = 1;
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS runtime_terminal_status_releases_lease
        AFTER UPDATE OF status ON tasks
        WHEN NEW.status IN ('blocked', 'waiting_approval', 'canceled', 'completed')
             AND OLD.status IS NOT NEW.status
             AND NEW.active_attempt_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM runtime_claim_suppressions
                 WHERE task_id = NEW.task_id
             )
        BEGIN
            INSERT INTO lifecycle_events (
                task_id, attempt_id, from_status, to_status,
                reason_code, actor, timestamp, metadata_json
            )
            SELECT
                NEW.task_id,
                NEW.active_attempt_id,
                status,
                NEW.status,
                'runtime_attempt_closed_by_task_status',
                'runtime_admission_trigger',
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                '{"task_status":"' || NEW.status || '"}'
            FROM attempts
            WHERE attempt_id = NEW.active_attempt_id;

            UPDATE runtime_leases
            SET is_active = 0,
                released_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                release_reason = 'task_status:' || NEW.status
            WHERE attempt_id = NEW.active_attempt_id
              AND is_active = 1;

            UPDATE attempts
            SET status = NEW.status,
                is_active = 0,
                ended_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                execution_result = CASE
                    WHEN NEW.status = 'waiting_approval' THEN 'completed'
                    ELSE NEW.status
                END,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE attempt_id = NEW.active_attempt_id
              AND is_active = 1;

            UPDATE tasks
            SET active_attempt_id = NULL
            WHERE task_key = NEW.task_key;
        END
        """,
    )


def migrate_runtime_admission(db_path: str | Path | None = None) -> None:
    """Install atomic pickup, ownership, lease, and heartbeat enforcement."""
    migrate_task_attempt_lifecycle(db_path)
    with closing(connect(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        for statement in _schema_statements():
            conn.execute(statement)
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations(name, applied_at)
            VALUES (?, ?)
            """,
            (RUNTIME_ADMISSION_MIGRATION, utc_now_iso()),
        )
