"""Level 8A scheduler watcher dry-run candidate preview.

This module is a read-only preview surface. It scans scheduler candidate
discovery output and reports which tasks would be eligible for a future,
operator-confirmed task-to-draft-PR automation path.

It never runs tasks, never calls one-shot or task-to-draft-PR pipelines, never
invokes approved_task_runner, never writes artifacts/events, and never mutates
GitHub.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_taskflow.scheduler_candidate_discovery import (
    SchedulerCandidateDiscoveryError,
    SchedulerCandidateDiscoveryRequest,
    discover_scheduler_candidates,
)
from agent_taskflow.store import TaskMirrorStore


WATCHER_PREVIEW_SCHEMA_VERSION = "scheduler_watcher_preview.v1"
WATCHER_PREVIEW_SOURCE = "scheduler_watcher_preview"

WATCHER_PREVIEW_SAFETY_FLAGS: dict[str, bool] = {
    "dry_run_preview": True,
    "read_only": True,
    "task_execution_started": False,
    "one_shot_pipeline_called": False,
    "task_to_draft_pr_pipeline_called": False,
    "approved_task_runner_called": False,
    "executor_started": False,
    "validators_started": False,
    "github_mutated": False,
    "branch_pushed": False,
    "draft_pr_created": False,
    "approved": False,
    "merged": False,
    "cleanup_performed": False,
    "scheduler_loop_started": False,
    "background_worker_started": False,
    "automatic_task_picking_started": False,
}

REQUIRED_OPERATOR_FLAGS: tuple[str, ...] = (
    "--confirm-run-one-shot-pipeline",
    "--confirm-prepare-pr",
    "--confirm-github-mutations",
    "--confirm-branch-push",
    "--confirm-draft-pr",
)

RUNNABLE_STATUSES: frozenset[str] = frozenset({"queued"})
WAITING_APPROVAL_STATUS = "waiting_approval"
BLOCKED_STATUS = "blocked"
COMPLETED_OR_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"accepted", "cleaned", "completed", "canceled", "done"}
)

QUEUED_SUPPORTED_COMMAND_KINDS: frozenset[str] = frozenset(
    {"create_task_execution_package", "queued_task_handoff"}
)
WAITING_SUPPORTED_COMMAND_KINDS: frozenset[str] = frozenset(
    {"pr_handoff_package", "branch_push_review", "draft_pr_review"}
)
NO_ACTION_COMMAND_KINDS: frozenset[str] = frozenset(
    {"no_action", "human_pr_review"}
)
TERMINAL_COMMAND_KINDS: frozenset[str] = frozenset(
    {"cleanup_continue", "post_merge_cleanup_review"}
)
TERMINAL_ARTIFACT_TYPES: frozenset[str] = frozenset(
    {"draft_pr", "local_cleanup", "remote_branch_cleanup", "task_closeout"}
)


class SchedulerWatcherPreviewError(RuntimeError):
    """Raised when the watcher preview cannot read scheduler candidates safely."""


@dataclass(frozen=True)
class SchedulerWatcherPreviewRequest:
    """Inputs for the Level 8A dry-run watcher preview."""

    db_path: Path
    limit: int = 10
    project: str | None = None
    status: str | None = None
    recommended_command_kind: str | None = None
    include_blocked: bool = False
    include_waiting_approval: bool = False
    include_completed: bool = False
    include_no_action: bool = False
    operator: str | None = None
    operator_note: str | None = None

    def __post_init__(self) -> None:
        db_path = Path(self.db_path).expanduser()
        if not db_path.is_absolute():
            raise ValueError("db_path must be an absolute path")
        object.__setattr__(self, "db_path", db_path)

        if self.limit < 0:
            raise ValueError("limit must be zero or positive")

        for field_name in (
            "project",
            "status",
            "recommended_command_kind",
            "operator",
            "operator_note",
        ):
            value = getattr(self, field_name)
            if value is None:
                continue
            stripped = str(value).strip()
            object.__setattr__(self, field_name, stripped or None)


def build_scheduler_watcher_preview(
    request: SchedulerWatcherPreviewRequest,
) -> dict[str, Any]:
    """Build a read-only watcher preview for future automation candidates."""

    # The store is intentionally instantiated only; preview reads go through
    # existing read-only scheduler candidate discovery.
    TaskMirrorStore(request.db_path)

    try:
        discovery = discover_scheduler_candidates(
            SchedulerCandidateDiscoveryRequest(
                db_path=request.db_path,
                project=request.project,
                status=request.status,
                include_not_ready=True,
                include_no_action=True,
                limit=None,
                completed_limit=1000,
            )
        )
    except (ValueError, SchedulerCandidateDiscoveryError) as exc:
        raise SchedulerWatcherPreviewError(
            f"could not build scheduler watcher preview: {exc}"
        ) from exc

    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    blocked_backlog: list[dict[str, Any]] = []
    summary = _empty_summary()

    for item in discovery.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        decision = _classify_item(item, request)
        if decision["would_run"]:
            if len(candidates) < request.limit:
                candidates.append(_candidate_item(item, decision["reason"]))
            else:
                skipped.append(
                    _skipped_item(
                        item,
                        reason="not_ready",
                        warnings=["candidate limit reached"],
                    )
                )
                summary["not_ready_count"] += 1
        else:
            skipped.append(
                _skipped_item(
                    item,
                    reason=decision["reason"],
                    warnings=decision["warnings"],
                )
            )
            if item.get("status") == BLOCKED_STATUS:
                blocked_backlog.append(_blocked_backlog_item(item))
            _increment_skip_summary(summary, decision["reason"])

    summary["would_run_count"] = len(candidates)
    summary["blocked_backlog_count"] = len(blocked_backlog)

    return {
        "ok": True,
        "schema_version": WATCHER_PREVIEW_SCHEMA_VERSION,
        "source": WATCHER_PREVIEW_SOURCE,
        "mode": "dry_run_preview",
        "db_path": str(request.db_path),
        "filters": _filters(request),
        "candidate_count": len(candidates),
        "skipped_count": len(skipped),
        "blocked_backlog_count": len(blocked_backlog),
        "candidates": candidates,
        "skipped": skipped,
        "blocked_backlog": blocked_backlog,
        "summary": summary,
        "safety": dict(WATCHER_PREVIEW_SAFETY_FLAGS),
    }


def _classify_item(
    item: dict[str, Any],
    request: SchedulerWatcherPreviewRequest,
) -> dict[str, Any]:
    warnings = list(item.get("consistency_warnings") or [])
    status = str(item.get("status") or "")
    kind = str(item.get("recommended_command_kind") or "")

    if request.recommended_command_kind and kind != request.recommended_command_kind:
        return {
            "would_run": False,
            "reason": "unsupported_command_kind",
            "warnings": warnings
            + [
                "recommended_command_kind filter did not match: "
                f"{request.recommended_command_kind}"
            ],
        }

    if _missing_required_metadata(item):
        return {
            "would_run": False,
            "reason": "missing_metadata",
            "warnings": warnings + ["task_key, project, and title are required"],
        }

    if status == BLOCKED_STATUS:
        if not request.include_blocked:
            return {"would_run": False, "reason": "blocked", "warnings": warnings}
        return {
            "would_run": False,
            "reason": "unsupported_command_kind",
            "warnings": warnings + ["blocked tasks are never executable preview items"],
        }

    if status == WAITING_APPROVAL_STATUS and not request.include_waiting_approval:
        return {
            "would_run": False,
            "reason": "waiting_approval",
            "warnings": warnings,
        }

    if status in COMPLETED_OR_TERMINAL_STATUSES and not request.include_completed:
        return {"would_run": False, "reason": "completed", "warnings": warnings}

    if kind in NO_ACTION_COMMAND_KINDS:
        if not request.include_no_action:
            return {"would_run": False, "reason": "no_action", "warnings": warnings}
        return {
            "would_run": False,
            "reason": "no_action",
            "warnings": warnings + ["no_action recommendations are never executed"],
        }

    if kind in TERMINAL_COMMAND_KINDS or _has_terminal_artifact(item):
        return {
            "would_run": False,
            "reason": "no_action",
            "warnings": warnings + ["terminal PR, approval, merge, or cleanup state detected"],
        }

    if status in RUNNABLE_STATUSES and kind in QUEUED_SUPPORTED_COMMAND_KINDS:
        return {
            "would_run": True,
            "reason": "queued task is eligible for future task-to-draft-PR automation",
            "warnings": warnings,
        }

    if (
        status == WAITING_APPROVAL_STATUS
        and request.include_waiting_approval
        and kind in WAITING_SUPPORTED_COMMAND_KINDS
    ):
        return {
            "would_run": True,
            "reason": "waiting_approval task is eligible for future PR preparation automation",
            "warnings": warnings,
        }

    if kind not in QUEUED_SUPPORTED_COMMAND_KINDS | WAITING_SUPPORTED_COMMAND_KINDS:
        return {
            "would_run": False,
            "reason": "unsupported_command_kind",
            "warnings": warnings,
        }

    return {"would_run": False, "reason": "not_ready", "warnings": warnings}


def _candidate_item(item: dict[str, Any], reason: str) -> dict[str, Any]:
    task_key = str(item.get("task_key") or "")
    return {
        "task_key": task_key,
        "project": item.get("project"),
        "title": item.get("title"),
        "status": item.get("status"),
        "recommended_command_kind": item.get("recommended_command_kind"),
        "would_run": True,
        "would_run_pipeline": "task_to_draft_pr",
        "reason": reason,
        "required_operator_flags": list(REQUIRED_OPERATOR_FLAGS),
        "suggested_command": _suggested_command(task_key),
        "suggested_command_executed": False,
        "safety": {
            "preview_only": True,
            "would_mutate_if_confirmed_later": True,
            "executed_now": False,
            "github_mutated_now": False,
        },
    }


def _skipped_item(
    item: dict[str, Any],
    *,
    reason: str,
    warnings: list[str],
) -> dict[str, Any]:
    payload = {
        "task_key": item.get("task_key"),
        "status": item.get("status"),
        "would_run": False,
        "reason": reason,
        "warnings": warnings,
    }
    blocked_reason = item.get("blocked_reason")
    if blocked_reason:
        payload["blocked_reason"] = blocked_reason
    return payload


def _blocked_backlog_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_key": item.get("task_key"),
        "project": item.get("project"),
        "title": item.get("title"),
        "status": item.get("status"),
        "recommended_command_kind": item.get("recommended_command_kind"),
        "blocked_reason": item.get("blocked_reason"),
        "would_run": False,
        "reason": "blocked",
        "required_operator_action": "inspect_manually",
        "recovery_hint": (
            "Inspect the blocker, repair the underlying issue, then use an "
            "explicit operator workflow to move the task out of blocked state."
        ),
        "safety": {
            "preview_only": True,
            "executed_now": False,
            "status_changed_now": False,
            "github_mutated_now": False,
        },
    }


def _filters(request: SchedulerWatcherPreviewRequest) -> dict[str, Any]:
    return {
        "limit": request.limit,
        "project": request.project,
        "status": request.status,
        "recommended_command_kind": request.recommended_command_kind,
        "include_blocked": request.include_blocked,
        "include_waiting_approval": request.include_waiting_approval,
        "include_completed": request.include_completed,
        "include_no_action": request.include_no_action,
        "operator": request.operator,
        "operator_note": request.operator_note,
    }


def _empty_summary() -> dict[str, Any]:
    return {
        "would_run_count": 0,
        "blocked_count": 0,
        "blocked_backlog_count": 0,
        "waiting_approval_count": 0,
        "completed_count": 0,
        "not_ready_count": 0,
        "no_action_count": 0,
        "unsupported_command_kind_count": 0,
        "missing_metadata_count": 0,
        "execution_started": False,
        "github_mutated": False,
    }


def _increment_skip_summary(summary: dict[str, Any], reason: str) -> None:
    key_by_reason = {
        "blocked": "blocked_count",
        "waiting_approval": "waiting_approval_count",
        "completed": "completed_count",
        "not_ready": "not_ready_count",
        "no_action": "no_action_count",
        "unsupported_command_kind": "unsupported_command_kind_count",
        "missing_metadata": "missing_metadata_count",
    }
    key = key_by_reason.get(reason, "not_ready_count")
    summary[key] += 1


def _missing_required_metadata(item: dict[str, Any]) -> bool:
    for key in ("task_key", "project", "title"):
        value = item.get(key)
        if value is None or not str(value).strip():
            return True
    return False


def _has_terminal_artifact(item: dict[str, Any]) -> bool:
    for artifact in item.get("related_artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        if artifact.get("artifact_type") in TERMINAL_ARTIFACT_TYPES:
            return True
    return False


def _suggested_command(task_key: str) -> str:
    return (
        "scripts/run_task_to_draft_pr_pipeline.py "
        f"--task-key {task_key} "
        "--confirm-run-one-shot-pipeline "
        "--confirm-prepare-pr "
        "--confirm-github-mutations "
        "--confirm-branch-push "
        "--confirm-draft-pr"
    )


__all__ = [
    "SchedulerWatcherPreviewError",
    "SchedulerWatcherPreviewRequest",
    "WATCHER_PREVIEW_SAFETY_FLAGS",
    "WATCHER_PREVIEW_SCHEMA_VERSION",
    "WATCHER_PREVIEW_SOURCE",
    "build_scheduler_watcher_preview",
]
