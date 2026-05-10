"""SQLite store for the Agent Taskflow local task state mirror.

This store mirrors Hermes/Kanban task state for local querying. It does not
replace Hermes/Kanban as the task authority.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from agent_taskflow.models import (
    TaskArtifactRecord,
    TaskEventRecord,
    TaskRecord,
    TaskWorktreeRecord,
    require_absolute_path,
    utc_now_iso,
    validate_task_status,
    validate_task_worktree_status,
)


def default_db_path() -> Path:
    """Return the default local state database path."""
    return Path.home() / ".agent-taskflow" / "state.db"


def _db_path(path: str | Path | None) -> Path:
    if path is None:
        return default_db_path()
    return require_absolute_path(path, "db_path")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys enabled."""
    db_path = _db_path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: str | Path | None = None) -> None:
    """Initialize the mirror database. Safe to run repeatedly."""
    with connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_key TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                board TEXT,
                hermes_task_id TEXT,
                title TEXT,
                status TEXT NOT NULL,
                repo_path TEXT NOT NULL,
                artifact_dir TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_synced_at TEXT
            );

            CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_key TEXT NOT NULL,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL,
                message TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_key) REFERENCES tasks(task_key)
            );

            CREATE TABLE IF NOT EXISTS task_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_key TEXT NOT NULL,
                artifact_type TEXT NOT NULL,
                path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_key) REFERENCES tasks(task_key)
            );

            CREATE TABLE IF NOT EXISTS task_worktrees (
                task_key TEXT PRIMARY KEY,
                repo_path TEXT NOT NULL,
                worktree_path TEXT NOT NULL,
                branch TEXT NOT NULL,
                base_branch TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                cleaned_at TEXT,
                FOREIGN KEY(task_key) REFERENCES tasks(task_key)
            );
            """
        )


def _row_to_task(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        task_key=row["task_key"],
        project=row["project"],
        board=row["board"],
        hermes_task_id=row["hermes_task_id"],
        title=row["title"],
        status=row["status"],
        repo_path=Path(row["repo_path"]),
        artifact_dir=Path(row["artifact_dir"]) if row["artifact_dir"] else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_synced_at=row["last_synced_at"],
    )


def _row_to_event(row: sqlite3.Row) -> TaskEventRecord:
    return TaskEventRecord(
        task_key=row["task_key"],
        event_type=row["event_type"],
        source=row["source"],
        message=row["message"],
        payload_json=row["payload_json"],
        created_at=row["created_at"],
    )


def _row_to_artifact(row: sqlite3.Row) -> TaskArtifactRecord:
    return TaskArtifactRecord(
        task_key=row["task_key"],
        artifact_type=row["artifact_type"],
        path=Path(row["path"]),
        created_at=row["created_at"],
    )


def _row_to_worktree(row: sqlite3.Row) -> TaskWorktreeRecord:
    return TaskWorktreeRecord(
        task_key=row["task_key"],
        repo_path=Path(row["repo_path"]),
        worktree_path=Path(row["worktree_path"]),
        branch=row["branch"],
        base_branch=row["base_branch"],
        status=row["status"],
        created_at=row["created_at"],
        cleaned_at=row["cleaned_at"],
    )


class TaskMirrorStore:
    """Small SQLite-backed store for mirrored task state."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = _db_path(db_path)

    def init_db(self) -> None:
        init_db(self.db_path)

    def upsert_task(self, record: TaskRecord) -> None:
        now = utc_now_iso()
        created_at = record.created_at or now
        updated_at = record.updated_at or now
        last_synced_at = record.last_synced_at or now

        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    task_key,
                    project,
                    board,
                    hermes_task_id,
                    title,
                    status,
                    repo_path,
                    artifact_dir,
                    created_at,
                    updated_at,
                    last_synced_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_key) DO UPDATE SET
                    project = excluded.project,
                    board = excluded.board,
                    hermes_task_id = excluded.hermes_task_id,
                    title = excluded.title,
                    status = excluded.status,
                    repo_path = excluded.repo_path,
                    artifact_dir = excluded.artifact_dir,
                    updated_at = excluded.updated_at,
                    last_synced_at = excluded.last_synced_at
                """,
                (
                    record.task_key,
                    record.project,
                    record.board,
                    record.hermes_task_id,
                    record.title,
                    record.status,
                    str(record.repo_path),
                    str(record.artifact_dir) if record.artifact_dir else None,
                    created_at,
                    updated_at,
                    last_synced_at,
                ),
            )

    def get_task(self, task_key: str) -> TaskRecord | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT *
                FROM tasks
                WHERE task_key = ?
                """,
                (task_key,),
            ).fetchone()

        if row is None:
            return None
        return _row_to_task(row)

    def list_tasks(
        self,
        *,
        project: str | None = None,
        status: str | None = None,
    ) -> list[TaskRecord]:
        params: list[str] = []
        clauses: list[str] = []

        if project is not None:
            clauses.append("project = ?")
            params.append(project)

        if status is not None:
            clauses.append("status = ?")
            params.append(validate_task_status(status))

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM tasks
                {where}
                ORDER BY updated_at DESC, task_key ASC
                """,
                params,
            ).fetchall()

        return [_row_to_task(row) for row in rows]

    def update_task_status(
        self,
        task_key: str,
        status: str,
        *,
        message: str | None = None,
        source: str = "local_mirror",
    ) -> None:
        validated_status = validate_task_status(status)
        now = utc_now_iso()

        with connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, last_synced_at = ?
                WHERE task_key = ?
                """,
                (validated_status, now, now, task_key),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Task not found: {task_key}")

            conn.execute(
                """
                INSERT INTO task_events (
                    task_key,
                    event_type,
                    source,
                    message,
                    payload_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task_key,
                    "status_changed",
                    source,
                    message,
                    json.dumps({"status": validated_status}, sort_keys=True),
                    now,
                ),
            )

    def record_task_event(
        self,
        task_key: str,
        event_type: str,
        source: str,
        *,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        record = TaskEventRecord(
            task_key=task_key,
            event_type=event_type,
            source=source,
            message=message,
            payload_json=json.dumps(payload, sort_keys=True) if payload else None,
            created_at=utc_now_iso(),
        )

        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO task_events (
                    task_key,
                    event_type,
                    source,
                    message,
                    payload_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.task_key,
                    record.event_type,
                    record.source,
                    record.message,
                    record.payload_json,
                    record.created_at,
                ),
            )

    def list_task_events(self, task_key: str) -> list[TaskEventRecord]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM task_events
                WHERE task_key = ?
                ORDER BY id ASC
                """,
                (task_key,),
            ).fetchall()

        return [_row_to_event(row) for row in rows]

    def record_task_artifact(
        self,
        task_key: str,
        artifact_type: str,
        path: str | Path,
    ) -> None:
        record = TaskArtifactRecord(
            task_key=task_key,
            artifact_type=artifact_type,
            path=path,
            created_at=utc_now_iso(),
        )

        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO task_artifacts (
                    task_key,
                    artifact_type,
                    path,
                    created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    record.task_key,
                    record.artifact_type,
                    str(record.path),
                    record.created_at,
                ),
            )

    def list_task_artifacts(self, task_key: str) -> list[TaskArtifactRecord]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM task_artifacts
                WHERE task_key = ?
                ORDER BY id ASC
                """,
                (task_key,),
            ).fetchall()

        return [_row_to_artifact(row) for row in rows]

    def upsert_task_worktree(self, record: TaskWorktreeRecord) -> None:
        now = utc_now_iso()
        created_at = record.created_at or now

        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO task_worktrees (
                    task_key,
                    repo_path,
                    worktree_path,
                    branch,
                    base_branch,
                    status,
                    created_at,
                    cleaned_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_key) DO UPDATE SET
                    repo_path = excluded.repo_path,
                    worktree_path = excluded.worktree_path,
                    branch = excluded.branch,
                    base_branch = excluded.base_branch,
                    status = excluded.status,
                    cleaned_at = excluded.cleaned_at
                """,
                (
                    record.task_key,
                    str(record.repo_path),
                    str(record.worktree_path),
                    record.branch,
                    record.base_branch,
                    record.status,
                    created_at,
                    record.cleaned_at,
                ),
            )

    def get_task_worktree(self, task_key: str) -> TaskWorktreeRecord | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT *
                FROM task_worktrees
                WHERE task_key = ?
                """,
                (task_key,),
            ).fetchone()

        if row is None:
            return None
        return _row_to_worktree(row)

    def list_task_worktrees(
        self,
        *,
        project: str | None = None,
        status: str | None = None,
    ) -> list[TaskWorktreeRecord]:
        params: list[str] = []
        clauses: list[str] = []

        if project is not None:
            clauses.append("tasks.project = ?")
            params.append(project)

        if status is not None:
            clauses.append("task_worktrees.status = ?")
            params.append(validate_task_worktree_status(status))

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT task_worktrees.*
                FROM task_worktrees
                JOIN tasks ON tasks.task_key = task_worktrees.task_key
                {where}
                ORDER BY task_worktrees.created_at DESC, task_worktrees.task_key ASC
                """,
                params,
            ).fetchall()

        return [_row_to_worktree(row) for row in rows]


