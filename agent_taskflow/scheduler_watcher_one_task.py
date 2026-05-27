"""Level 8B one-task-at-a-time confirmed scheduler watcher.

This module composes the Level 8A scheduler watcher preview with the
Level 7D task-to-draft-PR pipeline to run exactly one selected candidate
through the existing task-to-draft-PR chain.

Level 8B is a one-shot watcher command, not a background daemon, not a
scheduler loop, not a cron job, not a webhook, and not a poller. It
processes at most one task per invocation and always requires an
explicit selection mode (an explicit ``task_key`` or a confirmed
first-candidate selection). It never silently picks a task.

It does not approve, merge, clean up, close out tasks, delete branches,
delete worktrees, expose any API surface, or interact with Mission
Control action UI. Human final review remains the gate after the draft
PR has been opened by the underlying pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent_taskflow.scheduler_watcher_preview import (
    SchedulerWatcherPreviewError,
    SchedulerWatcherPreviewRequest,
    build_scheduler_watcher_preview,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.task_to_draft_pr_pipeline import (
    TaskToDraftPRPipelineRequest,
    run_task_to_draft_pr_pipeline,
)
from agent_taskflow.tasks import normalize_task_key


WATCHER_ONE_TASK_SCHEMA_VERSION = "scheduler_watcher_one_task.v1"
WATCHER_ONE_TASK_SOURCE = "scheduler_watcher_one_task"

WATCHER_ONE_TASK_SAFETY_FLAGS: dict[str, bool] = {
    "one_task_only": True,
    "operator_triggered": True,
    "confirmed_watcher": True,
    "preview_only": False,
    "task_to_draft_pr_pipeline_called": False,
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
    "multi_task_batch_started": False,
    "human_review_required": True,
}

_FAILED_STAGE_PREVIEW = "preview"
_FAILED_STAGE_SELECTION = "selection"
_FAILED_STAGE_CONFIRMATION_FLAGS = "confirmation_flags"
_FAILED_STAGE_TASK_TO_DRAFT_PR = "task_to_draft_pr"


class SchedulerWatcherOneTaskError(RuntimeError):
    """Raised when the Level 8B watcher cannot proceed safely."""


@dataclass(frozen=True)
class SchedulerWatcherOneTaskRequest:
    """Inputs for the Level 8B one-task-at-a-time confirmed watcher."""

    db_path: Path
    artifact_root: Path

    dry_run: bool = True
    confirm_run_watcher_one_task: bool = False

    limit: int = 10
    project: str | None = None
    status: str | None = None
    recommended_command_kind: str | None = None

    task_key: str | None = None
    select_first_candidate: bool = False
    confirm_select_first_candidate: bool = False

    resume_existing: bool = True
    resume_pr_preparation: bool = True

    confirm_run_one_shot_pipeline: bool = False
    confirm_prepare_pr: bool = False
    confirm_github_mutations: bool = False
    confirm_branch_push: bool = False
    confirm_draft_pr: bool = False

    operator: str | None = None
    operator_note: str | None = None

    proposal_max_items: int = 1
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

        if self.limit < 0:
            raise ValueError("limit must be zero or positive")

        if self.proposal_max_items < 1:
            raise ValueError("proposal_max_items must be >= 1")

        for field_name in (
            "project",
            "status",
            "recommended_command_kind",
            "operator",
            "operator_note",
            "base_branch",
        ):
            value = getattr(self, field_name)
            if value is None:
                continue
            stripped = str(value).strip()
            object.__setattr__(self, field_name, stripped or None)

        if self.task_key is not None:
            stripped_key = str(self.task_key).strip()
            if not stripped_key:
                object.__setattr__(self, "task_key", None)
            else:
                object.__setattr__(self, "task_key", normalize_task_key(stripped_key))

        remote = self.remote.strip()
        if not remote:
            raise ValueError("remote must not be empty")
        object.__setattr__(self, "remote", remote)


def run_scheduler_watcher_one_task(
    request: SchedulerWatcherOneTaskRequest,
    *,
    approved_task_runner_fn: Callable[..., dict[str, Any]] | None = None,
    branch_push_fn: Callable[..., dict[str, Any]] | None = None,
    draft_pr_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the Level 8B one-task confirmed watcher."""

    if not request.draft:
        raise SchedulerWatcherOneTaskError(
            "Level 8B supports draft PR creation only"
        )

    try:
        preview = build_scheduler_watcher_preview(
            SchedulerWatcherPreviewRequest(
                db_path=request.db_path,
                limit=request.limit,
                project=request.project,
                status=request.status,
                recommended_command_kind=request.recommended_command_kind,
                operator=request.operator,
                operator_note=request.operator_note,
            )
        )
    except (ValueError, SchedulerWatcherPreviewError) as exc:
        return _failure_response(
            request,
            failed_stage=_FAILED_STAGE_PREVIEW,
            reasons=[f"watcher_preview_failed: {exc}"],
            preview=None,
            selected_candidate=None,
            stage_result=None,
        )

    candidates = list(preview.get("candidates") or [])
    skipped = list(preview.get("skipped") or [])
    candidate_count = len(candidates)

    if request.dry_run:
        return _dry_run_response(
            request,
            preview=preview,
            candidates=candidates,
            skipped=skipped,
            candidate_count=candidate_count,
        )

    if not request.confirm_run_watcher_one_task:
        return _failure_response(
            request,
            failed_stage=_FAILED_STAGE_CONFIRMATION_FLAGS,
            reasons=[
                "confirmed watcher requires --confirm-run-watcher-one-task"
            ],
            preview=preview,
            selected_candidate=None,
            stage_result=None,
        )

    selection = _select_candidate(request, candidates, skipped)
    if selection.get("ok") is not True:
        return _failure_response(
            request,
            failed_stage=_FAILED_STAGE_SELECTION,
            reasons=list(selection.get("reasons") or ["selection_failed"]),
            preview=preview,
            selected_candidate=selection.get("candidate"),
            stage_result=None,
        )

    selected_candidate: dict[str, Any] = selection["candidate"]
    selected_index: int = selection["index"]
    selected_task_key: str = selected_candidate["task_key"]

    missing = _missing_downstream_confirmations(request)
    if missing:
        return _failure_response(
            request,
            failed_stage=_FAILED_STAGE_CONFIRMATION_FLAGS,
            reasons=[
                "confirmed watcher requires all task-to-draft-PR confirmations: "
                + ", ".join(missing)
            ],
            preview=preview,
            selected_candidate=selected_candidate,
            stage_result={"missing_confirmations": missing},
        )

    pipeline_result = run_task_to_draft_pr_pipeline(
        TaskToDraftPRPipelineRequest(
            db_path=request.db_path,
            artifact_root=request.artifact_root,
            task_key=selected_task_key,
            dry_run=False,
            confirm_run_one_shot_pipeline=True,
            resume_existing=request.resume_existing,
            resume_pr_preparation=request.resume_pr_preparation,
            confirm_prepare_pr=True,
            confirm_github_mutations=True,
            confirm_branch_push=True,
            confirm_draft_pr=True,
            operator=request.operator,
            operator_note=request.operator_note,
            proposal_max_items=request.proposal_max_items,
            recommended_command_kind=request.recommended_command_kind,
            remote=request.remote,
            base_branch=request.base_branch,
            draft=True,
        ),
        approved_task_runner_fn=approved_task_runner_fn,
        branch_push_fn=branch_push_fn,
        draft_pr_fn=draft_pr_fn,
    )

    pipeline_safety = pipeline_result.get("safety") or {}
    if pipeline_result.get("ok") is not True:
        return _failure_response(
            request,
            failed_stage=_FAILED_STAGE_TASK_TO_DRAFT_PR,
            reasons=list(
                pipeline_result.get("reasons") or ["task_to_draft_pr_failed"]
            ),
            preview=preview,
            selected_candidate=selected_candidate,
            stage_result=pipeline_result,
            selected_index=selected_index,
            selected_task_key=selected_task_key,
            pipeline_safety=pipeline_safety,
        )

    pr_stage = (pipeline_result.get("stages") or {}).get("pr_preparation") or {}
    return {
        "ok": True,
        "schema_version": WATCHER_ONE_TASK_SCHEMA_VERSION,
        "source": WATCHER_ONE_TASK_SOURCE,
        "status": "completed_one_task",
        "mode": "confirmed",
        "selected_task_key": selected_task_key,
        "selected_candidate": selected_candidate,
        "preview": {
            "candidate_count": candidate_count,
            "selected_index": selected_index,
        },
        "task_to_draft_pr": {
            "ok": True,
            "status": pipeline_result.get("status"),
            "final_task_status": pipeline_result.get("final_task_status"),
            "pr_url": pr_stage.get("pr_url"),
            "pr_number": pr_stage.get("pr_number"),
        },
        "safety": _confirmed_safety(
            pipeline_called=True,
            processed_task_count=1,
            approved_task_runner_called=bool(
                pipeline_safety.get("approved_task_runner_called")
            ),
            github_mutated=bool(pipeline_safety.get("github_mutated")),
            branch_pushed=bool(pipeline_safety.get("branch_pushed")),
            draft_pr_created=bool(pipeline_safety.get("draft_pr_created")),
        ),
    }


