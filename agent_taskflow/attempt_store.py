"""Storage API for Level 2 Task, Attempt, and lifecycle records."""

from __future__ import annotations

from contextlib import closing
import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_taskflow.attempt_models import (
    ATTEMPT_STATUSES,
    ActiveAttemptExistsError,
    AttemptNotActiveError,
    AttemptRecord,
    LifecycleEventRecord,
    TaskIdentityRecord,
    default_task_id,
    require_non_empty,
    row_to_attempt,
    row_to_lifecycle_event,
    row_to_task_identity,
    validate_attempt_status,
)
from agent_taskflow.attempt_schema import (
    TASK_ATTEMPT_LIFECYCLE_MIGRATION,
    migrate_task_attempt_lifecycle,
)
from agent_taskflow.models import require_absolute_path, utc_now_iso
from agent_taskflow.store import connect, default_db_path
from agent_taskflow.tasks import normalize_task_key

__all__ = [
    "ATTEMPT_STATUSES",
    "TASK_ATTEMPT_LIFECYCLE_MIGRATION",
    "ActiveAttemptExistsError",
    "AttemptNotActiveError",
    "AttemptRecord",
    "AttemptStore",
    "LifecycleEventRecord",
    "TaskIdentityRecord",
    "default_task_id",
    "migrate_task_attempt_lifecycle",
    "validate_attempt_status",
]


