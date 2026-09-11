"""V1 Step 3 runtime progress vocabulary (SPEC §14).

This module is an *extension* of the existing observability shapes in
:mod:`agent_taskflow.execution_observability`. It does not introduce a second
progress engine: an observed step is still an
:class:`~agent_taskflow.execution_observability.ExecutionObservedStep`, and the
JSON-safe coercion is still ``to_observability_dict``.

What Step 3 adds on top of that shape:

* the §14.1 first-level step vocabulary — Prepare, Scout, Planner, Implementer,
  Reviewer, Validator, Integration;
* the §14.1 step status vocabulary — pending / running / passed / failed /
  blocked;
* an attempt-scoped snapshot (§14.0: ObservedStep records hang off an Attempt,
  not off the Ticket);
* the §14.2 guard that refuses any fake completion signal.

The module is pure: no I/O, no DB, no git, no GitHub, no lifecycle writes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agent_taskflow.execution_observability import (
    ExecutionObservedStep,
    to_observability_dict,
)


RUNTIME_PROGRESS_SCHEMA_VERSION = "runtime_progress.v1"

# §14.1 first-level steps, in the order the spec lists them. This ordering is
# also the stored ``step_order`` and the render order.
RUNTIME_STEPS: tuple[str, ...] = (
    "Prepare",
    "Scout",
    "Planner",
    "Implementer",
    "Reviewer",
    "Validator",
    "Integration",
)

# §14.2 renders shorter verbs than the §14.1 record names.
RUNTIME_STEP_LABELS: dict[str, str] = {
    "Prepare": "Prepare",
    "Scout": "Scout",
    "Planner": "Plan",
    "Implementer": "Implement",
    "Reviewer": "Review",
    "Validator": "Validate",
    "Integration": "Integrate",
}

RUNTIME_STEP_STATUS_PENDING = "pending"
RUNTIME_STEP_STATUS_RUNNING = "running"
RUNTIME_STEP_STATUS_PASSED = "passed"
RUNTIME_STEP_STATUS_FAILED = "failed"
RUNTIME_STEP_STATUS_BLOCKED = "blocked"

RUNTIME_STEP_STATUSES: tuple[str, ...] = (
    RUNTIME_STEP_STATUS_PENDING,
    RUNTIME_STEP_STATUS_RUNNING,
    RUNTIME_STEP_STATUS_PASSED,
    RUNTIME_STEP_STATUS_FAILED,
    RUNTIME_STEP_STATUS_BLOCKED,
)

# §14.2 renders lifecycle glyphs, never a number.
RUNTIME_STEP_GLYPHS: dict[str, str] = {
    RUNTIME_STEP_STATUS_PENDING: "○",
    RUNTIME_STEP_STATUS_RUNNING: "●",
    RUNTIME_STEP_STATUS_PASSED: "✓",
    RUNTIME_STEP_STATUS_FAILED: "✗",
    RUNTIME_STEP_STATUS_BLOCKED: "⊘",
}

_STEP_INDEX = {name.lower(): index for index, name in enumerate(RUNTIME_STEPS)}
_STEP_CANONICAL = {name.lower(): name for name in RUNTIME_STEPS}

# V1 Step 4 lost-write guard: within one Attempt a step only moves forward,
# pending -> running -> an outcome. The three outcomes share a rank, so a write
# never lowers the rank but may still replace one outcome with another; Step
# 3's store tests write every status in sequence on one step.
RUNTIME_STEP_STATUS_RANK: dict[str, int] = {
    RUNTIME_STEP_STATUS_PENDING: 0,
    RUNTIME_STEP_STATUS_RUNNING: 1,
    RUNTIME_STEP_STATUS_PASSED: 2,
    RUNTIME_STEP_STATUS_FAILED: 2,
    RUNTIME_STEP_STATUS_BLOCKED: 2,
}


class RuntimeProgressError(ValueError):
    """Raised for an invalid step, status, or a forbidden completion signal."""


class ObservedStepRegressionError(RuntimeProgressError):
    """Raised when a write would move a step backwards for its Attempt."""


# -- §14.2 "No Fake Percentage" guard --------------------------------------
#
# The guard is deliberately spelled out here, in one place, so every writer and
# every emitted payload can be checked against the same rule.

_KEY_SPLIT = re.compile(r"[^0-9a-zA-Z]+|(?<=[a-z0-9])(?=[A-Z])")

_ESTIMATE_KEY_TOKENS = frozenset({"percent", "percentage", "pct", "eta", "etas"})

_ESTIMATE_KEY_PHRASES = frozenset(
    {
        "estimated_completion",
        "estimated_finish",
        "estimated_time",
        "estimated_remaining",
        "completion_estimate",
        "completion_ratio",
        "progress_ratio",
        "progress_fraction",
        "fraction_complete",
        "time_remaining",
        "seconds_remaining",
        "minutes_remaining",
        "hours_remaining",
        "remaining_seconds",
        "remaining_minutes",
        "remaining_time",
    }
)

_ESTIMATE_TEXT = re.compile(
    r"\d+(?:\.\d+)?\s*%"
    r"|\bETAs?\b"
    r"|\bpercent(?:age)?\b"
    r"|\b(?:time|seconds?|minutes?|hours?)\s+remaining\b"
    r"|\bremaining\s+(?:time|seconds?|minutes?|hours?)\b"
    r"|\bcompletion\s+estimate\b"
    r"|\bestimated\s+(?:completion|finish|time|remaining)\b",
    re.IGNORECASE,
)


def _key_is_estimate(key: str) -> bool:
    tokens = [token.lower() for token in _KEY_SPLIT.split(key) if token]
    if any(token in _ESTIMATE_KEY_TOKENS for token in tokens):
        return True
    return "_".join(tokens) in _ESTIMATE_KEY_PHRASES


def _walk(value: Any, path: str, found: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            child = f"{path}.{key_text}" if path else key_text
            if _key_is_estimate(key_text):
                found.append(child)
                continue
            _walk(item, child, found)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _walk(item, f"{path}[{index}]", found)
    elif isinstance(value, str):
        if _ESTIMATE_TEXT.search(value):
            found.append(path or "<value>")


def find_progress_estimates(payload: Any) -> tuple[str, ...]:
    """Return the dotted paths at which ``payload`` claims a fake completion.

    A path is reported for a key that names a share-of-work / countdown signal,
    or for a string value that states one. An empty tuple means the payload is
    clean under §14.2.
    """

    found: list[str] = []
    _walk(to_observability_dict(payload), "", found)
    return tuple(found)


def assert_no_progress_estimate(payload: Any, *, context: str) -> None:
    """Raise :class:`RuntimeProgressError` if ``payload`` violates §14.2."""

    found = find_progress_estimates(payload)
    if found:
        raise RuntimeProgressError(
            f"{context} may not claim a share of work or a countdown "
            f"(SPEC 14.2); offending paths: {', '.join(found)}"
        )


# -- vocabulary validation -------------------------------------------------


def validate_runtime_step(name: str) -> str:
    """Return the canonical §14.1 step name for ``name``."""

    text = str(name or "").strip()
    canonical = _STEP_CANONICAL.get(text.lower())
    if canonical is None:
        raise RuntimeProgressError(
            f"Unknown runtime step: {name!r}; expected one of "
            f"{', '.join(RUNTIME_STEPS)}"
        )
    return canonical


def validate_runtime_step_status(status: str) -> str:
    """Return the canonical §14.1 step status for ``status``."""

    text = str(status or "").strip().lower()
    if text not in RUNTIME_STEP_STATUSES:
        raise RuntimeProgressError(
            f"Unknown runtime step status: {status!r}; expected one of "
            f"{', '.join(RUNTIME_STEP_STATUSES)}"
        )
    return text


def runtime_step_order(name: str) -> int:
    """Return the §14.1 ordering index for a step name."""

    return _STEP_INDEX[validate_runtime_step(name).lower()]


def is_step_status_regression(current: str | None, new: str) -> bool:
    """Return whether writing ``new`` over ``current`` moves a step backwards."""

    if current is None:
        return False
    return (
        RUNTIME_STEP_STATUS_RANK[validate_runtime_step_status(new)]
        < RUNTIME_STEP_STATUS_RANK[validate_runtime_step_status(current)]
    )


def step_glyph(status: str) -> str:
    """Return the §14.2 lifecycle glyph for a step status."""

    return RUNTIME_STEP_GLYPHS[validate_runtime_step_status(status)]


def observed_step(
    name: str,
    status: str,
    *,
    summary: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ExecutionObservedStep:
    """Build a validated, §14.2-clean :class:`ExecutionObservedStep`."""

    canonical_name = validate_runtime_step(name)
    canonical_status = validate_runtime_step_status(status)
    payload = {"summary": summary, "metadata": dict(metadata or {})}
    assert_no_progress_estimate(
        payload, context=f"observed step {canonical_name!r}"
    )
    return ExecutionObservedStep(
        name=canonical_name,
        status=canonical_status,
        summary=summary,
        metadata=dict(metadata or {}),
    )


def pending_step(name: str) -> ExecutionObservedStep:
    """Return the default ``pending`` record for an unrecorded step."""

    return ExecutionObservedStep(
        name=validate_runtime_step(name), status=RUNTIME_STEP_STATUS_PENDING
    )


# -- attempt-scoped snapshot (§14.0) ---------------------------------------


@dataclass(frozen=True)
class AttemptProgressSnapshot:
    """Runtime progress for one Attempt.

    §14.0: ObservedStep records attach to an Attempt, not to the Ticket. A
    retry produces a new Attempt and the earlier Attempt's records survive.
    """

    attempt_id: str
    task_key: str
    task_id: str
    attempt_number: int
    is_active: bool = False
    current_phase: str | None = None
    current_activity: str | None = None
    updated_at: str | None = None
    steps: tuple[ExecutionObservedStep, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.current_phase is not None:
            object.__setattr__(
                self, "current_phase", validate_runtime_step(self.current_phase)
            )
        assert_no_progress_estimate(
            {"current_activity": self.current_activity},
            context=f"attempt {self.attempt_id!r} current activity",
        )
        object.__setattr__(
            self,
            "steps",
            tuple(
                sorted(self.steps, key=lambda step: runtime_step_order(step.name))
            ),
        )

    def step_status(self, name: str) -> str:
        """Return the recorded status for a step, or ``pending``."""

        canonical = validate_runtime_step(name)
        for step in self.steps:
            if step.name == canonical:
                return step.status
        return RUNTIME_STEP_STATUS_PENDING

    def ordered_steps(self) -> tuple[ExecutionObservedStep, ...]:
        """Return all seven §14.1 steps in spec order, defaulting to pending."""

        recorded = {step.name: step for step in self.steps}
        return tuple(
            recorded.get(name) or pending_step(name) for name in RUNTIME_STEPS
        )


__all__ = [
    "RUNTIME_PROGRESS_SCHEMA_VERSION",
    "RUNTIME_STEPS",
    "RUNTIME_STEP_GLYPHS",
    "RUNTIME_STEP_LABELS",
    "RUNTIME_STEP_STATUSES",
    "RUNTIME_STEP_STATUS_BLOCKED",
    "RUNTIME_STEP_STATUS_FAILED",
    "RUNTIME_STEP_STATUS_PASSED",
    "RUNTIME_STEP_STATUS_PENDING",
    "RUNTIME_STEP_STATUS_RANK",
    "RUNTIME_STEP_STATUS_RUNNING",
    "AttemptProgressSnapshot",
    "ObservedStepRegressionError",
    "RuntimeProgressError",
    "assert_no_progress_estimate",
    "find_progress_estimates",
    "is_step_status_regression",
    "observed_step",
    "pending_step",
    "runtime_step_order",
    "step_glyph",
    "validate_runtime_step",
    "validate_runtime_step_status",
]
