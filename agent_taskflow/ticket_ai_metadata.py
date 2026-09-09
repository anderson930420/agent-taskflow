"""Bounded AI metadata adapter for Ticket creation (SPEC §10.1).

AI may suggest a human-facing title, a branch slug, and a commit message. It
owns none of them: *AI metadata failure must never block Ticket creation*.
Every failure mode — raising, hanging, returning nothing, returning blank
text — resolves to the deterministic fallback instead.

The adapter is injected. Step 1 wires no concrete AI backend, so the default
resolution path is fully deterministic and makes no subprocess calls.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from agent_taskflow.ticket_metadata import (
    TITLE_FALLBACK_MAX_CHARS,
    fallback_title_from_prompt,
    normalize_prompt,
    slugify_branch_component,
)
from agent_taskflow.ticket_models import (
    METADATA_SOURCE_AI,
    METADATA_SOURCE_FALLBACK,
)


# A hung adapter must not hold up Ticket creation, so the wrapper enforces a
# wall-clock deadline of its own in addition to catching exceptions.
DEFAULT_AI_METADATA_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class TicketAIMetadataRequest:
    """What the adapter is allowed to see. Read-only, prompt-scoped."""

    prompt: str
    repository: str
    priority: str


@dataclass(frozen=True)
class TicketAIMetadataSuggestion:
    """Optional AI suggestions. Any field may be missing or blank."""

    title: str | None = None
    branch_slug: str | None = None
    commit_message: str | None = None


class TicketAIMetadataAdapter(Protocol):
    """Callable that suggests human-facing Ticket metadata."""

    def __call__(
        self,
        request: TicketAIMetadataRequest,
    ) -> TicketAIMetadataSuggestion | None:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True)
class ResolvedTicketMetadata:
    """Resolved human-facing metadata plus provenance for the audit record."""

    title: str
    title_source: str
    branch_slug: str
    branch_slug_source: str
    commit_message_suggestion: str | None
    ai_attempted: bool
    ai_error: str | None

    def to_audit_payload(self) -> dict[str, Any]:
        return {
            "title_source": self.title_source,
            "branch_slug_source": self.branch_slug_source,
            "ai_attempted": self.ai_attempted,
            "ai_error": self.ai_error,
            "commit_message_suggested": self.commit_message_suggestion is not None,
        }


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = normalize_prompt(str(value))
    return normalized or None


def _call_with_deadline(
    call: Callable[[], TicketAIMetadataSuggestion | None],
    timeout_seconds: float | None,
) -> tuple[TicketAIMetadataSuggestion | None, str | None]:
    """Run `call`, returning (suggestion, error_message).

    A raised exception becomes an error message. A call that outruns the
    deadline is abandoned on a daemon thread so it cannot block creation or
    interpreter shutdown.
    """
    if timeout_seconds is None:
        try:
            return call(), None
        except Exception as exc:  # AI metadata never blocks creation.
            return None, f"{type(exc).__name__}: {exc}"

    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = call()
        except Exception as exc:  # AI metadata never blocks creation.
            box["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(
        target=runner,
        name="ticket-ai-metadata",
        daemon=True,
    )
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        return None, f"TimeoutError: AI metadata timed out after {timeout_seconds}s"
    if "error" in box:
        return None, str(box["error"])
    return box.get("value"), None


def resolve_ticket_metadata(
    *,
    prompt: str,
    repository: str,
    priority: str,
    adapter: TicketAIMetadataAdapter | None = None,
    timeout_seconds: float | None = DEFAULT_AI_METADATA_TIMEOUT_SECONDS,
) -> ResolvedTicketMetadata:
    """Return human-facing Ticket metadata, falling back deterministically.

    Never raises because of the adapter. The only ValueError this can raise
    comes from the prompt itself being unusable, which is a request-validation
    failure, not an AI failure.
    """
    fallback_title = fallback_title_from_prompt(prompt)
    fallback_slug = slugify_branch_component(fallback_title)

    if adapter is None:
        return ResolvedTicketMetadata(
            title=fallback_title,
            title_source=METADATA_SOURCE_FALLBACK,
            branch_slug=fallback_slug,
            branch_slug_source=METADATA_SOURCE_FALLBACK,
            commit_message_suggestion=None,
            ai_attempted=False,
            ai_error=None,
        )

    request = TicketAIMetadataRequest(
        prompt=prompt,
        repository=repository,
        priority=priority,
    )
    suggestion, error = _call_with_deadline(
        lambda: adapter(request),
        timeout_seconds,
    )

    suggested_title: str | None = None
    suggested_slug: str | None = None
    commit_message: str | None = None
    if suggestion is not None:
        try:
            suggested_title = _blank_to_none(suggestion.title)
            raw_slug = _blank_to_none(suggestion.branch_slug)
            suggested_slug = (
                slugify_branch_component(raw_slug) if raw_slug else None
            )
            commit_message = _blank_to_none(suggestion.commit_message)
        except Exception as exc:  # Malformed suggestion is still not fatal.
            error = error or f"{type(exc).__name__}: {exc}"
            suggested_title = None
            suggested_slug = None
            commit_message = None

    # An AI title shares the same display budget as the §10.1 fallback.
    title = (
        suggested_title[:TITLE_FALLBACK_MAX_CHARS].strip()
        if suggested_title
        else None
    )
    return ResolvedTicketMetadata(
        title=title or fallback_title,
        title_source=(
            METADATA_SOURCE_AI if title else METADATA_SOURCE_FALLBACK
        ),
        branch_slug=suggested_slug or fallback_slug,
        branch_slug_source=(
            METADATA_SOURCE_AI if suggested_slug else METADATA_SOURCE_FALLBACK
        ),
        commit_message_suggestion=commit_message,
        ai_attempted=True,
        ai_error=error,
    )


__all__ = [
    "DEFAULT_AI_METADATA_TIMEOUT_SECONDS",
    "ResolvedTicketMetadata",
    "TicketAIMetadataAdapter",
    "TicketAIMetadataRequest",
    "TicketAIMetadataSuggestion",
    "resolve_ticket_metadata",
]
