"""Level 7D task_key to draft PR pipeline composition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent_taskflow.one_shot_task_pipeline import (
    OneShotTaskPipelineRequest,
    run_one_shot_task_pipeline,
)
from agent_taskflow.pr_preparation_pipeline import (
    PRPreparationPipelineRequest,
    run_pr_preparation_pipeline,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key


TASK_TO_DRAFT_PR_PIPELINE_SCHEMA_VERSION = "task_to_draft_pr_pipeline.v1"
TASK_TO_DRAFT_PR_PIPELINE_SOURCE = "task_to_draft_pr_pipeline"

TASK_TO_DRAFT_PR_PIPELINE_SAFETY_FLAGS: dict[str, bool] = {
    "one_task_only": True,
    "operator_triggered": True,
    "single_use_enforced": True,
    "resume_already_processed": False,
    "duplicate_trigger_suppressed": False,
    "approved_task_runner_called": False,
    "github_mutated": False,
    "branch_pushed": False,
    "draft_pr_created": False,
    "approved": False,
    "merged": False,
    "cleanup_performed": False,
    "scheduler_loop_started": False,
    "background_worker_started": False,
    "automatic_task_picking_started": False,
    "human_review_required": True,
}

_STAGE_ONE_SHOT = "one_shot"
_STAGE_PR_PREPARATION = "pr_preparation"


class TaskToDraftPRPipelineError(RuntimeError):
    """Raised when the Level 7D coordinator cannot proceed safely."""


@dataclass(frozen=True)
class TaskToDraftPRPipelineRequest:
    """Inputs for the Level 7D task_key to draft PR pipeline."""

    db_path: Path
    artifact_root: Path
    task_key: str

    dry_run: bool = True

    confirm_run_one_shot_pipeline: bool = False
    resume_existing: bool = False
    resume_pr_preparation: bool = False

    confirm_prepare_pr: bool = False
    confirm_github_mutations: bool = False
    confirm_branch_push: bool = False
    confirm_draft_pr: bool = False

    operator: str | None = None
    operator_note: str | None = None

    proposal_max_items: int = 1
    recommended_command_kind: str | None = None

    remote: str = "origin"
    base_branch: str | None = None
    draft: bool = True

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

        for field_name in (
            "operator",
            "operator_note",
            "recommended_command_kind",
            "base_branch",
        ):
            value = getattr(self, field_name)
            if value is None:
                continue
            stripped = value.strip()
            object.__setattr__(self, field_name, stripped or None)

        remote = self.remote.strip()
        if not remote:
            raise ValueError("remote must not be empty")
        object.__setattr__(self, "remote", remote)


def run_task_to_draft_pr_pipeline(
    request: TaskToDraftPRPipelineRequest,
    *,
    approved_task_runner_fn: Callable[..., dict[str, Any]] | None = None,
    branch_push_fn: Callable[..., dict[str, Any]] | None = None,
    draft_pr_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the Level 7D single-task composition from task_key to draft PR."""

    if not request.draft:
        raise TaskToDraftPRPipelineError("Level 7D supports draft PR creation only")

    if request.dry_run:
        one_shot_preview = run_one_shot_task_pipeline(
            OneShotTaskPipelineRequest(
                db_path=request.db_path,
                artifact_root=request.artifact_root,
                task_key=request.task_key,
                dry_run=True,
                confirm_run_one_shot_pipeline=False,
                operator=request.operator,
                operator_note=request.operator_note,
                proposal_max_items=request.proposal_max_items,
                recommended_command_kind=request.recommended_command_kind,
                resume_existing=request.resume_existing,
            ),
            approved_task_runner_fn=approved_task_runner_fn,
        )
        pr_preparation_preview = run_pr_preparation_pipeline(
            PRPreparationPipelineRequest(
                db_path=request.db_path,
                artifact_root=request.artifact_root,
                task_key=request.task_key,
                dry_run=True,
                operator=request.operator,
                operator_note=request.operator_note,
                remote=request.remote,
                base_branch=request.base_branch,
                draft=True,
                resume_existing=request.resume_pr_preparation,
            ),
            branch_push_fn=branch_push_fn,
            draft_pr_fn=draft_pr_fn,
        )
        return _dry_run_response(
            request,
            one_shot_preview=one_shot_preview,
            pr_preparation_preview=pr_preparation_preview,
        )

    missing = _missing_confirmations(request)
    if missing:
        failed_stage = (
            _STAGE_ONE_SHOT
            if "--confirm-run-one-shot-pipeline" in missing
            else _STAGE_PR_PREPARATION
        )
        return _failure_response(
            request,
            failed_stage=failed_stage,
            reasons=[
                "confirmed task-to-draft-PR pipeline requires all confirmations: "
                + ", ".join(missing)
            ],
            stage_result={"missing_confirmations": missing},
            safety=_safety(dry_run=False),
        )

    one_shot_result = run_one_shot_task_pipeline(
        OneShotTaskPipelineRequest(
            db_path=request.db_path,
            artifact_root=request.artifact_root,
            task_key=request.task_key,
            dry_run=False,
            confirm_run_one_shot_pipeline=True,
            operator=request.operator,
            operator_note=request.operator_note,
            proposal_max_items=request.proposal_max_items,
            recommended_command_kind=request.recommended_command_kind,
            resume_existing=request.resume_existing,
        ),
        approved_task_runner_fn=approved_task_runner_fn,
    )
    if one_shot_result.get("ok") is not True:
        return _failure_response(
            request,
            failed_stage=_STAGE_ONE_SHOT,
            reasons=list(one_shot_result.get("reasons") or ["one_shot_failed"]),
            stage_result=one_shot_result,
            safety=_safety(
                dry_run=False,
                approved_task_runner_called=_one_shot_runner_called(one_shot_result),
            ),
        )

    final_task_status = _current_task_status(request)
    if final_task_status != "waiting_approval":
        return _failure_response(
            request,
            failed_stage=_STAGE_ONE_SHOT,
            reasons=[
                "task_status_not_ready_for_pr_preparation: "
                f"{final_task_status or 'missing'}"
            ],
            stage_result=one_shot_result,
            safety=_safety(
                dry_run=False,
                approved_task_runner_called=_one_shot_runner_called(one_shot_result),
            ),
        )

    pr_preparation_result = run_pr_preparation_pipeline(
        PRPreparationPipelineRequest(
            db_path=request.db_path,
            artifact_root=request.artifact_root,
            task_key=request.task_key,
            dry_run=False,
            confirm_prepare_pr=True,
            confirm_github_mutations=True,
            confirm_branch_push=True,
            confirm_draft_pr=True,
            operator=request.operator,
            operator_note=request.operator_note,
            remote=request.remote,
            base_branch=request.base_branch,
            draft=True,
            resume_existing=request.resume_pr_preparation,
        ),
        branch_push_fn=branch_push_fn,
        draft_pr_fn=draft_pr_fn,
    )
    if pr_preparation_result.get("ok") is not True:
        pr_safety = pr_preparation_result.get("safety") or {}
        return _failure_response(
            request,
            failed_stage=_STAGE_PR_PREPARATION,
            reasons=list(
                pr_preparation_result.get("reasons") or ["pr_preparation_failed"]
            ),
            stage_result=pr_preparation_result,
            safety=_safety(
                dry_run=False,
                approved_task_runner_called=_one_shot_runner_called(one_shot_result),
                github_mutated=bool(pr_safety.get("github_mutated")),
                branch_pushed=bool(pr_safety.get("branch_pushed")),
                draft_pr_created=bool(pr_safety.get("draft_pr_created")),
            ),
        )

    pr_safety = pr_preparation_result.get("safety") or {}
    status = str(pr_preparation_result.get("status") or "draft_pr_created")
    one_shot_summary = _one_shot_summary(one_shot_result)
    pr_preparation_summary = _pr_preparation_summary(pr_preparation_result)
    resume_already_processed = bool(
        one_shot_summary.get("already_executed")
        and pr_preparation_summary.get("draft_pr_already_created")
    )
    duplicate_trigger_suppressed = bool(
        resume_already_processed
        or pr_safety.get("duplicate_trigger_suppressed")
    )
    return {
        "ok": True,
        "schema_version": TASK_TO_DRAFT_PR_PIPELINE_SCHEMA_VERSION,
        "source": TASK_TO_DRAFT_PR_PIPELINE_SOURCE,
        "status": status,
        "mode": "confirmed",
        "task_key": request.task_key,
        "final_task_status": final_task_status,
        "single_use_enforced": True,
        "resume_already_processed": resume_already_processed,
        "duplicate_trigger_suppressed": duplicate_trigger_suppressed,
        "stages": {
            _STAGE_ONE_SHOT: one_shot_summary,
            _STAGE_PR_PREPARATION: pr_preparation_summary,
        },
        "safety": _safety(
            dry_run=False,
            approved_task_runner_called=_one_shot_runner_called(one_shot_result),
            github_mutated=bool(pr_safety.get("github_mutated", True)),
            branch_pushed=bool(pr_safety.get("branch_pushed", True)),
            draft_pr_created=bool(pr_safety.get("draft_pr_created", True)),
            resume_already_processed=resume_already_processed,
            duplicate_trigger_suppressed=duplicate_trigger_suppressed,
        ),
    }


