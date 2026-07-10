"""Models and validation helpers for Level 2 Task/Attempt persistence."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from agent_taskflow.models import require_absolute_path
from agent_taskflow.tasks import normalize_task_key


ATTEMPT_STATUSES = {
    "created",
    "preparing",
    "implementing",
    "validating",
    "waiting_approval",
    "validation_failed",
    "execution_timeout",
    "execution_aborted",
    "blocked",
    "completed",
    "failed",
    "canceled",
}


class ActiveAttemptExistsError(RuntimeError):
    """Raised when a task already owns an active attempt."""


class AttemptNotActiveError(RuntimeError):
    """Raised when an operation requires an active attempt but finds none."""


def require_non_empty(value: str, field_name: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized:
        raise ValueError(f"{field_name} must be a non-empty string")
    return normalized


def validate_attempt_status(status: str) -> str:
    normalized = require_non_empty(status, "status")
    if normalized not in ATTEMPT_STATUSES:
        raise ValueError(f"Invalid attempt status: {status!r}")
    return normalized


def default_task_id(task_key: str) -> str:
    """Return the deterministic identity used for migrated mirror tasks."""
    return f"task:{normalize_task_key(task_key)}"


@dataclass(frozen=True)
class TaskIdentityRecord:
    task_id: str
    task_key: str
    project: str
    task_class: str
    current_status: str
    active_attempt_id: str | None
    final_outcome: str | None
    created_at: str
    closed_at: str | None
    is_legacy: bool


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    task_id: str
    attempt_number: int
    status: str
    is_active: bool
    is_legacy: bool
    executor: str | None = None
    model: str | None = None
    base_commit: str | None = None
    policy_version: str | None = None
    config_snapshot_hash: str | None = None
    prompt_template_version: str | None = None
    permission_profile: str | None = None
    worktree_path: Path | None = None
    artifact_root: Path | None = None
    started_at: str | None = None
    ended_at: str | None = None
    execution_result: str | None = None
    validation_result: str | None = None
    merge_recommendation: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "attempt_id",
            require_non_empty(self.attempt_id, "attempt_id"),
        )
        object.__setattr__(
            self,
            "task_id",
            require_non_empty(self.task_id, "task_id"),
        )
        if self.attempt_number < 1:
            raise ValueError("attempt_number must be >= 1")
        object.__setattr__(self, "status", validate_attempt_status(self.status))
        if self.worktree_path is not None:
            object.__setattr__(
                self,
                "worktree_path",
                require_absolute_path(self.worktree_path, "worktree_path"),
            )
        if self.artifact_root is not None:
            object.__setattr__(
                self,
                "artifact_root",
                require_absolute_path(self.artifact_root, "artifact_root"),
            )


@dataclass(frozen=True)
class LifecycleEventRecord:
    event_id: int
    task_id: str
    attempt_id: str | None
    from_status: str | None
    to_status: str
    reason_code: str
    actor: str
    timestamp: str
    metadata_json: str


def row_to_task_identity(row: sqlite3.Row) -> TaskIdentityRecord:
    return TaskIdentityRecord(
        task_id=row["task_id"],
        task_key=row["task_key"],
        project=row["project"],
        task_class=row["task_class"],
        current_status=row["status"],
        active_attempt_id=row["active_attempt_id"],
        final_outcome=row["final_outcome"],
        created_at=row["created_at"],
        closed_at=row["closed_at"],
        is_legacy=bool(row["is_legacy"]),
    )


def row_to_attempt(row: sqlite3.Row) -> AttemptRecord:
    return AttemptRecord(
        attempt_id=row["attempt_id"],
        task_id=row["task_id"],
        attempt_number=row["attempt_number"],
        status=row["status"],
        is_active=bool(row["is_active"]),
        is_legacy=bool(row["is_legacy"]),
        executor=row["executor"],
        model=row["model"],
        base_commit=row["base_commit"],
        policy_version=row["policy_version"],
        config_snapshot_hash=row["config_snapshot_hash"],
        prompt_template_version=row["prompt_template_version"],
        permission_profile=row["permission_profile"],
        worktree_path=Path(row["worktree_path"]) if row["worktree_path"] else None,
        artifact_root=Path(row["artifact_root"]) if row["artifact_root"] else None,
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        execution_result=row["execution_result"],
        validation_result=row["validation_result"],
        merge_recommendation=row["merge_recommendation"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def row_to_lifecycle_event(row: sqlite3.Row) -> LifecycleEventRecord:
    return LifecycleEventRecord(
        event_id=row["event_id"],
        task_id=row["task_id"],
        attempt_id=row["attempt_id"],
        from_status=row["from_status"],
        to_status=row["to_status"],
        reason_code=row["reason_code"],
        actor=row["actor"],
        timestamp=row["timestamp"],
        metadata_json=row["metadata_json"],
    )
