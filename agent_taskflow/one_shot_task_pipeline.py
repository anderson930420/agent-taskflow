"""Level 7B task_key based one-shot pipeline.

This module is the explicit operator-triggered bridge that walks one
already-known ``task_key`` through the existing gated chain:

scheduler_proposal -> scheduler_confirmation ->
scheduler_confirmation_verifier_report -> intake_runner_handoff ->
runtime preflight -> approved_task_runner invocation ->
runtime_handoff_execution audit evidence.

It is intentionally thin: it composes the existing Level 2 / Level 3 /
Level 4A / Level 5A / Level 6A helpers without bypassing their dry-run
defaults, confirm flags, hash/binding checks, or duplicate detection.
When explicitly requested, it can resume by reusing valid matching
evidence that was already recorded by those helpers. Existing runtime
execution evidence is never silently rerun.

Level 7B is not a scheduler loop, not a background worker, not a cron
job, not a webhook, not a poller, and not automatic task picking. It
does not ingest GitHub Issues, does not push branches, does not create
PRs, does not merge, does not approve, does not clean up, does not
touch Mission Control, and does not expose any API surface.

A one-shot pipeline invocation produces only the audit evidence each
underlying helper already records. Human review remains the final gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent_taskflow.intake_runner_handoff_from_verifier_report import (
    HANDOFF_ARTIFACT_TYPE,
    HANDOFF_EVENT_TYPE,
    IntakeRunnerHandoffFromVerifierReportRequest,
    check_intake_runner_handoff_binding,
    create_intake_runner_handoff_from_verifier_report,
)
from agent_taskflow.runtime_handoff_execution_from_handoff import (
    RUNTIME_EXECUTION_ARTIFACT_TYPE,
    RUNTIME_FINISHED_EVENT_TYPE,
    RUNTIME_PREFLIGHT_EVENT_TYPE,
    RUNTIME_STARTED_EVENT_TYPE,
    RuntimeHandoffExecutionRequest,
    check_runtime_handoff_preflight,
    run_runtime_handoff_execution_from_handoff,
)
from agent_taskflow.scheduler_confirmation_from_proposal import (
    SchedulerConfirmationFromProposalRequest,
    create_scheduler_confirmation_from_proposal,
)
from agent_taskflow.scheduler_confirmation_readback import (
    list_task_scheduler_confirmation_readbacks,
)
from agent_taskflow.scheduler_confirmation_verifier_report import (
    VERIFIER_REPORT_ARTIFACT_TYPE,
    VERIFIER_REPORT_EVENT_TYPE,
    SchedulerConfirmationVerifierReportRequest,
    check_scheduler_confirmation_verifier_binding,
    create_scheduler_confirmation_verifier_report,
)
from agent_taskflow.scheduler_proposal_readback import (
    list_task_scheduler_proposal_readbacks,
)
from agent_taskflow.scheduler_proposals import (
    SchedulerProposalRequest,
    create_scheduler_proposal,
    verify_proposal_hashes,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key


ONE_SHOT_PIPELINE_SCHEMA_VERSION = "one_shot_task_pipeline.v1"
ONE_SHOT_PIPELINE_SOURCE = "one_shot_task_pipeline"


ONE_SHOT_PIPELINE_SAFETY_FLAGS: dict[str, bool] = {
    "one_task_only": True,
    "operator_triggered": True,
    "approved_task_runner_called": False,
    "runtime_rerun_allowed": False,
    "runtime_rerun_performed": False,
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
    resume_existing: bool = False
    allow_runtime_rerun: bool = False

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
    if request.allow_runtime_rerun:
        raise OneShotTaskPipelineError(
            "Level 7B does not allow runtime rerun; existing runtime evidence "
            "must be reviewed instead"
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

    if runtime_stage.get("already_executed") is True:
        return _already_executed_response(
            request,
            final_status=final_status,
            stages=stages,
        )

    return {
        "ok": True,
        "schema_version": ONE_SHOT_PIPELINE_SCHEMA_VERSION,
        "source": ONE_SHOT_PIPELINE_SOURCE,
        "status": "completed",
        "mode": "confirmed",
        "task_key": request.task_key,
        "resume_existing": request.resume_existing,
        "final_task_status": final_status,
        "stages": stages,
        "safety": _confirmed_safety(approved_task_runner_called=True),
    }


# --------------------------------------------------------------------------
# Stage implementations
# --------------------------------------------------------------------------


def _run_proposal_stage(request: OneShotTaskPipelineRequest) -> dict[str, Any]:
    if request.resume_existing:
        store = TaskMirrorStore(request.db_path)
        reusable = _find_reusable_proposal(request, store)
        if reusable["found"]:
            return reusable

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
        "id": proposal.get("proposal_id"),
        "created": True,
        "reused": False,
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
    if request.resume_existing:
        store = TaskMirrorStore(request.db_path)
        reusable = _find_reusable_confirmation(request, store, proposal_stage)
        if reusable["found"]:
            return reusable

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
        "id": confirmation.get("confirmation_id"),
        "created": True,
        "reused": False,
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
    if request.resume_existing:
        store = TaskMirrorStore(request.db_path)
        reusable = _find_reusable_verifier_report(
            request,
            store,
            confirmation_stage,
        )
        if reusable["found"]:
            return reusable

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
        "id": verifier_report.get("verifier_report_id"),
        "created": True,
        "reused": False,
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
    if request.resume_existing:
        store = TaskMirrorStore(request.db_path)
        reusable = _find_reusable_handoff(request, store, verifier_stage)
        if reusable["found"]:
            return reusable

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
        "id": handoff.get("handoff_id"),
        "created": True,
        "reused": False,
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
    store = TaskMirrorStore(request.db_path)
    existing_runtime = _find_existing_runtime_execution(request, store, handoff_stage)
    if existing_runtime["found"]:
        return existing_runtime

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
        "id": runtime_execution.get("runtime_execution_id"),
        "created": True,
        "reused": False,
        "already_executed": False,
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
# Resume helpers
# --------------------------------------------------------------------------


def _find_reusable_proposal(
    request: OneShotTaskPipelineRequest,
    store: TaskMirrorStore,
) -> dict[str, Any]:
    try:
        readback = list_task_scheduler_proposal_readbacks(store, request.task_key)
    except Exception as exc:
        return _stage_failure(
            stage=_STAGE_PROPOSAL,
            reasons=[f"proposal_readback_error: {exc.__class__.__name__}: {exc}"],
            payload=None,
        ) | {"found": True}

    candidates = [
        item
        for item in readback.get("items") or []
        if isinstance(item, dict)
        and item.get("task_key") == request.task_key
        and (
            request.recommended_command_kind is None
            or item.get("recommended_command_kind")
            == request.recommended_command_kind
        )
    ]
    if not candidates:
        return {"found": False}

    selected = _latest_readback_item(candidates)
    warnings = list(selected.get("readback_warnings") or [])
    missing = list(selected.get("missing_evidence") or [])
    artifact_payload, load_reasons = _load_json_artifact(
        selected.get("artifact_path"),
        missing_path_reason="proposal_artifact_path_missing",
        missing_file_reason="proposal_artifact_file_missing",
        malformed_reason="proposal_artifact_json_malformed",
        not_object_reason="proposal_artifact_json_not_object",
    )
    reasons = [*warnings, *missing, *load_reasons]
    artifact_item: dict[str, Any] | None = None

    if artifact_payload is not None:
        hash_report = verify_proposal_hashes(artifact_payload)
        if hash_report.get("proposal_hash_valid") is not True:
            reasons.append("proposal_hash_mismatch")
        if hash_report.get("actual_proposal_hash") != selected.get("proposal_hash"):
            reasons.append("proposal_hash_mismatch")

        artifact_item = _find_proposal_item(
            artifact_payload,
            str(selected.get("proposal_item_id") or ""),
        )
        if artifact_item is None:
            reasons.append("proposal_item_id_missing_from_artifact")
        else:
            item_hash_report = _find_item_hash_report(
                hash_report,
                str(selected.get("proposal_item_id") or ""),
            )
            if (
                item_hash_report is None
                or item_hash_report.get("item_hash_valid") is not True
                or item_hash_report.get("actual_item_hash")
                != selected.get("item_hash")
                or artifact_item.get("item_hash") != selected.get("item_hash")
            ):
                reasons.append("item_hash_mismatch")
            if artifact_item.get("task_key") != request.task_key:
                reasons.append("proposal_task_key_mismatch")
            if (
                request.recommended_command_kind is not None
                and artifact_item.get("recommended_command_kind")
                != request.recommended_command_kind
            ):
                reasons.append("recommended_command_kind_mismatch")
            safety = artifact_item.get("safety_flags")
            if not isinstance(safety, dict) or safety.get("will_execute") is not False:
                reasons.append("proposal_item_safety_flags_invalid")

    unique_reasons = _unique_strings(reasons)
    if unique_reasons:
        return _stage_failure(
            stage=_STAGE_PROPOSAL,
            reasons=unique_reasons,
            payload={"readback_item": selected},
        ) | {"found": True}

    assert artifact_item is not None
    summary = {
        "id": selected.get("proposal_id"),
        "created": False,
        "reused": True,
        "proposal_id": selected.get("proposal_id"),
        "proposal_hash": selected.get("proposal_hash"),
        "proposal_item_id": selected.get("proposal_item_id"),
        "item_hash": selected.get("item_hash"),
        "recommended_command_kind": selected.get("recommended_command_kind"),
        "artifact_path": selected.get("artifact_path"),
    }
    return {
        "found": True,
        "ok": True,
        "stage": _STAGE_PROPOSAL,
        "summary": summary,
        "proposal": artifact_payload,
        "selected_item": artifact_item,
        "artifact_path": selected.get("artifact_path"),
        "expected_status": artifact_item.get("expected_status")
        or artifact_item.get("status"),
        "reasons": [],
    }


def _find_reusable_confirmation(
    request: OneShotTaskPipelineRequest,
    store: TaskMirrorStore,
    proposal_stage: dict[str, Any],
) -> dict[str, Any]:
    proposal_summary = proposal_stage["summary"]
    try:
        readback = list_task_scheduler_confirmation_readbacks(store, request.task_key)
    except Exception as exc:
        return _stage_failure(
            stage=_STAGE_CONFIRMATION,
            reasons=[
                f"confirmation_readback_error: {exc.__class__.__name__}: {exc}"
            ],
            payload=None,
        ) | {"found": True}

    candidates = [
        item
        for item in readback.get("items") or []
        if isinstance(item, dict)
        and item.get("proposal_hash") == proposal_summary.get("proposal_hash")
        and item.get("proposal_item_id")
        == proposal_summary.get("proposal_item_id")
        and item.get("item_hash") == proposal_summary.get("item_hash")
        and (
            proposal_summary.get("recommended_command_kind") is None
            or item.get("recommended_command_kind")
            == proposal_summary.get("recommended_command_kind")
        )
    ]
    if not candidates:
        return {"found": False}

    selected = _latest_readback_item(candidates)
    confirmation, reasons = _load_and_validate_confirmation(selected)
    if reasons:
        return _stage_failure(
            stage=_STAGE_CONFIRMATION,
            reasons=reasons,
            payload={"readback_item": selected},
        ) | {"found": True}

    summary = {
        "id": confirmation.get("confirmation_id"),
        "created": False,
        "reused": True,
        "confirmation_id": confirmation.get("confirmation_id"),
        "artifact_path": confirmation.get("artifact_path"),
    }
    return {
        "found": True,
        "ok": True,
        "stage": _STAGE_CONFIRMATION,
        "summary": summary,
        "confirmation": confirmation,
        "result": {"status": "reused"},
        "reasons": [],
    }


def _find_reusable_verifier_report(
    request: OneShotTaskPipelineRequest,
    store: TaskMirrorStore,
    confirmation_stage: dict[str, Any],
) -> dict[str, Any]:
    confirmation = confirmation_stage["confirmation"]
    bindings = {
        "confirmation_id": confirmation.get("confirmation_id"),
        "proposal_hash": confirmation.get("proposal_hash"),
        "proposal_item_id": confirmation.get("proposal_item_id"),
        "item_hash": confirmation.get("item_hash"),
    }
    candidates, candidate_errors = _matching_artifact_candidates(
        store,
        request.task_key,
        artifact_type=VERIFIER_REPORT_ARTIFACT_TYPE,
        event_type=VERIFIER_REPORT_EVENT_TYPE,
        bindings=bindings,
    )
    if candidate_errors:
        return _stage_failure(
            stage=_STAGE_VERIFIER_REPORT,
            reasons=candidate_errors,
            payload=None,
        ) | {"found": True}
    if not candidates:
        return {"found": False}

    selected = _latest_candidate(candidates)
    verifier_report = selected["payload"]
    binding_request = SchedulerConfirmationVerifierReportRequest(
        db_path=request.db_path,
        artifact_root=request.artifact_root,
        task_key=request.task_key,
        confirmation_id=str(verifier_report["confirmation_id"]),
        proposal_hash=str(verifier_report["proposal_hash"]),
        proposal_item_id=str(verifier_report["proposal_item_id"]),
        item_hash=str(verifier_report["item_hash"]),
        recommended_command_kind=str(verifier_report["recommended_command_kind"]),
        confirmation_artifact_path=Path(str(verifier_report["confirmation_artifact_path"])),
        dry_run=True,
        confirm_create_verifier_report=False,
        operator=request.operator,
        operator_note=request.operator_note,
    )
    binding = check_scheduler_confirmation_verifier_binding(binding_request)
    reasons = _verifier_report_reuse_reasons(verifier_report, selected, binding)
    if reasons:
        return _stage_failure(
            stage=_STAGE_VERIFIER_REPORT,
            reasons=reasons,
            payload={"verifier_report": verifier_report, "binding": binding},
        ) | {"found": True}

    summary = {
        "id": verifier_report.get("verifier_report_id"),
        "created": False,
        "reused": True,
        "verifier_report_id": verifier_report.get("verifier_report_id"),
        "artifact_path": verifier_report.get("artifact_path"),
    }
    return {
        "found": True,
        "ok": True,
        "stage": _STAGE_VERIFIER_REPORT,
        "summary": summary,
        "verifier_report": verifier_report,
        "result": {"status": "reused", "binding": binding},
        "reasons": [],
    }


def _find_reusable_handoff(
    request: OneShotTaskPipelineRequest,
    store: TaskMirrorStore,
    verifier_stage: dict[str, Any],
) -> dict[str, Any]:
    verifier_report = verifier_stage["verifier_report"]
    bindings = {
        "verifier_report_id": verifier_report.get("verifier_report_id"),
        "confirmation_id": verifier_report.get("confirmation_id"),
        "proposal_hash": verifier_report.get("proposal_hash"),
        "proposal_item_id": verifier_report.get("proposal_item_id"),
        "item_hash": verifier_report.get("item_hash"),
    }
    candidates, candidate_errors = _matching_artifact_candidates(
        store,
        request.task_key,
        artifact_type=HANDOFF_ARTIFACT_TYPE,
        event_type=HANDOFF_EVENT_TYPE,
        bindings=bindings,
    )
    if candidate_errors:
        return _stage_failure(
            stage=_STAGE_HANDOFF,
            reasons=candidate_errors,
            payload=None,
        ) | {"found": True}
    if not candidates:
        return {"found": False}

    selected = _latest_candidate(candidates)
    handoff = selected["payload"]
    binding_request = IntakeRunnerHandoffFromVerifierReportRequest(
        db_path=request.db_path,
        artifact_root=request.artifact_root,
        task_key=request.task_key,
        verifier_report_id=str(handoff["verifier_report_id"]),
        confirmation_id=str(handoff["confirmation_id"]),
        proposal_hash=str(handoff["proposal_hash"]),
        proposal_item_id=str(handoff["proposal_item_id"]),
        item_hash=str(handoff["item_hash"]),
        recommended_command_kind=str(handoff["recommended_command_kind"]),
        verifier_report_artifact_path=Path(str(handoff["verifier_report_artifact_path"])),
        dry_run=True,
        confirm_create_handoff=False,
        operator=request.operator,
        operator_note=request.operator_note,
    )
    binding = check_intake_runner_handoff_binding(binding_request)
    reasons = _handoff_reuse_reasons(handoff, selected, binding)
    if reasons:
        return _stage_failure(
            stage=_STAGE_HANDOFF,
            reasons=reasons,
            payload={"handoff": handoff, "binding": binding},
        ) | {"found": True}

    summary = {
        "id": handoff.get("handoff_id"),
        "created": False,
        "reused": True,
        "handoff_id": handoff.get("handoff_id"),
        "artifact_path": handoff.get("artifact_path"),
    }
    return {
        "found": True,
        "ok": True,
        "stage": _STAGE_HANDOFF,
        "summary": summary,
        "handoff": handoff,
        "result": {"status": "reused", "binding": binding},
        "reasons": [],
    }


def _find_existing_runtime_execution(
    request: OneShotTaskPipelineRequest,
    store: TaskMirrorStore,
    handoff_stage: dict[str, Any],
) -> dict[str, Any]:
    handoff = handoff_stage["handoff"]
    bindings = {
        "handoff_id": handoff.get("handoff_id"),
        "verifier_report_id": handoff.get("verifier_report_id"),
        "confirmation_id": handoff.get("confirmation_id"),
        "proposal_hash": handoff.get("proposal_hash"),
        "proposal_item_id": handoff.get("proposal_item_id"),
        "item_hash": handoff.get("item_hash"),
    }
    candidates, candidate_errors = _matching_artifact_candidates(
        store,
        request.task_key,
        artifact_type=RUNTIME_EXECUTION_ARTIFACT_TYPE,
        event_type=RUNTIME_FINISHED_EVENT_TYPE,
        bindings=bindings,
    )
    if candidate_errors:
        return _stage_failure(
            stage=_STAGE_RUNTIME_EXECUTION,
            reasons=candidate_errors,
            payload=None,
        ) | {"found": True}
    if not candidates:
        return {"found": False}

    if len(candidates) > 1:
        return _stage_failure(
            stage=_STAGE_RUNTIME_EXECUTION,
            reasons=["runtime_execution_ambiguous"],
            payload={"candidate_count": len(candidates)},
        ) | {"found": True}

    selected = candidates[0]
    runtime_execution = selected["payload"]
    preflight_request = RuntimeHandoffExecutionRequest(
        db_path=request.db_path,
        artifact_root=request.artifact_root,
        task_key=request.task_key,
        handoff_id=str(runtime_execution["handoff_id"]),
        verifier_report_id=str(runtime_execution["verifier_report_id"]),
        confirmation_id=str(runtime_execution["confirmation_id"]),
        proposal_hash=str(runtime_execution["proposal_hash"]),
        proposal_item_id=str(runtime_execution["proposal_item_id"]),
        item_hash=str(runtime_execution["item_hash"]),
        recommended_command_kind=str(runtime_execution["recommended_command_kind"]),
        handoff_artifact_path=Path(str(runtime_execution["handoff_artifact_path"])),
        dry_run=True,
        confirm_run_approved_task_runner=False,
        operator=request.operator,
        operator_note=request.operator_note,
    )
    preflight = check_runtime_handoff_preflight(preflight_request)
    reasons = _runtime_reuse_reasons(store, request.task_key, runtime_execution, selected, preflight)
    if reasons:
        return _stage_failure(
            stage=_STAGE_RUNTIME_EXECUTION,
            reasons=reasons,
            payload={"runtime_execution": runtime_execution, "preflight": preflight},
        ) | {"found": True}

    summary = {
        "id": runtime_execution.get("runtime_execution_id"),
        "created": False,
        "reused": True,
        "already_executed": True,
        "runtime_execution_id": runtime_execution.get("runtime_execution_id"),
        "artifact_path": runtime_execution.get("artifact_path"),
        "approved_task_runner_called": False,
        "recorded_approved_task_runner_called": runtime_execution.get(
            "approved_task_runner_called"
        ),
        "runner_ok": runtime_execution.get("runner_ok"),
        "runner_status": runtime_execution.get("runner_status"),
        "runner_phase": runtime_execution.get("runner_phase"),
    }
    return {
        "found": True,
        "ok": True,
        "stage": _STAGE_RUNTIME_EXECUTION,
        "already_executed": True,
        "summary": summary,
        "runtime_execution": runtime_execution,
        "result": {"status": "already_executed", "preflight": preflight},
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
        "resume_existing": request.resume_existing,
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
        "resume_existing": request.resume_existing,
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
        "summary": {"created": False, "reused": False, "reasons": list(reasons)},
        "reasons": list(reasons),
        "payload": payload,
    }


def _dry_run_safety() -> dict[str, bool]:
    safety = dict(ONE_SHOT_PIPELINE_SAFETY_FLAGS)
    safety["dry_run"] = True
    safety["approved_task_runner_called"] = False
    safety["runtime_started"] = False
    safety["runtime_rerun_allowed"] = False
    safety["runtime_rerun_performed"] = False
    return safety


def _confirmed_safety(*, approved_task_runner_called: bool) -> dict[str, bool]:
    safety = dict(ONE_SHOT_PIPELINE_SAFETY_FLAGS)
    safety["approved_task_runner_called"] = approved_task_runner_called
    safety["runtime_started"] = approved_task_runner_called
    safety["runtime_rerun_allowed"] = False
    safety["runtime_rerun_performed"] = False
    return safety


def _already_executed_response(
    request: OneShotTaskPipelineRequest,
    *,
    final_status: str | None,
    stages: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ok": True,
        "schema_version": ONE_SHOT_PIPELINE_SCHEMA_VERSION,
        "source": ONE_SHOT_PIPELINE_SOURCE,
        "status": "already_executed",
        "mode": "confirmed",
        "task_key": request.task_key,
        "resume_existing": request.resume_existing,
        "final_task_status": final_status,
        "stages": stages,
        "safety": _confirmed_safety(approved_task_runner_called=False),
    }


# --------------------------------------------------------------------------
# Generic evidence helpers
# --------------------------------------------------------------------------


def _latest_readback_item(items: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(items, key=_readback_sort_key)[-1]


def _readback_sort_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _string_value(item.get("event_created_at"))
        or _string_value(item.get("artifact_created_at"))
        or "",
        _string_value(item.get("artifact_path")) or "",
        _string_value(item.get("id")) or "",
    )


def _latest_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(candidates, key=_candidate_sort_key)[-1]


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[str, str]:
    return (
        _string_value(candidate.get("created_at")) or "",
        _string_value(candidate.get("artifact_path")) or "",
    )


def _load_json_artifact(
    artifact_path: Any,
    *,
    missing_path_reason: str,
    missing_file_reason: str,
    malformed_reason: str,
    not_object_reason: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    path_text = _string_value(artifact_path)
    if path_text is None:
        return None, [missing_path_reason]
    try:
        text = Path(path_text).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None, [missing_file_reason]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, [malformed_reason]
    if not isinstance(payload, dict):
        return None, [not_object_reason]
    return payload, []


def _load_event_payload(payload_json: str | None) -> dict[str, Any] | None:
    if not payload_json:
        return None
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _matching_artifact_candidates(
    store: TaskMirrorStore,
    task_key: str,
    *,
    artifact_type: str,
    event_type: str,
    bindings: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    event_payloads = [
        payload
        for event in store.list_task_events(task_key)
        if event.event_type == event_type
        for payload in [_load_event_payload(event.payload_json)]
        if payload is not None
    ]
    matching_events = [
        payload for payload in event_payloads if _payload_matches(payload, bindings)
    ]

    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    for artifact in store.list_task_artifacts(task_key):
        if artifact.artifact_type != artifact_type:
            continue
        payload, reasons = _load_json_artifact(
            artifact.path,
            missing_path_reason=f"{artifact_type}_artifact_path_missing",
            missing_file_reason=f"{artifact_type}_artifact_file_missing",
            malformed_reason=f"{artifact_type}_artifact_json_malformed",
            not_object_reason=f"{artifact_type}_artifact_json_not_object",
        )
        if payload is None:
            if any(
                _payload_matches(event_payload, bindings)
                and _event_artifact_path(event_payload) == str(artifact.path)
                for event_payload in matching_events
            ):
                errors.extend(reasons)
            continue
        if not _payload_matches(payload, bindings):
            continue
        artifact_path = _string_value(payload.get("artifact_path")) or str(artifact.path)
        event_matches = [
            event_payload
            for event_payload in matching_events
            if _event_artifact_path(event_payload) == artifact_path
        ]
        if not event_matches:
            errors.append(f"{artifact_type}_event_missing")
            continue
        candidates.append(
            {
                "payload": payload,
                "artifact_path": artifact_path,
                "created_at": artifact.created_at,
                "event_payloads": event_matches,
            }
        )

    if matching_events and not candidates and not errors:
        errors.append(f"{artifact_type}_artifact_missing")
    return candidates, _unique_strings(errors)


def _event_artifact_path(payload: dict[str, Any]) -> str | None:
    return _string_value(payload.get("artifact_path")) or _string_value(
        payload.get("runtime_execution_artifact_path")
    )


def _payload_matches(payload: dict[str, Any], bindings: dict[str, Any]) -> bool:
    for key, expected in bindings.items():
        expected_text = _string_value(expected)
        if expected_text is None:
            return False
        if _string_value(payload.get(key)) != expected_text:
            return False
    return True


def _load_and_validate_confirmation(
    selected: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    reasons = [
        *list(selected.get("readback_warnings") or []),
        *list(selected.get("missing_evidence") or []),
    ]
    confirmation, load_reasons = _load_json_artifact(
        selected.get("artifact_path"),
        missing_path_reason="confirmation_artifact_path_missing",
        missing_file_reason="confirmation_artifact_file_missing",
        malformed_reason="confirmation_artifact_json_malformed",
        not_object_reason="confirmation_artifact_json_not_object",
    )
    reasons.extend(load_reasons)
    if confirmation is None:
        return {}, _unique_strings(reasons)

    for key in (
        "confirmation_id",
        "proposal_hash",
        "proposal_item_id",
        "item_hash",
        "recommended_command_kind",
        "artifact_path",
    ):
        if _string_value(confirmation.get(key)) != _string_value(selected.get(key)):
            reasons.append(f"{key}_mismatch")

    for flag in (
        "not_execution_permission",
        "not_verifier_report",
        "not_handoff",
        "not_runtime",
        "requires_next_gate",
    ):
        if confirmation.get(flag) is not True:
            reasons.append("confirmation_safety_flags_invalid")
            break

    proposal_payload, proposal_reasons = _load_json_artifact(
        confirmation.get("proposal_artifact_path"),
        missing_path_reason="proposal_artifact_path_missing",
        missing_file_reason="proposal_artifact_file_missing",
        malformed_reason="proposal_artifact_json_malformed",
        not_object_reason="proposal_artifact_json_not_object",
    )
    reasons.extend(proposal_reasons)
    if proposal_payload is not None:
        reasons.extend(
            _proposal_binding_reasons(
                proposal_payload,
                proposal_hash=confirmation.get("proposal_hash"),
                proposal_item_id=confirmation.get("proposal_item_id"),
                item_hash=confirmation.get("item_hash"),
                recommended_command_kind=confirmation.get(
                    "recommended_command_kind"
                ),
            )
        )
    return confirmation, _unique_strings(reasons)


def _proposal_binding_reasons(
    proposal_payload: dict[str, Any],
    *,
    proposal_hash: Any,
    proposal_item_id: Any,
    item_hash: Any,
    recommended_command_kind: Any,
) -> list[str]:
    reasons: list[str] = []
    hash_report = verify_proposal_hashes(proposal_payload)
    if (
        hash_report.get("proposal_hash_valid") is not True
        or hash_report.get("actual_proposal_hash") != proposal_hash
    ):
        reasons.append("proposal_hash_mismatch")

    proposal_item = _find_proposal_item(
        proposal_payload,
        _string_value(proposal_item_id) or "",
    )
    if proposal_item is None:
        reasons.append("proposal_item_id_missing_from_artifact")
        return reasons

    item_report = _find_item_hash_report(
        hash_report,
        _string_value(proposal_item_id) or "",
    )
    if (
        item_report is None
        or item_report.get("item_hash_valid") is not True
        or item_report.get("actual_item_hash") != item_hash
        or proposal_item.get("item_hash") != item_hash
    ):
        reasons.append("item_hash_mismatch")
    if proposal_item.get("recommended_command_kind") != recommended_command_kind:
        reasons.append("recommended_command_kind_mismatch")
    return reasons


def _verifier_report_reuse_reasons(
    verifier_report: dict[str, Any],
    selected: dict[str, Any],
    binding: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    if verifier_report.get("verification_passed") is not True:
        reasons.append("verifier_report_failed")
    if verifier_report.get("not_execution_permission") is not True:
        reasons.append("verifier_report_safety_flags_invalid")
    if verifier_report.get("not_runtime") is not True:
        reasons.append("verifier_report_safety_flags_invalid")
    if verifier_report.get("requires_next_gate") is not True:
        reasons.append("verifier_report_safety_flags_invalid")
    if _string_value(verifier_report.get("artifact_path")) != _string_value(
        selected.get("artifact_path")
    ):
        reasons.append("verifier_report_artifact_path_mismatch")
    reasons.extend(
        _binding_reasons_ignoring_duplicates(
            binding,
            duplicate_check_name="duplicate_verifier_report_absent",
            duplicate_reason="duplicate_active_verifier_report",
            ignored_reasons={"task_status_mismatch"},
            ignored_checks={"task_status_matches_expected"},
        )
    )
    return _unique_strings(reasons)


def _handoff_reuse_reasons(
    handoff: dict[str, Any],
    selected: dict[str, Any],
    binding: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    if handoff.get("handoff_allowed") is not True:
        reasons.append("handoff_not_allowed")
    for key, expected in (
        ("not_execution_permission", True),
        ("not_runtime", True),
        ("approved_task_runner_called", False),
        ("requires_runtime_preflight", True),
        ("requires_next_gate", True),
    ):
        if handoff.get(key) is not expected:
            reasons.append("handoff_safety_flags_invalid")
            break
    if _string_value(handoff.get("artifact_path")) != _string_value(
        selected.get("artifact_path")
    ):
        reasons.append("handoff_artifact_path_mismatch")
    reasons.extend(
        _binding_reasons_ignoring_duplicates(
            binding,
            duplicate_check_name="duplicate_handoff_absent",
            duplicate_reason="duplicate_active_handoff",
            ignored_reasons={"task_status_mismatch"},
            ignored_checks={"task_status_matches_expected"},
        )
    )
    return _unique_strings(reasons)


def _runtime_reuse_reasons(
    store: TaskMirrorStore,
    task_key: str,
    runtime_execution: dict[str, Any],
    selected: dict[str, Any],
    preflight: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    for key, expected in (
        ("preflight_passed", True),
        ("approved_task_runner_called", True),
        ("not_approval", True),
        ("not_merge", True),
        ("not_cleanup", True),
    ):
        if runtime_execution.get(key) is not expected:
            reasons.append("runtime_execution_safety_flags_invalid")
            break
    if _string_value(runtime_execution.get("artifact_path")) != _string_value(
        selected.get("artifact_path")
    ):
        reasons.append("runtime_execution_artifact_path_mismatch")
    reasons.extend(
        _binding_reasons_ignoring_duplicates(
            preflight,
            duplicate_check_name="duplicate_runtime_execution_absent",
            duplicate_reason="duplicate_runtime_execution",
            ignored_reasons={"task_status_mismatch"},
            ignored_checks={"task_status_matches_expected"},
        )
    )
    reasons.extend(_runtime_audit_event_reasons(store, task_key, runtime_execution))
    return _unique_strings(reasons)


def _binding_reasons_ignoring_duplicates(
    binding: dict[str, Any],
    *,
    duplicate_check_name: str,
    duplicate_reason: str,
    ignored_reasons: set[str] | None = None,
    ignored_checks: set[str] | None = None,
) -> list[str]:
    ignored_reasons = set(ignored_reasons or set()) | {duplicate_reason}
    ignored_checks = set(ignored_checks or set()) | {duplicate_check_name}
    reasons = [
        reason
        for reason in list(binding.get("reasons") or [])
        if reason not in ignored_reasons
    ]
    checks = dict(binding.get("checks") or {})
    for name, passed in checks.items():
        if name in ignored_checks:
            continue
        if passed is not True:
            reasons.append(f"{name}_failed")
    return _unique_strings(reasons)


def _runtime_audit_event_reasons(
    store: TaskMirrorStore,
    task_key: str,
    runtime_execution: dict[str, Any],
) -> list[str]:
    runtime_execution_id = runtime_execution.get("runtime_execution_id")
    expected = {
        RUNTIME_PREFLIGHT_EVENT_TYPE: False,
        RUNTIME_STARTED_EVENT_TYPE: False,
        RUNTIME_FINISHED_EVENT_TYPE: False,
    }
    for event in store.list_task_events(task_key):
        if event.event_type not in expected:
            continue
        payload = _load_event_payload(event.payload_json)
        if not payload:
            continue
        if payload.get("runtime_execution_id") == runtime_execution_id:
            expected[event.event_type] = True
    return [
        f"{event_type}_missing"
        for event_type, present in expected.items()
        if not present
    ]


def _find_proposal_item(
    proposal_payload: dict[str, Any],
    proposal_item_id: str,
) -> dict[str, Any] | None:
    for item in proposal_payload.get("items") or []:
        if isinstance(item, dict) and item.get("proposal_item_id") == proposal_item_id:
            return item
    return None


def _find_item_hash_report(
    hash_report: dict[str, Any],
    proposal_item_id: str,
) -> dict[str, Any] | None:
    for item in hash_report.get("items") or []:
        if isinstance(item, dict) and item.get("proposal_item_id") == proposal_item_id:
            return item
    return None


def _unique_strings(values: list[Any]) -> list[str]:
    return [str(value) for value in dict.fromkeys(values) if str(value)]


def _string_value(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, Path):
        return str(value)
    return None


__all__ = [
    "ONE_SHOT_PIPELINE_SAFETY_FLAGS",
    "ONE_SHOT_PIPELINE_SCHEMA_VERSION",
    "ONE_SHOT_PIPELINE_SOURCE",
    "OneShotTaskPipelineError",
    "OneShotTaskPipelineRequest",
    "run_one_shot_task_pipeline",
]
