"""Level 7A task_key based one-shot pipeline.

This module is the explicit operator-triggered bridge that walks one
already-known ``task_key`` through the existing gated chain:

scheduler_proposal -> scheduler_confirmation ->
scheduler_confirmation_verifier_report -> intake_runner_handoff ->
runtime preflight -> approved_task_runner invocation ->
runtime_handoff_execution audit evidence.

It is intentionally thin: it composes the existing Level 2 / Level 3 /
Level 4A / Level 5A / Level 6A helpers without bypassing their dry-run
defaults, confirm flags, hash/binding checks, or duplicate detection.

Level 7A is not a scheduler loop, not a background worker, not a cron
job, not a webhook, not a poller, and not automatic task picking. It
does not ingest GitHub Issues, does not push branches, does not create
PRs, does not merge, does not approve, does not clean up, does not
touch Mission Control, and does not expose any API surface.

A one-shot pipeline invocation produces only the audit evidence each
underlying helper already records. Human review remains the final gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent_taskflow.intake_runner_handoff_from_verifier_report import (
    IntakeRunnerHandoffFromVerifierReportRequest,
    create_intake_runner_handoff_from_verifier_report,
)
from agent_taskflow.runtime_handoff_execution_from_handoff import (
    RuntimeHandoffExecutionRequest,
    run_runtime_handoff_execution_from_handoff,
)
from agent_taskflow.scheduler_confirmation_from_proposal import (
    SchedulerConfirmationFromProposalRequest,
    create_scheduler_confirmation_from_proposal,
)
from agent_taskflow.scheduler_confirmation_verifier_report import (
    SchedulerConfirmationVerifierReportRequest,
    create_scheduler_confirmation_verifier_report,
)
from agent_taskflow.scheduler_proposals import (
    SchedulerProposalRequest,
    create_scheduler_proposal,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key


ONE_SHOT_PIPELINE_SCHEMA_VERSION = "one_shot_task_pipeline.v1"
ONE_SHOT_PIPELINE_SOURCE = "one_shot_task_pipeline"


ONE_SHOT_PIPELINE_SAFETY_FLAGS: dict[str, bool] = {
    "one_task_only": True,
    "operator_triggered": True,
    "approved_task_runner_called": False,
    "scheduler_loop_started": False,
    "background_worker_started": False,
    "automatic_task_picking_started": False,
    "github_mutated": False,
    "approved": False,
    "merged": False,
    "cleanup_performed": False,
    "human_review_required": True,
}


_STAGE_PROPOSAL = "proposal"
_STAGE_CONFIRMATION = "confirmation"
_STAGE_VERIFIER_REPORT = "verifier_report"
_STAGE_HANDOFF = "handoff"
_STAGE_RUNTIME_EXECUTION = "runtime_execution"


class OneShotTaskPipelineError(RuntimeError):
    """Raised when the one-shot pipeline cannot proceed safely."""


@dataclass(frozen=True)
class OneShotTaskPipelineRequest:
    """Inputs to the Level 7A task_key based one-shot pipeline."""

    db_path: Path
    artifact_root: Path
    task_key: str
    dry_run: bool = True
    confirm_run_one_shot_pipeline: bool = False
    operator: str | None = None
    operator_note: str | None = None
    proposal_max_items: int = 1
    recommended_command_kind: str | None = None

    def __post_init__(self) -> None:
        db_path = Path(self.db_path).expanduser()
        if not db_path.is_absolute():
            raise ValueError("db_path must be an absolute path")
        object.__setattr__(self, "db_path", db_path)

        artifact_root = Path(self.artifact_root).expanduser()
        if not artifact_root.is_absolute():
            raise ValueError("artifact_root must be an absolute path")
        object.__setattr__(self, "artifact_root", artifact_root)

        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))

        if self.proposal_max_items < 1:
            raise ValueError("proposal_max_items must be >= 1")

        for field_name in ("operator", "operator_note", "recommended_command_kind"):
            value = getattr(self, field_name)
            if value is None:
                continue
            stripped = value.strip()
            object.__setattr__(self, field_name, stripped or None)


def run_one_shot_task_pipeline(
    request: OneShotTaskPipelineRequest,
    *,
    approved_task_runner_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the Level 7A task_key based one-shot pipeline for one task.

    Dry-run by default. Confirmed mode requires
    ``confirm_run_one_shot_pipeline=True``; either dry-run flag alone is
    rejected. Confirmed mode walks proposal -> confirmation -> verifier
    report -> handoff -> runtime preflight -> approved_task_runner via
    the existing helpers without bypassing their safety gates.
    """

    if not request.dry_run and not request.confirm_run_one_shot_pipeline:
        raise OneShotTaskPipelineError(
            "Non-dry-run one-shot task pipeline requires "
            "confirm_run_one_shot_pipeline=True"
        )

    if request.dry_run:
        return _dry_run_response(request)

    if not request.db_path.exists():
        return _failure_response(
            request,
            failed_stage=_STAGE_PROPOSAL,
            reasons=["task_missing: state DB not found"],
            stage_result=None,
        )

    store = TaskMirrorStore(request.db_path)
    task_record = store.get_task(request.task_key)
    if task_record is None:
        return _failure_response(
            request,
            failed_stage=_STAGE_PROPOSAL,
            reasons=["task_missing"],
            stage_result=None,
        )

    stages: dict[str, Any] = {}

    proposal_stage = _run_proposal_stage(request)
    stages[_STAGE_PROPOSAL] = proposal_stage["summary"]
    if not proposal_stage["ok"]:
        return _failure_response(
            request,
            failed_stage=_STAGE_PROPOSAL,
            reasons=proposal_stage["reasons"],
            stage_result=proposal_stage,
            stages=stages,
        )

    confirmation_stage = _run_confirmation_stage(request, proposal_stage)
    stages[_STAGE_CONFIRMATION] = confirmation_stage["summary"]
    if not confirmation_stage["ok"]:
        return _failure_response(
            request,
            failed_stage=_STAGE_CONFIRMATION,
            reasons=confirmation_stage["reasons"],
            stage_result=confirmation_stage,
            stages=stages,
        )

    verifier_stage = _run_verifier_report_stage(request, confirmation_stage)
    stages[_STAGE_VERIFIER_REPORT] = verifier_stage["summary"]
    if not verifier_stage["ok"]:
        return _failure_response(
            request,
            failed_stage=_STAGE_VERIFIER_REPORT,
            reasons=verifier_stage["reasons"],
            stage_result=verifier_stage,
            stages=stages,
        )

    handoff_stage = _run_handoff_stage(request, verifier_stage)
    stages[_STAGE_HANDOFF] = handoff_stage["summary"]
    if not handoff_stage["ok"]:
        return _failure_response(
            request,
            failed_stage=_STAGE_HANDOFF,
            reasons=handoff_stage["reasons"],
            stage_result=handoff_stage,
            stages=stages,
        )

    runtime_stage = _run_runtime_execution_stage(
        request,
        handoff_stage,
        approved_task_runner_fn=approved_task_runner_fn,
    )
    stages[_STAGE_RUNTIME_EXECUTION] = runtime_stage["summary"]
    if not runtime_stage["ok"]:
        return _failure_response(
            request,
            failed_stage=_STAGE_RUNTIME_EXECUTION,
            reasons=runtime_stage["reasons"],
            stage_result=runtime_stage,
            stages=stages,
        )

    final_task = store.get_task(request.task_key)
    final_status = final_task.status if final_task is not None else None

    return {
        "ok": True,
        "schema_version": ONE_SHOT_PIPELINE_SCHEMA_VERSION,
        "source": ONE_SHOT_PIPELINE_SOURCE,
        "status": "completed",
        "mode": "confirmed",
        "task_key": request.task_key,
        "final_task_status": final_status,
        "stages": stages,
        "safety": _confirmed_safety(approved_task_runner_called=True),
    }