def _missing_confirmations(request: TaskToDraftPRPipelineRequest) -> list[str]:
    missing: list[str] = []
    if not request.confirm_run_one_shot_pipeline:
        missing.append("--confirm-run-one-shot-pipeline")
    if not request.confirm_prepare_pr:
        missing.append("--confirm-prepare-pr")
    if not request.confirm_github_mutations:
        missing.append("--confirm-github-mutations")
    if not request.confirm_branch_push:
        missing.append("--confirm-branch-push")
    if not request.confirm_draft_pr:
        missing.append("--confirm-draft-pr")
    return missing


def _current_task_status(request: TaskToDraftPRPipelineRequest) -> str | None:
    if not request.db_path.exists():
        return None
    task = TaskMirrorStore(request.db_path).get_task(request.task_key)
    return task.status if task is not None else None


def _dry_run_response(
    request: TaskToDraftPRPipelineRequest,
    *,
    one_shot_preview: dict[str, Any],
    pr_preparation_preview: dict[str, Any],
) -> dict[str, Any]:
    one_shot_runtime = (one_shot_preview.get("stages") or {}).get(
        "runtime_execution"
    ) or {}
    pr_preparation_safety = pr_preparation_preview.get("safety") or {}
    return {
        "ok": True,
        "schema_version": TASK_TO_DRAFT_PR_PIPELINE_SCHEMA_VERSION,
        "source": TASK_TO_DRAFT_PR_PIPELINE_SOURCE,
        "status": "dry_run",
        "mode": "dry_run",
        "task_key": request.task_key,
        "would_run_task_to_draft_pr": True,
        "resume_existing": request.resume_existing,
        "resume_pr_preparation": request.resume_pr_preparation,
        "single_use_enforced": True,
        "resume_already_processed": False,
        "duplicate_trigger_suppressed": False,
        "stages": {
            _STAGE_ONE_SHOT: {
                "would_run": True,
                "would_call_approved_task_runner": bool(
                    one_shot_runtime.get("would_call_approved_task_runner", True)
                ),
            },
            _STAGE_PR_PREPARATION: {
                "would_prepare_pr": True,
                "would_push_branch": True,
                "would_create_draft_pr": True,
                "dry_run_checked": pr_preparation_preview.get("mode") == "dry_run",
                "ready_now": bool(pr_preparation_preview.get("ok")),
            },
        },
        "safety": _safety(
            dry_run=True,
            github_mutated=bool(pr_preparation_safety.get("github_mutated")),
            branch_pushed=bool(pr_preparation_safety.get("branch_pushed")),
            draft_pr_created=bool(pr_preparation_safety.get("draft_pr_created")),
        ),
    }