def upsert_task(db_path: str | Path | None, record: TaskRecord) -> None:
    TaskMirrorStore(db_path).upsert_task(record)


def get_task(db_path: str | Path | None, task_key: str) -> TaskRecord | None:
    return TaskMirrorStore(db_path).get_task(task_key)


def list_tasks(
    db_path: str | Path | None,
    *,
    project: str | None = None,
    status: str | None = None,
) -> list[TaskRecord]:
    return TaskMirrorStore(db_path).list_tasks(project=project, status=status)


def record_task_event(
    db_path: str | Path | None,
    task_key: str,
    event_type: str,
    source: str,
    *,
    message: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    TaskMirrorStore(db_path).record_task_event(
        task_key,
        event_type,
        source,
        message=message,
        payload=payload,
    )


def list_task_events(
    db_path: str | Path | None,
    task_key: str,
) -> list[TaskEventRecord]:
    return TaskMirrorStore(db_path).list_task_events(task_key)


def record_task_artifact(
    db_path: str | Path | None,
    task_key: str,
    artifact_type: str,
    path: str | Path,
) -> None:
    TaskMirrorStore(db_path).record_task_artifact(task_key, artifact_type, path)


def list_task_artifacts(
    db_path: str | Path | None,
    task_key: str,
) -> list[TaskArtifactRecord]:
    return TaskMirrorStore(db_path).list_task_artifacts(task_key)


def upsert_task_worktree(
    db_path: str | Path | None,
    record: TaskWorktreeRecord,
) -> None:
    TaskMirrorStore(db_path).upsert_task_worktree(record)


def get_task_worktree(
    db_path: str | Path | None,
    task_key: str,
) -> TaskWorktreeRecord | None:
    return TaskMirrorStore(db_path).get_task_worktree(task_key)


def list_task_worktrees(
    db_path: str | Path | None,
    *,
    project: str | None = None,
    status: str | None = None,
) -> list[TaskWorktreeRecord]:
    return TaskMirrorStore(db_path).list_task_worktrees(project=project, status=status)