# --------------------------------------------------------------------------
# Stage implementations
# --------------------------------------------------------------------------


def _run_proposal_stage(request: OneShotTaskPipelineRequest) -> dict[str, Any]:
    proposal_request = SchedulerProposalRequest(
        db_path=request.db_path,
        artifact_root=request.artifact_root,
        task_key=request.task_key,
        max_items=request.proposal_max_items,
        include_completed=False,
        include_no_action=False,
        include_unknown=False,
        dry_run=False,
        confirm_create_proposal=True,
    )
    try:
        proposal = create_scheduler_proposal(proposal_request)
    except Exception as exc:
        return _stage_failure(
            stage=_STAGE_PROPOSAL,
            reasons=[f"proposal_helper_error: {exc.__class__.__name__}: {exc}"],
            payload=None,
        )

    items = proposal.get("items") if isinstance(proposal, dict) else None
    if not isinstance(items, list) or not items:
        return _stage_failure(
            stage=_STAGE_PROPOSAL,
            reasons=["proposal_no_items_for_task_key"],
            payload=proposal,
        )

    matching = [
        item
        for item in items
        if isinstance(item, dict) and item.get("task_key") == request.task_key
    ]
    if not matching:
        return _stage_failure(
            stage=_STAGE_PROPOSAL,
            reasons=["proposal_no_items_for_task_key"],
            payload=proposal,
        )

    if request.recommended_command_kind is not None:
        matching = [
            item
            for item in matching
            if item.get("recommended_command_kind")
            == request.recommended_command_kind
        ]
        if not matching:
            return _stage_failure(
                stage=_STAGE_PROPOSAL,
                reasons=["proposal_recommended_command_kind_not_found"],
                payload=proposal,
            )

    if len(matching) != 1:
        return _stage_failure(
            stage=_STAGE_PROPOSAL,
            reasons=["proposal_item_ambiguous"],
            payload=proposal,
        )

    selected = matching[0]
    artifact_path = proposal.get("artifact_path")
    summary = {
        "created": True,
        "proposal_id": proposal.get("proposal_id"),
        "proposal_hash": proposal.get("proposal_hash"),
        "proposal_item_id": selected.get("proposal_item_id"),
        "item_hash": selected.get("item_hash"),
        "recommended_command_kind": selected.get("recommended_command_kind"),
        "artifact_path": artifact_path,
    }
    return {
        "ok": True,
        "stage": _STAGE_PROPOSAL,
        "summary": summary,
        "proposal": proposal,
        "selected_item": selected,
        "artifact_path": artifact_path,
        "expected_status": selected.get("expected_status") or selected.get("status"),
        "reasons": [],
    }