def _failure_response(
    request: TaskToDraftPRPipelineRequest,
    *,
    failed_stage: str,
    reasons: list[str],
    stage_result: dict[str, Any] | None,
    safety: dict[str, bool],
) -> dict[str, Any]:
    return {
        "ok": False,
        "schema_version": TASK_TO_DRAFT_PR_PIPELINE_SCHEMA_VERSION,
        "source": TASK_TO_DRAFT_PR_PIPELINE_SOURCE,
        "status": "failed",
        "mode": "dry_run" if request.dry_run else "confirmed",
        "failed_stage": failed_stage,
        "task_key": request.task_key,
        "reasons": _unique_strings([str(reason) for reason in reasons if reason]),
        "stage_result": stage_result,
        "safety": safety,
    }


def _one_shot_summary(result: dict[str, Any]) -> dict[str, Any]:
    stages = result.get("stages") or {}
    runtime_stage = (result.get("stages") or {}).get("runtime_execution") or {}
    already_executed = bool(
        result.get("status") == "already_executed"
        or runtime_stage.get("already_executed")
    )
    return {
        "ok": bool(result.get("ok")),
        "status": result.get("status"),
        "reused": already_executed,
        "already_executed": already_executed,
        "proposal_reused": bool((stages.get("proposal") or {}).get("reused")),
        "confirmation_reused": bool(
            (stages.get("confirmation") or {}).get("reused")
        ),
        "verifier_report_reused": bool(
            (stages.get("verifier_report") or {}).get("reused")
        ),
        "handoff_reused": bool((stages.get("handoff") or {}).get("reused")),
        "runtime_reused": bool(runtime_stage.get("reused")),
        "runtime_execution_id": runtime_stage.get("runtime_execution_id"),
        "runner_status": runtime_stage.get("runner_status"),
        "approved_task_runner_called": bool(
            runtime_stage.get("approved_task_runner_called")
        ),
    }


