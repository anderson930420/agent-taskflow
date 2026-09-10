"""Ticket domain model for the V1 master spec (SPEC §7, §12, §12.1).

A Ticket is the V1 unit of work: repository + prompt + priority in, one
isolated worktree identity out. It is deliberately a separate record from the
legacy ``TaskRecord`` mirror in :mod:`agent_taskflow.models`, which mirrors
Hermes/Kanban state and uses a different status vocabulary.

This module is domain vocabulary only. It performs no persistence, no Git,
and no scheduling.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_taskflow._helpers import require_non_empty
from agent_taskflow.models import require_absolute_path
from agent_taskflow.tasks import normalize_task_key


# SPEC §12. Ordered for stable display; membership checks use TICKET_STATUSES.
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
# writes it, so it is a valid enum member that the creation service refuses.
RESERVED_INITIAL_TICKET_STATUS = "queued"
READY_TICKET_STATUS = "ready"
BLOCKED_TICKET_STATUS = "blocked"

# SPEC §7. Highest precedence first.
TICKET_PRIORITY_SEQUENCE: tuple[str, ...] = ("critical", "high", "normal", "low")
TICKET_PRIORITIES = frozenset(TICKET_PRIORITY_SEQUENCE)
DEFAULT_TICKET_PRIORITY = "normal"

TICKET_EVENT_TYPES = frozenset({"ticket_created"})

# Whether a human-facing metadata field came from the AI adapter or from the
# deterministic fallback (SPEC §10.1).
METADATA_SOURCE_AI = "ai"
METADATA_SOURCE_FALLBACK = "fallback"
METADATA_SOURCES = frozenset({METADATA_SOURCE_AI, METADATA_SOURCE_FALLBACK})


def validate_ticket_status(status: str) -> str:
    """Return a normalized Ticket status or raise ValueError."""
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


def validate_metadata_source(source: str) -> str:
    """Return a normalized metadata-source marker or raise ValueError."""
    normalized = require_non_empty(source, "metadata source")
    if normalized not in METADATA_SOURCES:
        raise ValueError(f"Invalid metadata source: {source!r}")
    return normalized


def validate_ticket_event_type(event_type: str) -> str:
    """Return a normalized Ticket event type or raise ValueError."""
    normalized = require_non_empty(event_type, "event_type")
    if normalized not in TICKET_EVENT_TYPES:
        raise ValueError(f"Invalid ticket event type: {event_type!r}")
    return normalized


def initial_ticket_status(blocked_by: str | None) -> str:
    """Return the SPEC §12.1 initial status for a newly created Ticket.

    `ready`, or `blocked` when the Ticket already carries a `blocked_by` at
    creation. Never `queued`.
    """
    if blocked_by is not None and blocked_by.strip():
        return BLOCKED_TICKET_STATUS
    return READY_TICKET_STATUS


@dataclass(frozen=True)
class TicketRecord:
    """One Ticket and its Python-derived metadata (SPEC §10.1, §12)."""

    ticket_id: str
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
    ticket_prefix: str
    ticket_sequence: int
    title_source: str = METADATA_SOURCE_FALLBACK
    branch_slug_source: str = METADATA_SOURCE_FALLBACK
    github_repo: str | None = None
    blocked_by: str | None = None
    commit_message_suggestion: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticket_id", normalize_task_key(self.ticket_id))
        object.__setattr__(
            self,
            "repository",
            require_non_empty(self.repository, "repository"),
        )
        object.__setattr__(self, "prompt", require_non_empty(self.prompt, "prompt"))
        object.__setattr__(self, "title", require_non_empty(self.title, "title"))
        object.__setattr__(self, "priority", validate_ticket_priority(self.priority))
        object.__setattr__(self, "status", validate_ticket_status(self.status))
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
            "ticket_prefix",
            require_non_empty(self.ticket_prefix, "ticket_prefix"),
        )
        if self.ticket_sequence < 1:
            raise ValueError("ticket_sequence must be >= 1")
        object.__setattr__(
            self,
            "title_source",
            validate_metadata_source(self.title_source),
        )
        object.__setattr__(
            self,
            "branch_slug_source",
            validate_metadata_source(self.branch_slug_source),
        )
        if self.blocked_by is not None:
            blocked_by = normalize_task_key(self.blocked_by)
            if blocked_by == self.ticket_id:
                raise ValueError("A ticket cannot block itself")
            object.__setattr__(self, "blocked_by", blocked_by)


@dataclass(frozen=True)
class TicketEventRecord:
    """One append-only audit record for a Ticket (SPEC §44 auditability)."""

    ticket_id: str
    event_type: str
    actor: str
    message: str | None = None
    payload_json: str | None = None
    created_at: str | None = None
    event_id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticket_id", normalize_task_key(self.ticket_id))
        object.__setattr__(
            self,
            "event_type",
            validate_ticket_event_type(self.event_type),
        )
        object.__setattr__(self, "actor", require_non_empty(self.actor, "actor"))


__all__ = [
    "BLOCKED_TICKET_STATUS",
    "DEFAULT_TICKET_PRIORITY",
    "METADATA_SOURCES",
    "METADATA_SOURCE_AI",
    "METADATA_SOURCE_FALLBACK",
    "READY_TICKET_STATUS",
    "RESERVED_INITIAL_TICKET_STATUS",
    "TICKET_EVENT_TYPES",
    "TICKET_PRIORITIES",
    "TICKET_PRIORITY_SEQUENCE",
    "TICKET_STATUSES",
    "TICKET_STATUS_SEQUENCE",
    "TicketEventRecord",
    "TicketRecord",
    "initial_ticket_status",
    "validate_metadata_source",
    "validate_ticket_event_type",
    "validate_ticket_priority",
    "validate_ticket_status",
]