def _run_confirmation_stage(
    request: OneShotTaskPipelineRequest,
    proposal_stage: dict[str, Any],
) -> dict[str, Any]:
    summary = proposal_stage["summary"]
    artifact_path = (
        Path(str(summary["artifact_path"])) if summary.get("artifact_path") else None
    )
    confirmation_request = SchedulerConfirmationFromProposalRequest(
        db_path=request.db_path,
        artifact_root=request.artifact_root,
        task_key=request.task_key,
        proposal_item_id=str(summary["proposal_item_id"]),
        proposal_hash=str(summary["proposal_hash"]),
        proposal_id=str(summary["proposal_id"]),
        item_hash=str(summary["item_hash"]),
        recommended_command_kind=str(summary["recommended_command_kind"]),
        expected_status=proposal_stage.get("expected_status"),
        proposal_artifact_path=artifact_path,
        dry_run=False,
        confirm_create_confirmation=True,
        operator=request.operator,
        operator_note=request.operator_note,
    )
    try:
        result = create_scheduler_confirmation_from_proposal(confirmation_request)
    except Exception as exc:
        return _stage_failure(
            stage=_STAGE_CONFIRMATION,
            reasons=[f"confirmation_helper_error: {exc.__class__.__name__}: {exc}"],
            payload=None,
        )

    if not result.get("ok"):
        return _stage_failure(
            stage=_STAGE_CONFIRMATION,
            reasons=list(result.get("reasons") or ["confirmation_not_ok"]),
            payload=result,
        )

    confirmation = result.get("confirmation") or {}
    confirmation_artifact_path = confirmation.get("artifact_path")
    stage_summary = {
        "created": True,
        "confirmation_id": confirmation.get("confirmation_id"),
        "artifact_path": confirmation_artifact_path,
    }
    return {
        "ok": True,
        "stage": _STAGE_CONFIRMATION,
        "summary": stage_summary,
        "confirmation": confirmation,
        "result": result,
        "reasons": [],
    }