class AttemptStore:
    """SQLite access for Level 2 Task/Attempt/lifecycle records."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = (
            default_db_path()
            if db_path is None
            else require_absolute_path(db_path, "db_path")
        )

    def init_db(self) -> None:
        migrate_task_attempt_lifecycle(self.db_path)

    def _ensure_task_identity(
        self,
        conn: sqlite3.Connection,
        task_key: str,
    ) -> sqlite3.Row:
        normalized_key = normalize_task_key(task_key)
        row = conn.execute(
            """
            SELECT task_key, task_id, task_class, status, active_attempt_id, is_legacy
            FROM tasks
            WHERE task_key = ?
            """,
            (normalized_key,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Task not found: {normalized_key}")
        if not row["task_id"]:
            conn.execute(
                """
                UPDATE tasks
                SET task_id = ?, task_class = 'legacy', is_legacy = 1
                WHERE task_key = ?
                """,
                (default_task_id(normalized_key), normalized_key),
            )
            row = conn.execute(
                """
                SELECT task_key, task_id, task_class, status,
                       active_attempt_id, is_legacy
                FROM tasks
                WHERE task_key = ?
                """,
                (normalized_key,),
            ).fetchone()
            assert row is not None
        return row

    def register_task_identity(
        self,
        task_key: str,
        *,
        task_class: str,
        task_id: str | None = None,
        is_legacy: bool = False,
    ) -> TaskIdentityRecord:
        """Attach explicit Level 2 identity metadata to an existing mirror task."""
        normalized_key = normalize_task_key(task_key)
        normalized_class = require_non_empty(task_class, "task_class")
        resolved_task_id = task_id or default_task_id(normalized_key)
        require_non_empty(resolved_task_id, "task_id")

        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT task_id FROM tasks WHERE task_key = ?",
                (normalized_key,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Task not found: {normalized_key}")
            if row["task_id"] and row["task_id"] != resolved_task_id:
                raise ValueError(
                    f"Task {normalized_key} already has stable task_id "
                    f"{row['task_id']}"
                )
            conn.execute(
                """
                UPDATE tasks
                SET task_id = ?,
                    task_class = ?,
                    is_legacy = ?,
                    updated_at = ?
                WHERE task_key = ?
                """,
                (
                    resolved_task_id,
                    normalized_class,
                    1 if is_legacy else 0,
                    utc_now_iso(),
                    normalized_key,
                ),
            )

        identity = self.get_task_identity(normalized_key)
        assert identity is not None
        return identity

    def get_task_identity(self, task_key: str) -> TaskIdentityRecord | None:
        normalized_key = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn, conn:
            row = conn.execute(
                """
                SELECT task_id, task_key, project, task_class, status,
                       active_attempt_id, final_outcome, created_at, closed_at,
                       is_legacy
                FROM tasks
                WHERE task_key = ? AND task_id IS NOT NULL
                """,
                (normalized_key,),
            ).fetchone()
        return row_to_task_identity(row) if row is not None else None

    @staticmethod
    def _insert_lifecycle_event(
        conn: sqlite3.Connection,
        *,
        task_id: str,
        attempt_id: str | None,
        from_status: str | None,
        to_status: str,
        reason_code: str,
        actor: str,
        timestamp: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO lifecycle_events (
                task_id,
                attempt_id,
                from_status,
                to_status,
                reason_code,
                actor,
                timestamp,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                attempt_id,
                from_status,
                require_non_empty(to_status, "to_status"),
                require_non_empty(reason_code, "reason_code"),
                require_non_empty(actor, "actor"),
                timestamp,
                json.dumps(metadata or {}, sort_keys=True),
            ),
        )

    def create_attempt(
        self,
        task_key: str,
        *,
        executor: str | None = None,
        model: str | None = None,
        base_commit: str | None = None,
        policy_version: str | None = None,
        config_snapshot_hash: str | None = None,
        prompt_template_version: str | None = None,
        permission_profile: str | None = None,
        worktree_path: str | Path | None = None,
        artifact_root: str | Path | None = None,
        status: str = "created",
        attempt_id: str | None = None,
        reason_code: str = "attempt_created",
        actor: str = "attempt_store",
        metadata: dict[str, Any] | None = None,
    ) -> AttemptRecord:
        validated_status = validate_attempt_status(status)
        normalized_worktree = (
            require_absolute_path(worktree_path, "worktree_path")
            if worktree_path is not None
            else None
        )
        normalized_artifact_root = (
            require_absolute_path(artifact_root, "artifact_root")
            if artifact_root is not None
            else None
        )
        created_at = utc_now_iso()
        resolved_attempt_id = attempt_id or f"attempt-{uuid4().hex}"
        require_non_empty(resolved_attempt_id, "attempt_id")

        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            task = self._ensure_task_identity(conn, task_key)
            active = conn.execute(
                """
                SELECT attempt_id
                FROM attempts
                WHERE task_id = ? AND is_active = 1
                """,
                (task["task_id"],),
            ).fetchone()
            if task["active_attempt_id"] is not None or active is not None:
                active_id = task["active_attempt_id"] or active["attempt_id"]
                raise ActiveAttemptExistsError(
                    f"Task {task['task_key']} already has active attempt {active_id}"
                )

            next_number = conn.execute(
                """
                SELECT COALESCE(MAX(attempt_number), 0) + 1
                FROM attempts
                WHERE task_id = ?
                """,
                (task["task_id"],),
            ).fetchone()[0]

            conn.execute(
                """
                INSERT INTO attempts (
                    attempt_id,
                    task_id,
                    attempt_number,
                    status,
                    is_active,
                    is_legacy,
                    executor,
                    model,
                    base_commit,
                    policy_version,
                    config_snapshot_hash,
                    prompt_template_version,
                    permission_profile,
                    worktree_path,
                    artifact_root,
                    started_at,
                    ended_at,
                    execution_result,
                    validation_result,
                    merge_recommendation,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    resolved_attempt_id,
                    task["task_id"],
                    next_number,
                    validated_status,
                    executor,
                    model,
                    base_commit,
                    policy_version,
                    config_snapshot_hash,
                    prompt_template_version,
                    permission_profile,
                    str(normalized_worktree) if normalized_worktree else None,
                    str(normalized_artifact_root) if normalized_artifact_root else None,
                    created_at,
                    created_at,
                    created_at,
                ),
            )
            conn.execute(
                """
                UPDATE tasks
                SET active_attempt_id = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (resolved_attempt_id, created_at, task["task_id"]),
            )
            self._insert_lifecycle_event(
                conn,
                task_id=task["task_id"],
                attempt_id=resolved_attempt_id,
                from_status=None,
                to_status=validated_status,
                reason_code=reason_code,
                actor=actor,
                timestamp=created_at,
                metadata=metadata,
            )

        attempt = self.get_attempt(resolved_attempt_id)
        assert attempt is not None
        return attempt

    def close_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        reason_code: str,
        actor: str,
        execution_result: str | None = None,
        validation_result: str | None = None,
        merge_recommendation: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AttemptRecord:
        validated_status = validate_attempt_status(status)
        ended_at = utc_now_iso()
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT attempts.*, tasks.active_attempt_id
                FROM attempts
                JOIN tasks ON tasks.task_id = attempts.task_id
                WHERE attempts.attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Attempt not found: {attempt_id}")
            if not row["is_active"] or row["active_attempt_id"] != attempt_id:
                raise AttemptNotActiveError(f"Attempt is not active: {attempt_id}")

            conn.execute(
                """
                UPDATE attempts
                SET status = ?,
                    is_active = 0,
                    ended_at = ?,
                    execution_result = ?,
                    validation_result = ?,
                    merge_recommendation = ?,
                    updated_at = ?
                WHERE attempt_id = ? AND is_active = 1
                """,
                (
                    validated_status,
                    ended_at,
                    execution_result,
                    validation_result,
                    merge_recommendation,
                    ended_at,
                    attempt_id,
                ),
            )
            cursor = conn.execute(
                """
                UPDATE tasks
                SET active_attempt_id = NULL, updated_at = ?
                WHERE task_id = ? AND active_attempt_id = ?
                """,
                (ended_at, row["task_id"], attempt_id),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError(
                    "task active_attempt_id changed while closing attempt"
                )
            self._insert_lifecycle_event(
                conn,
                task_id=row["task_id"],
                attempt_id=attempt_id,
                from_status=row["status"],
                to_status=validated_status,
                reason_code=reason_code,
                actor=actor,
                timestamp=ended_at,
                metadata=metadata,
            )

        attempt = self.get_attempt(attempt_id)
        assert attempt is not None
        return attempt

    def get_attempt(self, attempt_id: str) -> AttemptRecord | None:
        with closing(connect(self.db_path)) as conn, conn:
            row = conn.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return row_to_attempt(row) if row is not None else None

    def get_active_attempt(self, task_key: str) -> AttemptRecord | None:
        normalized_key = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn, conn:
            row = conn.execute(
                """
                SELECT attempts.*
                FROM attempts
                JOIN tasks ON tasks.task_id = attempts.task_id
                WHERE tasks.task_key = ? AND attempts.is_active = 1
                """,
                (normalized_key,),
            ).fetchone()
        return row_to_attempt(row) if row is not None else None

    def list_attempts(self, task_key: str) -> list[AttemptRecord]:
        normalized_key = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn, conn:
            rows = conn.execute(
                """
                SELECT attempts.*
                FROM attempts
                JOIN tasks ON tasks.task_id = attempts.task_id
                WHERE tasks.task_key = ?
                ORDER BY attempts.attempt_number ASC
                """,
                (normalized_key,),
            ).fetchall()
        return [row_to_attempt(row) for row in rows]

    def append_lifecycle_event(
        self,
        task_key: str,
        *,
        attempt_id: str | None,
        from_status: str | None,
        to_status: str,
        reason_code: str,
        actor: str,
        metadata: dict[str, Any] | None = None,
    ) -> LifecycleEventRecord:
        timestamp = utc_now_iso()
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            task = self._ensure_task_identity(conn, task_key)
            self._insert_lifecycle_event(
                conn,
                task_id=task["task_id"],
                attempt_id=attempt_id,
                from_status=from_status,
                to_status=to_status,
                reason_code=reason_code,
                actor=actor,
                timestamp=timestamp,
                metadata=metadata,
            )
            event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        with closing(connect(self.db_path)) as conn, conn:
            row = conn.execute(
                "SELECT * FROM lifecycle_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        assert row is not None
        return row_to_lifecycle_event(row)

    def list_lifecycle_events(self, task_key: str) -> list[LifecycleEventRecord]:
        normalized_key = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn, conn:
            rows = conn.execute(
                """
                SELECT lifecycle_events.*
                FROM lifecycle_events
                JOIN tasks ON tasks.task_id = lifecycle_events.task_id
                WHERE tasks.task_key = ?
                ORDER BY lifecycle_events.event_id ASC
                """,
                (normalized_key,),
            ).fetchall()
        return [row_to_lifecycle_event(row) for row in rows]
