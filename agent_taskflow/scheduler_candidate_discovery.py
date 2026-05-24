"""Read-only scheduler candidate discovery (Phase G — Level 1).

This module lists which tasks in the mirror are candidates for the scheduler
flow. It NEVER creates proposals, NEVER creates confirmations, NEVER creates
handoff artifacts, NEVER invokes runtime execution, and NEVER calls
``approved_task_runner``. It is strictly Level 1 read-only discovery as
described in ``docs/semi-automatic-scheduler-readiness-checkpoint.md`` §4.

Being listed as a candidate is **not** execution permission. Human/operator
confirmation remains required, ``validation_result`` remains authoritative,
and Mission Control remains read-only.

The discovery layer reuses :mod:`agent_taskflow.task_recommendations` for its
underlying read-only classification. It does not import
``approved_task_runner``, ``queued_task_handoff``, ``intake_runner_handoff``,
``scheduler_proposals``, ``scheduler_confirmations``, or any executor / GitHub
mutation surface, by design.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_taskflow.models import validate_task_status
from agent_taskflow.store import default_db_path
from agent_taskflow.task_recommendations import (
    TaskRecommendationsError,
    TaskRecommendationsRequest,
    list_task_recommendations,
)
from agent_taskflow.tasks import normalize_task_key


SCHEMA_VERSION = "scheduler_candidate_discovery.v1"
DISCOVERY_MODE = "read_only"


ACTIONABLE_CANDIDATE_KINDS: frozenset[str] = frozenset(
    {
        "create_task_execution_package",
        "queued_task_handoff",
        "branch_push_review",
        "draft_pr_review",
        "pr_handoff_package",
        "cleanup_continue",
        "post_merge_cleanup_review",
        "inspect_blocker",
        "inspect_evidence",
    }
)

NO_ACTION_KINDS: frozenset[str] = frozenset({"no_action"})
NOT_READY_KINDS: frozenset[str] = frozenset({"unknown", "human_pr_review"})


_GATE_AND_OPERATOR_ACTION: dict[str, tuple[str, str]] = {
    "create_task_execution_package": (
        "scheduler_proposal",
        "create_scheduler_proposal",
    ),
    "queued_task_handoff": (
        "scheduler_proposal_then_confirmation_then_verifier_then_handoff",
        "create_scheduler_proposal",
    ),
    "branch_push_review": (
        "scheduler_proposal_then_confirmation_then_command_specific_confirm",
        "create_scheduler_proposal",
    ),
    "draft_pr_review": (
        "scheduler_proposal_then_confirmation_then_command_specific_confirm",
        "create_scheduler_proposal",
    ),
    "pr_handoff_package": (
        "scheduler_proposal_then_confirmation_then_command_specific_confirm",
        "create_scheduler_proposal",
    ),
    "cleanup_continue": (
        "scheduler_proposal_then_confirmation_then_command_specific_confirm",
        "create_scheduler_proposal",
    ),
    "post_merge_cleanup_review": (
        "scheduler_proposal_then_confirmation_then_command_specific_confirm",
        "create_scheduler_proposal",
    ),
    "inspect_blocker": ("human_inspection", "inspect_manually"),
    "inspect_evidence": ("human_inspection", "inspect_manually"),
    "no_action": ("none", "none"),
    "unknown": ("manual_triage", "inspect_manually"),
    "human_pr_review": ("human_github_review", "none"),
}


CANDIDATE_SAFETY_FLAGS: dict[str, bool] = {
    "read_only": True,
    "proposal_created": False,
    "confirmation_created": False,
    "handoff_created": False,
    "runtime_started": False,
    "approved_task_runner_called": False,
    "github_mutated": False,
    "approved": False,
    "merged": False,
    "cleanup_performed": False,
    "background_worker_started": False,
}


DISCOVERY_SAFETY_FLAGS: dict[str, bool] = {
    "read_only": True,
    "db_written": False,
    "artifact_written": False,
    "proposal_created": False,
    "confirmation_created": False,
    "handoff_created": False,
    "verifier_report_created": False,
    "runtime_started": False,
    "approved_task_runner_called": False,
    "github_mutated": False,
    "approved": False,
    "merged": False,
    "cleanup_performed": False,
    "background_worker_started": False,
    "task_status_changed": False,
    "scheduler_loop_started": False,
}


DISCOVERY_NOTE = (
    "Candidate discovery is read-only and is NOT execution permission. "
    "Human/operator confirmation remains required; validation_result "
    "remains authoritative; Mission Control remains read-only."
)


class SchedulerCandidateDiscoveryError(RuntimeError):
    """Raised when scheduler candidate discovery cannot read its inputs safely."""


@dataclass(frozen=True)
class SchedulerCandidateDiscoveryRequest:
    """Inputs for a read-only scheduler candidate listing."""

    db_path: str | Path | None = None
    task_key: str | None = None
    project: str | None = None
    status: str | None = None
    include_not_ready: bool = False
    include_no_action: bool = False
    limit: int | None = None
    completed_limit: int = 20

    def __post_init__(self) -> None:
        if self.db_path is None:
            object.__setattr__(self, "db_path", default_db_path())
        else:
            object.__setattr__(self, "db_path", Path(self.db_path).expanduser())

        if self.task_key is not None:
            object.__setattr__(self, "task_key", normalize_task_key(self.task_key))

        if self.project is not None:
            project = self.project.strip()
            if not project:
                raise ValueError("project must not be empty")
            object.__setattr__(self, "project", project)

        if self.status is not None:
            object.__setattr__(self, "status", validate_task_status(self.status))

        if self.limit is not None and self.limit < 0:
            raise ValueError("limit must be zero or positive")

        if self.completed_limit < 0:
            raise ValueError("completed_limit must be zero or positive")


def discover_scheduler_candidates(
    request: SchedulerCandidateDiscoveryRequest,
) -> dict[str, Any]:
    """Return a read-only list of scheduler candidates from the live mirror.

    This function does NOT mutate the DB, write artifacts, create scheduler
    proposals, create confirmations, create handoffs, start runtime
    execution, invoke ``approved_task_runner``, mutate GitHub, approve,
    merge, or run cleanup. It is strictly Level 1 discovery.
    """

    db_path = request.db_path
    assert db_path is not None

    try:
        rec_payload = list_task_recommendations(
            TaskRecommendationsRequest(
                db_path=db_path,
                status=request.status,
                project=request.project,
                task_key=request.task_key,
                completed_limit=request.completed_limit,
            )
        )
    except TaskRecommendationsError as exc:
        raise SchedulerCandidateDiscoveryError(
            f"could not read task recommendations: {exc}"
        ) from exc

    raw_items = rec_payload.get("items") or []
    candidates: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        candidate = _build_candidate(item)
        if candidate is None:
            continue
        if not _should_include(
            candidate,
            include_not_ready=request.include_not_ready,
            include_no_action=request.include_no_action,
        ):
            continue
        candidates.append(candidate)

    if request.limit is not None:
        candidates = candidates[: request.limit]

    summary = _summary(candidates)

    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "mode": DISCOVERY_MODE,
        "discovery_note": DISCOVERY_NOTE,
        "db_path": str(db_path),
        "filters": {
            "status": request.status,
            "project": request.project,
            "task_key": request.task_key,
            "include_not_ready": request.include_not_ready,
            "include_no_action": request.include_no_action,
            "limit": request.limit,
        },
        "candidate_count": len(candidates),
        "candidates": candidates,
        "summary": summary,
        "safety": dict(DISCOVERY_SAFETY_FLAGS),
    }


def list_scheduler_candidates(
    db_path: str | Path | None = None,
    *,
    task_key: str | None = None,
    project: str | None = None,
    status: str | None = None,
    include_not_ready: bool = False,
    include_no_action: bool = False,
    limit: int | None = None,
    completed_limit: int = 20,
) -> dict[str, Any]:
    """Convenience wrapper around :func:`discover_scheduler_candidates`."""

    request = SchedulerCandidateDiscoveryRequest(
        db_path=db_path,
        task_key=task_key,
        project=project,
        status=status,
        include_not_ready=include_not_ready,
        include_no_action=include_no_action,
        limit=limit,
        completed_limit=completed_limit,
    )
    return discover_scheduler_candidates(request)


def _build_candidate(rec_item: dict[str, Any]) -> dict[str, Any] | None:
    kind = rec_item.get("recommended_command_kind")
    if not isinstance(kind, str) or not kind:
        return None

    gate, operator_action = _GATE_AND_OPERATOR_ACTION.get(
        kind, ("manual_triage", "inspect_manually")
    )
    candidate_ready = kind in ACTIONABLE_CANDIDATE_KINDS

    missing_evidence = list(rec_item.get("missing_evidence") or [])
    consistency_warnings = list(rec_item.get("consistency_warnings") or [])
    related_artifacts = [
        {
            "artifact_type": artifact.get("artifact_type"),
            "path": artifact.get("path"),
            "created_at": artifact.get("created_at"),
        }
        for artifact in (rec_item.get("related_artifacts") or [])
        if isinstance(artifact, dict)
    ]

    return {
        "task_key": rec_item.get("task_key"),
        "project": rec_item.get("project"),
        "title": rec_item.get("title"),
        "status": rec_item.get("status"),
        "current_phase_label": rec_item.get("current_phase_label"),
        "recommended_command_kind": kind,
        "recommended_next_action": rec_item.get("recommended_next_action"),
        "candidate_ready": candidate_ready,
        "required_next_gate": gate,
        "required_operator_action": operator_action,
        "missing_evidence": missing_evidence,
        "consistency_warnings": consistency_warnings,
        "related_artifacts": related_artifacts,
        "severity": rec_item.get("severity"),
        "confidence": rec_item.get("confidence"),
        "reason": rec_item.get("reason"),
        "blocked_reason": rec_item.get("blocked_reason"),
        "safety": dict(CANDIDATE_SAFETY_FLAGS),
    }


def _should_include(
    candidate: dict[str, Any],
    *,
    include_not_ready: bool,
    include_no_action: bool,
) -> bool:
    kind = candidate["recommended_command_kind"]
    if candidate["candidate_ready"]:
        return True
    if kind in NO_ACTION_KINDS:
        return include_no_action
    if kind in NOT_READY_KINDS:
        return include_not_ready
    return include_not_ready


def _summary(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    kind_counts: dict[str, int] = {}
    ready_count = 0
    warning_count = 0
    for candidate in candidates:
        kind = candidate["recommended_command_kind"]
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        if candidate["candidate_ready"]:
            ready_count += 1
        warning_count += len(candidate.get("consistency_warnings") or [])
    return {
        "candidate_count": len(candidates),
        "candidate_ready_count": ready_count,
        "warning_count": warning_count,
        "recommended_command_kind_counts": dict(sorted(kind_counts.items())),
        "execution_allowed": False,
        "requires_human_review": True,
    }


__all__ = [
    "ACTIONABLE_CANDIDATE_KINDS",
    "CANDIDATE_SAFETY_FLAGS",
    "DISCOVERY_MODE",
    "DISCOVERY_NOTE",
    "DISCOVERY_SAFETY_FLAGS",
    "NOT_READY_KINDS",
    "NO_ACTION_KINDS",
    "SCHEMA_VERSION",
    "SchedulerCandidateDiscoveryError",
    "SchedulerCandidateDiscoveryRequest",
    "discover_scheduler_candidates",
    "list_scheduler_candidates",
]