def _run_verifier_report_stage(
    request: OneShotTaskPipelineRequest,
    confirmation_stage: dict[str, Any],
) -> dict[str, Any]:
    confirmation = confirmation_stage["confirmation"]
    artifact_path = (
        Path(str(confirmation["artifact_path"])) if confirmation.get("artifact_path") else None
    )
    verifier_request = SchedulerConfirmationVerifierReportRequest(
        db_path=request.db_path,
        artifact_root=request.artifact_root,
        task_key=request.task_key,
        confirmation_id=str(confirmation["confirmation_id"]),
        proposal_hash=str(confirmation["proposal_hash"]),
        proposal_item_id=str(confirmation["proposal_item_id"]),
        item_hash=str(confirmation["item_hash"]),
        recommended_command_kind=str(confirmation["recommended_command_kind"]),
        confirmation_artifact_path=artifact_path,
        dry_run=False,
        confirm_create_verifier_report=True,
        operator=request.operator,
        operator_note=request.operator_note,
    )
    try:
        result = create_scheduler_confirmation_verifier_report(verifier_request)
    except Exception as exc:
        return _stage_failure(
            stage=_STAGE_VERIFIER_REPORT,
            reasons=[
                f"verifier_report_helper_error: {exc.__class__.__name__}: {exc}"
            ],
            payload=None,
        )

    if not result.get("ok"):
        return _stage_failure(
            stage=_STAGE_VERIFIER_REPORT,
            reasons=list(result.get("reasons") or ["verifier_report_not_ok"]),
            payload=result,
        )

    verifier_report = result.get("verifier_report") or {}
    stage_summary = {
        "created": True,
        "verifier_report_id": verifier_report.get("verifier_report_id"),
        "artifact_path": verifier_report.get("artifact_path"),
    }
    return {
        "ok": True,
        "stage": _STAGE_VERIFIER_REPORT,
        "summary": stage_summary,
        "verifier_report": verifier_report,
        "result": result,
        "reasons": [],
    }


def _run_handoff_stage(
    request: OneShotTaskPipelineRequest,
    verifier_stage: dict[str, Any],
) -> dict[str, Any]:
    verifier_report = verifier_stage["verifier_report"]
    artifact_path = (
        Path(str(verifier_report["artifact_path"]))
        if verifier_report.get("artifact_path")
        else None
    )
    handoff_request = IntakeRunnerHandoffFromVerifierReportRequest(
        db_path=request.db_path,
        artifact_root=request.artifact_root,
        task_key=request.task_key,
        verifier_report_id=str(verifier_report["verifier_report_id"]),
        confirmation_id=str(verifier_report["confirmation_id"]),
        proposal_hash=str(verifier_report["proposal_hash"]),
        proposal_item_id=str(verifier_report["proposal_item_id"]),
        item_hash=str(verifier_report["item_hash"]),
        recommended_command_kind=str(verifier_report["recommended_command_kind"]),
        verifier_report_artifact_path=artifact_path,
        dry_run=False,
        confirm_create_handoff=True,
        operator=request.operator,
        operator_note=request.operator_note,
    )
    try:
        result = create_intake_runner_handoff_from_verifier_report(handoff_request)
    except Exception as exc:
        return _stage_failure(
            stage=_STAGE_HANDOFF,
            reasons=[f"handoff_helper_error: {exc.__class__.__name__}: {exc}"],
            payload=None,
        )

    if not result.get("ok"):
        return _stage_failure(
            stage=_STAGE_HANDOFF,
            reasons=list(result.get("reasons") or ["handoff_not_ok"]),
            payload=result,
        )

    handoff = result.get("handoff") or {}
    stage_summary = {
        "created": True,
        "handoff_id": handoff.get("handoff_id"),
        "artifact_path": handoff.get("artifact_path"),
    }
    return {
        "ok": True,
        "stage": _STAGE_HANDOFF,
        "summary": stage_summary,
        "handoff": handoff,
        "result": result,
        "reasons": [],
    }


