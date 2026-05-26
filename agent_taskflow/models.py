"""Local task state mirror models for Agent Taskflow.

These models describe a local SQLite mirror of Hermes/Kanban task state.
They are not a replacement task authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agent_taskflow._helpers import require_non_empty as _require_non_empty
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.worktree import ensure_absolute_path
from agent_taskflow.workflow_policy_artifacts import (
    WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE,
    WORKFLOW_POLICY_ARTIFACT_INDEX_TYPE,
)


TASK_STATUSES = {
    "unknown",
    "created",
    "queued",
    "preparing",
    "implementing",
    "validating",
    "waiting_approval",
    "waiting_for_review",
    "blocked",
    "accepted",
    "rejected",
    "cleaned",
    "completed",
    "canceled",
    # Common external Kanban/Hermes-style mirror values.
    "backlog",
    "todo",
    "in_progress",
    "review",
    "done",
}

TASK_EVENT_TYPES = {
    "created",
    "mirrored",
    "status_changed",
    "artifact_recorded",
    "github_issue_ingested",
    "task_execution_package_created",
    "pr_handoff_created",
    "pr_handoff_package_created",
    "draft_pr_created",
    "branch_pushed",
    "branch_push_completed",
    "local_cleanup_completed",
    "remote_branch_cleanup_completed",
    "task_closeout_completed",
    "scheduler_proposal_created",
    "scheduler_confirmation_created",
    "scheduler_confirmation_verifier_report_created",
    "intake_runner_handoff_created",
    "runtime_preflight_finished",
    "runtime_execution_started",
    "runtime_execution_finished",
    "worktree_recorded",
    "cleanup_recorded",
    "note",
}

TASK_ARTIFACT_TYPES = {
    "spec",
    "issue_spec",
    "decision",
    "worker_log",
    "review_log",
    "manifest",
    "implementation_prompt",
    "task_execution_package",
    "pr_handoff",
    "pr_handoff_package",
    "draft_pr",
    "branch_push",
    "local_cleanup",
    "remote_branch_cleanup",
    "task_closeout",
    "scheduler_proposal",
    "scheduler_confirmation",
    "scheduler_confirmation_verifier_report",
    "intake_runner_handoff",
    "runtime_handoff_execution",
    WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE,
    WORKFLOW_POLICY_ARTIFACT_INDEX_TYPE,
    "other",
}

TASK_WORKTREE_STATUSES = {
    "active",
    "cleaned",
    "missing",
    "unknown",
}


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )


def require_absolute_path(path: str | Path, field_name: str) -> Path:
    """Return an absolute path or raise ValueError."""
    return ensure_absolute_path(path, name=field_name)


def validate_task_status(status: str) -> str:
    normalized = _require_non_empty(status, "status")
    if normalized not in TASK_STATUSES:
        raise ValueError(f"Invalid task status: {status!r}")
    return normalized


def validate_task_event_type(event_type: str) -> str:
    normalized = _require_non_empty(event_type, "event_type")
    if normalized not in TASK_EVENT_TYPES:
        raise ValueError(f"Invalid task event type: {event_type!r}")
    return normalized


def validate_task_artifact_type(artifact_type: str) -> str:
    normalized = _require_non_empty(artifact_type, "artifact_type")
    if normalized not in TASK_ARTIFACT_TYPES:
        raise ValueError(f"Invalid task artifact type: {artifact_type!r}")
    return normalized


def validate_task_worktree_status(status: str) -> str:
    normalized = _require_non_empty(status, "status")
    if normalized not in TASK_WORKTREE_STATUSES:
        raise ValueError(f"Invalid task worktree status: {status!r}")
    return normalized


@dataclass(frozen=True)
class TaskRecord:
    """Mirrored task state from Hermes/Kanban."""

    task_key: str
    project: str
    status: str
    repo_path: Path
    board: str | None = None
    hermes_task_id: str | None = None
    title: str | None = None
    artifact_dir: Path | None = None
    blocked_reason: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    last_synced_at: str | None = None
    # Executor selection fields (Phase 13)
    executor: str | None = None
    model: str | None = None
    provider: str | None = None
    tools: list[str] | None = None
    pi_bin: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        object.__setattr__(self, "project", _require_non_empty(self.project, "project"))
        object.__setattr__(self, "status", validate_task_status(self.status))
        object.__setattr__(
            self,
            "repo_path",
            require_absolute_path(self.repo_path, "repo_path"),
        )
        if self.artifact_dir is not None:
            object.__setattr__(
                self,
                "artifact_dir",
                require_absolute_path(self.artifact_dir, "artifact_dir"),
            )


@dataclass(frozen=True)
class TaskEventRecord:
    """Append-only mirrored task event."""

    task_key: str
    event_type: str
    source: str
    message: str | None = None
    payload_json: str | None = None
    created_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        object.__setattr__(
            self,
            "event_type",
            validate_task_event_type(self.event_type),
        )
        object.__setattr__(self, "source", _require_non_empty(self.source, "source"))


@dataclass(frozen=True)
class TaskArtifactRecord:
    """Mirrored artifact path for a task."""

    task_key: str
    artifact_type: str
    path: Path
    created_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        object.__setattr__(
            self,
            "artifact_type",
            validate_task_artifact_type(self.artifact_type),
        )
        object.__setattr__(self, "path", require_absolute_path(self.path, "path"))


@dataclass(frozen=True)
class TaskWorktreeRecord:
    """Mirrored worktree reference for a task."""

    task_key: str
    repo_path: Path
    worktree_path: Path
    branch: str
    status: str
    base_branch: str | None = None
    base_sha: str | None = None
    created_at: str | None = None
    cleaned_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        object.__setattr__(
            self,
            "repo_path",
            require_absolute_path(self.repo_path, "repo_path"),
        )
        object.__setattr__(
            self,
            "worktree_path",
            require_absolute_path(self.worktree_path, "worktree_path"),
        )
        object.__setattr__(self, "branch", _require_non_empty(self.branch, "branch"))
        object.__setattr__(
            self,
            "status",
            validate_task_worktree_status(self.status),
        )
