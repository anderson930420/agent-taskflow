"""Explicit scheduler proposal generation from a live candidate.

This module is the Phase J1 bridge from Level 1 candidate discovery to the
existing scheduler proposal generator. It is intentionally thin: it
rediscovers the candidate at command time, applies stale-candidate and safety
guards, then delegates all proposal hashing, item hashing, artifact schema,
and persistence to :mod:`agent_taskflow.scheduler_proposals`.

It is not a scheduler loop, not a background worker, not confirmation, not
handoff creation, and not runtime execution. A successful confirmed call writes
only scheduler proposal evidence through the existing proposal module.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_taskflow.models import validate_task_status
from agent_taskflow.scheduler_candidate_discovery import (
    SchedulerCandidateDiscoveryError,
    SchedulerCandidateDiscoveryRequest,
    discover_scheduler_candidates,
)
from agent_taskflow.scheduler_proposals import (
    SchedulerProposalError,
    SchedulerProposalRequest,
    create_scheduler_proposal,
)
from agent_taskflow.store import default_db_path
from agent_taskflow.tasks import normalize_task_key


SCHEMA_VERSION = "scheduler_candidate_proposal.v1"
SOURCE = "scheduler_candidate_proposals"


@dataclass(frozen=True)
class SchedulerCandidateProposalRequest:
    """Inputs for explicit proposal generation from one live candidate."""

    task_key: str
    db_path: Path | str | None = None
    artifact_root: Path | str | None = None
    confirm_create_proposal: bool = False
    dry_run: bool = True
    expected_recommended_command_kind: str | None = None
    expected_status: str | None = None
    include_not_ready: bool = False
    include_no_action: bool = False

    def __post_init__(self) -> None:
        if self.db_path is None:
            object.__setattr__(self, "db_path", default_db_path())
        else:
            object.__setattr__(self, "db_path", Path(self.db_path).expanduser())

        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))

        if self.artifact_root is not None:
            object.__setattr__(
                self,
                "artifact_root",
                Path(self.artifact_root).expanduser(),
            )

        if self.expected_status is not None:
            object.__setattr__(
                self,
                "expected_status",
                validate_task_status(self.expected_status),
            )

        if self.expected_recommended_command_kind is not None:
            expected_kind = self.expected_recommended_command_kind.strip()
            if not expected_kind:
                raise ValueError("expected_recommended_command_kind must not be empty")
            object.__setattr__(
                self,
                "expected_recommended_command_kind",
                expected_kind,
            )


def create_scheduler_proposal_from_candidate(
    request: SchedulerCandidateProposalRequest,
) -> dict[str, Any]:
    """Create or preview a scheduler proposal for one live candidate.

    The candidate is always rediscovered from the current DB state. User
    supplied expected values are stale-candidate guards only; they are never
    treated as authority.
    """

    try:
        discovery = discover_scheduler_candidates(
            SchedulerCandidateDiscoveryRequest(
                db_path=request.db_path,
                task_key=request.task_key,
                include_not_ready=request.include_not_ready,
                include_no_action=request.include_no_action,
                limit=1,
            )
        )
    except (ValueError, SchedulerCandidateDiscoveryError) as exc:
        return _error_result(request, str(exc))

    candidates = discovery.get("candidates") or []
    candidate = candidates[0] if candidates else None
    if not isinstance(candidate, dict):
        return _blocked_result(request, "candidate_not_found", candidate=None)

    if not candidate.get("candidate_ready"):
        return _blocked_result(request, "candidate_not_ready", candidate=candidate)

    expected_status = request.expected_status
    if expected_status is not None and candidate.get("status") != expected_status:
        return _blocked_result(
            request,
            "stale_expected_status",
            candidate=candidate,
        )

    expected_kind = request.expected_recommended_command_kind
    if (
        expected_kind is not None
        and candidate.get("recommended_command_kind") != expected_kind
    ):
        return _blocked_result(
            request,
            "stale_expected_recommended_command_kind",
            candidate=candidate,
        )

    if not request.dry_run and not request.confirm_create_proposal:
        return _blocked_result(
            request,
            "confirm_create_proposal_required",
            candidate=candidate,
        )

    artifact_root = request.artifact_root
    if artifact_root is None:
        return _blocked_result(
            request,
            "artifact_root_required",
            candidate=candidate,
        )

    try:
        preview = create_scheduler_proposal(
            _proposal_request(
                request,
                artifact_root=artifact_root,
                candidate=candidate,
                dry_run=True,
                confirm_create_proposal=False,
            )
        )
    except (ValueError, SchedulerProposalError) as exc:
        return _error_result(request, str(exc), candidate=candidate)

    match = _selected_item_match(preview, request.task_key, candidate)
    if not match["ok"]:
        return _blocked_result(
            request,
            str(match["block_reason"]),
            candidate=candidate,
            proposal=_proposal_summary(preview, item=match.get("item"), created=False),
        )

    preview_item = match["item"]
    if request.dry_run:
        return _success_result(
            request,
            status="preview",
            candidate=candidate,
            proposal=_proposal_summary(preview, item=preview_item, created=False),
            proposal_created=False,
        )

    try:
        created = create_scheduler_proposal(
            _proposal_request(
                request,
                artifact_root=artifact_root,
                candidate=candidate,
                dry_run=False,
                confirm_create_proposal=True,
            )
        )
    except (ValueError, SchedulerProposalError) as exc:
        return _error_result(request, str(exc), candidate=candidate)

    created_match = _selected_item_match(created, request.task_key, candidate)
    if not created_match["ok"]:
        return _error_result(
            request,
            f"created_proposal_mismatch: {created_match['block_reason']}",
            candidate=candidate,
        )

    created_item = created_match["item"]
    return _success_result(
        request,
        status="created",
        candidate=candidate,
        proposal=_proposal_summary(created, item=created_item, created=True),
        proposal_created=True,
    )


def propose_candidate_task(
    task_key: str,
    *,
    db_path: Path | str | None = None,
    artifact_root: Path | str | None = None,
    confirm_create_proposal: bool = False,
    dry_run: bool = True,
    expected_recommended_command_kind: str | None = None,
    expected_status: str | None = None,
    include_not_ready: bool = False,
    include_no_action: bool = False,
) -> dict[str, Any]:
    """Convenience wrapper around create_scheduler_proposal_from_candidate."""

    return create_scheduler_proposal_from_candidate(
        SchedulerCandidateProposalRequest(
            task_key=task_key,
            db_path=db_path,
            artifact_root=artifact_root,
            confirm_create_proposal=confirm_create_proposal,
            dry_run=dry_run,
            expected_recommended_command_kind=expected_recommended_command_kind,
            expected_status=expected_status,
            include_not_ready=include_not_ready,
            include_no_action=include_no_action,
        )
    )


def candidate_proposal_safety(
    *,
    dry_run: bool,
    proposal_created: bool,
) -> dict[str, bool]:
    """Return the locked safety block used by all result paths."""

    return {
        "explicit_operator_request": True,
        "dry_run": dry_run,
        "read_only_preview": dry_run and not proposal_created,
        "proposal_created": proposal_created,
        "confirmation_created": False,
        "verifier_report_created": False,
        "handoff_created": False,
        "runtime_started": False,
        "approved_task_runner_called": False,
        "executor_started": False,
        "validators_started": False,
        "github_mutated": False,
        "approved": False,
        "merged": False,
        "cleanup_performed": False,
        "background_worker_started": False,
        "not_execution_permission": True,
    }


def _proposal_request(
    request: SchedulerCandidateProposalRequest,
    *,
    artifact_root: Path,
    candidate: dict[str, Any],
    dry_run: bool,
    confirm_create_proposal: bool,
) -> SchedulerProposalRequest:
    db_path = request.db_path
    assert db_path is not None

    recommended_kind = candidate.get("recommended_command_kind")
    if not isinstance(recommended_kind, str) or not recommended_kind.strip():
        raise ValueError("candidate recommended_command_kind must be a non-empty str")
    recommended_kind = recommended_kind.strip()

    status = candidate.get("status")
    if not isinstance(status, str) or not status.strip():
        raise ValueError("candidate status must be a non-empty str")
    status = validate_task_status(status)

    return SchedulerProposalRequest(
        db_path=db_path,
        artifact_root=artifact_root,
        status=status,
        task_key=request.task_key,
        include_no_action=request.include_no_action,
        include_unknown=request.include_not_ready,
        include_command_kinds=(recommended_kind,),
        max_items=1,
        dry_run=dry_run,
        confirm_create_proposal=confirm_create_proposal,
    )


def _selected_item_match(
    proposal: dict[str, Any],
    task_key: str,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    raw_items = proposal.get("items")
    items = raw_items if isinstance(raw_items, list) else []
    selected = [
        item
        for item in items
        if isinstance(item, dict) and item.get("task_key") == task_key
    ]
    if len(selected) != 1:
        return {
            "ok": False,
            "block_reason": "proposal_selected_item_count_mismatch",
            "item": selected[0] if selected else None,
        }

    item = selected[0]
    if item.get("recommended_command_kind") != candidate.get(
        "recommended_command_kind"
    ):
        return {
            "ok": False,
            "block_reason": "proposal_recommended_command_kind_mismatch",
            "item": item,
        }

    return {"ok": True, "block_reason": None, "item": item}


def _proposal_summary(
    proposal: dict[str, Any],
    *,
    item: dict[str, Any] | None,
    created: bool,
) -> dict[str, Any]:
    return {
        "proposal_id": proposal.get("proposal_id"),
        "proposal_artifact_path": proposal.get("artifact_path"),
        "proposal_hash": proposal.get("proposal_hash"),
        "proposal_item_id": item.get("proposal_item_id") if item else None,
        "item_hash": item.get("item_hash") if item else None,
        "recommended_command_kind": (
            item.get("recommended_command_kind") if item else None
        ),
        "created": created,
    }


def _success_result(
    request: SchedulerCandidateProposalRequest,
    *,
    status: str,
    candidate: dict[str, Any],
    proposal: dict[str, Any],
    proposal_created: bool,
) -> dict[str, Any]:
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "status": status,
        "mode": _mode(request),
        "task_key": request.task_key,
        "block_reason": None,
        "candidate": candidate,
        "proposal": proposal,
        "safety": candidate_proposal_safety(
            dry_run=request.dry_run,
            proposal_created=proposal_created,
        ),
    }


def _blocked_result(
    request: SchedulerCandidateProposalRequest,
    block_reason: str,
    *,
    candidate: dict[str, Any] | None,
    proposal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "status": "blocked",
        "mode": _mode(request),
        "task_key": request.task_key,
        "block_reason": block_reason,
        "candidate": candidate,
        "proposal": proposal,
        "safety": candidate_proposal_safety(
            dry_run=request.dry_run,
            proposal_created=False,
        ),
    }


def _error_result(
    request: SchedulerCandidateProposalRequest,
    error: str,
    *,
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "status": "error",
        "mode": _mode(request),
        "task_key": request.task_key,
        "block_reason": None,
        "error": error,
        "candidate": candidate,
        "proposal": None,
        "safety": candidate_proposal_safety(
            dry_run=request.dry_run,
            proposal_created=False,
        ),
    }


def _mode(request: SchedulerCandidateProposalRequest) -> str:
    return "dry_run" if request.dry_run else "confirmed"


__all__ = [
    "SCHEMA_VERSION",
    "SOURCE",
    "SchedulerCandidateProposalRequest",
    "candidate_proposal_safety",
    "create_scheduler_proposal_from_candidate",
    "propose_candidate_task",
]
