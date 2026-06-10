"""P5-e: legacy-vs-engine fallback hardening for the scheduler opt-in path.

This module provides a **pure, behavior-free** fallback / readiness
classification layer for the staged scheduler-to-ExecutionEngine migration plan
defined by the P5-a boundary document
(``docs/scheduler-execution-engine-migration-boundary.md``):

* P5-a defined the scheduler-to-ExecutionEngine migration boundary.
* P5-b added a pure scheduler ExecutionEngine request builder
  (``agent_taskflow/scheduler_execution_engine_request_builder.py``).
* P5-c added a pure legacy-vs-engine shadow / compare layer
  (``agent_taskflow/scheduler_execution_engine_shadow_compare.py``).
* P5-d added the explicit ``--use-execution-engine`` opt-in path
  (``agent_taskflow/scheduler_execution_engine_opt_in.py``), off by default.
* P5-e (this module) classifies the P5-d ``execution_engine`` evidence block so
  fallback semantics are explicit, machine-readable, and impossible to confuse
  with approval or execution authority.

The classification never changes any behavior. The **legacy scheduler path
remains the effective authority**: the scheduler tick payload ``ok`` and
``status`` continue to come from the legacy path, and engine output never
changes the legacy tick decision. Every assessment pins
``effective_authority="legacy_scheduler"``, ``engine_authority=False``, and
``engine_result_accepted_as_authority=False`` by construction. A clean engine
candidate may be marked usable for *future* migration, but it is never
authoritative in P5-e and is never approval authority: deterministic validators
and human review gates remain the validation and approval authority.

The module is pure: it reads two mappings and returns a value. It performs no
filesystem, DB, GitHub, or runtime access, calls no engine / executor /
validator / approved-task-runner entrypoint, runs no subprocess, and mutates no
state.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from agent_taskflow.execution_engine_contract import to_json_dict


SCHEDULER_EXECUTION_ENGINE_FALLBACK_SCHEMA_VERSION = (
    "scheduler_execution_engine_fallback.v1"
)
SCHEDULER_EXECUTION_ENGINE_FALLBACK_SOURCE = (
    "scheduler_execution_engine_fallback"
)

# The one and only effective authority P5-e can ever report. The engine path is
# runtime evidence only; making it authoritative is future migration work.
EFFECTIVE_AUTHORITY_LEGACY_SCHEDULER = "legacy_scheduler"

# Machine-readable fallback reasons.
FALLBACK_REASON_ENGINE_EVIDENCE_ABSENT = "engine_evidence_absent"
FALLBACK_REASON_ENGINE_NOT_ENABLED = "engine_not_enabled"
FALLBACK_REASON_ENGINE_NOT_EXECUTED = "engine_not_executed"
FALLBACK_REASON_ENGINE_NOT_OK = "engine_not_ok"
FALLBACK_REASON_SHADOW_COMPARE_MISSING = "shadow_compare_missing"
FALLBACK_REASON_SHADOW_COMPARE_MISMATCH = "shadow_compare_mismatch"
FALLBACK_REASON_ENGINE_SAFETY_BLOCK_MISSING = "engine_safety_block_missing"
FALLBACK_REASON_UNSAFE_ENGINE_SAFETY_MARKER = "unsafe_engine_safety_marker"
FALLBACK_REASON_PUBLICATION_BOUNDARY_VIOLATION = (
    "publication_boundary_violation"
)
FALLBACK_REASON_LEGACY_OK_MISSING = "legacy_ok_missing"
FALLBACK_REASON_LEGACY_STATUS_MISSING = "legacy_status_missing"

# Engine statuses containing any of these markers indicate the engine run is
# not a clean candidate (error / not executed / blocked / failed).
_FAILURE_STATUS_MARKERS = ("error", "not_executed", "blocked", "failed")

# Safety markers that must never be true on the engine evidence. Any of these
# being true means the engine path crossed a governance boundary it must never
# cross, and the candidate is unusable.
_UNSAFE_SAFETY_MARKERS = (
    "approval_authority",
    "approved",
    "merged",
    "github_mutated",
    "branch_pushed",
    "draft_pr_created",
    "cleanup_performed",
    "archived",
    "closed_out",
    "branch_deleted",
    "worktree_deleted",
    "daemon_started",
    "webhook_started",
    "background_worker_started",
    "scheduler_loop_started",
    "multi_task_batch_started",
)

_MISSING = object()


@dataclass(frozen=True)
class SchedulerExecutionEngineFallbackAssessmentInput:
    """A legacy scheduler tick payload paired with P5-d engine evidence.

    ``execution_engine_evidence`` is the (possibly absent) ``execution_engine``
    opt-in evidence block produced by the P5-d path. Construction copies the
    dict-like inputs defensively and performs no filesystem, DB, GitHub, or
    runtime access.
    """

    legacy_tick_payload: Mapping[str, Any]
    execution_engine_evidence: Mapping[str, Any] | None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.legacy_tick_payload, Mapping):
            raise TypeError("legacy_tick_payload must be a mapping")
        if self.execution_engine_evidence is not None and not isinstance(
            self.execution_engine_evidence, Mapping
        ):
            raise TypeError(
                "execution_engine_evidence must be a mapping or None"
            )
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        # Defensive deep copies so later mutation of the caller's dicts (or any
        # nested container inside them) cannot mutate this input or a result.
        object.__setattr__(
            self,
            "legacy_tick_payload",
            MappingProxyType(copy.deepcopy(dict(self.legacy_tick_payload))),
        )
        if self.execution_engine_evidence is not None:
            object.__setattr__(
                self,
                "execution_engine_evidence",
                MappingProxyType(
                    copy.deepcopy(dict(self.execution_engine_evidence))
                ),
            )
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(copy.deepcopy(dict(self.metadata))),
        )


@dataclass(frozen=True)
class SchedulerExecutionEngineFallbackAssessment:
    """Fallback / readiness classification of one P5-d engine evidence block.

    ``ok`` is true only when no fallback was required (the engine candidate
    evidence was clean). Even then the candidate carries no authority:
    ``effective_authority`` is always ``"legacy_scheduler"``,
    ``engine_authority`` is always false, and
    ``engine_result_accepted_as_authority`` is always false.
    """

    ok: bool
    schema_version: str
    source: str
    effective_authority: str
    engine_authority: bool
    fallback_required: bool
    fallback_reason: str | None
    fallback_reasons: tuple[str, ...]
    engine_candidate_usable_for_future_migration: bool
    engine_result_accepted_as_authority: bool
    legacy_ok_preserved: bool
    legacy_status_preserved: bool
    publication_boundary_preserved: bool
    safety_boundary_preserved: bool
    summary: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "fallback_reasons", tuple(self.fallback_reasons)
        )
        object.__setattr__(
            self,
            "summary",
            MappingProxyType(dict(self.summary)),
        )


def assess_scheduler_execution_engine_fallback(
    input: SchedulerExecutionEngineFallbackAssessmentInput,
) -> SchedulerExecutionEngineFallbackAssessment:
    """Classify the P5-d engine evidence as fallback-required or clean.

    Pure inspection only: this reads the two values and returns a
    classification. It executes nothing, touches no filesystem, DB, GitHub,
    cron, or subprocess, and never changes the legacy tick payload — the legacy
    ``ok`` / ``status`` remain the effective tick decision regardless of what
    this assessment reports.
    """

    legacy = input.legacy_tick_payload
    evidence = input.execution_engine_evidence

    reasons: list[str] = []
    summary: dict[str, Any] = {}

    # --- Legacy authority (always recorded) -----------------------------------
    legacy_ok_present = "ok" in legacy
    legacy_status_present = "status" in legacy
    summary["legacy_ok"] = legacy.get("ok")
    summary["legacy_status"] = (
        None if legacy.get("status") is None else str(legacy.get("status"))
    )
    summary["effective_authority"] = EFFECTIVE_AUTHORITY_LEGACY_SCHEDULER
    summary["engine_authority"] = False
    summary["engine_result_accepted_as_authority"] = False
    if not legacy_ok_present:
        reasons.append(FALLBACK_REASON_LEGACY_OK_MISSING)
    if not legacy_status_present:
        reasons.append(FALLBACK_REASON_LEGACY_STATUS_MISSING)

    # --- Engine evidence presence / lifecycle ---------------------------------
    summary["engine_evidence_present"] = evidence is not None
    if evidence is None:
        reasons.append(FALLBACK_REASON_ENGINE_EVIDENCE_ABSENT)
        summary["engine_enabled"] = None
        summary["engine_executed"] = None
        summary["engine_ok"] = None
        summary["engine_status"] = None
        return _build_assessment(
            reasons=reasons,
            summary=summary,
            legacy_ok_preserved=legacy_ok_present,
            legacy_status_preserved=legacy_status_present,
            # Absent evidence means the engine path never ran; nothing crossed
            # the publication or safety boundary.
            publication_boundary_preserved=True,
            safety_boundary_preserved=True,
        )

    engine_enabled = evidence.get("enabled")
    engine_executed = evidence.get("executed")
    engine_ok = evidence.get("ok")
    engine_status_value = evidence.get("status")
    engine_status = (
        None if engine_status_value is None else str(engine_status_value)
    )
    summary["engine_enabled"] = engine_enabled
    summary["engine_executed"] = engine_executed
    summary["engine_ok"] = engine_ok
    summary["engine_status"] = engine_status

    if engine_enabled is not True:
        reasons.append(FALLBACK_REASON_ENGINE_NOT_ENABLED)
    if engine_executed is not True:
        reasons.append(FALLBACK_REASON_ENGINE_NOT_EXECUTED)
    if engine_ok is not True:
        reasons.append(FALLBACK_REASON_ENGINE_NOT_OK)
    failure_status_reason = _failure_status_reason(engine_status)
    if failure_status_reason is not None:
        reasons.append(failure_status_reason)

    # --- Shadow compare --------------------------------------------------------
    shadow_compare = evidence.get("shadow_compare")
    if not isinstance(shadow_compare, Mapping):
        summary["shadow_compare"] = {
            "present": False,
            "matched": None,
            "mismatch_count": None,
            "mismatches": [],
        }
        reasons.append(FALLBACK_REASON_SHADOW_COMPARE_MISSING)
    else:
        matched = shadow_compare.get("matched")
        mismatches_value = shadow_compare.get("mismatches")
        mismatches = (
            [str(item) for item in mismatches_value]
            if isinstance(mismatches_value, (list, tuple))
            else []
        )
        summary["shadow_compare"] = {
            "present": True,
            "matched": matched,
            "mismatch_count": len(mismatches),
            "mismatches": mismatches,
        }
        if matched is not True:
            reasons.append(FALLBACK_REASON_SHADOW_COMPARE_MISMATCH)

    # --- Safety boundary --------------------------------------------------------
    safety = evidence.get("safety")
    unsafe_markers: list[str] = []
    if not isinstance(safety, Mapping):
        summary["engine_safety_present"] = False
        summary["unsafe_safety_markers"] = []
        reasons.append(FALLBACK_REASON_ENGINE_SAFETY_BLOCK_MISSING)
        safety_boundary_preserved = False
    else:
        summary["engine_safety_present"] = True
        for marker in _UNSAFE_SAFETY_MARKERS:
            if bool(safety.get(marker, False)):
                unsafe_markers.append(marker)
        summary["unsafe_safety_markers"] = unsafe_markers
        if unsafe_markers:
            reasons.append(FALLBACK_REASON_UNSAFE_ENGINE_SAFETY_MARKER)
        safety_boundary_preserved = not unsafe_markers

    # --- Publication boundary ----------------------------------------------------
    publication = _publication_markers(evidence)
    publication_boundary_preserved = _publication_boundary_preserved(
        publication,
        engine_executed=engine_executed,
    )
    summary["publication_boundary"] = {
        "publish_after_execution": publication["publish_after_execution"],
        "mode": publication["mode"],
        "execution_only": publication["execution_only"],
        "markers_found": publication["markers_found"],
        "preserved": publication_boundary_preserved,
    }
    if not publication_boundary_preserved:
        reasons.append(FALLBACK_REASON_PUBLICATION_BOUNDARY_VIOLATION)

    return _build_assessment(
        reasons=reasons,
        summary=summary,
        legacy_ok_preserved=legacy_ok_present,
        legacy_status_preserved=legacy_status_present,
        publication_boundary_preserved=publication_boundary_preserved,
        safety_boundary_preserved=safety_boundary_preserved,
    )


def scheduler_execution_engine_fallback_assessment_to_json_dict(
    result: SchedulerExecutionEngineFallbackAssessment,
) -> dict[str, Any]:
    """Return the assessment as a JSON-compatible dict via the contract codec."""

    payload = to_json_dict(result)
    if not isinstance(payload, dict):
        raise TypeError(
            "SchedulerExecutionEngineFallbackAssessment did not serialize to a"
            f" dict: {type(payload).__name__}"
        )
    return payload


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _build_assessment(
    *,
    reasons: list[str],
    summary: dict[str, Any],
    legacy_ok_preserved: bool,
    legacy_status_preserved: bool,
    publication_boundary_preserved: bool,
    safety_boundary_preserved: bool,
) -> SchedulerExecutionEngineFallbackAssessment:
    fallback_required = bool(reasons)
    summary["fallback_required"] = fallback_required
    summary["fallback_reasons"] = list(reasons)
    return SchedulerExecutionEngineFallbackAssessment(
        ok=not fallback_required,
        schema_version=SCHEDULER_EXECUTION_ENGINE_FALLBACK_SCHEMA_VERSION,
        source=SCHEDULER_EXECUTION_ENGINE_FALLBACK_SOURCE,
        effective_authority=EFFECTIVE_AUTHORITY_LEGACY_SCHEDULER,
        engine_authority=False,
        fallback_required=fallback_required,
        fallback_reason=reasons[0] if reasons else None,
        fallback_reasons=tuple(reasons),
        engine_candidate_usable_for_future_migration=not fallback_required,
        engine_result_accepted_as_authority=False,
        legacy_ok_preserved=legacy_ok_preserved,
        legacy_status_preserved=legacy_status_preserved,
        publication_boundary_preserved=publication_boundary_preserved,
        safety_boundary_preserved=safety_boundary_preserved,
        summary=summary,
    )


def _failure_status_reason(engine_status: str | None) -> str | None:
    """Return a machine-readable reason when the status indicates failure.

    Statuses containing ``error`` / ``not_executed`` / ``blocked`` / ``failed``
    (for example ``engine_error``, ``validator_failed``, ``executor_failed``,
    ``preflight_failed``, ``blocked``, ``not_executed``) are not clean
    candidates. The reason carries the offending status so the classification
    stays machine-readable: ``engine_failure_status:<status>``.
    """

    if engine_status is None:
        return None
    normalized = engine_status.strip().lower()
    for marker in _FAILURE_STATUS_MARKERS:
        if marker in normalized:
            return f"engine_failure_status:{normalized}"
    return None


def _publication_markers(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Read the execution-only publication markers from the engine evidence.

    The markers are read from the evidence ``request_summary`` first and fall
    back, per key, to the engine request ``metadata``. Values are read only;
    nothing is executed or mutated.
    """

    candidates: list[Mapping[str, Any]] = []
    request_summary = evidence.get("request_summary")
    if isinstance(request_summary, Mapping):
        candidates.append(request_summary)
    request = evidence.get("request")
    if isinstance(request, Mapping):
        metadata = request.get("metadata")
        if isinstance(metadata, Mapping):
            candidates.append(metadata)

    values: dict[str, Any] = {}
    markers_found = False
    for key in ("publish_after_execution", "mode", "execution_only"):
        value = _MISSING
        for candidate in candidates:
            if key in candidate and candidate[key] is not None:
                value = candidate[key]
                break
        if value is _MISSING:
            values[key] = None
        else:
            values[key] = value
            markers_found = True
    values["markers_found"] = markers_found
    return values


