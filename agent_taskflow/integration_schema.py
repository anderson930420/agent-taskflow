"""Authoritative Step 2 integration schema (V1 Master Spec §32.1, §12).

§32.1 is the authoritative field list for the Ticket PR / integration state.
Names, types, enum values and defaults are fixed there, so they are declared
once here and every other Step 2 module derives from these declarations.

Ownership rule: Step 2 creates these fields and is their only writer. Every
other component reads them and must tolerate ``None``.

Integration-internal state that §32.1 does not list (``previous_integrated_
base_sha``, ``new_target_sha``, validator/review/conflict evidence) is *not*
part of this field list and lives in Step-2-private storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from agent_taskflow.status_vocab import to_persisted_status


__all__ = [
    "CANCELLED",
    "CI_STATUS_VALUES",
    "COMPLETED",
    "INTEGRATING",
    "INTEGRATION_OWNED_STATUSES",
    "NEEDS_DECISION",
    "NEEDS_REVIEW",
    "PAUSED",
    "PR_STATE_VALUES",
    "PrFieldSpec",
    "READY_FOR_INTEGRATION",
    "REVIEW_DECISION_VALUES",
    "TICKET_PR_FIELDS",
    "TICKET_PR_FIELD_NAMES",
    "can_transition",
    "default_pr_state",
    "sqlite_column_type",
    "validate_pr_state",
    "validate_transition",
]


# -- §12 statuses owned by the integration controller ----------------------
#
# SPEC §12.2 ruling: §12 names are the Mission Control *display* vocabulary and
# the persisted canonical vocabulary stays `TASK_STATUSES`. These constants are
# named for the §12 display concept but hold the **persisted** spelling, which
# is resolved through `status_vocab` rather than duplicated here.
#
# Four of the six are not identity:
#     needs_review -> waiting_for_review
#     cancelled    -> canceled
#     completed    -> cleaned
# so nothing in Step 2 may compare a task status against a §12 name directly.
READY_FOR_INTEGRATION = to_persisted_status("ready_for_integration")
INTEGRATING = to_persisted_status("integrating")
NEEDS_REVIEW = to_persisted_status("needs_review")
NEEDS_DECISION = to_persisted_status("needs_decision")
CANCELLED = to_persisted_status("cancelled")
COMPLETED = to_persisted_status("completed")
# Not integration-owned, but a source of CANCELLED / COMPLETED: see below.
PAUSED = to_persisted_status("paused")

INTEGRATION_OWNED_STATUSES = frozenset(
    {
        READY_FOR_INTEGRATION,
        INTEGRATING,
        NEEDS_REVIEW,
        NEEDS_DECISION,
        CANCELLED,
        COMPLETED,
    }
)

# Transitions the integration controller, its watcher, and cleanup may perform.
# This table is the runtime source of truth: `can_transition` reads it, and the
# watcher and cleanup consult it rather than keeping their own status lists.
#
# READY_FOR_INTEGRATION, NEEDS_DECISION and PAUSED can all reach CANCELLED and
# COMPLETED because of the §32 pickup ruling: the watcher picks up every Ticket
# with an open PR whatever its status, and a human can merge or close that PR
# on GitHub while the Ticket waits in any of them. The outcome must still land.
#
# Deliberately absent:
#   INTEGRATING    -> COMPLETED / CANCELLED   an in-flight integration is never
#                                             interrupted (§25.0)
#   NEEDS_DECISION -> NEEDS_REVIEW            only a human disposition leaves
#                                             needs_decision (§33.3, §33.4)
#   PAUSED         -> NEEDS_DECISION          a pause is the user's call (§13)
INTEGRATION_TRANSITIONS: dict[str, frozenset[str]] = {
    READY_FOR_INTEGRATION: frozenset({INTEGRATING, CANCELLED, COMPLETED}),
    INTEGRATING: frozenset({READY_FOR_INTEGRATION, NEEDS_REVIEW, NEEDS_DECISION}),
    NEEDS_REVIEW: frozenset(
        {READY_FOR_INTEGRATION, NEEDS_DECISION, CANCELLED, COMPLETED}
    ),
    NEEDS_DECISION: frozenset({CANCELLED, COMPLETED}),
    PAUSED: frozenset({CANCELLED, COMPLETED}),
    CANCELLED: frozenset(),
    COMPLETED: frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    """Return True when Step 2 may move a Ticket from ``current`` to ``target``."""
    return target in INTEGRATION_TRANSITIONS.get(current, frozenset())


def validate_transition(current: str, target: str) -> None:
    """Raise ValueError unless integration may move ``current`` to ``target``."""
    allowed = INTEGRATION_TRANSITIONS.get(current)
    if allowed is None:
        raise ValueError(
            f"{current!r} is not a status the integration controller owns"
        )
    if target not in allowed:
        raise ValueError(
            f"Integration may not transition {current!r} -> {target!r}"
        )


# -- §32.1 Ticket PR fields ------------------------------------------------
PR_STATE_VALUES = ("open", "closed")
REVIEW_DECISION_VALUES = ("none", "approved", "changes_requested")
CI_STATUS_VALUES = ("none", "pending", "success", "failure")


@dataclass(frozen=True)
class PrFieldSpec:
    """One §32.1 field: its name, type, nullability, default and enum."""

    name: str
    python_type: str
    nullable: bool
    default: Any = None
    enum: tuple[str, ...] | None = None


TICKET_PR_FIELDS: tuple[PrFieldSpec, ...] = (
    PrFieldSpec("pr_number", "int", True),
    PrFieldSpec("pr_url", "str", True),
    PrFieldSpec("pr_state", "str", True, enum=PR_STATE_VALUES),
    PrFieldSpec("pr_merged", "bool", False, default=False),
    PrFieldSpec("pr_head_sha", "str", True),
    PrFieldSpec("merge_commit_sha", "str", True),
    PrFieldSpec("review_decision", "str", True, enum=REVIEW_DECISION_VALUES),
    PrFieldSpec("ci_status", "str", True, enum=CI_STATUS_VALUES),
    PrFieldSpec("integrated_base_sha", "str", True),
    PrFieldSpec("reintegration_count", "int", False, default=0),
    PrFieldSpec("reintegration_required", "bool", False, default=False),
    PrFieldSpec("pr_last_polled_at", "datetime", True),
)

TICKET_PR_FIELD_NAMES: tuple[str, ...] = tuple(spec.name for spec in TICKET_PR_FIELDS)

_FIELDS_BY_NAME: dict[str, PrFieldSpec] = {spec.name: spec for spec in TICKET_PR_FIELDS}

_SQLITE_TYPES = {"int": "INTEGER", "bool": "INTEGER", "str": "TEXT", "datetime": "TEXT"}


def sqlite_column_type(spec: PrFieldSpec) -> str:
    """Return the SQLite column type for one §32.1 field."""
    return _SQLITE_TYPES[spec.python_type]


def default_pr_state() -> dict[str, Any]:
    """Return the §32.1 defaults, which every reader must tolerate."""
    return {spec.name: spec.default for spec in TICKET_PR_FIELDS}


def _coerce(spec: PrFieldSpec, value: Any) -> Any:
    if value is None:
        if not spec.nullable:
            raise ValueError(f"{spec.name} is not nullable")
        return None

    if spec.python_type == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{spec.name} must be a bool, got {value!r}")
        return value
    if spec.python_type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{spec.name} must be an int, got {value!r}")
        return value
    if not isinstance(value, str):
        raise ValueError(f"{spec.name} must be a string, got {value!r}")

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{spec.name} must not be empty")
    if spec.enum is not None and normalized not in spec.enum:
        raise ValueError(
            f"{spec.name} must be one of {spec.enum}, got {value!r}"
        )
    return normalized


def validate_pr_state(values: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a partial §32.1 update and return the normalized values.

    Unknown field names are rejected: the §32.1 set is closed, so a typo or a
    well-meaning extra field is a bug rather than an extension point.
    """
    unknown = sorted(set(values) - set(TICKET_PR_FIELD_NAMES))
    if unknown:
        raise ValueError(
            f"Unknown Ticket PR field(s): {', '.join(unknown)}. "
            f"§32.1 defines exactly: {', '.join(TICKET_PR_FIELD_NAMES)}"
        )
    return {name: _coerce(_FIELDS_BY_NAME[name], value) for name, value in values.items()}
