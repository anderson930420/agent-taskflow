"""Additive schema for Attempt-scoped runtime filesystem resources."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from agent_taskflow.canonical_runtime_schema import migrate_canonical_runtime_admission
from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import connect

ATTEMPT_RESOURCES_MIGRATION = "level2_attempt_scoped_resources_v1"


def migrate_attempt_resources(db_path: str | Path | None = None) -> None:
    """Install Attempt-scoped branch/worktree/lock/PID/artifact persistence."""
    migrate_canonical_runtime_admission(db_path)
    with closing(connect(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS attempt_resources (
                attempt_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                task_key TEXT NOT NULL,
                attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
                owner_id TEXT NOT NULL,
                repo_path TEXT NOT NULL,
                base_branch TEXT NOT NULL,
                base_sha TEXT,
                branch_name TEXT NOT NULL UNIQUE,
                worktree_root TEXT NOT NULL,
                worktree_path TEXT NOT NULL UNIQUE,
                artifact_base_root TEXT NOT NULL,
                artifact_root TEXT NOT NULL UNIQUE,
                lock_path TEXT NOT NULL UNIQUE,
                pid_path TEXT NOT NULL UNIQUE,
                runtime_pid INTEGER,
                status TEXT NOT NULL CHECK(status IN (
                    'allocated', 'active', 'released', 'reaped',
                    'reap_blocked_live_pid', 'allocation_failed'
                )),
                allocated_at TEXT NOT NULL,
                activated_at TEXT,
                released_at TEXT,
                reaped_at TEXT,
                updated_at TEXT NOT NULL,
                release_reason TEXT,
                FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id),
                FOREIGN KEY(task_id) REFERENCES tasks(task_id),
                UNIQUE(task_id, attempt_number)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_attempt_resources_task_status
            ON attempt_resources(task_id, status, attempt_number)
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS attempt_resources_identity_guard
            BEFORE INSERT ON attempt_resources
            WHEN NOT EXISTS (
                SELECT 1
                FROM attempts
                WHERE attempts.attempt_id = NEW.attempt_id
                  AND attempts.task_id = NEW.task_id
                  AND attempts.attempt_number = NEW.attempt_number
            )
            BEGIN
                SELECT RAISE(ABORT, 'attempt resource identity mismatch');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS attempt_resources_immutable_paths
            BEFORE UPDATE OF task_id, task_key, attempt_number, repo_path,
                branch_name, worktree_root, worktree_path, artifact_base_root,
                artifact_root, lock_path, pid_path
            ON attempt_resources
            BEGIN
                SELECT RAISE(ABORT, 'attempt resource identity and paths are immutable');
            END
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations(name, applied_at)
            VALUES (?, ?)
            """,
            (ATTEMPT_RESOURCES_MIGRATION, utc_now_iso()),
        )


__all__ = ["ATTEMPT_RESOURCES_MIGRATION", "migrate_attempt_resources"]