def _publication_boundary_preserved(
    publication: Mapping[str, Any],
    *,
    engine_executed: Any,
) -> bool:
    """Whether the engine evidence preserves the execution-only boundary.

    The boundary requires ``publish_after_execution=False``,
    ``mode=execution_only``, and ``execution_only=True``. When the engine
    actually executed, all three markers must be present and clean; an executed
    engine run whose publication markers cannot be verified is treated as a
    violation. When the engine did not execute, absent markers are not a
    violation — nothing ran, so nothing could have published.
    """

    if not publication["markers_found"]:
        return engine_executed is not True
    return (
        publication["publish_after_execution"] is False
        and publication["mode"] == "execution_only"
        and publication["execution_only"] is True
    )


__all__ = [
    "EFFECTIVE_AUTHORITY_LEGACY_SCHEDULER",
    "SCHEDULER_EXECUTION_ENGINE_FALLBACK_SCHEMA_VERSION",
    "SCHEDULER_EXECUTION_ENGINE_FALLBACK_SOURCE",
    "SchedulerExecutionEngineFallbackAssessment",
    "SchedulerExecutionEngineFallbackAssessmentInput",
    "assess_scheduler_execution_engine_fallback",
    "scheduler_execution_engine_fallback_assessment_to_json_dict",
]