def _select_candidate(
    request: SchedulerWatcherOneTaskRequest,
    candidates: list[dict[str, Any]],
    skipped: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    explicit_key = request.task_key
    first_mode = request.select_first_candidate
    skipped = skipped or []

    if explicit_key and first_mode:
        return {
            "ok": False,
            "reasons": ["ambiguous_selection_mode"],
            "candidate": None,
        }

    if not explicit_key and not first_mode:
        return {
            "ok": False,
            "reasons": ["selection_required"],
            "candidate": None,
        }

    if explicit_key:
        for index, candidate in enumerate(candidates):
            if str(candidate.get("task_key") or "") == explicit_key and bool(
                candidate.get("would_run")
            ):
                return {"ok": True, "candidate": candidate, "index": index}
        resume_candidate = _resume_already_processed_candidate(
            request, explicit_key, skipped
        )
        if resume_candidate is not None:
            return {"ok": True, "candidate": resume_candidate, "index": -1}
        return {
            "ok": False,
            "reasons": [f"task_key_not_in_preview: {explicit_key}"],
            "candidate": None,
        }

    if not request.confirm_select_first_candidate:
        return {
            "ok": False,
            "reasons": ["first_candidate_selection_not_confirmed"],
            "candidate": None,
        }

    if not candidates:
        return {
            "ok": False,
            "reasons": ["no_eligible_candidates"],
            "candidate": None,
        }

    candidate = candidates[0]
    if not bool(candidate.get("would_run")):
        return {
            "ok": False,
            "reasons": ["first_candidate_not_eligible"],
            "candidate": candidate,
        }

    return {"ok": True, "candidate": candidate, "index": 0}


def _resume_already_processed_candidate(
    request: SchedulerWatcherOneTaskRequest,
    explicit_key: str,
    skipped: list[dict[str, Any]],
) -> dict[str, Any] | None:
    resume_requested = bool(
        request.resume_existing or request.resume_pr_preparation
    )
    if not resume_requested:
        return None
    if not request.db_path.exists():
        return None
    store = TaskMirrorStore(request.db_path)
    task = store.get_task(explicit_key)
    if task is None or task.status != "waiting_approval":
        return None
    artifact_types = {
        artifact.artifact_type for artifact in store.list_task_artifacts(explicit_key)
    }
    if "draft_pr" not in artifact_types:
        return None
    skipped_reason: str | None = None
    for item in skipped:
        if str(item.get("task_key") or "") == explicit_key:
            skipped_reason = str(item.get("reason") or "") or None
            break
    return {
        "task_key": explicit_key,
        "status": task.status,
        "would_run": False,
        "reason": "resume_already_processed",
        "resume_via_skipped_preview": True,
        "skipped_reason": skipped_reason,
    }


def _missing_downstream_confirmations(
    request: SchedulerWatcherOneTaskRequest,
) -> list[str]:
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


def _dry_run_response(
    request: SchedulerWatcherOneTaskRequest,
    *,
    preview: dict[str, Any],
    candidates: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    candidate_count: int,
) -> dict[str, Any]:
    selection_preview = _select_candidate(request, candidates, skipped)
    selected_candidate = (
        selection_preview.get("candidate")
        if selection_preview.get("ok")
        else None
    )
    would_run_one_task = bool(selection_preview.get("ok"))
    return {
        "ok": True,
        "schema_version": WATCHER_ONE_TASK_SCHEMA_VERSION,
        "source": WATCHER_ONE_TASK_SOURCE,
        "status": "dry_run",
        "mode": "dry_run",
        "preview": preview,
        "selected_candidate": selected_candidate,
        "candidate_count": candidate_count,
        "would_run_one_task": would_run_one_task,
        "safety": _dry_run_safety(),
    }


def _failure_response(
    request: SchedulerWatcherOneTaskRequest,
    *,
    failed_stage: str,
    reasons: list[str],
    preview: dict[str, Any] | None,
    selected_candidate: dict[str, Any] | None,
    stage_result: dict[str, Any] | None,
    selected_index: int | None = None,
    selected_task_key: str | None = None,
    pipeline_safety: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pipeline_called = failed_stage == _FAILED_STAGE_TASK_TO_DRAFT_PR
    processed_task_count = 0
    pipeline_safety = pipeline_safety or {}
    if pipeline_called:
        approved_task_runner_called = bool(
            pipeline_safety.get("approved_task_runner_called")
        )
        github_mutated = bool(pipeline_safety.get("github_mutated"))
        branch_pushed = bool(pipeline_safety.get("branch_pushed"))
        draft_pr_created = bool(pipeline_safety.get("draft_pr_created"))
    else:
        approved_task_runner_called = False
        github_mutated = False
        branch_pushed = False
        draft_pr_created = False

    payload: dict[str, Any] = {
        "ok": False,
        "schema_version": WATCHER_ONE_TASK_SCHEMA_VERSION,
        "source": WATCHER_ONE_TASK_SOURCE,
        "status": "failed",
        "mode": "dry_run" if request.dry_run else "confirmed",
        "failed_stage": failed_stage,
        "reasons": _unique_strings([str(reason) for reason in reasons if reason]),
        "preview": preview,
        "selected_candidate": selected_candidate,
        "stage_result": stage_result,
        "safety": _confirmed_safety(
            pipeline_called=pipeline_called,
            processed_task_count=processed_task_count,
            approved_task_runner_called=approved_task_runner_called,
            github_mutated=github_mutated,
            branch_pushed=branch_pushed,
            draft_pr_created=draft_pr_created,
        ),
    }
    if selected_index is not None:
        payload["selected_index"] = selected_index
    if selected_task_key is not None:
        payload["selected_task_key"] = selected_task_key
    return payload


def _dry_run_safety() -> dict[str, bool]:
    safety = dict(WATCHER_ONE_TASK_SAFETY_FLAGS)
    safety["dry_run"] = True
    safety["preview_only"] = True
    safety["task_to_draft_pr_pipeline_called"] = False
    safety["approved_task_runner_called"] = False
    safety["github_mutated"] = False
    safety["branch_pushed"] = False
    safety["draft_pr_created"] = False
    safety["scheduler_loop_started"] = False
    safety["background_worker_started"] = False
    safety["automatic_task_picking_started"] = False
    safety["multi_task_batch_started"] = False
    safety["approved"] = False
    safety["merged"] = False
    safety["cleanup_performed"] = False
    safety["human_review_required"] = True
    return safety


def _confirmed_safety(
    *,
    pipeline_called: bool,
    processed_task_count: int,
    approved_task_runner_called: bool,
    github_mutated: bool,
    branch_pushed: bool,
    draft_pr_created: bool,
) -> dict[str, Any]:
    safety: dict[str, Any] = dict(WATCHER_ONE_TASK_SAFETY_FLAGS)
    safety["dry_run"] = False
    safety["preview_only"] = False
    safety["task_to_draft_pr_pipeline_called"] = pipeline_called
    safety["processed_task_count"] = processed_task_count
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
    safety["multi_task_batch_started"] = False
    safety["human_review_required"] = True
    return safety


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


__all__ = [
    "SchedulerWatcherOneTaskError",
    "SchedulerWatcherOneTaskRequest",
    "WATCHER_ONE_TASK_SAFETY_FLAGS",
    "WATCHER_ONE_TASK_SCHEMA_VERSION",
    "WATCHER_ONE_TASK_SOURCE",
    "run_scheduler_watcher_one_task",
]
