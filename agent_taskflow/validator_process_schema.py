"""PR-9 migration for validator managed process-group coverage."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from agent_taskflow.executor_process_schema import migrate_executor_process_lifecycle
from agent_taskflow.models import utc_now_iso
from agent_taskflow.reset_lineage_schema import migrate_reset_lineage
from agent_taskflow.store import connect

VALIDATOR_PROCESS_MIGRATION = "level2_validator_process_lifecycle_v1"


def migrate_validator_process_lifecycle(db_path: str | Path | None = None) -> None:
    """Upgrade the PR-7 registry into a shared executor/validator process registry."""
    migrate_reset_lineage(db_path)
    migrate_executor_process_lifecycle(db_path)
    with closing(connect(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
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
            CREATE INDEX IF NOT EXISTS ix_executor_processes_role_state
            ON executor_processes(process_role, state, created_at)
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations(name, applied_at)
            VALUES (?, ?)
            """,
            (VALIDATOR_PROCESS_MIGRATION, utc_now_iso()),
        )


__all__ = [
    "VALIDATOR_PROCESS_MIGRATION",
    "migrate_validator_process_lifecycle",
]