def _one_shot_runner_called(result: dict[str, Any]) -> bool:
    runtime_stage = (result.get("stages") or {}).get("runtime_execution") or {}
    return bool(runtime_stage.get("approved_task_runner_called"))


def _pr_preparation_summary(result: dict[str, Any]) -> dict[str, Any]:
    stages = result.get("stages") or {}
    handoff_stage = stages.get("pr_handoff") or {}
    branch_stage = stages.get("branch_push") or {}
    draft_stage = stages.get("draft_pr") or {}
    reused = bool(
        handoff_stage.get("reused")
        and branch_stage.get("reused")
        and draft_stage.get("reused")
    )
    return {
        "ok": bool(result.get("ok")),
        "status": result.get("status"),
        "reused": reused,
        "pr_handoff_reused": bool(handoff_stage.get("reused")),
        "branch_pushed": bool(branch_stage.get("pushed")),
        "draft_pr_created": bool(draft_stage.get("created")),
        "branch_push_reused": bool(branch_stage.get("reused")),
        "branch_push_already_pushed": bool(branch_stage.get("already_pushed")),
        "draft_pr_reused": bool(draft_stage.get("reused")),
        "draft_pr_already_created": bool(draft_stage.get("already_created")),
        "pr_url": draft_stage.get("pr_url"),
        "pr_number": draft_stage.get("pr_number"),
    }


def _safety(
    *,
    dry_run: bool,
    approved_task_runner_called: bool = False,
    github_mutated: bool = False,
    branch_pushed: bool = False,
    draft_pr_created: bool = False,
    resume_already_processed: bool = False,
    duplicate_trigger_suppressed: bool = False,
) -> dict[str, bool]:
    safety = dict(TASK_TO_DRAFT_PR_PIPELINE_SAFETY_FLAGS)
    safety["dry_run"] = dry_run
    safety["single_use_enforced"] = True
    safety["resume_already_processed"] = resume_already_processed
    safety["duplicate_trigger_suppressed"] = duplicate_trigger_suppressed
    safety["approved_task_runner_called"] = approved_task_runner_called
    safety["github_mutated"] = github_mutated
    safety["branch_pushed"] = branch_pushed
    safety["draft_pr_created"] = draft_pr_created
    safety["approved"] = False
    safety["merged"] = False
    safety["cleanup_performed"] = False
    safety["scheduler_loop_started"] = False
    safety["background_worker_started"] = False
    safety["automatic_task_picking_started"] = False
    safety["human_review_required"] = True
    return safety


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


__all__ = [
    "TASK_TO_DRAFT_PR_PIPELINE_SAFETY_FLAGS",
    "TASK_TO_DRAFT_PR_PIPELINE_SCHEMA_VERSION",
    "TASK_TO_DRAFT_PR_PIPELINE_SOURCE",
    "TaskToDraftPRPipelineError",
    "TaskToDraftPRPipelineRequest",
    "run_task_to_draft_pr_pipeline",
]
