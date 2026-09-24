"""Attempt failure class for Level 2 Roadmap M2's second Exit Gate row.

The runner must tell execution failure, validation failure and tool error
apart in durable state, not only in memory. The dispatcher already knows which
of the Ticket failure kinds (:mod:`agent_taskflow.ticket_lifecycle`) stopped a
run; this module maps that kind to one failure class and names the key it is
persisted under.

The class is written into the ``metadata_json`` of the Attempt's terminal
``lifecycle_events`` row, in the same transaction that terminalizes the
Attempt. That table is append-only, so the class can never be rewritten, and no
schema migration is needed. Statuses are not touched: this is an extra fact
beside the SPEC §29 status, never a replacement for it.

Mapping (fail closed: anything not listed is recorded as ``unknown``):

======================  ======================
failure kind            failure class
======================  ======================
``executor``            ``execution_failure``
``validator_red``       ``validation_failure``
``validator_error``     ``tool_error``
anything else           ``unknown``
======================  ======================

``governance_refusal`` and ``worktree_preparation`` stop a run before any claim,
so in practice no Attempt exists to carry them. ``lease_expired`` is written by
the reaper, not by the dispatcher. None of the three is one of the Exit Gate's
classes, so each stays ``unknown`` rather than being guessed. A failed Attempt
closed by a caller that supplies no kind at all (the legacy approved runner, for
example) is also recorded as ``unknown``, with its own reason.

Nothing in this module is a lifecycle, merge or approval authority.
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
from typing import Any

from agent_taskflow.store import connect
from agent_taskflow.ticket_lifecycle import (
    FAILURE_EXECUTOR,
    FAILURE_VALIDATOR_ERROR,
    FAILURE_VALIDATOR_RED,
)

FAILURE_CLASS_EXECUTION = "execution_failure"
FAILURE_CLASS_VALIDATION = "validation_failure"
FAILURE_CLASS_TOOL_ERROR = "tool_error"
FAILURE_CLASS_UNKNOWN = "unknown"

#: The three classes the M2 Exit Gate requires the runner to distinguish.
FAILURE_CLASSES = frozenset(
    {FAILURE_CLASS_EXECUTION, FAILURE_CLASS_VALIDATION, FAILURE_CLASS_TOOL_ERROR}
)
#: Every value that may be persisted, including the explicit fail-closed one.
RECORDED_FAILURE_CLASSES = FAILURE_CLASSES | {FAILURE_CLASS_UNKNOWN}

FAILURE_CLASS_BY_KIND = {
    FAILURE_EXECUTOR: FAILURE_CLASS_EXECUTION,
    FAILURE_VALIDATOR_RED: FAILURE_CLASS_VALIDATION,
    FAILURE_VALIDATOR_ERROR: FAILURE_CLASS_TOOL_ERROR,
}

#: Keys in the terminal lifecycle event's ``metadata_json``.
FAILURE_CLASS_KEY = "failure_class"
FAILURE_KIND_KEY = "failure_kind"
FAILURE_CLASS_REASON_KEY = "failure_class_reason"

#: Task statuses whose release terminalizes a failed Attempt. `canceled` is an
#: operator decision, not a failure, and the success terminals carry no class.
FAILURE_TASK_STATUSES = frozenset({"blocked", "failed", "needs_decision"})

REASON_KIND_NOT_SUPPLIED = "failure_kind_not_supplied"
REASON_KIND_UNMAPPED = "failure_kind_unmapped"


def failure_class_for_kind(kind: str | None) -> str:
    """Return the failure class for ``kind``; ``unknown`` when unmapped."""
    return FAILURE_CLASS_BY_KIND.get(kind or "", FAILURE_CLASS_UNKNOWN)


def failure_class_metadata(task_status: str, kind: str | None) -> dict[str, Any]:
    """Return the terminal-event metadata recording one failed Attempt's class.

    Empty for a task status that does not end a failed Attempt, so success and
    cancel releases are unchanged.
    """
    if task_status not in FAILURE_TASK_STATUSES:
        return {}
    failure_class = failure_class_for_kind(kind)
    metadata: dict[str, Any] = {
        FAILURE_CLASS_KEY: failure_class,
        FAILURE_KIND_KEY: kind,
    }
    if failure_class == FAILURE_CLASS_UNKNOWN:
        metadata[FAILURE_CLASS_REASON_KEY] = (
            REASON_KIND_NOT_SUPPLIED if kind is None else REASON_KIND_UNMAPPED
        )
    return metadata


def read_attempt_failure_class(
    db_path: str | Path, attempt_id: str
) -> dict[str, Any] | None:
    """Return the failure class recorded for exactly ``attempt_id``, read-only.

    Only a terminal release writes the class, so the newest of this Attempt's
    lifecycle events that carries one is its terminal event; a later event bound
    to the same ended Attempt cannot hide it. Returns ``None`` when no event of
    that Attempt records a class: a successful or canceled Attempt, an Attempt
    that is still active, or one that terminalized without a recorded class.
    """
    with closing(connect(db_path)) as conn:
        row = conn.execute(
            """
            SELECT event_id, to_status, reason_code, metadata_json
            FROM lifecycle_events
            WHERE attempt_id = ?
              AND json_extract(metadata_json, '$.failure_class') IS NOT NULL
            ORDER BY event_id DESC
            LIMIT 1
            """,
            (attempt_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except ValueError:
        return None
    if not isinstance(metadata, dict) or FAILURE_CLASS_KEY not in metadata:
        return None
    return {
        FAILURE_CLASS_KEY: metadata.get(FAILURE_CLASS_KEY),
        FAILURE_KIND_KEY: metadata.get(FAILURE_KIND_KEY),
        FAILURE_CLASS_REASON_KEY: metadata.get(FAILURE_CLASS_REASON_KEY),
        "event_id": row["event_id"],
        "to_status": row["to_status"],
        "reason_code": row["reason_code"],
    }


__all__ = [
    "FAILURE_CLASSES",
    "FAILURE_CLASS_BY_KIND",
    "FAILURE_CLASS_EXECUTION",
    "FAILURE_CLASS_KEY",
    "FAILURE_CLASS_REASON_KEY",
    "FAILURE_CLASS_TOOL_ERROR",
    "FAILURE_CLASS_UNKNOWN",
    "FAILURE_CLASS_VALIDATION",
    "FAILURE_KIND_KEY",
    "FAILURE_TASK_STATUSES",
    "REASON_KIND_NOT_SUPPLIED",
    "REASON_KIND_UNMAPPED",
    "RECORDED_FAILURE_CLASSES",
    "failure_class_for_kind",
    "failure_class_metadata",
    "read_attempt_failure_class",
]