def _run_runtime_execution_stage(
    request: OneShotTaskPipelineRequest,
    handoff_stage: dict[str, Any],
    *,
    approved_task_runner_fn: Callable[..., dict[str, Any]] | None,
) -> dict[str, Any]:
    handoff = handoff_stage["handoff"]
    artifact_path = (
        Path(str(handoff["artifact_path"])) if handoff.get("artifact_path") else None
    )
    runtime_request = RuntimeHandoffExecutionRequest(
        db_path=request.db_path,
        artifact_root=request.artifact_root,
        task_key=request.task_key,
        handoff_id=str(handoff["handoff_id"]),
        verifier_report_id=str(handoff["verifier_report_id"]),
        confirmation_id=str(handoff["confirmation_id"]),
        proposal_hash=str(handoff["proposal_hash"]),
        proposal_item_id=str(handoff["proposal_item_id"]),
        item_hash=str(handoff["item_hash"]),
        recommended_command_kind=str(handoff["recommended_command_kind"]),
        handoff_artifact_path=artifact_path,
        dry_run=False,
        confirm_run_approved_task_runner=True,
        operator=request.operator,
        operator_note=request.operator_note,
    )
    try:
        result = run_runtime_handoff_execution_from_handoff(
            runtime_request,
            approved_task_runner_fn=approved_task_runner_fn,
        )
    except Exception as exc:
        return _stage_failure(
            stage=_STAGE_RUNTIME_EXECUTION,
            reasons=[
                f"runtime_execution_helper_error: {exc.__class__.__name__}: {exc}"
            ],
            payload=None,
        )

    if not result.get("ok"):
        return _stage_failure(
            stage=_STAGE_RUNTIME_EXECUTION,
            reasons=list(result.get("reasons") or ["runtime_execution_not_ok"]),
            payload=result,
        )

    runtime_execution = result.get("runtime_execution") or {}
    stage_summary = {
        "created": True,
        "runtime_execution_id": runtime_execution.get("runtime_execution_id"),
        "artifact_path": runtime_execution.get("artifact_path"),
        "approved_task_runner_called": runtime_execution.get(
            "approved_task_runner_called"
        ),
        "runner_ok": runtime_execution.get("runner_ok"),
        "runner_status": runtime_execution.get("runner_status"),
        "runner_phase": runtime_execution.get("runner_phase"),
    }
    return {
        "ok": True,
        "stage": _STAGE_RUNTIME_EXECUTION,
        "summary": stage_summary,
        "runtime_execution": runtime_execution,
        "result": result,
        "reasons": [],
    }


# --------------------------------------------------------------------------
# Response builders
# --------------------------------------------------------------------------


def _dry_run_response(request: OneShotTaskPipelineRequest) -> dict[str, Any]:
    return {
        "ok": True,
        "schema_version": ONE_SHOT_PIPELINE_SCHEMA_VERSION,
        "source": ONE_SHOT_PIPELINE_SOURCE,
        "status": "dry_run",
        "mode": "dry_run",
        "task_key": request.task_key,
        "would_run_pipeline": True,
        "stages": {
            _STAGE_PROPOSAL: {
                "would_create": True,
                "proposal_max_items": request.proposal_max_items,
                "recommended_command_kind": request.recommended_command_kind,
            },
            _STAGE_CONFIRMATION: {"would_create": True},
            _STAGE_VERIFIER_REPORT: {"would_create": True},
            _STAGE_HANDOFF: {"would_create": True},
            _STAGE_RUNTIME_EXECUTION: {
                "would_call_approved_task_runner": True,
            },
        },
        "safety": _dry_run_safety(),
    }


def _failure_response(
    request: OneShotTaskPipelineRequest,
    *,
    failed_stage: str,
    reasons: list[str],
    stage_result: dict[str, Any] | None,
    stages: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "schema_version": ONE_SHOT_PIPELINE_SCHEMA_VERSION,
        "source": ONE_SHOT_PIPELINE_SOURCE,
        "status": "failed",
        "mode": "confirmed",
        "task_key": request.task_key,
        "failed_stage": failed_stage,
        "reasons": list(reasons),
        "stage_result": stage_result,
        "stages": stages or {},
        "safety": _confirmed_safety(approved_task_runner_called=False),
    }


def _stage_failure(
    *,
    stage: str,
    reasons: list[str],
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "stage": stage,
        "summary": {"created": False, "reasons": list(reasons)},
        "reasons": list(reasons),
        "payload": payload,
    }


def _dry_run_safety() -> dict[str, bool]:
    safety = dict(ONE_SHOT_PIPELINE_SAFETY_FLAGS)
    safety["dry_run"] = True
    safety["approved_task_runner_called"] = False
    safety["runtime_started"] = False
    return safety


def _confirmed_safety(*, approved_task_runner_called: bool) -> dict[str, bool]:
    safety = dict(ONE_SHOT_PIPELINE_SAFETY_FLAGS)
    safety["approved_task_runner_called"] = approved_task_runner_called
    safety["runtime_started"] = approved_task_runner_called
    return safety


__all__ = [
    "ONE_SHOT_PIPELINE_SAFETY_FLAGS",
    "ONE_SHOT_PIPELINE_SCHEMA_VERSION",
    "ONE_SHOT_PIPELINE_SOURCE",
    "OneShotTaskPipelineError",
    "OneShotTaskPipelineRequest",
    "run_one_shot_task_pipeline",
]
