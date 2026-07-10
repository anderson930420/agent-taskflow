"""Canonical explicit-token runtime admission migration.

PR-3 installed a compatibility boundary that could create ``implicit_status``
leases from a persisted ``-> preparing`` transition. PR-4 removes that fallback
and requires every new runtime pickup to pass through the explicit claim API.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from agent_taskflow.models import utc_now_iso
from agent_taskflow.runtime_admission_schema import migrate_runtime_admission
from agent_taskflow.store import connect

CANONICAL_RUNTIME_ADMISSION_MIGRATION = "level2_canonical_runtime_admission_v1"


def _migration_recorded(db_path: str | Path | None) -> bool:
    """Return whether the canonical migration is already recorded."""
    try:
        with closing(connect(db_path)) as conn:
            table = conn.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'schema_migrations'
                """
            ).fetchone()
            if table is None:
                return False
            row = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name = ?",
                (CANONICAL_RUNTIME_ADMISSION_MIGRATION,),
            ).fetchone()
            return row is not None
    except Exception:
        return False


def migrate_canonical_runtime_admission(
    db_path: str | Path | None = None,
) -> None:
    """Install fail-closed explicit-token runtime admission.

    The migration refuses to proceed while an active PR-3 compatibility lease
    exists. Such a lease has no recoverable raw token and cannot be safely
    upgraded in place.
    """
    if _migration_recorded(db_path):
        return

    migrate_runtime_admission(db_path)

    with closing(connect(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")

        json_value = conn.execute(
            "SELECT json_extract('{\"value\": 1}', '$.value')"
        ).fetchone()[0]
        if json_value != 1:
            raise RuntimeError("SQLite JSON functions are required")

        active_implicit = conn.execute(
            """
            SELECT COUNT(*)
            FROM runtime_leases
            WHERE is_active = 1 AND auth_mode = 'implicit_status'
            """
        ).fetchone()[0]
        if active_implicit:
            raise RuntimeError(
                "Cannot enable canonical runtime admission while active "
                "implicit_status leases exist; finish or reap them first"
            )

        for trigger_name in (
            "runtime_pickup_claim_after_preparing",
            "runtime_task_event_heartbeat",
            "runtime_terminal_status_releases_lease",
            "runtime_executor_start_requires_live_lease",
            "runtime_preparing_requires_canonical_claim",
            "runtime_executor_start_requires_canonical_claim",
            "runtime_token_terminal_requires_owned_release",
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")

        conn.execute(
            """
            CREATE TRIGGER runtime_preparing_requires_canonical_claim
            BEFORE UPDATE OF status ON tasks
            WHEN NEW.status = 'preparing'
                 AND OLD.status IS NOT 'preparing'
                 AND NOT EXISTS (
                     SELECT 1
                     FROM runtime_claim_suppressions
                     WHERE task_id = COALESCE(NEW.task_id, 'task:' || NEW.task_key)
                 )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'canonical runtime admission claim required'
                );
            END
            """
        )

        conn.execute(
            """
            CREATE TRIGGER runtime_executor_start_requires_canonical_claim
            BEFORE INSERT ON task_events
            WHEN NEW.event_type = 'note'
                 AND json_extract(COALESCE(NEW.payload_json, '{}'), '$.kind') =
                     'executor_run_started'
                 AND NOT EXISTS (
                     SELECT 1
                     FROM tasks
                     JOIN runtime_leases
                       ON runtime_leases.task_id = tasks.task_id
                      AND runtime_leases.attempt_id = tasks.active_attempt_id
                     WHERE tasks.task_key = NEW.task_key
                       AND runtime_leases.auth_mode = 'token'
                       AND runtime_leases.is_active = 1
                       AND julianday(runtime_leases.expires_at) >
                           julianday(COALESCE(NEW.created_at, 'now'))
                       AND runtime_leases.attempt_id = json_extract(
                           NEW.payload_json,
                           '$.runtime_attempt_id'
                       )
                       AND runtime_leases.lease_id = json_extract(
                           NEW.payload_json,
                           '$.runtime_lease_id'
                       )
                       AND runtime_leases.owner_id = json_extract(
                           NEW.payload_json,
                           '$.runtime_owner_id'
                       )
                 )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'executor start requires canonical runtime claim metadata'
                );
            END
            """
        )

        conn.execute(
            """
            CREATE TRIGGER runtime_token_terminal_requires_owned_release
            BEFORE UPDATE OF status ON tasks
            WHEN NEW.status IN ('blocked', 'waiting_approval', 'canceled', 'completed')
                 AND OLD.status IS NOT NEW.status
                 AND NEW.active_attempt_id IS NOT NULL
                 AND EXISTS (
                     SELECT 1
                     FROM runtime_leases
                     WHERE runtime_leases.task_id = NEW.task_id
                       AND runtime_leases.attempt_id = NEW.active_attempt_id
                       AND runtime_leases.auth_mode = 'token'
                       AND runtime_leases.is_active = 1
                 )
                 AND NOT EXISTS (
                     SELECT 1
                     FROM runtime_claim_suppressions
                     WHERE task_id = NEW.task_id
                 )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'token runtime lease requires owned release'
                );
            END
            """
        )

        # Keep the PR-3 trigger names occupied with canonical-safe definitions.
        # ``migrate_runtime_admission`` uses CREATE TRIGGER IF NOT EXISTS, so
        # read-only PR-3 tooling cannot reinstall the old implicit pickup or
        # event-driven token heartbeat behavior after this migration.
        conn.execute(
            """
            CREATE TRIGGER runtime_pickup_claim_after_preparing
            AFTER UPDATE OF status ON tasks
            WHEN 0
            BEGIN
                SELECT 1;
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER runtime_executor_start_requires_live_lease
            BEFORE INSERT ON task_events
            WHEN 0
            BEGIN
                SELECT 1;
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER runtime_terminal_status_releases_lease
            AFTER UPDATE OF status ON tasks
            WHEN 0
            BEGIN
                SELECT 1;
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER runtime_task_event_heartbeat
            AFTER INSERT ON task_events
            WHEN EXISTS (
                SELECT 1
                FROM tasks
                JOIN runtime_leases
                  ON runtime_leases.task_id = tasks.task_id
                 AND runtime_leases.attempt_id = tasks.active_attempt_id
                WHERE tasks.task_key = NEW.task_key
                  AND runtime_leases.auth_mode = 'implicit_status'
                  AND runtime_leases.is_active = 1
                  AND julianday(runtime_leases.expires_at) >
                      julianday(COALESCE(NEW.created_at, 'now'))
            )
            BEGIN
                UPDATE runtime_leases
                SET heartbeat_at = NEW.created_at,
                    expires_at = strftime(
                        '%Y-%m-%dT%H:%M:%fZ',
                        NEW.created_at,
                        '+' || ttl_seconds || ' seconds'
                    )
                WHERE task_id = (
                        SELECT task_id FROM tasks WHERE task_key = NEW.task_key
                    )
                  AND attempt_id = (
                        SELECT active_attempt_id
                        FROM tasks
                        WHERE task_key = NEW.task_key
                    )
                  AND auth_mode = 'implicit_status'
                  AND is_active = 1;
            END
            """
        )

        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations(name, applied_at)
            VALUES (?, ?)
            """,
            (CANONICAL_RUNTIME_ADMISSION_MIGRATION, utc_now_iso()),
        )


__all__ = [
    "CANONICAL_RUNTIME_ADMISSION_MIGRATION",
    "migrate_canonical_runtime_admission",
]
