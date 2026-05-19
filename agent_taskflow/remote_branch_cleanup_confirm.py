"""Explicit remote branch cleanup confirmation after local cleanup evidence.

This module deletes only the verified remote task branch after explicit human
confirmation. It requires merged PR evidence, Phase 6A cleanup
recommendation evidence, and Phase 6B local cleanup evidence. It does not
close issues, archive tasks, update task status, merge, approve, delete local
branches, or remove worktrees.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import subprocess
from typing import Any, Callable, Protocol

from agent_taskflow.models import TaskRecord, utc_now_iso
from agent_taskflow.post_merge_cleanup_recommendation import (
    PostMergeCleanupRecommendationRequest,
    recommend_post_merge_cleanup,
)
from agent_taskflow.remote_branch_cleanup_confirm_helpers import (
    PROTECTED_BRANCHES,
    LOCAL_ARTIFACT_KIND,
    LOCAL_EVENT_TYPE,
    LOCAL_CONFIRM_FLAG,
    build_git_ls_remote_heads_command,
    build_git_push_delete_command,
    cleanup_recommendation_snapshot as _cleanup_recommendation_snapshot,
    dedupe_preserve_order as _dedupe_preserve_order,
    empty_cleanup_recommendation as _empty_cleanup_recommendation,
    empty_draft_pr_evidence as _empty_draft_pr_evidence,
    empty_local_cleanup_evidence as _empty_local_cleanup_evidence,
    empty_remote_branch as _empty_remote_branch,
    latest_event_payload as _latest_event_payload,
    normalize_branch_name as _normalize_branch_name,
    safety_block as _safety_block,
    validate_branch_name as _validate_branch_name,
)
from agent_taskflow.store import TaskMirrorStore, default_db_path
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.worktree import ensure_absolute_path


ARTIFACT_KIND = "remote_branch_cleanup"
EVENT_TYPE = "remote_branch_cleanup_completed"
SOURCE = "remote_branch_cleanup_confirm"
DEFAULT_REMOTE = "origin"
EXPECTED_CONFIRM_FLAG = "--confirm-remote-branch-delete"


class RemoteBranchCleanupConfirmError(RuntimeError):
    """Raised when remote branch cleanup cannot proceed safely."""


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., CompletedProcessLike]


@dataclass(frozen=True)
class RemoteBranchCleanupConfirmRequest:
    """Request for previewing or confirming remote branch cleanup."""

    task_key: str
    repo_path: Path
    db_path: Path | None = None
    artifact_root: Path | None = None
    remote: str = DEFAULT_REMOTE
    branch: str | None = None
    offline_pr_json: Path | None = None
    dry_run: bool = False
    confirm_remote_branch_delete: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        object.__setattr__(self, "repo_path", ensure_absolute_path(self.repo_path, name="repo_path"))
        if self.db_path is not None:
            object.__setattr__(self, "db_path", ensure_absolute_path(self.db_path, name="db_path"))
        if self.artifact_root is not None:
            object.__setattr__(
                self,
                "artifact_root",
                ensure_absolute_path(self.artifact_root, name="artifact_root"),
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


@dataclass(frozen=True)
class RemoteBranchCleanupConfirmResult:
    """Structured remote branch cleanup preview or confirmation result."""

    ok: bool
    status: str
    task_key: str
    task_status: str | None
    cleanup_recommendation: dict[str, Any]
    draft_pr: dict[str, Any]
    local_cleanup: dict[str, Any]
    remote_branch: dict[str, Any]
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
    remote_branch_cleanup_performed: bool
    artifact_recorded: bool
    event_recorded: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(asdict(self), sort_keys=True))


def confirm_remote_branch_cleanup(
    request: RemoteBranchCleanupConfirmRequest,
    *,
    store: TaskMirrorStore | None = None,
    runner: Runner | None = None,
) -> RemoteBranchCleanupConfirmResult:
    """Preview or confirm remote task branch cleanup after merged PR evidence."""

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

    if task.repo_path.resolve() != request.repo_path.resolve():
        error = f"Provided repo_path {request.repo_path} does not match task repo_path {task.repo_path}"
        return _blocked_result(
            request=request,
            task=task,
            cleanup_recommendation=_empty_cleanup_recommendation(),
            draft_pr=_empty_draft_pr_evidence(),
            local_cleanup=_empty_local_cleanup_evidence(),
            remote_branch=_empty_remote_branch(request.remote),
            warnings=[error],
            error=error,
        )

    worktree = current_store.get_task_worktree(request.task_key)
    if worktree is None:
        return _blocked_result(
            request=request,
            task=task,
            cleanup_recommendation=_empty_cleanup_recommendation(),
            draft_pr=_empty_draft_pr_evidence(),
            local_cleanup=_empty_local_cleanup_evidence(),
            remote_branch=_empty_remote_branch(request.remote),
            warnings=[f"TaskWorktreeRecord missing for task: {request.task_key}"],
            error=f"TaskWorktreeRecord missing for task: {request.task_key}",
        )

    draft_pr = _read_draft_pr_evidence(current_store, request.task_key)
    if not draft_pr["available"]:
        return _blocked_result(
            request=request,
            task=task,
            cleanup_recommendation=_empty_cleanup_recommendation(),
            draft_pr=draft_pr,
            local_cleanup=_empty_local_cleanup_evidence(),
            remote_branch=_empty_remote_branch(request.remote, branch=worktree.branch),
            warnings=list(draft_pr["warnings"]),
            error=draft_pr["warnings"][0] if draft_pr["warnings"] else "Draft PR evidence is missing",
        )

    cleanup_request = PostMergeCleanupRecommendationRequest(
        task_key=request.task_key,
        repo=str(draft_pr["repo"]),
        repo_path=request.repo_path,
        db_path=db_path,
        artifact_root=request.artifact_root,
        remote=request.remote,
        offline_pr_json=request.offline_pr_json,
        allow_non_waiting=True,
    )
    recommendation = recommend_post_merge_cleanup(
        cleanup_request,
        store=current_store,
        runner=runner,
    )
    if not recommendation.ok:
        return _blocked_from_recommendation(
            request=request,
            task=task,
            recommendation=recommendation,
            draft_pr=draft_pr,
            local_cleanup=_empty_local_cleanup_evidence(),
            remote_branch=_empty_remote_branch(request.remote, branch=worktree.branch),
        )

    cleanup_recommendation = _cleanup_recommendation_snapshot(recommendation)
    local_cleanup = _read_local_cleanup_evidence(current_store, request.task_key)
    if not local_cleanup["available"]:
        return _blocked_result(
            request=request,
            task=task,
            cleanup_recommendation=cleanup_recommendation,
            draft_pr=draft_pr,
            local_cleanup=local_cleanup,
            remote_branch=_empty_remote_branch(request.remote, branch=worktree.branch),
            warnings=list(local_cleanup["warnings"]),
            error=local_cleanup["warnings"][0] if local_cleanup["warnings"] else "Local cleanup evidence is missing",
        )

    warnings = _dedupe_preserve_order(
        list(draft_pr["warnings"])
        + list(recommendation.blocking_warnings)
        + list(recommendation.non_blocking_warnings)
        + list(local_cleanup["warnings"])
    )

    resolved_branch, branch_warnings, branch_error = _resolve_branch_name(
        request=request,
        task=task,
        worktree=worktree,
        draft_pr=draft_pr,
        local_cleanup=local_cleanup,
        recommendation=recommendation,
    )
    warnings.extend(w for w in branch_warnings if w not in warnings)
    if branch_error is not None:
        return _blocked_result(
            request=request,
            task=task,
            cleanup_recommendation=cleanup_recommendation,
            draft_pr=draft_pr,
            local_cleanup=local_cleanup,
            remote_branch=_empty_remote_branch(request.remote, branch=resolved_branch),
            warnings=warnings + [branch_error],
            error=branch_error,
        )

    remote_branch = _inspect_remote_branch(
        repo_path=request.repo_path,
        remote=request.remote,
        branch=resolved_branch,
        base_branch=str(draft_pr.get("base_branch") or worktree.base_branch or ""),
        runner=runner,
    )
    warnings.extend(w for w in remote_branch["warnings"] if w not in warnings)

    readiness_issues = _readiness_issues(
        request=request,
        task=task,
        draft_pr=draft_pr,
        local_cleanup=local_cleanup,
        cleanup_recommendation=cleanup_recommendation,
        remote_branch=remote_branch,
        branch=resolved_branch,
    )
    if readiness_issues:
        return _blocked_result(
            request=request,
            task=task,
            cleanup_recommendation=cleanup_recommendation,
            draft_pr=draft_pr,
            local_cleanup=local_cleanup,
            remote_branch=remote_branch,
            warnings=warnings + readiness_issues,
            error=readiness_issues[0],
        )

    if request.dry_run:
        return _preview_result(
            request=request,
            task=task,
            cleanup_recommendation=cleanup_recommendation,
            draft_pr=draft_pr,
            local_cleanup=local_cleanup,
            remote_branch=remote_branch,
            branch=resolved_branch,
            warnings=warnings,
        )

    if not request.confirm_remote_branch_delete:
        error = f"Remote branch cleanup requires {EXPECTED_CONFIRM_FLAG}"
        return _blocked_result(
            request=request,
            task=task,
            cleanup_recommendation=cleanup_recommendation,
            draft_pr=draft_pr,
            local_cleanup=local_cleanup,
            remote_branch={**remote_branch, "safe_to_delete": remote_branch.get("safe_to_delete", False), "delete_attempted": False},
            warnings=warnings + [error],
            error=error,
        )

    deleted, delete_error = _delete_remote_branch(
        request=request,
        branch=resolved_branch,
        runner=runner,
    )
    if not deleted:
        return _blocked_result(
            request=request,
            task=task,
            cleanup_recommendation=cleanup_recommendation,
            draft_pr=draft_pr,
            local_cleanup=local_cleanup,
            remote_branch={**remote_branch, "delete_attempted": True, "deleted": False, "delete_error": delete_error},
            warnings=warnings + ([delete_error] if delete_error else []),
            error=delete_error or "Remote branch deletion failed",
        )

    exists_after = _remote_branch_exists(
        repo_path=request.repo_path,
        remote=request.remote,
        branch=resolved_branch,
        runner=runner,
    )
    if exists_after is None:
        error = "Could not verify remote branch deletion"
        return _blocked_result(
            request=request,
            task=task,
            cleanup_recommendation=cleanup_recommendation,
            draft_pr=draft_pr,
            local_cleanup=local_cleanup,
            remote_branch={
                **remote_branch,
                "delete_attempted": True,
                "deleted": True,
                "exists_after": None,
                "delete_error": error,
            },
            warnings=warnings + [error],
            error=error,
        )
    if exists_after:
        error = "Remote branch still exists after git push --delete"
        return _blocked_result(
            request=request,
            task=task,
            cleanup_recommendation=cleanup_recommendation,
            draft_pr=draft_pr,
            local_cleanup=local_cleanup,
            remote_branch={
                **remote_branch,
                "delete_attempted": True,
                "deleted": False,
                "exists_after": True,
                "delete_error": error,
            },
            warnings=warnings + [error],
            error=error,
        )

    artifact_payload = _remote_branch_cleanup_evidence(
        task_key=request.task_key,
        task_status=task.status,
        remote=request.remote,
        branch=resolved_branch,
        remote_branch_deleted=True,
        issue_closed=False,
        task_status_changed=False,
        task_completed=False,
        task_archived=False,
        cleanup_scope="remote_branch",
        requires_human_confirmation=True,
        confirmation_flag=EXPECTED_CONFIRM_FLAG,
        remote_branch_exists_before=bool(remote_branch.get("exists_before")),
        remote_branch_exists_after=False,
        delete_attempted=True,
        delete_error=None,
        cleanup_recommendation=cleanup_recommendation,
        draft_pr=draft_pr,
        local_cleanup=local_cleanup,
    )
    artifact_recorded, event_recorded, artifact_path = _record_remote_branch_cleanup_evidence(
        store=current_store,
        task=task,
        artifact_root=request.artifact_root,
        artifact_payload=artifact_payload,
    )

    return _success_result(
        request=request,
        task=task,
        cleanup_recommendation=cleanup_recommendation,
        draft_pr=draft_pr,
        local_cleanup=local_cleanup,
        remote_branch={
            **remote_branch,
            "delete_attempted": True,
            "deleted": True,
            "exists_after": False,
            "delete_error": None,
        },
        branch=resolved_branch,
        warnings=warnings,
        artifact_recorded=artifact_recorded,
        event_recorded=event_recorded,
        artifact_path=artifact_path,
    )


def _resolve_branch_name(
    *,
    request: RemoteBranchCleanupConfirmRequest,
    task: TaskRecord,
    worktree: Any,
    draft_pr: dict[str, Any],
    local_cleanup: dict[str, Any],
    recommendation: Any,
) -> tuple[str | None, list[str], str | None]:
    warnings: list[str] = []
    candidates: list[str] = []

    for candidate in (
        local_cleanup.get("local_branch"),
        draft_pr.get("head_branch"),
        recommendation.remote_branch.get("name"),
        worktree.branch,
    ):
        normalized = _normalize_branch_name(candidate)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    override = _normalize_branch_name(request.branch)
    if override is not None:
        branch_error = _validate_branch_name(override)
        if branch_error is not None:
            return None, warnings + [branch_error], branch_error
        if not candidates:
            error = "Branch override cannot be validated without trusted task branch evidence"
            return override, warnings + [error], error
        if override not in candidates:
            error = f"Provided branch override {override!r} does not match trusted task branch evidence"
            return override, warnings + [error], error
        if override in PROTECTED_BRANCHES:
            error = f"Protected branch cannot be deleted: {override}"
            return override, warnings + [error], error
        return override, warnings, None

    if not candidates:
        error = "Could not determine a verified task branch from evidence"
        return None, warnings, error

    if len(candidates) > 1:
        error = f"Branch evidence is inconsistent: {', '.join(candidates)}"
        return None, warnings, error

    branch = candidates[0]
    branch_error = _validate_branch_name(branch)
    if branch_error is not None:
        return branch, warnings + [branch_error], branch_error
    if branch in PROTECTED_BRANCHES:
        error = f"Protected branch cannot be deleted: {branch}"
        return branch, warnings + [error], error
    return branch, warnings, None


def _readiness_issues(
    *,
    request: RemoteBranchCleanupConfirmRequest,
    task: TaskRecord,
    draft_pr: dict[str, Any],
    local_cleanup: dict[str, Any],
    cleanup_recommendation: dict[str, Any],
    remote_branch: dict[str, Any],
    branch: str,
) -> list[str]:
    issues: list[str] = []

    if not cleanup_recommendation.get("available"):
        issues.append("Phase 6A cleanup recommendation is unavailable")
    if not cleanup_recommendation.get("merged"):
        issues.append("PR is not merged")
    if not cleanup_recommendation.get("remote_branch_cleanup_recommended"):
        issues.append("Phase 6A cleanup recommendation does not recommend remote branch cleanup")
    if not draft_pr.get("available"):
        issues.append("Draft PR evidence is missing")
    if not local_cleanup.get("available"):
        issues.append("Phase 6B local cleanup evidence is missing")

    local_cleanup_payload = local_cleanup.get("payload") or {}
    if local_cleanup.get("event_type") != LOCAL_EVENT_TYPE:
        issues.append("Local cleanup evidence event type must be local_cleanup_completed")
    if local_cleanup.get("artifact_kind") != LOCAL_ARTIFACT_KIND:
        issues.append("Local cleanup evidence artifact kind must be local_cleanup")
    if local_cleanup.get("confirmation_flag") != LOCAL_CONFIRM_FLAG:
        issues.append("Local cleanup evidence confirmation flag must be --confirm-local-cleanup")
    if local_cleanup_payload.get("cleanup_scope") != "local":
        issues.append("Local cleanup evidence cleanup_scope must be local")
    if not local_cleanup_payload.get("worktree_removed"):
        issues.append("Local cleanup evidence must indicate worktree_removed true")
    if local_cleanup_payload.get("remote_branch_deleted") is not False:
        issues.append("Local cleanup evidence must not indicate remote_branch_deleted true")
    if local_cleanup_payload.get("issue_closed") is not False:
        issues.append("Local cleanup evidence must not indicate issue_closed true")
    if local_cleanup_payload.get("task_status_changed") is not False:
        issues.append("Local cleanup evidence must not indicate task_status_changed true")
    if local_cleanup_payload.get("task_archived") is not False:
        issues.append("Local cleanup evidence must not indicate task_archived true")
    if local_cleanup_payload.get("task_completed") is not False:
        issues.append("Local cleanup evidence must not indicate task_completed true")

    if not remote_branch.get("exists_before"):
        issues.append("Remote branch is missing")
    if remote_branch.get("name") != branch:
        issues.append("Remote branch does not match the verified task branch")
    if remote_branch.get("protected"):
        issues.append("Remote branch is protected")
    if remote_branch.get("base_branch") and remote_branch.get("base_branch") == branch:
        issues.append("Remote branch must not match the base branch")
    if remote_branch.get("is_empty"):
        issues.append("Remote branch is empty")
    if local_cleanup_payload.get("task_status") and task.status != local_cleanup_payload.get("task_status"):
        issues.append("Task status does not match the local cleanup evidence")

    return _dedupe_preserve_order(issues)


def _inspect_remote_branch(
    *,
    repo_path: Path,
    remote: str,
    branch: str,
    base_branch: str,
    runner: Runner | None,
) -> dict[str, Any]:
    completed = _run_git(
        build_git_ls_remote_heads_command(remote, branch),
        cwd=repo_path,
        runner=runner,
    )
    warnings: list[str] = []
    exists = None
    if completed.returncode != 0:
        warnings.append(
            f"Could not inspect remote branch existence: {completed.stderr.strip() or completed.stdout.strip() or 'git ls-remote failed'}"
        )
    else:
        exists = bool(completed.stdout.strip())

    return {
        "available": True,
        "remote": remote,
        "name": branch,
        "base_branch": base_branch or None,
        "exists_before": exists,
        "exists_after": exists,
        "safe_to_delete": bool(exists),
        "deleted": False,
        "delete_attempted": False,
        "delete_error": None,
        "protected": branch in PROTECTED_BRANCHES,
        "is_empty": exists is False,
        "warnings": warnings,
    }


def _remote_branch_exists(
    *,
    repo_path: Path,
    remote: str,
    branch: str,
    runner: Runner | None,
) -> bool | None:
    completed = _run_git(
        build_git_ls_remote_heads_command(remote, branch),
        cwd=repo_path,
        runner=runner,
    )
    if completed.returncode != 0:
        return None
    return bool(completed.stdout.strip())


def _delete_remote_branch(
    *,
    request: RemoteBranchCleanupConfirmRequest,
    branch: str,
    runner: Runner | None,
) -> tuple[bool, str | None]:
    completed = _run_git(
        build_git_push_delete_command(request.remote, branch),
        cwd=request.repo_path,
        runner=runner,
    )
    if completed.returncode != 0:
        return False, (
            f"git push {request.remote} --delete {branch} failed with {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return True, None


def _read_draft_pr_evidence(store: TaskMirrorStore, task_key: str) -> dict[str, Any]:
    artifacts = [artifact for artifact in store.list_task_artifacts(task_key) if artifact.artifact_type == "draft_pr"]
    events = [event for event in store.list_task_events(task_key) if event.event_type == "draft_pr_created"]
    warnings: list[str] = []

    artifact_payload: dict[str, Any] | None = None
    artifact_path: Path | None = None
    if artifacts:
        artifact_path = artifacts[-1].path
        try:
            artifact_payload = _load_json_object(artifact_path)
        except OSError as exc:
            warnings.append(f"Could not read draft PR artifact: {exc}")
        except json.JSONDecodeError as exc:
            warnings.append(f"Draft PR artifact is not valid JSON: {exc}")

    event_payload = _latest_event_payload(events)
    evidence = artifact_payload or event_payload or {}
    available = bool(artifacts and events and isinstance(evidence, dict) and not warnings)
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


def _read_local_cleanup_evidence(store: TaskMirrorStore, task_key: str) -> dict[str, Any]:
    artifacts = [artifact for artifact in store.list_task_artifacts(task_key) if artifact.artifact_type == LOCAL_ARTIFACT_KIND]
    events = [event for event in store.list_task_events(task_key) if event.event_type == LOCAL_EVENT_TYPE]
    warnings: list[str] = []

    artifact_payload: dict[str, Any] | None = None
    artifact_path: Path | None = None
    if artifacts:
        artifact_path = artifacts[-1].path
        try:
            artifact_payload = _load_json_object(artifact_path)
        except OSError as exc:
            warnings.append(f"Could not read local cleanup artifact: {exc}")
        except json.JSONDecodeError as exc:
            warnings.append(f"Local cleanup artifact is not valid JSON: {exc}")

    event_payload = _latest_event_payload(events)
    evidence = artifact_payload or event_payload or {}
    available = bool(artifacts and events and isinstance(evidence, dict) and not warnings)

    return {
        "available": available,
        "artifact_recorded": bool(artifacts),
        "event_recorded": bool(events),
        "artifact_path": str(artifact_path) if artifact_path is not None else None,
        "event_type": LOCAL_EVENT_TYPE,
        "artifact_kind": LOCAL_ARTIFACT_KIND,
        "payload": evidence if isinstance(evidence, dict) else {},
        "local_branch": evidence.get("local_branch"),
        "cleanup_scope": evidence.get("cleanup_scope"),
        "worktree_removed": evidence.get("worktree_removed"),
        "local_branch_deleted": evidence.get("local_branch_deleted"),
        "remote_branch_deleted": evidence.get("remote_branch_deleted"),
        "issue_closed": evidence.get("issue_closed"),
        "task_status_changed": evidence.get("task_status_changed"),
        "task_completed": evidence.get("task_completed"),
        "task_archived": evidence.get("task_archived"),
        "requires_human_confirmation": evidence.get("requires_human_confirmation"),
        "confirmation_flag": evidence.get("confirmation_flag"),
        "task_status": evidence.get("task_status"),
        "warnings": warnings or ([] if available else ["Local cleanup evidence is missing"]),
    }


def _remote_branch_cleanup_evidence(
    *,
    task_key: str,
    task_status: str,
    remote: str,
    branch: str,
    remote_branch_deleted: bool,
    issue_closed: bool,
    task_status_changed: bool,
    task_completed: bool,
    task_archived: bool,
    cleanup_scope: str,
    requires_human_confirmation: bool,
    confirmation_flag: str,
    remote_branch_exists_before: bool,
    remote_branch_exists_after: bool | None,
    delete_attempted: bool,
    delete_error: str | None,
    cleanup_recommendation: dict[str, Any],
    draft_pr: dict[str, Any],
    local_cleanup: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "artifact_type": ARTIFACT_KIND,
        "kind": EVENT_TYPE,
        "task_key": task_key,
        "task_status": task_status,
        "remote": remote,
        "branch": branch,
        "remote_branch_deleted": remote_branch_deleted,
        "remote_branch_exists_before": remote_branch_exists_before,
        "remote_branch_exists_after": remote_branch_exists_after,
        "remote_branch_delete_attempted": delete_attempted,
        "remote_branch_delete_error": delete_error,
        "issue_closed": issue_closed,
        "task_status_changed": task_status_changed,
        "task_completed": task_completed,
        "task_archived": task_archived,
        "cleanup_scope": cleanup_scope,
        "requires_human_confirmation": requires_human_confirmation,
        "confirmation_flag": confirmation_flag,
        "cleanup_recommendation": cleanup_recommendation,
        "draft_pr": draft_pr,
        "local_cleanup": local_cleanup,
        "recorded_at": utc_now_iso(),
    }


def _record_remote_branch_cleanup_evidence(
    *,
    store: TaskMirrorStore,
    task: TaskRecord,
    artifact_root: Path | None,
    artifact_payload: dict[str, Any],
) -> tuple[bool, bool, Path | None]:
    output_root = _resolve_cleanup_artifact_root(task, artifact_root)
    artifact_path = output_root / task.task_key / "remote_branch_cleanup.json"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(artifact_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    store.record_task_artifact(task.task_key, ARTIFACT_KIND, artifact_path)
    store.record_task_event(
        task.task_key,
        EVENT_TYPE,
        SOURCE,
        message="Remote branch cleanup completed",
        payload=artifact_payload,
    )
    return True, True, artifact_path


def _resolve_cleanup_artifact_root(task: TaskRecord, artifact_root: Path | None) -> Path:
    if artifact_root is not None:
        return artifact_root / ARTIFACT_KIND
    if task.artifact_dir is not None:
        return task.artifact_dir.resolve().parent / ARTIFACT_KIND
    return task.repo_path / ".agent-taskflow" / "artifacts" / ARTIFACT_KIND


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise OSError(f"Could not read JSON file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise json.JSONDecodeError("JSON object required", doc="", pos=0)
    return payload


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


def _blocked_from_recommendation(
    *,
    request: RemoteBranchCleanupConfirmRequest,
    task: TaskRecord,
    recommendation: Any,
    draft_pr: dict[str, Any],
    local_cleanup: dict[str, Any],
    remote_branch: dict[str, Any],
) -> RemoteBranchCleanupConfirmResult:
    cleanup_recommendation = _cleanup_recommendation_snapshot(recommendation)
    warnings = list(recommendation.blocking_warnings) + list(recommendation.non_blocking_warnings)
    return _blocked_result(
        request=request,
        task=task,
        cleanup_recommendation=cleanup_recommendation,
        draft_pr=draft_pr,
        local_cleanup=local_cleanup,
        remote_branch=remote_branch,
        warnings=warnings,
        error=recommendation.error or recommendation.summary.get("next_phase") or "Cleanup recommendation is blocked",
    )


def _blocked_result(
    *,
    request: RemoteBranchCleanupConfirmRequest,
    task: TaskRecord,
    cleanup_recommendation: dict[str, Any],
    draft_pr: dict[str, Any],
    local_cleanup: dict[str, Any],
    remote_branch: dict[str, Any],
    warnings: list[str],
    error: str,
) -> RemoteBranchCleanupConfirmResult:
    return RemoteBranchCleanupConfirmResult(
        ok=False,
        status="blocked",
        task_key=request.task_key,
        task_status=task.status,
        cleanup_recommendation=cleanup_recommendation,
        draft_pr=draft_pr,
        local_cleanup=local_cleanup,
        remote_branch=remote_branch,
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_KIND,
            "artifact_path": None,
            "cleanup_scope": "remote_branch",
            "requires_human_confirmation": True,
            "confirmation_flag": EXPECTED_CONFIRM_FLAG,
        },
        next_allowed_actions=[
            "resolve blocking warnings",
            "rerun explicit remote branch cleanup confirm once the task branch is safe",
            "explicit task closeout / archive confirm in a later phase",
        ],
        actions_not_performed=[
            "remote branch deletion",
            "local branch deletion",
            "local worktree removal",
            "issue close",
            "task status update",
            "task archive",
            "task complete",
            "merge",
            "approval",
            "force deletion",
        ],
        summary={
            "remote_branch_cleanup_performed": False,
            "remote_branch_deleted": False,
            "issue_closed": False,
            "task_status_changed": False,
            "task_archived": False,
            "task_completed": False,
            "requires_human_review": True,
            "next_phase": "explicit_task_closeout_archive_confirm",
        },
        safety=_safety_block(
            human_confirmation_confirmed=False,
            remote_branch_cleanup_performed=False,
            remote_branch_deleted=False,
        ),
        warnings=_dedupe_preserve_order(warnings + [error]),
        blocking_warnings=_dedupe_preserve_order([error, *warnings]),
        performed=False,
        dry_run=request.dry_run,
        confirmation_required=not request.dry_run,
        remote_branch_cleanup_performed=False,
        artifact_recorded=False,
        event_recorded=False,
        error=error,
    )


def _preview_result(
    *,
    request: RemoteBranchCleanupConfirmRequest,
    task: TaskRecord,
    cleanup_recommendation: dict[str, Any],
    draft_pr: dict[str, Any],
    local_cleanup: dict[str, Any],
    remote_branch: dict[str, Any],
    branch: str,
    warnings: list[str],
) -> RemoteBranchCleanupConfirmResult:
    return RemoteBranchCleanupConfirmResult(
        ok=True,
        status="dry_run",
        task_key=request.task_key,
        task_status=task.status,
        cleanup_recommendation=cleanup_recommendation,
        draft_pr=draft_pr,
        local_cleanup=local_cleanup,
        remote_branch={
            **remote_branch,
            "name": branch,
            "safe_to_delete": bool(remote_branch.get("exists_before")),
            "delete_attempted": False,
            "deleted": False,
            "exists_after": remote_branch.get("exists_before"),
        },
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_KIND,
            "artifact_path": None,
            "cleanup_scope": "remote_branch",
            "requires_human_confirmation": True,
            "confirmation_flag": EXPECTED_CONFIRM_FLAG,
        },
        next_allowed_actions=[
            "manual verification of remote branch cleanup readiness",
            "explicit remote branch cleanup confirm with --confirm-remote-branch-delete",
            "explicit task closeout / archive confirm in a later phase",
        ],
        actions_not_performed=[
            "remote branch deletion",
            "local branch deletion",
            "local worktree removal",
            "issue close",
            "task status update",
            "task archive",
            "task complete",
            "merge",
            "approval",
            "force deletion",
        ],
        summary={
            "remote_branch_cleanup_performed": False,
            "remote_branch_deleted": False,
            "issue_closed": False,
            "task_status_changed": False,
            "task_archived": False,
            "task_completed": False,
            "requires_human_review": True,
            "next_phase": "explicit_task_closeout_archive_confirm",
        },
        safety=_safety_block(
            human_confirmation_confirmed=False,
            remote_branch_cleanup_performed=False,
            remote_branch_deleted=False,
        ),
        warnings=warnings,
        blocking_warnings=[],
        performed=False,
        dry_run=True,
        confirmation_required=True,
        remote_branch_cleanup_performed=False,
        artifact_recorded=False,
        event_recorded=False,
        error=None,
    )


def _success_result(
    *,
    request: RemoteBranchCleanupConfirmRequest,
    task: TaskRecord,
    cleanup_recommendation: dict[str, Any],
    draft_pr: dict[str, Any],
    local_cleanup: dict[str, Any],
    remote_branch: dict[str, Any],
    branch: str,
    warnings: list[str],
    artifact_recorded: bool,
    event_recorded: bool,
    artifact_path: Path | None,
) -> RemoteBranchCleanupConfirmResult:
    return RemoteBranchCleanupConfirmResult(
        ok=True,
        status="remote_branch_cleanup_completed",
        task_key=request.task_key,
        task_status=task.status,
        cleanup_recommendation=cleanup_recommendation,
        draft_pr=draft_pr,
        local_cleanup=local_cleanup,
        remote_branch=remote_branch,
        evidence={
            "artifact_recorded": artifact_recorded,
            "event_recorded": event_recorded,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_KIND,
            "artifact_path": str(artifact_path) if artifact_path is not None else None,
            "cleanup_scope": "remote_branch",
            "requires_human_confirmation": True,
            "confirmation_flag": EXPECTED_CONFIRM_FLAG,
        },
        next_allowed_actions=[
            "manual verification of remote branch cleanup",
            "explicit task closeout / archive confirm in a later phase",
        ],
        actions_not_performed=[
            "local branch deletion",
            "local worktree removal",
            "issue close",
            "task status update",
            "task archive",
            "task complete",
            "merge",
            "approval",
            "force deletion",
        ],
        summary={
            "remote_branch_cleanup_performed": True,
            "remote_branch_deleted": True,
            "issue_closed": False,
            "task_status_changed": False,
            "task_archived": False,
            "task_completed": False,
            "requires_human_review": True,
            "next_phase": "explicit_task_closeout_archive_confirm",
        },
        safety=_safety_block(
            human_confirmation_confirmed=True,
            remote_branch_cleanup_performed=True,
            remote_branch_deleted=True,
        ),
        warnings=warnings,
        blocking_warnings=[],
        performed=True,
        dry_run=False,
        confirmation_required=True,
        remote_branch_cleanup_performed=True,
        artifact_recorded=artifact_recorded,
        event_recorded=event_recorded,
        error=None,
    )


def _not_found_result(
    *,
    request: RemoteBranchCleanupConfirmRequest,
    error: str,
) -> RemoteBranchCleanupConfirmResult:
    return RemoteBranchCleanupConfirmResult(
        ok=False,
        status="not_found",
        task_key=request.task_key,
        task_status=None,
        cleanup_recommendation=_empty_cleanup_recommendation(),
        draft_pr=_empty_draft_pr_evidence(),
        local_cleanup=_empty_local_cleanup_evidence(),
        remote_branch=_empty_remote_branch(request.remote),
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_KIND,
            "artifact_path": None,
            "cleanup_scope": "remote_branch",
            "requires_human_confirmation": True,
            "confirmation_flag": EXPECTED_CONFIRM_FLAG,
        },
        next_allowed_actions=["resolve the missing task record and retry"],
        actions_not_performed=[
            "remote branch deletion",
            "local branch deletion",
            "local worktree removal",
            "issue close",
            "task status update",
            "task archive",
            "task complete",
            "merge",
            "approval",
            "force deletion",
        ],
        summary={
            "remote_branch_cleanup_performed": False,
            "remote_branch_deleted": False,
            "issue_closed": False,
            "task_status_changed": False,
            "task_archived": False,
            "task_completed": False,
            "requires_human_review": True,
            "next_phase": "explicit_task_closeout_archive_confirm",
        },
        safety=_safety_block(
            human_confirmation_confirmed=False,
            remote_branch_cleanup_performed=False,
            remote_branch_deleted=False,
        ),
        warnings=[error],
        blocking_warnings=[error],
        performed=False,
        dry_run=request.dry_run,
        confirmation_required=not request.dry_run,
        remote_branch_cleanup_performed=False,
        artifact_recorded=False,
        event_recorded=False,
        error=error,
    )


