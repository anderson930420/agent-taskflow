"""Ticket creation service (SPEC §10, §10.1, §12.1, §12.2).

The user supplies three things: repository, prompt, priority. Everything else
is derived deterministically by Python here. The Ticket is a row in the
canonical `tasks` table; there is no separate Ticket table.

This service creates *state only*. It runs no Git command, creates no
worktree, no branch and no directory: worktree paths and branch names are
derived strings that Step 2 will later act on.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_taskflow._helpers import require_non_empty
from agent_taskflow.status_vocab import to_display_status, to_persisted_status
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.ticket_ai_metadata import (
    DEFAULT_AI_METADATA_TIMEOUT_SECONDS,
    ResolvedTicketMetadata,
    TicketAIMetadataAdapter,
    resolve_ticket_metadata,
)
from agent_taskflow.ticket_metadata import derive_ticket_metadata, normalize_prompt
from agent_taskflow.ticket_models import (
    AI_TITLE_GENERATED,
    DEFAULT_TICKET_PRIORITY,
    TicketRecord,
    initial_ticket_status,
    validate_ticket_priority,
)
from agent_taskflow.ticket_repositories import (
    DEFAULT_PROJECTS_CONFIG_PATH,
    TicketRepository,
    TicketRepositoryError,
    resolve_ticket_repository,
)
from agent_taskflow.ticket_store import TicketStore, TicketStoreError


# `kind` recorded in the `created` task_events payload for this path.
TICKET_CREATED_EVENT = "ticket_created"
DEFAULT_TICKET_ACTOR = "mission_control"

# Recorded on the creation event so a reviewer can see, from the audit record
# alone, that Ticket creation is state-only.
CREATION_SAFETY_FLAGS: dict[str, bool] = {
    "git_mutation": False,
    "worktree_created": False,
    "branch_created": False,
    "directory_created": False,
    "executor_started": False,
}


class TicketCreationError(ValueError):
    """Raised when a Ticket creation request is invalid or cannot persist."""


@dataclass(frozen=True)
class TicketCreationRequest:
    """SPEC §10: repository, prompt, priority. Nothing else is user-supplied."""

    repository: str
    prompt: str
    priority: str = DEFAULT_TICKET_PRIORITY
    blocked_by: str | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(
                self,
                "repository",
                require_non_empty(self.repository, "repository"),
            )
            object.__setattr__(
                self,
                "priority",
                validate_ticket_priority(self.priority or DEFAULT_TICKET_PRIORITY),
            )
        except ValueError as exc:
            raise TicketCreationError(str(exc)) from exc

        normalized_prompt = normalize_prompt(self.prompt)
        if not normalized_prompt:
            raise TicketCreationError(
                "prompt must contain at least one displayable character"
            )
        object.__setattr__(self, "prompt", normalized_prompt)

        if self.blocked_by is not None:
            blocked_by = self.blocked_by.strip()
            if not blocked_by:
                object.__setattr__(self, "blocked_by", None)
            else:
                try:
                    object.__setattr__(
                        self,
                        "blocked_by",
                        normalize_task_key(blocked_by),
                    )
                except ValueError as exc:
                    raise TicketCreationError(str(exc)) from exc


@dataclass(frozen=True)
class TicketCreationResult:
    """The created Ticket plus how its human-facing metadata was resolved."""

    ticket: TicketRecord
    repository: TicketRepository
    metadata: ResolvedTicketMetadata

    @property
    def used_ai_title(self) -> bool:
        return self.metadata.ai_title_status == AI_TITLE_GENERATED


def create_ticket(
    request: TicketCreationRequest,
    *,
    store: TicketStore,
    repository: TicketRepository | None = None,
    projects_config_path: str | Path = DEFAULT_PROJECTS_CONFIG_PATH,
    ai_adapter: TicketAIMetadataAdapter | None = None,
    ai_timeout_seconds: float | None = DEFAULT_AI_METADATA_TIMEOUT_SECONDS,
    actor: str = DEFAULT_TICKET_ACTOR,
) -> TicketCreationResult:
    """Create one Ticket from repository / prompt / priority."""
    if repository is None:
        try:
            repository = resolve_ticket_repository(
                request.repository,
                projects_config_path,
            )
        except TicketRepositoryError as exc:
            raise TicketCreationError(str(exc)) from exc

    # SPEC §10.1: AI metadata is best-effort. Any failure resolves to the
    # deterministic fallback rather than failing the creation.
    metadata = resolve_ticket_metadata(
        prompt=request.prompt,
        repository=repository.repository,
        priority=request.priority,
        adapter=ai_adapter,
        timeout_seconds=ai_timeout_seconds,
    )

    # SPEC §12.1 picks the display status; §12.2 persists it in the legacy
    # TASK_STATUSES vocabulary (`ready` -> `created`, `blocked` -> `blocked`).
    display_status = initial_ticket_status(request.blocked_by)
    persisted_status = to_persisted_status(display_status)
    resolved_repository = repository

    def build(task_key: str) -> TicketRecord:
        derived = derive_ticket_metadata(
            resolved_repository,
            task_key,
            metadata.branch_slug,
        )
        return TicketRecord(
            task_key=task_key,
            repository=resolved_repository.repository,
            prompt=request.prompt,
            title=metadata.title,
            priority=request.priority,
            status=persisted_status,
            repo_path=derived.repo_path,
            base_branch=derived.base_branch,
            branch=derived.branch,
            worktree_path=derived.worktree_path,
            artifact_dir=derived.artifact_dir,
            ai_title_status=metadata.ai_title_status,
            branch_slug_source=metadata.branch_slug_source,
            github_repo=derived.github_repo,
            blocked_by=request.blocked_by,
            commit_message_suggestion=metadata.commit_message_suggestion,
        )

    store.init_db()
    try:
        ticket = store.create_ticket(
            build=build,
            actor=actor,
            message="Ticket created from repository, prompt and priority",
            payload=_creation_payload(
                request,
                repository,
                metadata,
                persisted_status,
                display_status,
            ),
            blocked_by=request.blocked_by,
        )
    except (TicketStoreError, ValueError) as exc:
        raise TicketCreationError(str(exc)) from exc

    return TicketCreationResult(
        ticket=ticket,
        repository=repository,
        metadata=metadata,
    )


def _creation_payload(
    request: TicketCreationRequest,
    repository: TicketRepository,
    metadata: ResolvedTicketMetadata,
    persisted_status: str,
    display_status: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": TICKET_CREATED_EVENT,
        "repository": repository.repository,
        "priority": request.priority,
        "initial_status": persisted_status,
        "initial_display_status": display_status,
        "blocked_by": request.blocked_by,
        "prompt_chars": len(request.prompt),
        "safety_flags": dict(CREATION_SAFETY_FLAGS),
    }
    payload.update(metadata.to_audit_payload())
    return payload


def ticket_to_dict(ticket: TicketRecord) -> dict[str, Any]:
    """Return a JSON-safe view of a Ticket for the API and Mission Control.

    `status` is the persisted value; `display_status` is its §12 name.
    """
    return {
        "task_key": ticket.task_key,
        "repository": ticket.repository,
        "prompt": ticket.prompt,
        "title": ticket.title,
        "ai_title_status": ticket.ai_title_status,
        "priority": ticket.priority,
        "status": ticket.status,
        "display_status": to_display_status(ticket.status),
        "blocked_by": ticket.blocked_by,
        "repo_path": str(ticket.repo_path),
        "github_repo": ticket.github_repo,
        "base_branch": ticket.base_branch,
        "branch": ticket.branch,
        "branch_slug_source": ticket.branch_slug_source,
        "worktree_path": str(ticket.worktree_path),
        "artifact_dir": str(ticket.artifact_dir),
        "commit_message_suggestion": ticket.commit_message_suggestion,
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
    }


__all__ = [
    "CREATION_SAFETY_FLAGS",
    "DEFAULT_TICKET_ACTOR",
    "TICKET_CREATED_EVENT",
    "TicketCreationError",
    "TicketCreationRequest",
    "TicketCreationResult",
    "create_ticket",
    "ticket_to_dict",
]
