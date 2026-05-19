"""Read-only post-merge cleanup recommendation for Agent Taskflow.

This module inspects the local task mirror, draft PR evidence, and read-only
Git / GitHub state to recommend cleanup actions after a PR has been merged
outside Agent Taskflow. It never deletes branches, removes worktrees, archives
artifacts, updates task status, or mutates GitHub.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
import json
from pathlib import Path
from typing import Any, Callable, Protocol

from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore, default_db_path
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.worktree import ensure_absolute_path


DEFAULT_REMOTE = "origin"
DEFAULT_REPO_JSON_FIELDS = (
    "number",
    "url",
    "state",
    "isDraft",
    "mergedAt",
    "mergeCommit",
    "headRefName",
    "baseRefName",
    "title",
)
ARTIFACT_TYPE = "draft_pr"
EVENT_TYPE = "draft_pr_created"
SOURCE = "post_merge_cleanup_recommendation"


class PostMergeCleanupRecommendationError(RuntimeError):
    """Raised when a cleanup recommendation cannot be generated safely."""


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., CompletedProcessLike]


@dataclass(frozen=True)
class PostMergeCleanupRecommendationRequest:
    """Request for a one-shot post-merge cleanup recommendation."""

    task_key: str
    repo: str
    repo_path: Path
    db_path: Path | None = None
    artifact_root: Path | None = None
    remote: str = DEFAULT_REMOTE
    pr_number: int | None = None
    pr_url: str | None = None
    offline_pr_json: Path | None = None
    allow_non_waiting: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        object.__setattr__(self, "repo", _normalize_repo(self.repo))
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

        if self.pr_number is not None and self.pr_number <= 0:
            raise ValueError("pr_number must be positive")
        if self.pr_url is not None and not self.pr_url.strip():
            raise ValueError("pr_url must not be empty")


@dataclass(frozen=True)
class PostMergeCleanupRecommendationResult:
    """Structured read-only cleanup recommendation result."""

    ok: bool
    status: str
    task_key: str
    task_status: str | None
    repo: str
    pr: dict[str, Any]
    draft_pr_evidence: dict[str, Any]
    workspace: dict[str, Any]
    local_branch: dict[str, Any]
    remote_branch: dict[str, Any]
    recommended_cleanup: list[dict[str, Any]]
    blocking_warnings: list[str]
    non_blocking_warnings: list[str]
    next_allowed_actions: list[str]
    actions_not_performed: list[str]
    summary: dict[str, Any]
    safety: dict[str, Any]
    performed: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(asdict(self), sort_keys=True))


def recommend_post_merge_cleanup(
    request: PostMergeCleanupRecommendationRequest,
    *,
    store: TaskMirrorStore | None = None,
    runner: Runner | None = None,
) -> PostMergeCleanupRecommendationResult:
    """Inspect read-only state and recommend post-merge cleanup actions."""

    db_path = request.db_path or default_db_path()
    if not db_path.exists():
        return _error_result(
            request=request,
            status="not_found",
            error=f"SQLite state DB not found: {db_path}",
            task_status=None,
            pr=_empty_pr(),
            draft_pr_evidence=_empty_draft_pr_evidence(),
            workspace=_empty_workspace(),
            local_branch=_empty_local_branch(),
            remote_branch=_empty_remote_branch(request.remote),
        )

    current_store = store or TaskMirrorStore(db_path)
    task = current_store.get_task(request.task_key)
    if task is None:
        return _error_result(
            request=request,
            status="not_found",
            error=f"Task not found: {request.task_key}",
            task_status=None,
            pr=_empty_pr(),
            draft_pr_evidence=_empty_draft_pr_evidence(),
            workspace=_empty_workspace(),
            local_branch=_empty_local_branch(),
            remote_branch=_empty_remote_branch(request.remote),
        )

    task_worktree = current_store.get_task_worktree(request.task_key)
    if task_worktree is None:
        return _error_result(
            request=request,
            status="blocked",
            error=f"TaskWorktreeRecord missing for task: {request.task_key}",
            task_status=task.status,
            pr=_empty_pr(),
            draft_pr_evidence=_empty_draft_pr_evidence(),
            workspace=_workspace_from_task(task, None),
            local_branch=_empty_local_branch(),
            remote_branch=_empty_remote_branch(request.remote),
            warnings=[
                "Local worktree state is missing from the task mirror; cleanup cannot be recommended safely",
            ],
        )

    warnings: list[str] = []
    if task.status != "waiting_approval" and not request.allow_non_waiting:
        warnings.append(
            f"Task status is {task.status}; cleanup recommendation is still read-only but this is not the waiting_approval state"
        )

    draft_pr_evidence = _read_draft_pr_evidence(current_store, request.task_key)
    if not draft_pr_evidence["available"]:
        return _error_result(
            request=request,
            status="blocked",
            error="Draft PR evidence is missing",
            task_status=task.status,
            pr=_empty_pr(),
            draft_pr_evidence=draft_pr_evidence,
            workspace=_workspace_from_task(task, task_worktree),
            local_branch=_empty_local_branch(task_worktree.branch),
            remote_branch=_empty_remote_branch(request.remote, task_worktree.branch),
            warnings=warnings + draft_pr_evidence["warnings"],
        )

    try:
        pr = _read_pr_status(
            request=request,
            draft_pr_evidence=draft_pr_evidence,
            runner=runner,
        )
    except PostMergeCleanupRecommendationError as exc:
        return _error_result(
            request=request,
            status="blocked",
            error=str(exc),
            task_status=task.status,
            pr=_empty_pr(),
            draft_pr_evidence=draft_pr_evidence,
            workspace=_workspace_from_task(task, task_worktree),
            local_branch=_empty_local_branch(task_worktree.branch),
            remote_branch=_empty_remote_branch(request.remote, task_worktree.branch),
            warnings=warnings + draft_pr_evidence["warnings"],
        )

    pr_merge_state = _normalize_merge_state(pr)
    merged = bool(pr_merge_state["merged"])
    pr_warnings = list(draft_pr_evidence["warnings"]) + list(pr_merge_state["warnings"])
    warnings.extend(w for w in pr_warnings if w not in warnings)

    workspace = _inspect_workspace(task, task_worktree, runner=runner)
    local_branch = _inspect_local_branch(task_worktree, runner=runner)
    remote_branch = _inspect_remote_branch(task_worktree, request.remote, runner=runner)

    warnings.extend(w for w in workspace["warnings"] if w not in warnings)
    warnings.extend(w for w in local_branch["warnings"] if w not in warnings)
    warnings.extend(w for w in remote_branch["warnings"] if w not in warnings)

    if not merged:
        return _not_merged_result(
            request=request,
            task=task,
            task_worktree=task_worktree,
            pr=pr,
            draft_pr_evidence=draft_pr_evidence,
            workspace=workspace,
            local_branch=local_branch,
            remote_branch=remote_branch,
            warnings=warnings,
        )

    recommended_cleanup = _build_recommendations(
        pr=pr,
        workspace=workspace,
        local_branch=local_branch,
        remote_branch=remote_branch,
    )
    blocking_warnings = [
        warning
        for warning in warnings
        if warning
        in {
            "PR status could not be checked",
        }
    ]
    non_blocking_warnings = [warning for warning in warnings if warning not in blocking_warnings]
    merged_commit = pr.get("merge_commit")
    merged_at = pr.get("merged_at")

    summary = {
        "merged": True,
        "merged_commit": merged_commit,
        "merged_at": merged_at,
        "cleanup_recommended": bool(recommended_cleanup),
        "requires_human_confirmation": True,
        "cleanup_performed": False,
        "next_phase": "explicit_cleanup_confirm",
        "task_status": task.status,
    }

    safety = _safety_block()
    return PostMergeCleanupRecommendationResult(
        ok=True,
        status="merged_recommend_cleanup",
        task_key=request.task_key,
        task_status=task.status,
        repo=request.repo,
        pr=pr,
        draft_pr_evidence=draft_pr_evidence,
        workspace=workspace,
        local_branch=local_branch,
        remote_branch=remote_branch,
        recommended_cleanup=recommended_cleanup,
        blocking_warnings=blocking_warnings,
        non_blocking_warnings=non_blocking_warnings,
        next_allowed_actions=[
            "manual review of cleanup recommendation",
            "explicit local cleanup confirm in a later phase",
            "explicit remote branch cleanup confirm in a later phase",
            "explicit task closeout confirm in a later phase",
        ],
        actions_not_performed=_actions_not_performed(),
        summary=summary,
        safety=safety,
        performed=False,
        error=None,
    )


def _not_merged_result(
    *,
    request: PostMergeCleanupRecommendationRequest,
    task: TaskRecord,
    task_worktree: TaskWorktreeRecord,
    pr: dict[str, Any],
    draft_pr_evidence: dict[str, Any],
    workspace: dict[str, Any],
    local_branch: dict[str, Any],
    remote_branch: dict[str, Any],
    warnings: list[str],
) -> PostMergeCleanupRecommendationResult:
    reason = "PR is not merged; cleanup should not proceed"
    summary = {
        "merged": False,
        "merged_commit": None,
        "merged_at": pr.get("merged_at"),
        "cleanup_recommended": False,
        "requires_human_confirmation": False,
        "cleanup_performed": False,
        "reason": reason,
        "next_phase": "wait_for_pr_merge",
        "task_status": task.status,
    }
    return PostMergeCleanupRecommendationResult(
        ok=True,
        status="not_merged",
        task_key=request.task_key,
        task_status=task.status,
        repo=request.repo,
        pr=pr,
        draft_pr_evidence=draft_pr_evidence,
        workspace=workspace,
        local_branch=local_branch,
        remote_branch=remote_branch,
        recommended_cleanup=[],
        blocking_warnings=[],
        non_blocking_warnings=_dedupe_preserve_order(warnings + [reason]),
        next_allowed_actions=[
            "monitor the merged state of the PR",
            "rerun the post-merge cleanup recommendation after the PR merges",
        ],
        actions_not_performed=_actions_not_performed(),
        summary=summary,
        safety=_safety_block(),
        performed=False,
        error=None,
    )


def _build_recommendations(
    *,
    pr: dict[str, Any],
    workspace: dict[str, Any],
    local_branch: dict[str, Any],
    remote_branch: dict[str, Any],
) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []

    worktree_exists = workspace.get("exists")
    worktree_clean = workspace.get("has_uncommitted_changes") is False
    worktree_missing = worktree_exists is False
    if worktree_missing:
        recommendations.append(
            _cleanup_item(
                "remove_local_worktree",
                recommended=False,
                risk_level="medium",
                reason="Worktree path is missing on disk",
                blockers=["worktree path missing"],
            )
        )
    elif worktree_exists is True and worktree_clean:
        recommendations.append(
            _cleanup_item(
                "remove_local_worktree",
                recommended=True,
                risk_level="medium",
                reason="PR is merged and the worktree is clean",
            )
        )
    else:
        blockers = ["worktree has uncommitted changes"] if workspace.get("has_uncommitted_changes") is True else ["worktree state is unavailable"]
        recommendations.append(
            _cleanup_item(
                "remove_local_worktree",
                recommended=False,
                risk_level="high",
                reason=(
                    "PR is merged but the worktree has uncommitted changes"
                    if workspace.get("has_uncommitted_changes") is True
                    else "PR is merged but the worktree state is not safe to remove yet"
                ),
                blockers=blockers,
            )
        )

    branch_exists = local_branch.get("exists")
    branch_merged = local_branch.get("merged_into_base")
    if branch_exists is True and branch_merged is True:
        recommendations.append(
            _cleanup_item(
                "delete_local_branch",
                recommended=True,
                risk_level="medium",
                reason="PR is merged and the local branch is merged into the base branch",
            )
        )
    elif branch_exists is True and branch_merged is False:
        recommendations.append(
            _cleanup_item(
                "delete_local_branch",
                recommended=False,
                risk_level="high",
                reason="Local branch is not merged into the base branch",
                blockers=["branch not merged into base"],
            )
        )
    else:
        recommendations.append(
            _cleanup_item(
                "delete_local_branch",
                recommended=False,
                risk_level="medium",
                reason="Local branch is missing or could not be inspected",
                blockers=["local branch missing or unavailable"],
            )
        )

    remote_exists = remote_branch.get("exists")
    if remote_exists is True:
        recommendations.append(
            _cleanup_item(
                "delete_remote_branch",
                recommended=True,
                risk_level="high",
                reason="PR is merged and the remote branch still exists",
            )
        )
    elif remote_exists is False:
        recommendations.append(
            _cleanup_item(
                "delete_remote_branch",
                recommended=False,
                risk_level="high",
                reason="Remote branch is already absent",
                blockers=["remote branch missing"],
            )
        )
    else:
        recommendations.append(
            _cleanup_item(
                "delete_remote_branch",
                recommended=False,
                risk_level="high",
                reason="Remote branch existence could not be checked",
                blockers=["remote branch state unavailable"],
            )
        )

    recommendations.append(
        _cleanup_item(
            "mark_task_complete",
            recommended=True,
            risk_level="medium",
            reason="PR is merged; task closeout can happen in a later explicit phase",
        )
    )

    return recommendations


def _cleanup_item(
    action: str,
    *,
    recommended: bool,
    risk_level: str,
    reason: str,
    blockers: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "recommended": recommended,
        "requires_human_confirmation": True,
        "risk_level": risk_level,
        "reason": reason,
        "blockers": blockers or [],
        "performed": False,
    }


def _read_draft_pr_evidence(
    store: TaskMirrorStore,
    task_key: str,
) -> dict[str, Any]:
    artifacts = [artifact for artifact in store.list_task_artifacts(task_key) if artifact.artifact_type == ARTIFACT_TYPE]
    events = [event for event in store.list_task_events(task_key) if event.event_type == EVENT_TYPE]

    warnings: list[str] = []
    artifact_payload: dict[str, Any] | None = None
    artifact_path = None
    if artifacts:
        artifact_path = artifacts[-1].path
        try:
            artifact_payload = _load_json_object(artifact_path)
        except PostMergeCleanupRecommendationError as exc:
            warnings.append(str(exc))
    elif events:
        warnings.append("Draft PR artifact record is missing; falling back to event payload")

    event_payload = _latest_event_payload(events)
    available = bool(artifacts or events)

    evidence = artifact_payload or event_payload or {}
    repo = evidence.get("repo")
    pr_number = evidence.get("pr_number")
    pr_url = evidence.get("pr_url")
    base_branch = evidence.get("base_branch")
    head_branch = evidence.get("head_branch")

    if not available:
        return {
            "available": False,
            "artifact_recorded": bool(artifacts),
            "event_recorded": bool(events),
            "artifact_kind": ARTIFACT_TYPE,
            "event_type": EVENT_TYPE,
            "artifact_path": str(artifact_path) if artifact_path is not None else None,
            "repo": None,
            "pr_number": None,
            "pr_url": None,
            "base_branch": None,
            "head_branch": None,
            "draft": None,
            "merged": None,
            "approved": None,
            "cleanup_performed": None,
            "issue_closed": None,
            "requires_human_confirmation": None,
            "warnings": ["Draft PR evidence is missing"],
        }

    required_fields = [repo, pr_number, pr_url, base_branch, head_branch]
    if any(value in {None, ""} for value in required_fields):
        warnings.append("Draft PR evidence is incomplete")

    return {
        "available": bool(artifacts and events and artifact_payload is not None and not warnings),
        "artifact_recorded": bool(artifacts),
        "event_recorded": bool(events),
        "artifact_kind": ARTIFACT_TYPE,
        "event_type": EVENT_TYPE,
        "artifact_path": str(artifact_path) if artifact_path is not None else None,
        "repo": repo,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "base_branch": base_branch,
        "head_branch": head_branch,
        "draft": evidence.get("draft"),
        "merged": evidence.get("merged"),
        "approved": evidence.get("approved"),
        "cleanup_performed": evidence.get("cleanup_performed"),
        "issue_closed": evidence.get("issue_closed"),
        "requires_human_confirmation": evidence.get("requires_human_confirmation"),
        "warnings": warnings,
    }


def _read_pr_status(
    *,
    request: PostMergeCleanupRecommendationRequest,
    draft_pr_evidence: dict[str, Any],
    runner: Runner | None,
) -> dict[str, Any]:
    if request.offline_pr_json is not None:
        payload = _load_json_object(request.offline_pr_json)
    else:
        selector = _resolve_pr_selector(request, draft_pr_evidence)
        completed = _run_command(
            [
                "gh",
                "pr",
                "view",
                selector,
                "--repo",
                request.repo,
                "--json",
                ",".join(DEFAULT_REPO_JSON_FIELDS),
            ],
            cwd=request.repo_path,
            runner=runner,
        )
        if completed.returncode != 0:
            raise PostMergeCleanupRecommendationError(
                f"gh pr view failed with {completed.returncode}: {completed.stderr.strip()}"
            )
        payload = _parse_json_object(completed.stdout, source="gh pr view")

    normalized = _normalize_pr_payload(payload)
    _validate_pr_payload(
        normalized,
        request=request,
        draft_pr_evidence=draft_pr_evidence,
    )
    return normalized


def _resolve_pr_selector(
    request: PostMergeCleanupRecommendationRequest,
    draft_pr_evidence: dict[str, Any],
) -> str:
    if request.pr_url is not None:
        if draft_pr_evidence.get("pr_url") and draft_pr_evidence["pr_url"] != request.pr_url:
            raise PostMergeCleanupRecommendationError(
                "Provided pr_url does not match the draft PR evidence"
            )
        return request.pr_url
    if request.pr_number is not None:
        if draft_pr_evidence.get("pr_number") not in {None, request.pr_number}:
            raise PostMergeCleanupRecommendationError(
                "Provided pr_number does not match the draft PR evidence"
            )
        return str(request.pr_number)
    if draft_pr_evidence.get("pr_url"):
        return str(draft_pr_evidence["pr_url"])
    if draft_pr_evidence.get("pr_number") is not None:
        return str(draft_pr_evidence["pr_number"])
    raise PostMergeCleanupRecommendationError("Draft PR evidence does not include a PR selector")


def _validate_pr_payload(
    payload: dict[str, Any],
    *,
    request: PostMergeCleanupRecommendationRequest,
    draft_pr_evidence: dict[str, Any],
) -> None:
    if draft_pr_evidence.get("repo") and draft_pr_evidence["repo"] != request.repo:
        raise PostMergeCleanupRecommendationError("Draft PR evidence repo does not match the requested repo")

    evidence_pr_number = draft_pr_evidence.get("pr_number")
    if request.pr_number is not None and evidence_pr_number not in {None, request.pr_number}:
        raise PostMergeCleanupRecommendationError("Provided pr_number does not match draft PR evidence")
    evidence_pr_url = draft_pr_evidence.get("pr_url")
    if request.pr_url is not None and evidence_pr_url not in {None, request.pr_url}:
        raise PostMergeCleanupRecommendationError("Provided pr_url does not match draft PR evidence")

    if payload.get("number") is None and evidence_pr_number is None:
        raise PostMergeCleanupRecommendationError("GitHub PR payload did not include a PR number")
    if payload.get("url") is None and evidence_pr_url is None:
        raise PostMergeCleanupRecommendationError("GitHub PR payload did not include a PR URL")

    payload_repo_state = str(payload.get("state") or "").strip().upper()
    if payload_repo_state not in {"OPEN", "MERGED", "CLOSED"}:
        raise PostMergeCleanupRecommendationError("GitHub PR payload did not include a valid state")

    evidence_head = draft_pr_evidence.get("head_branch")
    if evidence_head and payload.get("head_ref_name") and payload["head_ref_name"] != evidence_head:
        raise PostMergeCleanupRecommendationError("GitHub PR head branch does not match the draft PR evidence")
    evidence_base = draft_pr_evidence.get("base_branch")
    if evidence_base and payload.get("base_ref_name") and payload["base_ref_name"] != evidence_base:
        raise PostMergeCleanupRecommendationError("GitHub PR base branch does not match the draft PR evidence")


def _normalize_pr_payload(payload: dict[str, Any]) -> dict[str, Any]:
    merge_commit = payload.get("mergeCommit")
    normalized_merge_commit: str | None
    if isinstance(merge_commit, dict):
        normalized_merge_commit = merge_commit.get("oid") if isinstance(merge_commit.get("oid"), str) else None
    elif isinstance(merge_commit, str):
        normalized_merge_commit = merge_commit
    else:
        normalized_merge_commit = None

    return {
        "available": True,
        "number": payload.get("number"),
        "url": payload.get("url"),
        "state": str(payload.get("state") or "").strip().upper() or None,
        "is_draft": bool(payload.get("isDraft")) if payload.get("isDraft") is not None else None,
        "merged_at": payload.get("mergedAt"),
        "merge_commit": normalized_merge_commit,
        "head_ref_name": payload.get("headRefName"),
        "base_ref_name": payload.get("baseRefName"),
        "title": payload.get("title"),
        "warnings": [],
    }


def _normalize_merge_state(pr: dict[str, Any]) -> dict[str, Any]:
    warnings: list[str] = []
    state = str(pr.get("state") or "").upper()
    merged_at = pr.get("merged_at")
    merge_commit = pr.get("merge_commit")
    merged = state == "MERGED" or merged_at is not None

    if merged and merged_at is not None and merge_commit is None:
        warnings.append("PR is merged but mergeCommit is unavailable; mergedAt was used as merge evidence")
    if state == "MERGED" and merged_at is None:
        warnings.append("PR state is MERGED but mergedAt is missing")

    return {
        "merged": merged,
        "warnings": warnings,
    }


def _inspect_workspace(
    task: TaskRecord,
    task_worktree: TaskWorktreeRecord,
    *,
    runner: Runner | None,
) -> dict[str, Any]:
    worktree_path = task_worktree.worktree_path
    exists = worktree_path.exists()
    has_uncommitted_changes: bool | None = None
    warnings: list[str] = []

    if exists:
        completed = _run_command(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=worktree_path,
            runner=runner,
        )
        if completed.returncode != 0:
            warnings.append(
                f"Could not inspect worktree status: {completed.stderr.strip() or 'git status failed'}"
            )
        else:
            has_uncommitted_changes = bool(completed.stdout.strip())
    else:
        warnings.append(f"Worktree path is missing on disk: {worktree_path}")

    return {
        "available": True,
        "repo_path": str(task.repo_path),
        "worktree_path": str(worktree_path),
        "exists": exists,
        "branch": task_worktree.branch,
        "base_branch": task_worktree.base_branch,
        "base_sha": task_worktree.base_sha,
        "has_uncommitted_changes": has_uncommitted_changes,
        "warnings": warnings,
    }


def _inspect_local_branch(
    task_worktree: TaskWorktreeRecord,
    *,
    runner: Runner | None,
) -> dict[str, Any]:
    branch = task_worktree.branch
    repo_path = task_worktree.repo_path
    branch_exists = False
    merged_into_base: bool | None = None
    warnings: list[str] = []

    branch_list = _run_command(
        ["git", "branch", "--list", branch],
        cwd=repo_path,
        runner=runner,
    )
    if branch_list.returncode != 0:
        warnings.append(
            f"Could not inspect local branch existence: {branch_list.stderr.strip() or 'git branch --list failed'}"
        )
    else:
        branch_exists = bool(branch_list.stdout.strip())

    if branch_exists and task_worktree.base_branch:
        merged = _run_command(
            ["git", "branch", "--merged", task_worktree.base_branch],
            cwd=repo_path,
            runner=runner,
        )
        if merged.returncode != 0:
            warnings.append(
                f"Could not inspect whether the branch is merged into {task_worktree.base_branch}: {merged.stderr.strip() or 'git branch --merged failed'}"
            )
        else:
            merged_into_base = branch in {line.strip().lstrip("* ").strip() for line in merged.stdout.splitlines() if line.strip()}

    return {
        "available": True,
        "name": branch,
        "exists": branch_exists,
        "merged_into_base": merged_into_base,
        "has_uncommitted_changes": None,
        "warnings": warnings,
    }


def _inspect_remote_branch(
    task_worktree: TaskWorktreeRecord,
    remote: str,
    *,
    runner: Runner | None,
) -> dict[str, Any]:
    branch = task_worktree.branch
    completed = _run_command(
        ["git", "ls-remote", "--heads", remote, branch],
        cwd=task_worktree.repo_path,
        runner=runner,
    )
    warnings: list[str] = []
    exists: bool | None = None
    if completed.returncode != 0:
        warnings.append(
            f"Could not inspect remote branch existence: {completed.stderr.strip() or 'git ls-remote failed'}"
        )
    else:
        exists = bool(completed.stdout.strip())

    return {
        "available": True,
        "remote": remote,
        "name": branch,
        "exists": exists,
        "warnings": warnings,
    }


def _load_json_object(path: Path, *, source: str | None = None) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PostMergeCleanupRecommendationError(f"Could not read JSON file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        label = source or str(path)
        raise PostMergeCleanupRecommendationError(f"{label} returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        label = source or str(path)
        raise PostMergeCleanupRecommendationError(f"{label} did not contain a JSON object")
    return payload


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
    if not isinstance(payload, dict):
        return {}
    return payload


def _read_only_error_result(
    *,
    request: PostMergeCleanupRecommendationRequest,
    status: str,
    error: str,
    task_status: str | None,
    pr: dict[str, Any],
    draft_pr_evidence: dict[str, Any],
    workspace: dict[str, Any],
    local_branch: dict[str, Any],
    remote_branch: dict[str, Any],
    warnings: list[str] | None = None,
) -> PostMergeCleanupRecommendationResult:
    resolved_warnings = warnings or []
    summary = {
        "merged": False,
        "merged_commit": None,
        "cleanup_recommended": False,
        "requires_human_confirmation": False,
        "cleanup_performed": False,
        "reason": error,
        "next_phase": "resolve_blocking_warnings",
        "task_status": task_status,
    }
    return PostMergeCleanupRecommendationResult(
        ok=False,
        status=status,
        task_key=request.task_key,
        task_status=task_status,
        repo=request.repo,
        pr=pr,
        draft_pr_evidence=draft_pr_evidence,
        workspace=workspace,
        local_branch=local_branch,
        remote_branch=remote_branch,
        recommended_cleanup=[],
        blocking_warnings=_dedupe_preserve_order([error, *resolved_warnings]),
        non_blocking_warnings=[],
        next_allowed_actions=[
            "resolve blocking warnings",
            "rerun the post-merge cleanup recommendation after evidence is complete",
        ],
        actions_not_performed=_actions_not_performed(),
        summary=summary,
        safety=_safety_block(),
        performed=False,
        error=error,
    )


def _error_result(
    *,
    request: PostMergeCleanupRecommendationRequest,
    status: str,
    error: str,
    task_status: str | None,
    pr: dict[str, Any],
    draft_pr_evidence: dict[str, Any],
    workspace: dict[str, Any],
    local_branch: dict[str, Any],
    remote_branch: dict[str, Any],
    warnings: list[str] | None = None,
) -> PostMergeCleanupRecommendationResult:
    return _read_only_error_result(
        request=request,
        status=status,
        error=error,
        task_status=task_status,
        pr=pr,
        draft_pr_evidence=draft_pr_evidence,
        workspace=workspace,
        local_branch=local_branch,
        remote_branch=remote_branch,
        warnings=warnings,
    )


def _empty_pr() -> dict[str, Any]:
    return {
        "available": False,
        "number": None,
        "url": None,
        "state": None,
        "is_draft": None,
        "merged_at": None,
        "merge_commit": None,
        "head_ref_name": None,
        "base_ref_name": None,
        "title": None,
        "warnings": [],
    }


def _empty_draft_pr_evidence() -> dict[str, Any]:
    return {
        "available": False,
        "artifact_recorded": False,
        "event_recorded": False,
        "artifact_kind": ARTIFACT_TYPE,
        "event_type": EVENT_TYPE,
        "artifact_path": None,
        "repo": None,
        "pr_number": None,
        "pr_url": None,
        "base_branch": None,
        "head_branch": None,
        "draft": None,
        "merged": None,
        "approved": None,
        "cleanup_performed": None,
        "issue_closed": None,
        "requires_human_confirmation": None,
        "warnings": ["Draft PR evidence is missing"],
    }


def _workspace_from_task(
    task: TaskRecord,
    task_worktree: TaskWorktreeRecord | None,
) -> dict[str, Any]:
    if task_worktree is None:
        return {
            "available": False,
            "repo_path": str(task.repo_path),
            "worktree_path": None,
            "exists": False,
            "branch": None,
            "base_branch": None,
            "base_sha": None,
            "has_uncommitted_changes": None,
            "warnings": ["Local worktree state is missing from the task mirror"],
        }
    return {
        "available": True,
        "repo_path": str(task_worktree.repo_path),
        "worktree_path": str(task_worktree.worktree_path),
        "exists": task_worktree.worktree_path.exists(),
        "branch": task_worktree.branch,
        "base_branch": task_worktree.base_branch,
        "base_sha": task_worktree.base_sha,
        "has_uncommitted_changes": None,
        "warnings": [],
    }


def _empty_workspace() -> dict[str, Any]:
    return {
        "available": False,
        "repo_path": None,
        "worktree_path": None,
        "exists": False,
        "branch": None,
        "base_branch": None,
        "base_sha": None,
        "has_uncommitted_changes": None,
        "warnings": [],
    }


def _empty_local_branch(branch: str | None = None) -> dict[str, Any]:
    return {
        "available": False,
        "name": branch,
        "exists": False,
        "merged_into_base": None,
        "has_uncommitted_changes": None,
        "warnings": [],
    }


def _empty_remote_branch(remote: str, branch: str | None = None) -> dict[str, Any]:
    return {
        "available": False,
        "remote": remote,
        "name": branch,
        "exists": None,
        "warnings": [],
    }


def _safety_block() -> dict[str, Any]:
    return {
        "recommendation_only": True,
        "read_only": True,
        "task_status_changed": False,
        "db_written": False,
        "artifact_written": False,
        "cleanup_performed": False,
        "local_branch_deleted": False,
        "remote_branch_deleted": False,
        "worktree_removed": False,
        "issue_closed": False,
        "merged": False,
        "approved": False,
        "github_mutated": False,
        "background_worker_started": False,
    }


def _actions_not_performed() -> list[str]:
    return [
        "local worktree removal",
        "local branch deletion",
        "remote branch deletion",
        "task status update",
        "artifact archive",
        "issue close",
        "merge",
        "approval",
        "cleanup",
    ]


def _run_command(
    command: list[str],
    *,
    cwd: Path | None,
    runner: Runner | None,
) -> CompletedProcessLike:
    try:
        return (runner or _default_runner)(
            command,
            cwd=cwd,
            shell=False,
            check=False,
            text=True,
            stdout=_PIPE,
            stderr=_PIPE,
        )
    except OSError as exc:  # pragma: no cover - defensive runtime guard
        raise PostMergeCleanupRecommendationError(str(exc)) from exc


def _default_runner(*args: Any, **kwargs: Any) -> CompletedProcessLike:
    import subprocess

    return subprocess.run(*args, **kwargs)


def _parse_json_object(stdout: str, *, source: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise PostMergeCleanupRecommendationError(f"{source} returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise PostMergeCleanupRecommendationError(f"{source} returned non-object JSON")
    return payload


def _normalize_repo(repo: str) -> str:
    normalized = repo.strip()
    if not normalized:
        raise ValueError("repo must not be empty")
    if normalized.startswith("-") or any(ch.isspace() for ch in normalized):
        raise ValueError("repo must be a simple owner/name string")
    if normalized.count("/") != 1:
        raise ValueError("repo must be an owner/name string")
    return normalized


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


# ``subprocess.PIPE`` is imported lazily in _run_command to keep the public
# module surface small and make the helper easy to monkeypatch in tests.
_PIPE = __import__("subprocess").PIPE
