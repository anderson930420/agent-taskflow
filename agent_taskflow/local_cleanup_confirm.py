"""Explicit local cleanup confirmation from Phase 6A merged cleanup evidence.

This module performs only local cleanup after an explicit confirmation flag.
It may remove the verified local task worktree and optionally delete the local
task branch with safe ``git branch -d``. It does not delete remote branches,
close issues, archive tasks, merge, approve, or update task status.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
import json
from pathlib import Path
import subprocess
from typing import Any, Callable, Protocol

from agent_taskflow._helpers import (
    dedupe_non_empty_preserve_order as _dedupe_preserve_order,
)
from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord, utc_now_iso
from agent_taskflow.post_merge_cleanup_recommendation import (
    PostMergeCleanupRecommendationError,
    PostMergeCleanupRecommendationRequest,
    recommend_post_merge_cleanup,
)
from agent_taskflow.store import TaskMirrorStore, default_db_path
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.worktree import ensure_absolute_path


ARTIFACT_KIND = "local_cleanup"
EVENT_TYPE = "local_cleanup_completed"
SOURCE = "local_cleanup_confirm"
DEFAULT_REMOTE = "origin"
DEFAULT_WORKTREE_ROOT_NAME = ".worktrees"
PROTECTED_BRANCHES = {"main", "master"}


class LocalCleanupConfirmError(RuntimeError):
    """Raised when local cleanup cannot be performed safely."""


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., CompletedProcessLike]


@dataclass(frozen=True)
class LocalCleanupConfirmRequest:
    """Request for previewing or confirming explicit local cleanup."""

    task_key: str
    repo_path: Path
    db_path: Path | None = None
    artifact_root: Path | None = None
    worktree_root: Path | None = None
    remote: str = DEFAULT_REMOTE
    offline_pr_json: Path | None = None
    dry_run: bool = False
    confirm_local_cleanup: bool = False
    delete_local_branch: bool = False
    skip_local_branch_delete: bool = False
    allow_dirty_worktree: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        object.__setattr__(
            self,
            "repo_path",
            ensure_absolute_path(self.repo_path, name="repo_path"),
        )
        if self.db_path is not None:
            object.__setattr__(
                self,
                "db_path",
                ensure_absolute_path(self.db_path, name="db_path"),
            )
        if self.artifact_root is not None:
            object.__setattr__(
                self,
                "artifact_root",
                ensure_absolute_path(self.artifact_root, name="artifact_root"),
            )
        if self.worktree_root is not None:
            object.__setattr__(
                self,
                "worktree_root",
                ensure_absolute_path(self.worktree_root, name="worktree_root"),
            )
        if self.offline_pr_json is not None:
            object.__setattr__(
                self,
                "offline_pr_json",
                ensure_absolute_path(self.offline_pr_json, name="offline_pr_json"),
            )

        normalized_remote = self.remote.strip()
        if not normalized_remote:
            raise ValueError("remote must not be empty")
        if normalized_remote.startswith("-") or any(ch.isspace() for ch in normalized_remote):
            raise ValueError("remote must be a simple git remote name")
        object.__setattr__(self, "remote", normalized_remote)

        if self.delete_local_branch and self.skip_local_branch_delete:
            raise ValueError("delete_local_branch and skip_local_branch_delete are mutually exclusive")


@dataclass(frozen=True)
class LocalCleanupConfirmResult:
    """Structured local cleanup preview or confirmation result."""

    ok: bool
    status: str
    task_key: str
    task_status: str | None
    cleanup_recommendation: dict[str, Any]
    worktree: dict[str, Any]
    local_branch: dict[str, Any]
    evidence: dict[str, Any]
    next_allowed_actions: list[str]
    actions_not_performed: list[str]
    summary: dict[str, Any]
    safety: dict[str, Any]
    warnings: list[str]
    blocking_warnings: list[str]
    performed: bool
    dry_run: bool
    confirmation_required: bool
    delete_local_branch: bool
    skip_local_branch_delete: bool
    allow_dirty_worktree: bool
    artifact_recorded: bool
    event_recorded: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(asdict(self), sort_keys=True))


def confirm_local_cleanup(
    request: LocalCleanupConfirmRequest,
    *,
    store: TaskMirrorStore | None = None,
    runner: Runner | None = None,
) -> LocalCleanupConfirmResult:
    """Preview or confirm explicit local cleanup after merged PR evidence."""

    db_path = request.db_path or default_db_path()
    if not db_path.exists():
        return _not_found_result(
            request=request,
            error=f"SQLite state DB not found: {db_path}",
        )

    current_store = store or TaskMirrorStore(db_path)
    task = current_store.get_task(request.task_key)
    if task is None:
        return _not_found_result(request=request, error=f"Task not found: {request.task_key}")

    worktree = current_store.get_task_worktree(request.task_key)
    if worktree is None:
        return _blocked_result(
            request=request,
            task=task,
            cleanup_recommendation=_empty_cleanup_recommendation(),
            worktree=_empty_worktree(),
            local_branch=_empty_local_branch(),
            warnings=[f"TaskWorktreeRecord missing for task: {request.task_key}"],
            error=f"TaskWorktreeRecord missing for task: {request.task_key}",
        )

    draft_pr_evidence = _read_draft_pr_evidence(current_store, request.task_key)
    if not draft_pr_evidence["available"]:
        return _blocked_result(
            request=request,
            task=task,
            cleanup_recommendation=_empty_cleanup_recommendation(),
            worktree=_worktree_snapshot(task, worktree, request),
            local_branch=_local_branch_snapshot(task, worktree),
            warnings=list(draft_pr_evidence["warnings"]),
            error="Draft PR evidence is missing",
        )

    if task.repo_path.resolve() != request.repo_path.resolve():
        error = (
            f"Provided repo_path {request.repo_path} does not match task repo_path {task.repo_path}"
        )
        return _blocked_result(
            request=request,
            task=task,
            cleanup_recommendation=_empty_cleanup_recommendation(),
            worktree=_worktree_snapshot(task, worktree, request),
            local_branch=_local_branch_snapshot(task, worktree),
            warnings=[error],
            error=error,
        )

    repo = str(draft_pr_evidence.get("repo") or "").strip()
    if not repo:
        error = "Draft PR evidence does not include a GitHub repo"
        return _blocked_result(
            request=request,
            task=task,
            cleanup_recommendation=_empty_cleanup_recommendation(),
            worktree=_worktree_snapshot(task, worktree, request),
            local_branch=_local_branch_snapshot(task, worktree),
            warnings=[error],
            error=error,
        )

    recommendation_request = PostMergeCleanupRecommendationRequest(
        task_key=request.task_key,
        repo=repo,
        repo_path=request.repo_path,
        db_path=db_path,
        artifact_root=request.artifact_root,
        remote=request.remote,
        offline_pr_json=request.offline_pr_json,
        allow_non_waiting=True,
    )
    recommendation = recommend_post_merge_cleanup(
        recommendation_request,
        store=current_store,
        runner=runner,
    )
    if not recommendation.ok:
        return _blocked_from_recommendation(
            request=request,
            task=task,
            recommendation=recommendation,
            worktree=_worktree_snapshot(task, worktree, request),
            local_branch=_local_branch_snapshot(task, worktree),
        )

    cleanup_recommendation = _cleanup_recommendation_snapshot(recommendation)
    recommendation_warnings = list(recommendation.blocking_warnings) + list(
        recommendation.non_blocking_warnings
    )
    worktree_snapshot = _worktree_snapshot(task, worktree, request, runner=runner)
    local_branch_snapshot = _local_branch_snapshot(task, worktree, runner=runner)
    warnings = _dedupe_preserve_order(
        list(draft_pr_evidence["warnings"])
        + recommendation_warnings
        + list(worktree_snapshot["warnings"])
        + list(local_branch_snapshot["warnings"])
    )

    readiness_issues = _readiness_issues(
        request=request,
        task=task,
        worktree=worktree_snapshot,
        local_branch=local_branch_snapshot,
        cleanup_recommendation=cleanup_recommendation,
    )
    if readiness_issues:
        return _blocked_result(
            request=request,
            task=task,
            cleanup_recommendation=cleanup_recommendation,
            worktree=worktree_snapshot,
            local_branch=local_branch_snapshot,
            warnings=warnings + readiness_issues,
            error=readiness_issues[0],
        )

    delete_mode = _branch_delete_mode(request)
    delete_requested = delete_mode == "delete"
    branch_delete_follow_recommendation = not delete_requested and not request.skip_local_branch_delete
    branch_safe_to_delete = bool(local_branch_snapshot["merged_into_base"]) and not bool(
        local_branch_snapshot["protected"]
    )
    should_delete_branch = (
        branch_safe_to_delete
        and not request.skip_local_branch_delete
        and (delete_requested or branch_delete_follow_recommendation)
    )

    if request.dry_run:
        return _preview_result(
            request=request,
            task=task,
            cleanup_recommendation=cleanup_recommendation,
            worktree=worktree_snapshot,
            local_branch={
                **local_branch_snapshot,
                "safe_to_delete": branch_safe_to_delete,
                "delete_requested": delete_requested or branch_delete_follow_recommendation,
                "delete_skipped": not should_delete_branch,
            },
            warnings=warnings,
        )

    if not request.confirm_local_cleanup:
        return _blocked_result(
            request=request,
            task=task,
            cleanup_recommendation=cleanup_recommendation,
            worktree=worktree_snapshot,
            local_branch={
                **local_branch_snapshot,
                "safe_to_delete": branch_safe_to_delete,
                "delete_requested": delete_requested or branch_delete_follow_recommendation,
                "delete_skipped": True,
            },
            warnings=warnings + ["Local cleanup requires --confirm-local-cleanup"],
            error="Local cleanup requires --confirm-local-cleanup",
        )

    worktree_removed, worktree_remove_error = _remove_worktree(
        request=request,
        worktree_path=Path(str(worktree_snapshot["path"])),
        runner=runner,
    )
    if not worktree_removed:
        return _blocked_result(
            request=request,
            task=task,
            cleanup_recommendation=cleanup_recommendation,
            worktree={**worktree_snapshot, "removed": False, "exists_after": True},
            local_branch={
                **local_branch_snapshot,
                "safe_to_delete": branch_safe_to_delete,
                "delete_requested": delete_requested or branch_delete_follow_recommendation,
                "delete_skipped": True,
            },
            warnings=warnings + [worktree_remove_error or "Worktree removal failed"],
            error=worktree_remove_error or "Worktree removal failed",
        )

    branch_deleted = False
    branch_delete_error: str | None = None
    branch_delete_attempted = should_delete_branch
    if should_delete_branch:
        branch_deleted, branch_delete_error = _delete_local_branch(
            request=request,
            branch=worktree.branch,
            runner=runner,
        )

    artifact_payload = _local_cleanup_evidence(
        task_key=request.task_key,
        task_status=task.status,
        worktree_path=str(worktree.worktree_path),
        branch=worktree.branch,
        worktree_removed=True,
        local_branch_deleted=branch_deleted,
        remote_branch_deleted=False,
        issue_closed=False,
        task_status_changed=False,
        task_completed=False,
        task_archived=False,
        cleanup_scope="local",
        requires_human_confirmation=True,
        confirmation_flag="--confirm-local-cleanup",
        branch_delete_attempted=branch_delete_attempted,
        branch_delete_skipped=not branch_delete_attempted or not branch_deleted,
        worktree_remove_error=worktree_remove_error,
        branch_delete_error=branch_delete_error,
        cleanup_recommendation=cleanup_recommendation,
    )
    artifact_recorded, event_recorded, artifact_path = _record_local_cleanup_evidence(
        store=current_store,
        task=task,
        artifact_root=request.artifact_root,
        artifact_payload=artifact_payload,
    )

    status = "local_cleanup_completed" if branch_deleted else "partial_cleanup"

    summary = {
        "local_cleanup_performed": True,
        "worktree_removed": True,
        "local_branch_deleted": branch_deleted,
        "remote_branch_deleted": False,
        "issue_closed": False,
        "task_status_changed": False,
        "task_completed": False,
        "task_archived": False,
        "requires_human_review": True,
        "next_phase": "explicit_remote_branch_cleanup_confirm",
    }
    safety = _safety_block(
        human_confirmation_confirmed=True,
        local_cleanup_performed=True,
        worktree_removed=True,
        local_branch_deleted=branch_deleted,
    )
    return LocalCleanupConfirmResult(
        ok=True,
        status=status,
        task_key=request.task_key,
        task_status=task.status,
        cleanup_recommendation=cleanup_recommendation,
        worktree={
            **worktree_snapshot,
            "removed": True,
            "exists_after": False,
            "safe_to_remove": True,
        },
        local_branch={
            **local_branch_snapshot,
            "safe_to_delete": branch_safe_to_delete,
            "delete_requested": branch_delete_attempted,
            "delete_skipped": not branch_delete_attempted or not branch_deleted,
            "deleted": branch_deleted,
            "exists_after": not branch_deleted,
            "delete_error": branch_delete_error,
        },
        evidence={
            "artifact_recorded": artifact_recorded,
            "event_recorded": event_recorded,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_KIND,
            "artifact_path": str(artifact_path) if artifact_path else None,
            "cleanup_scope": "local",
            "requires_human_confirmation": True,
            "confirmation_flag": "--confirm-local-cleanup",
            "task_completed": False,
        },
        next_allowed_actions=[
            "manual verification of local cleanup",
            "explicit remote branch cleanup confirm in a later phase",
            "explicit task closeout / archive confirm in a later phase",
        ],
        actions_not_performed=[
            "remote branch deletion",
            "issue close",
            "task status update",
            "task archive",
            "merge",
            "approval",
            "force deletion",
        ],
        summary=summary,
        safety=safety,
        warnings=warnings + ([branch_delete_error] if branch_delete_error else []),
        blocking_warnings=[],
        performed=True,
        dry_run=False,
        confirmation_required=True,
        delete_local_branch=request.delete_local_branch,
        skip_local_branch_delete=request.skip_local_branch_delete,
        allow_dirty_worktree=request.allow_dirty_worktree,
        artifact_recorded=artifact_recorded,
        event_recorded=event_recorded,
        error=None,
    )


def _branch_delete_mode(request: LocalCleanupConfirmRequest) -> str:
    if request.skip_local_branch_delete:
        return "skip"
    if request.delete_local_branch:
        return "delete"
    return "default"


def _read_draft_pr_evidence(store: TaskMirrorStore, task_key: str) -> dict[str, Any]:
    artifacts = [artifact for artifact in store.list_task_artifacts(task_key) if artifact.artifact_type == "draft_pr"]
    events = [event for event in store.list_task_events(task_key) if event.event_type == "draft_pr_created"]
    warnings: list[str] = []
    artifact_payload: dict[str, Any] | None = None
    artifact_path: Path | None = None
    if artifacts:
        artifact_path = artifacts[-1].path
        try:
            artifact_payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        except OSError as exc:
            warnings.append(f"Could not read draft PR artifact: {exc}")
        except json.JSONDecodeError as exc:
            warnings.append(f"Draft PR artifact is not valid JSON: {exc}")
    elif events:
        warnings.append("Draft PR artifact record is missing; falling back to event payload")

    event_payload = _latest_event_payload(events)
    evidence = artifact_payload or event_payload or {}
    available = bool(artifacts or events) and isinstance(evidence, dict)
    return {
        "available": available,
        "artifact_recorded": bool(artifacts),
        "event_recorded": bool(events),
        "artifact_path": str(artifact_path) if artifact_path is not None else None,
        "repo": evidence.get("repo"),
        "pr_number": evidence.get("pr_number"),
        "pr_url": evidence.get("pr_url"),
        "base_branch": evidence.get("base_branch"),
        "head_branch": evidence.get("head_branch"),
        "merged": evidence.get("merged"),
        "cleanup_performed": evidence.get("cleanup_performed"),
        "issue_closed": evidence.get("issue_closed"),
        "requires_human_confirmation": evidence.get("requires_human_confirmation"),
        "warnings": warnings or ([] if available else ["Draft PR evidence is missing"]),
    }


def _latest_event_payload(events: list[Any]) -> dict[str, Any]:
    if not events:
        return {}
    payload_json = events[-1].payload_json
    if not payload_json:
        return {}
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _cleanup_recommendation_snapshot(result: Any) -> dict[str, Any]:
    return {
        "available": True,
        "status": result.status,
        "merged": bool(result.summary.get("merged")),
        "cleanup_recommended": bool(result.summary.get("cleanup_recommended")),
        "recommended_cleanup": result.recommended_cleanup,
        "blocking_warnings": list(result.blocking_warnings),
        "non_blocking_warnings": list(result.non_blocking_warnings),
        "next_allowed_actions": list(result.next_allowed_actions),
        "actions_not_performed": list(result.actions_not_performed),
        "summary": result.summary,
        "safety": result.safety,
    }


def _worktree_snapshot(
    task: TaskRecord,
    worktree: TaskWorktreeRecord,
    request: LocalCleanupConfirmRequest,
    *,
    runner: Runner | None = None,
) -> dict[str, Any]:
    path = worktree.worktree_path
    expected_root = request.worktree_root or (task.repo_path / DEFAULT_WORKTREE_ROOT_NAME)
    resolved_path = path.resolve()
    resolved_root = expected_root.resolve()
    exists = path.exists()
    registered = _worktree_is_registered(worktree.repo_path, path, runner=runner)
    clean, dirty_error = _worktree_is_clean(path, runner=runner)
    expected_path = resolved_root / task.task_key
    belongs_to_task = resolved_path == expected_path.resolve()
    inside_expected_root = _is_within_root(resolved_path, resolved_root)
    warnings: list[str] = []
    if not exists:
        warnings.append(f"Worktree path is missing on disk: {path}")
    if not inside_expected_root:
        warnings.append(f"Worktree path is outside expected root: {path}")
    if resolved_path in {task.repo_path.resolve(), Path("/"), Path.home().resolve()}:
        warnings.append("Worktree path is not safe to remove")
    if not belongs_to_task:
        warnings.append(f"Worktree path does not belong to task {task.task_key}")
    if not registered:
        warnings.append("Worktree path is not registered in git worktree list")
    if dirty_error:
        warnings.append(dirty_error)
    return {
        "path": str(path),
        "exists_before": exists,
        "exists_after": exists,
        "registered": registered,
        "inside_expected_root": inside_expected_root,
        "belongs_to_task": belongs_to_task,
        "is_repo_root": resolved_path == task.repo_path.resolve(),
        "is_home_directory": resolved_path == Path.home().resolve(),
        "is_root_directory": resolved_path == Path("/"),
        "clean": clean,
        "dirty_allowed": request.allow_dirty_worktree,
        "safe_to_remove": False,
        "removed": False,
        "warnings": warnings,
    }


def _local_branch_snapshot(
    task: TaskRecord,
    worktree: TaskWorktreeRecord,
    *,
    runner: Runner | None = None,
) -> dict[str, Any]:
    branch = worktree.branch
    repo_path = worktree.repo_path
    branch_exists = _branch_exists(repo_path, branch, runner=runner)
    current_branch = _current_branch(repo_path, runner=runner)
    merged_into_base = _branch_merged_into_base(repo_path, branch, worktree.base_branch, runner=runner)
    protected = branch in PROTECTED_BRANCHES
    warnings: list[str] = []
    if not branch_exists:
        warnings.append(f"Local branch is missing: {branch}")
    if current_branch == branch:
        warnings.append("Local branch is currently checked out in the main repository")
    if protected:
        warnings.append("Local branch is protected")
    if worktree.branch != f"task/{task.task_key}" and worktree.branch != task.task_key:
        warnings.append(f"Local branch does not match expected task branch for {task.task_key}")
    return {
        "name": branch,
        "exists_before": branch_exists,
        "exists_after": branch_exists,
        "merged_into_base": merged_into_base,
        "current_in_main_repo": current_branch == branch,
        "current_branch": current_branch,
        "protected": protected,
        "matches_task_branch": worktree.branch == f"task/{task.task_key}" or worktree.branch == task.task_key,
        "safe_to_delete": False,
        "delete_requested": False,
        "delete_skipped": False,
        "deleted": False,
        "warnings": warnings,
    }


def _readiness_issues(
    *,
    request: LocalCleanupConfirmRequest,
    task: TaskRecord,
    worktree: dict[str, Any],
    local_branch: dict[str, Any],
    cleanup_recommendation: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    if not cleanup_recommendation.get("available"):
        issues.append("Phase 6A cleanup recommendation is unavailable")
    if not cleanup_recommendation.get("merged"):
        issues.append("PR is not merged")
    if not cleanup_recommendation.get("cleanup_recommended") and not (
        request.allow_dirty_worktree and not worktree.get("clean")
    ):
        issues.append("Phase 6A cleanup recommendation does not recommend cleanup")
    recommended = cleanup_recommendation.get("recommended_cleanup") or []
    worktree_item = next(
        (
            item
            for item in recommended
            if isinstance(item, dict) and item.get("action") == "remove_local_worktree"
        ),
        None,
    )
    if not isinstance(worktree_item, dict) or not worktree_item.get("recommended"):
        if request.allow_dirty_worktree and not worktree.get("clean"):
            pass
        else:
            issues.append("Phase 6A cleanup recommendation does not include local worktree removal")

    if not worktree.get("exists_before"):
        issues.append("Worktree path is missing on disk")
    if not worktree.get("inside_expected_root"):
        issues.append("Worktree path is outside the expected worktree root")
    if worktree.get("is_repo_root"):
        issues.append("Worktree path must not be the repository root")
    if worktree.get("is_home_directory") or worktree.get("is_root_directory"):
        issues.append("Worktree path must not be the home directory or filesystem root")
    if not worktree.get("belongs_to_task"):
        issues.append("Worktree path does not belong to the expected task key")
    if not worktree.get("registered"):
        issues.append("Worktree path is not registered in git worktree list")
    if not worktree.get("clean") and not request.allow_dirty_worktree:
        issues.append("Worktree is dirty and --allow-dirty-worktree was not supplied")

    if not local_branch.get("exists_before"):
        issues.append("Local branch is missing")
    if not local_branch.get("matches_task_branch"):
        issues.append("Local branch does not match the expected task branch")
    if local_branch.get("protected"):
        issues.append("Local branch is protected")
    if local_branch.get("current_in_main_repo"):
        issues.append("Local branch is currently checked out in the main repository")
    return _dedupe_preserve_order(issues)


def _blocked_from_recommendation(
    *,
    request: LocalCleanupConfirmRequest,
    task: TaskRecord,
    recommendation: Any,
    worktree: dict[str, Any],
    local_branch: dict[str, Any],
) -> LocalCleanupConfirmResult:
    cleanup_recommendation = _cleanup_recommendation_snapshot(recommendation)
    warnings = list(recommendation.blocking_warnings) + list(recommendation.non_blocking_warnings)
    return _blocked_result(
        request=request,
        task=task,
        cleanup_recommendation=cleanup_recommendation,
        worktree=worktree,
        local_branch=local_branch,
        warnings=warnings,
        error=recommendation.error or recommendation.summary.get("next_phase") or "Cleanup recommendation is blocked",
    )


def _blocked_result(
    *,
    request: LocalCleanupConfirmRequest,
    task: TaskRecord,
    cleanup_recommendation: dict[str, Any],
    worktree: dict[str, Any],
    local_branch: dict[str, Any],
    warnings: list[str],
    error: str,
) -> LocalCleanupConfirmResult:
    return LocalCleanupConfirmResult(
        ok=False,
        status="blocked",
        task_key=request.task_key,
        task_status=task.status,
        cleanup_recommendation=cleanup_recommendation,
        worktree=worktree,
        local_branch=local_branch,
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_KIND,
            "artifact_path": None,
            "cleanup_scope": "local",
            "requires_human_confirmation": True,
            "confirmation_flag": "--confirm-local-cleanup",
        },
        next_allowed_actions=[
            "resolve blocking warnings",
            "rerun explicit local cleanup confirm once the worktree is safe",
        ],
        actions_not_performed=[
            "local worktree removal",
            "local branch deletion",
            "remote branch deletion",
            "issue close",
            "task status update",
            "task archive",
            "merge",
            "approval",
            "force deletion",
        ],
        summary={
            "local_cleanup_performed": False,
            "worktree_removed": False,
            "local_branch_deleted": False,
            "remote_branch_deleted": False,
            "issue_closed": False,
            "task_status_changed": False,
            "task_completed": False,
            "task_archived": False,
            "requires_human_review": True,
            "next_phase": "explicit_remote_branch_cleanup_confirm",
        },
        safety=_safety_block(
            human_confirmation_confirmed=False,
            local_cleanup_performed=False,
            worktree_removed=False,
            local_branch_deleted=False,
        ),
        warnings=_dedupe_preserve_order(warnings + [error]),
        blocking_warnings=_dedupe_preserve_order([error, *warnings]),
        performed=False,
        dry_run=request.dry_run,
        confirmation_required=not request.dry_run,
        delete_local_branch=request.delete_local_branch,
        skip_local_branch_delete=request.skip_local_branch_delete,
        allow_dirty_worktree=request.allow_dirty_worktree,
        artifact_recorded=False,
        event_recorded=False,
        error=error,
    )


def _preview_result(
    *,
    request: LocalCleanupConfirmRequest,
    task: TaskRecord,
    cleanup_recommendation: dict[str, Any],
    worktree: dict[str, Any],
    local_branch: dict[str, Any],
    warnings: list[str],
) -> LocalCleanupConfirmResult:
    return LocalCleanupConfirmResult(
        ok=True,
        status="dry_run",
        task_key=request.task_key,
        task_status=task.status,
        cleanup_recommendation=cleanup_recommendation,
        worktree={
            **worktree,
            "safe_to_remove": True,
            "removed": False,
        },
        local_branch={
            **local_branch,
            "safe_to_delete": bool(local_branch.get("merged_into_base")) and not bool(local_branch.get("protected")),
            "delete_requested": _branch_delete_mode(request) == "delete" or _branch_delete_mode(request) == "default",
            "delete_skipped": True,
            "deleted": False,
            "exists_after": bool(local_branch.get("exists_before")),
        },
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_KIND,
            "artifact_path": None,
            "cleanup_scope": "local",
            "requires_human_confirmation": True,
            "confirmation_flag": "--confirm-local-cleanup",
        },
        next_allowed_actions=[
            "manual verification of local cleanup",
            "explicit remote branch cleanup confirm in a later phase",
            "explicit task closeout / archive confirm in a later phase",
        ],
        actions_not_performed=[
            "local worktree removal",
            "local branch deletion",
            "remote branch deletion",
            "issue close",
            "task status update",
            "task archive",
            "merge",
            "approval",
            "force deletion",
        ],
        summary={
            "local_cleanup_performed": False,
            "worktree_removed": False,
            "local_branch_deleted": False,
            "remote_branch_deleted": False,
            "issue_closed": False,
            "task_status_changed": False,
            "task_completed": False,
            "task_archived": False,
            "requires_human_review": True,
            "next_phase": "explicit_remote_branch_cleanup_confirm",
        },
        safety=_safety_block(
            human_confirmation_confirmed=False,
            local_cleanup_performed=False,
            worktree_removed=False,
            local_branch_deleted=False,
        ),
        warnings=warnings,
        blocking_warnings=[],
        performed=False,
        dry_run=True,
        confirmation_required=not request.dry_run,
        delete_local_branch=request.delete_local_branch,
        skip_local_branch_delete=request.skip_local_branch_delete,
        allow_dirty_worktree=request.allow_dirty_worktree,
        artifact_recorded=False,
        event_recorded=False,
        error=None,
    )


def _not_found_result(
    *,
    request: LocalCleanupConfirmRequest,
    error: str,
) -> LocalCleanupConfirmResult:
    return LocalCleanupConfirmResult(
        ok=False,
        status="not_found",
        task_key=request.task_key,
        task_status=None,
        cleanup_recommendation=_empty_cleanup_recommendation(),
        worktree=_empty_worktree(),
        local_branch=_empty_local_branch(),
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_KIND,
            "artifact_path": None,
            "cleanup_scope": "local",
            "requires_human_confirmation": True,
            "confirmation_flag": "--confirm-local-cleanup",
        },
        next_allowed_actions=["resolve the missing task record and retry"],
        actions_not_performed=[
            "local worktree removal",
            "local branch deletion",
            "remote branch deletion",
            "issue close",
            "task status update",
            "task archive",
            "merge",
            "approval",
            "force deletion",
        ],
        summary={
            "local_cleanup_performed": False,
            "worktree_removed": False,
            "local_branch_deleted": False,
            "remote_branch_deleted": False,
            "issue_closed": False,
            "task_status_changed": False,
            "task_completed": False,
            "task_archived": False,
            "requires_human_review": True,
            "next_phase": "explicit_remote_branch_cleanup_confirm",
        },
        safety=_safety_block(
            human_confirmation_confirmed=False,
            local_cleanup_performed=False,
            worktree_removed=False,
            local_branch_deleted=False,
        ),
        warnings=[error],
        blocking_warnings=[error],
        performed=False,
        dry_run=request.dry_run,
        confirmation_required=not request.dry_run,
        delete_local_branch=request.delete_local_branch,
        skip_local_branch_delete=request.skip_local_branch_delete,
        allow_dirty_worktree=request.allow_dirty_worktree,
        artifact_recorded=False,
        event_recorded=False,
        error=error,
    )


def _record_local_cleanup_evidence(
    *,
    store: TaskMirrorStore,
    task: TaskRecord,
    artifact_root: Path | None,
    artifact_payload: dict[str, Any],
) -> tuple[bool, bool, Path | None]:
    output_root = _resolve_cleanup_artifact_root(task, artifact_root)
    artifact_path = output_root / task.task_key / "local_cleanup.json"
    atomic_write_json(artifact_path, artifact_payload, sort_keys=True)
    store.record_task_artifact(task.task_key, ARTIFACT_KIND, artifact_path)
    store.record_task_event(
        task.task_key,
        EVENT_TYPE,
        SOURCE,
        message="Local cleanup completed",
        payload=artifact_payload,
    )
    return True, True, artifact_path


def _resolve_cleanup_artifact_root(task: TaskRecord, artifact_root: Path | None) -> Path:
    if artifact_root is not None:
        return artifact_root / ARTIFACT_KIND
    if task.artifact_dir is not None:
        return task.artifact_dir.resolve().parent / ARTIFACT_KIND
    return task.repo_path / ".agent-taskflow" / "artifacts" / ARTIFACT_KIND


def _local_cleanup_evidence(
    *,
    task_key: str,
    task_status: str,
    worktree_path: str,
    branch: str,
    worktree_removed: bool,
    local_branch_deleted: bool,
    remote_branch_deleted: bool,
    issue_closed: bool,
    task_status_changed: bool,
    task_completed: bool,
    task_archived: bool,
    cleanup_scope: str,
    requires_human_confirmation: bool,
    confirmation_flag: str,
    branch_delete_attempted: bool,
    branch_delete_skipped: bool,
    worktree_remove_error: str | None,
    branch_delete_error: str | None,
    cleanup_recommendation: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "artifact_type": ARTIFACT_KIND,
        "kind": EVENT_TYPE,
        "task_key": task_key,
        "task_status": task_status,
        "worktree_path": worktree_path,
        "worktree_removed": worktree_removed,
        "local_branch": branch,
        "local_branch_deleted": local_branch_deleted,
        "branch_delete_attempted": branch_delete_attempted,
        "branch_delete_skipped": branch_delete_skipped,
        "remote_branch_deleted": remote_branch_deleted,
        "issue_closed": issue_closed,
        "task_status_changed": task_status_changed,
        "task_completed": task_completed,
        "task_archived": task_archived,
        "cleanup_scope": cleanup_scope,
        "requires_human_confirmation": requires_human_confirmation,
        "confirmation_flag": confirmation_flag,
        "worktree_remove_error": worktree_remove_error,
        "branch_delete_error": branch_delete_error,
        "cleanup_recommendation": cleanup_recommendation,
        "recorded_at": utc_now_iso(),
    }


def _remove_worktree(
    *,
    request: LocalCleanupConfirmRequest,
    worktree_path: Path,
    runner: Runner | None,
) -> tuple[bool, str | None]:
    completed = _run_git(
        ["git", "worktree", "remove", str(worktree_path)],
        cwd=request.repo_path,
        runner=runner,
    )
    if completed.returncode != 0:
        return False, f"git worktree remove failed with {completed.returncode}: {completed.stderr.strip() or completed.stdout.strip()}"
    return True, None


def _delete_local_branch(
    *,
    request: LocalCleanupConfirmRequest,
    branch: str,
    runner: Runner | None,
) -> tuple[bool, str | None]:
    completed = _run_git(
        ["git", "branch", "-d", branch],
        cwd=request.repo_path,
        runner=runner,
    )
    if completed.returncode != 0:
        return False, f"git branch -d failed with {completed.returncode}: {completed.stderr.strip() or completed.stdout.strip()}"
    return True, None


def _worktree_is_registered(
    repo_path: Path,
    worktree_path: Path,
    *,
    runner: Runner | None,
) -> bool:
    completed = _run_git(["git", "worktree", "list", "--porcelain"], cwd=repo_path, runner=runner)
    if completed.returncode != 0:
        return False
    target = str(worktree_path.resolve())
    current_worktree = None
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("worktree "):
            current_worktree = line[len("worktree ") :]
            if Path(current_worktree).resolve() == Path(target).resolve():
                return True
    return False


def _worktree_is_clean(worktree_path: Path, *, runner: Runner | None) -> tuple[bool, str | None]:
    completed = _run_git(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=worktree_path,
        runner=runner,
    )
    if completed.returncode != 0:
        return False, f"Could not inspect worktree status: {completed.stderr.strip() or completed.stdout.strip()}"
    clean = not completed.stdout.strip()
    return clean, None if clean else "Worktree has uncommitted or untracked changes"


def _branch_exists(repo_path: Path, branch: str, *, runner: Runner | None) -> bool:
    completed = _run_git(["git", "branch", "--list", branch], cwd=repo_path, runner=runner)
    return completed.returncode == 0 and bool(completed.stdout.strip())


def _current_branch(repo_path: Path, *, runner: Runner | None) -> str | None:
    completed = _run_git(["git", "branch", "--show-current"], cwd=repo_path, runner=runner)
    if completed.returncode != 0:
        return None
    branch = completed.stdout.strip()
    return branch or None


def _branch_merged_into_base(
    repo_path: Path,
    branch: str,
    base_branch: str | None,
    *,
    runner: Runner | None,
) -> bool:
    if not base_branch:
        return False
    completed = _run_git(["git", "branch", "--merged", base_branch], cwd=repo_path, runner=runner)
    if completed.returncode != 0:
        return False
    merged = {line.strip().lstrip("* ").strip() for line in completed.stdout.splitlines() if line.strip()}
    return branch in merged


def _run_git(args: list[str], *, cwd: Path, runner: Runner | None) -> CompletedProcessLike:
    run = runner or subprocess.run
    return run(
        args,
        cwd=cwd,
        shell=False,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _is_within_root(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except ValueError:
        return False


def _empty_cleanup_recommendation() -> dict[str, Any]:
    return {
        "available": False,
        "status": None,
        "merged": False,
        "cleanup_recommended": False,
        "recommended_cleanup": [],
        "blocking_warnings": [],
        "non_blocking_warnings": [],
        "next_allowed_actions": [],
        "actions_not_performed": [],
        "summary": {},
        "safety": {},
    }


def _empty_worktree() -> dict[str, Any]:
    return {
        "path": None,
        "exists_before": False,
        "exists_after": False,
        "registered": False,
        "inside_expected_root": False,
        "belongs_to_task": False,
        "is_repo_root": False,
        "is_home_directory": False,
        "is_root_directory": False,
        "clean": False,
        "dirty_allowed": False,
        "safe_to_remove": False,
        "removed": False,
        "warnings": [],
    }


def _empty_local_branch() -> dict[str, Any]:
    return {
        "name": None,
        "exists_before": False,
        "exists_after": False,
        "merged_into_base": False,
        "current_in_main_repo": False,
        "current_branch": None,
        "protected": False,
        "matches_task_branch": False,
        "safe_to_delete": False,
        "delete_requested": False,
        "delete_skipped": False,
        "deleted": False,
        "warnings": [],
    }


def _safety_block(
    *,
    human_confirmation_confirmed: bool,
    local_cleanup_performed: bool,
    worktree_removed: bool,
    local_branch_deleted: bool,
) -> dict[str, Any]:
    return {
        "human_confirmation_required": True,
        "human_confirmation_confirmed": human_confirmation_confirmed,
        "task_status_changed": False,
        "task_completed": False,
        "workspace_prepared": False,
        "executor_started": False,
        "validators_started": False,
        "local_cleanup_performed": local_cleanup_performed,
        "worktree_removed": worktree_removed,
        "local_branch_deleted": local_branch_deleted,
        "remote_branch_deleted": False,
        "github_mutated": False,
        "issue_closed": False,
        "task_archived": False,
        "merged": False,
        "approved": False,
        "force_delete": False,
        "background_worker_started": False,
        "webhook_started": False,
        "polling_loop_started": False,
    }
