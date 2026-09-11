"""Ticket domain vocabulary for the V1 master spec (SPEC §7, §12, §12.1).

A Ticket is a row in the canonical `tasks` table that was created through the
prompt-first path. There is no separate Ticket table: :class:`TicketRecord`
is a typed view over the Step 1 columns of `tasks`.

Persisted status uses the legacy `TASK_STATUSES` vocabulary. The §12 names
below are the Mission Control *display* vocabulary (SPEC §12.2) and are
bridged to persisted values by :mod:`agent_taskflow.status_vocab`.

This module is vocabulary only. It performs no persistence, no Git, and no
scheduling.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_taskflow._helpers import require_non_empty
from agent_taskflow.models import require_absolute_path, validate_task_status
from agent_taskflow.tasks import normalize_task_key


# SPEC §12 display vocabulary. Ordered for stable display.
TICKET_STATUS_SEQUENCE: tuple[str, ...] = (
    "queued",
    "ready",
    "blocked",
    "paused",
    "preparing",
    "running",
    "validating",
    "ready_for_integration",
    "integrating",
    "needs_review",
    "needs_decision",
    "completed",
    "failed",
    "cancelled",
)
TICKET_STATUSES = frozenset(TICKET_STATUS_SEQUENCE)

# SPEC §12.1: `queued` is reserved for future admission control. V1 never
# writes it, so it is a valid display name that the creation service refuses.
RESERVED_INITIAL_TICKET_STATUS = "queued"
READY_TICKET_STATUS = "ready"
BLOCKED_TICKET_STATUS = "blocked"

# SPEC §7. Highest precedence first.
TICKET_PRIORITY_SEQUENCE: tuple[str, ...] = ("critical", "high", "normal", "low")
TICKET_PRIORITIES = frozenset(TICKET_PRIORITY_SEQUENCE)
DEFAULT_TICKET_PRIORITY = "normal"

# What happened to the AI title (SPEC §10.1). Only `generated` means the
# stored title came from AI; the other two mean the deterministic fallback.
AI_TITLE_GENERATED = "generated"
AI_TITLE_FALLBACK = "fallback"
AI_TITLE_NOT_ATTEMPTED = "not_attempted"
AI_TITLE_STATUSES = frozenset(
    {AI_TITLE_GENERATED, AI_TITLE_FALLBACK, AI_TITLE_NOT_ATTEMPTED}
)

# Whether the branch slug came from the AI adapter or the fallback.
METADATA_SOURCE_AI = "ai"
METADATA_SOURCE_FALLBACK = "fallback"
METADATA_SOURCES = frozenset({METADATA_SOURCE_AI, METADATA_SOURCE_FALLBACK})


def validate_ticket_status(status: str) -> str:
    """Return a normalized §12 display status or raise ValueError."""
    normalized = require_non_empty(status, "status")
    if normalized not in TICKET_STATUSES:
        raise ValueError(f"Invalid ticket status: {status!r}")
    return normalized


def validate_ticket_priority(priority: str) -> str:
    """Return a normalized Ticket priority or raise ValueError."""
    normalized = require_non_empty(priority, "priority").lower()
    if normalized not in TICKET_PRIORITIES:
        allowed = ", ".join(TICKET_PRIORITY_SEQUENCE)
        raise ValueError(
            f"Invalid ticket priority: {priority!r}. Allowed: {allowed}"
        )
    return normalized


def validate_ai_title_status(status: str) -> str:
    """Return a normalized AI-title status or raise ValueError."""
    normalized = require_non_empty(status, "ai_title_status")
    if normalized not in AI_TITLE_STATUSES:
        raise ValueError(f"Invalid ai_title_status: {status!r}")
    return normalized


def validate_metadata_source(source: str) -> str:
    """Return a normalized metadata-source marker or raise ValueError."""
    normalized = require_non_empty(source, "metadata source")
    if normalized not in METADATA_SOURCES:
        raise ValueError(f"Invalid metadata source: {source!r}")
    return normalized


def initial_ticket_status(blocked_by: str | None) -> str:
    """Return the SPEC §12.1 initial *display* status for a new Ticket.

    `ready`, or `blocked` when the Ticket already carries a `blocked_by` at
    creation. Never `queued`. Callers persist it via status_vocab.
    """
    if blocked_by is not None and blocked_by.strip():
        return BLOCKED_TICKET_STATUS
    return READY_TICKET_STATUS


@dataclass(frozen=True)
class TicketRecord:
    """A prompt-first `tasks` row and its Python-derived metadata.

    `status` is the persisted `TASK_STATUSES` value, not a display name.
    """

    task_key: str
    repository: str
    prompt: str
    title: str
    priority: str
    status: str
    repo_path: Path
    base_branch: str
    branch: str
    worktree_path: Path
    artifact_dir: Path
    ai_title_status: str = AI_TITLE_NOT_ATTEMPTED
    branch_slug_source: str = METADATA_SOURCE_FALLBACK
    github_repo: str | None = None
    blocked_by: str | None = None
    commit_message_suggestion: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        object.__setattr__(
            self,
            "repository",
            require_non_empty(self.repository, "repository"),
        )
        object.__setattr__(self, "prompt", require_non_empty(self.prompt, "prompt"))
        object.__setattr__(self, "title", require_non_empty(self.title, "title"))
        object.__setattr__(self, "priority", validate_ticket_priority(self.priority))
        object.__setattr__(self, "status", validate_task_status(self.status))
        object.__setattr__(
            self,
            "repo_path",
            require_absolute_path(self.repo_path, "repo_path"),
        )
        object.__setattr__(
            self,
            "base_branch",
            require_non_empty(self.base_branch, "base_branch"),
        )
        object.__setattr__(self, "branch", require_non_empty(self.branch, "branch"))
        object.__setattr__(
            self,
            "worktree_path",
            require_absolute_path(self.worktree_path, "worktree_path"),
        )
        object.__setattr__(
            self,
            "artifact_dir",
            require_absolute_path(self.artifact_dir, "artifact_dir"),
        )
        object.__setattr__(
            self,
            "ai_title_status",
            validate_ai_title_status(self.ai_title_status),
        )
        object.__setattr__(
            self,
            "branch_slug_source",
            validate_metadata_source(self.branch_slug_source),
        )
        if self.blocked_by is not None:
            blocked_by = normalize_task_key(self.blocked_by)
            if blocked_by == self.task_key:
                raise ValueError("A ticket cannot block itself")
            object.__setattr__(self, "blocked_by", blocked_by)


__all__ = [
    "AI_TITLE_FALLBACK",
    "AI_TITLE_GENERATED",
    "AI_TITLE_NOT_ATTEMPTED",
    "AI_TITLE_STATUSES",
    "BLOCKED_TICKET_STATUS",
    "DEFAULT_TICKET_PRIORITY",
    "METADATA_SOURCES",
    "METADATA_SOURCE_AI",
    "METADATA_SOURCE_FALLBACK",
    "READY_TICKET_STATUS",
    "RESERVED_INITIAL_TICKET_STATUS",
    "TICKET_PRIORITIES",
    "TICKET_PRIORITY_SEQUENCE",
    "TICKET_STATUSES",
    "TICKET_STATUS_SEQUENCE",
    "TicketRecord",
    "initial_ticket_status",
    "validate_ai_title_status",
    "validate_metadata_source",
    "validate_ticket_priority",
    "validate_ticket_status",
]
