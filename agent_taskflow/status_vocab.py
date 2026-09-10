"""Bridge between the §12 display vocabulary and persisted TASK_STATUSES.

SPEC §12.2 ruling: there is no repo-wide status migration. The persisted
canonical vocabulary stays :data:`agent_taskflow.models.TASK_STATUSES`. The
§12 names are the Mission Control *display* vocabulary. This module is the
single place the two are bridged.

Two directions, deliberately asymmetric:

* :data:`DISPLAY_TO_PERSISTED` is injective. Each of the 14 §12 display names
  has exactly one canonical persisted spelling, so every display name
  round-trips exactly.
* :data:`PERSISTED_TO_DISPLAY` is total over `TASK_STATUSES` but not
  injective. The legacy vocabulary is larger and carries several spellings of
  the same idea (`waiting_approval` / `waiting_for_review`, `cleaned` /
  `completed` / `done`). Those extra spellings are recorded in
  :data:`PERSISTED_ALIASES` and round-trip to their canonical sibling rather
  than to themselves.

No legacy value is left unmapped, and no legacy value is renamed, removed or
repurposed. See HANDOFF.md for the reasoning behind each judgement call.
"""

from __future__ import annotations

from agent_taskflow._helpers import require_non_empty
from agent_taskflow.models import TASK_STATUSES
from agent_taskflow.ticket_models import TICKET_STATUS_SEQUENCE


# The §12 display vocabulary. Single source of truth is the Ticket status
# enum, which already spells §12 exactly.
DISPLAY_STATUS_SEQUENCE: tuple[str, ...] = TICKET_STATUS_SEQUENCE
DISPLAY_STATUSES = frozenset(DISPLAY_STATUS_SEQUENCE)


# §12 display name -> canonical persisted spelling.
#
# The five entries marked (§12.2) are fixed by the human ruling. The rest are
# identity: the legacy vocabulary already spells them the same way, or the
# name was added to TASK_STATUSES additively because no legacy spelling
# existed.
DISPLAY_TO_PERSISTED: dict[str, str] = {
    "queued": "queued",
    "ready": "created",  # §12.2
    "blocked": "blocked",
    "paused": "paused",
    "preparing": "preparing",
    "running": "implementing",  # §12.2
    "validating": "validating",
    "ready_for_integration": "ready_for_integration",
    "integrating": "integrating",
    "needs_review": "waiting_for_review",  # §12.2
    "needs_decision": "needs_decision",
    "completed": "cleaned",  # §12.2
    "failed": "failed",
    "cancelled": "canceled",  # §12.2
}


# Persisted value -> §12 display name. Total over TASK_STATUSES.
#
# Judgement calls, based on what the code actually does with each value:
#
# `waiting_approval` — written by the dispatcher once the executor and the
#   validators have passed, and required by pr_handoff, branch_push_confirm,
#   draft_pr_confirm and task_closeout_confirm before they will act. It is the
#   repo's human review gate, so it displays as `needs_review` alongside the
#   canonical `waiting_for_review`.
#
# `accepted` — written by the approve route once an operator has attested to
#   the proof-of-work. It does not push, merge or clean up (WORKFLOW.md), and
#   §33.1 keeps an approved-but-unmerged Ticket at `needs_review`, so that is
#   what it displays as.
#
# `rejected` — the human said no and must now choose retry / cancel / rework.
#   §33.2 routes exactly that situation to `needs_decision`.
#
# `unknown` — a mirror value meaning the local state is not trustworthy. It
#   displays as `needs_decision` so it routes to a human rather than implying
#   progress it cannot justify.
#
# `completed` / `done` — terminal success spellings alongside the canonical
#   `cleaned`. `archived` is a terminal abandon spelling alongside `canceled`.
PERSISTED_TO_DISPLAY: dict[str, str] = {
    # Canonical: these round-trip to themselves.
    "queued": "queued",
    "created": "ready",
    "blocked": "blocked",
    "paused": "paused",
    "preparing": "preparing",
    "implementing": "running",
    "validating": "validating",
    "ready_for_integration": "ready_for_integration",
    "integrating": "integrating",
    "waiting_for_review": "needs_review",
    "needs_decision": "needs_decision",
    "cleaned": "completed",
    "failed": "failed",
    "canceled": "cancelled",
    # Aliases: legacy spellings that share a display name with a canonical
    # value. They round-trip to that canonical sibling, not to themselves.
    "unknown": "needs_decision",
    "waiting_approval": "needs_review",
    "accepted": "needs_review",
    "rejected": "needs_decision",
    "completed": "completed",
    "archived": "cancelled",
    # External Kanban/Hermes mirror spellings.
    "backlog": "queued",
    "todo": "ready",
    "in_progress": "running",
    "review": "needs_review",
    "done": "completed",
}


CANONICAL_PERSISTED_STATUSES = frozenset(DISPLAY_TO_PERSISTED.values())

# Alias persisted value -> the canonical persisted value it collapses onto.
PERSISTED_ALIASES: dict[str, str] = {
    persisted: DISPLAY_TO_PERSISTED[display]
    for persisted, display in PERSISTED_TO_DISPLAY.items()
    if persisted not in CANONICAL_PERSISTED_STATUSES
}


class StatusVocabularyError(ValueError):
    """Raised when a status is outside both vocabularies."""


def to_persisted_status(display_status: str) -> str:
    """Return the canonical persisted spelling of a §12 display name."""
    normalized = require_non_empty(display_status, "display_status")
    try:
        return DISPLAY_TO_PERSISTED[normalized]
    except KeyError as exc:
        allowed = ", ".join(DISPLAY_STATUS_SEQUENCE)
        raise StatusVocabularyError(
            f"Unknown display status: {display_status!r}. Allowed: {allowed}"
        ) from exc


def to_display_status(persisted_status: str) -> str:
    """Return the §12 display name for a persisted TASK_STATUSES value."""
    normalized = require_non_empty(persisted_status, "persisted_status")
    try:
        return PERSISTED_TO_DISPLAY[normalized]
    except KeyError as exc:
        raise StatusVocabularyError(
            f"Unknown persisted status: {persisted_status!r}"
        ) from exc


def canonical_persisted_status(persisted_status: str) -> str:
    """Collapse an alias persisted value onto its canonical sibling.

    Canonical values are returned unchanged, so this is idempotent.
    """
    return to_persisted_status(to_display_status(persisted_status))


def is_alias_status(persisted_status: str) -> bool:
    """Return True when a persisted value is a non-canonical spelling."""
    return persisted_status in PERSISTED_ALIASES


def unmapped_persisted_statuses() -> frozenset[str]:
    """Return TASK_STATUSES values this module does not map. Always empty.

    Kept as an explicit, callable guard so a future addition to
    `TASK_STATUSES` that forgets this module is caught by the test suite
    rather than surfacing as a KeyError in Mission Control.
    """
    return frozenset(TASK_STATUSES) - frozenset(PERSISTED_TO_DISPLAY)


__all__ = [
    "CANONICAL_PERSISTED_STATUSES",
    "DISPLAY_STATUSES",
    "DISPLAY_STATUS_SEQUENCE",
    "DISPLAY_TO_PERSISTED",
    "PERSISTED_ALIASES",
    "PERSISTED_TO_DISPLAY",
    "StatusVocabularyError",
    "canonical_persisted_status",
    "is_alias_status",
    "to_display_status",
    "to_persisted_status",
    "unmapped_persisted_statuses",
]
