"""Explicit task closeout confirmation after merged PR and cleanup evidence.

This module performs only local task lifecycle closeout. It verifies the
merged PR, verified draft PR evidence, local cleanup evidence, and remote
branch cleanup evidence before optionally updating the local task status and
recording closeout evidence. It does not close GitHub issues, delete branches,
remove worktrees, merge, approve, or mutate GitHub.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import subprocess
from typing import Any, Callable, Protocol

from agent_taskflow._helpers import (
    dedupe_non_empty_preserve_order as _dedupe_preserve_order,
)
from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.models import TaskRecord, utc_now_iso, validate_task_status
from agent_taskflow.store import TaskMirrorStore, default_db_path
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.worktree import ensure_absolute_path


ARTIFACT_KIND = "task_closeout"
EVENT_TYPE = "task_closeout_completed"
SOURCE = "task_closeout_confirm"
DEFAULT_REMOTE = "origin"
DEFAULT_TARGET_STATUS = "completed"
TERMINAL_STATUSES = {"completed", "done"}
ELIGIBLE_MUTATION_STATUSES = {"waiting_approval"}
PR_JSON_FIELDS = (
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


class TaskCloseoutConfirmError(RuntimeError):
    """Raised when task closeout cannot proceed safely."""


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., CompletedProcessLike]


@dataclass(frozen=True)
class TaskCloseoutConfirmRequest:
    """Request for previewing or confirming task closeout."""

    task_key: str
    repo: str
    repo_path: Path
    db_path: Path | None = None
    artifact_root: Path | None = None
    offline_pr_json: Path | None = None
    target_status: str = DEFAULT_TARGET_STATUS
    dry_run: bool = False
    confirm_task_closeout: bool = False
    remote: str = DEFAULT_REMOTE

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        object.__setattr__(self, "repo", _normalize_repo(self.repo))
        object.__setattr__(self, "target_status", _normalize_target_status(self.target_status))
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
class TaskCloseoutConfirmResult:
    """Structured task closeout preview or confirmation result."""

    ok: bool
    status: str
    task_key: str
    task_status: str | None
    previous_task_status: str | None
    new_task_status: str | None
    repo: str
    pr: dict[str, Any]
    draft_pr: dict[str, Any]
    local_cleanup: dict[str, Any]
    remote_branch_cleanup: dict[str, Any]
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
    artifact_recorded: bool
    event_recorded: bool
    task_status_changed: bool
    db_written: bool
    task_closeout_performed: bool
    closeout_ready: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(asdict(self), sort_keys=True))


def confirm_task_closeout(
    request: TaskCloseoutConfirmRequest,
    *,
    store: TaskMirrorStore | None = None,
    runner: Runner | None = None,
) -> TaskCloseoutConfirmResult:
    """Preview or confirm task closeout after merged PR and cleanup evidence."""

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

    draft_pr = _read_draft_pr_evidence(current_store, request.task_key)
    local_cleanup = _read_local_cleanup_evidence(current_store, request.task_key)
    remote_branch_cleanup = _read_remote_branch_cleanup_evidence(current_store, request.task_key)

    if not draft_pr["available"]:
        return _blocked_result(
            request=request,
            task=task,
            pr=_empty_pr(),
            draft_pr=draft_pr,
            local_cleanup=local_cleanup,
            remote_branch_cleanup=remote_branch_cleanup,
            remote_branch=_empty_remote_branch_state(),
            warnings=list(draft_pr["warnings"]),
            error=draft_pr["warnings"][0] if draft_pr["warnings"] else "Draft PR evidence is missing",
        )

    pr = _read_pr_status(
        request=request,
        draft_pr=draft_pr,
        runner=runner,
    )
    remote_branch = _inspect_remote_branch(
        request=request,
        branch=_resolve_branch_name(draft_pr=draft_pr, local_cleanup=local_cleanup, remote_branch_cleanup=remote_branch_cleanup),
        runner=runner,
    )

    warnings = _dedupe_preserve_order(
        list(draft_pr["warnings"])
        + list(local_cleanup["warnings"])
        + list(remote_branch_cleanup["warnings"])
        + list(pr["warnings"])
        + list(remote_branch["warnings"])
    )

    readiness_issues = _readiness_issues(
        request=request,
        task=task,
        draft_pr=draft_pr,
        pr=pr,
        local_cleanup=local_cleanup,
        remote_branch_cleanup=remote_branch_cleanup,
        remote_branch=remote_branch,
    )

    if task.status in TERMINAL_STATUSES:
        if _task_closeout_evidence_exists(current_store, request.task_key) and not readiness_issues:
            return _already_completed_result(
                request=request,
                task=task,
                pr=pr,
                draft_pr=draft_pr,
                local_cleanup=local_cleanup,
                remote_branch_cleanup=remote_branch_cleanup,
                warnings=warnings,
            )
        if not readiness_issues:
            readiness_issues = [
                f"Task {request.task_key} is already terminal with status {task.status}, but closeout evidence is missing or inconsistent",
            ]

    if readiness_issues:
        return _blocked_result(
            request=request,
            task=task,
            pr=pr,
            draft_pr=draft_pr,
            local_cleanup=local_cleanup,
            remote_branch_cleanup=remote_branch_cleanup,
            remote_branch=remote_branch,
            warnings=warnings,
            error=readiness_issues[0],
        )

    if request.dry_run:
        return _dry_run_result(
            request=request,
            task=task,
            pr=pr,
            draft_pr=draft_pr,
            local_cleanup=local_cleanup,
            remote_branch_cleanup=remote_branch_cleanup,
            remote_branch=remote_branch,
            warnings=warnings,
        )

    if not request.confirm_task_closeout:
        return _blocked_result(
            request=request,
            task=task,
            pr=pr,
            draft_pr=draft_pr,
            local_cleanup=local_cleanup,
            remote_branch_cleanup=remote_branch_cleanup,
            remote_branch=remote_branch,
            warnings=warnings,
            error="Missing required --confirm-task-closeout flag",
        )

    previous_status = task.status
    target_status = request.target_status
    current_store.update_task_status(
        request.task_key,
        target_status,
        source=SOURCE,
        message="Task closeout confirmed after merged PR and cleanup evidence",
    )

    evidence = _task_closeout_evidence(
        task_key=request.task_key,
        previous_task_status=previous_status,
        new_task_status=target_status,
        repo=request.repo,
        pr=pr,
        draft_pr=draft_pr,
        local_cleanup=local_cleanup,
        remote_branch_cleanup=remote_branch_cleanup,
    )
    artifact_path = _record_task_closeout_evidence(
        store=current_store,
        task=task,
        artifact_root=request.artifact_root,
        artifact_payload=evidence,
    )

    updated_task = current_store.get_task(request.task_key)
    return _success_result(
        request=request,
        task=updated_task or task,
        previous_task_status=previous_status,
        new_task_status=target_status,
        pr=pr,
        draft_pr=draft_pr,
        local_cleanup=local_cleanup,
        remote_branch_cleanup=remote_branch_cleanup,
        remote_branch=remote_branch,
        warnings=warnings,
        artifact_path=artifact_path,
    )


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

    issues: list[str] = []
    if not artifacts:
        issues.append("Draft PR artifact record is missing")
    if not events:
        issues.append("Draft PR event is missing")
    if not isinstance(evidence, dict):
        issues.append("Draft PR evidence is not a JSON object")
    if evidence.get("verified") is not True:
        issues.append("Draft PR evidence must be verified")
    if evidence.get("pr_created") is not True:
        issues.append("Draft PR evidence must indicate pr_created true")
    if evidence.get("draft_pr_created") is not True:
        issues.append("Draft PR evidence must indicate draft_pr_created true")
    if evidence.get("issue_closed") is not False:
        issues.append("Draft PR evidence must not indicate issue_closed true")
    if evidence.get("merged") is not False:
        issues.append("Draft PR evidence must record merged false before PR merge")
    if not evidence.get("pr_number") or not evidence.get("pr_url"):
        issues.append("Draft PR evidence must include pr_number and pr_url")
    if evidence.get("repo") is None:
        issues.append("Draft PR evidence must include repo")
    if artifacts and events:
        artifact_payload = artifact_payload or {}
        event_payload = event_payload or {}
        for field in (
            "repo",
            "pr_number",
            "pr_url",
            "base_branch",
            "head_branch",
            "verified",
            "pr_created",
            "draft_pr_created",
            "issue_closed",
            "merged",
        ):
            if artifact_payload.get(field) != event_payload.get(field):
                issues.append(f"Draft PR artifact and event payloads disagree on {field}")
                break

    warnings.extend(issues)
    return {
        "available": available and not issues,
        "artifact_recorded": bool(artifacts),
        "event_recorded": bool(events),
        "artifact_kind": ARTIFACT_KIND,
        "event_type": "draft_pr_created",
        "artifact_path": str(artifact_path) if artifact_path is not None else None,
        "repo": evidence.get("repo"),
        "pr_number": evidence.get("pr_number"),
        "pr_url": evidence.get("pr_url"),
        "base_branch": evidence.get("base_branch"),
        "head_branch": evidence.get("head_branch"),
        "verified": evidence.get("verified") is True,
        "pr_created": evidence.get("pr_created") is True,
        "draft_pr_created": evidence.get("draft_pr_created") is True,
        "merged": evidence.get("merged") is True,
        "issue_closed": evidence.get("issue_closed") is True,
        "requires_human_confirmation": evidence.get("requires_human_confirmation"),
        "warnings": _dedupe_preserve_order(warnings or ([] if available else ["Draft PR evidence is missing"])),
    }


def _read_local_cleanup_evidence(store: TaskMirrorStore, task_key: str) -> dict[str, Any]:
    artifacts = [artifact for artifact in store.list_task_artifacts(task_key) if artifact.artifact_type == "local_cleanup"]
    events = [event for event in store.list_task_events(task_key) if event.event_type == "local_cleanup_completed"]
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

    issues: list[str] = []
    if not artifacts:
        issues.append("Local cleanup artifact record is missing")
    if not events:
        issues.append("Local cleanup event is missing")
    if evidence.get("cleanup_scope") != "local":
        issues.append("Local cleanup evidence must indicate cleanup_scope local")

    worktree_removed = evidence.get("worktree_removed") is True
    partial_cleanup_reason = any(
        bool(evidence.get(field))
        for field in (
            "worktree_remove_error",
            "branch_delete_error",
            "branch_delete_skipped",
            "partial_cleanup_reason",
        )
    )
    if not worktree_removed and not partial_cleanup_reason:
        issues.append("Local cleanup evidence must indicate worktree_removed true or provide a partial cleanup reason")
    if evidence.get("issue_closed") is not False:
        issues.append("Local cleanup evidence must not indicate issue_closed true")
    if evidence.get("task_archived") is not False:
        issues.append("Local cleanup evidence must not indicate task_archived true")
    if evidence.get("task_completed") is not False:
        issues.append("Local cleanup evidence must not indicate task_completed true")
    if evidence.get("task_status_changed") is not False:
        issues.append("Local cleanup evidence must not indicate task_status_changed true")

    if artifacts and events:
        artifact_payload = artifact_payload or {}
        event_payload = event_payload or {}
        for field in (
            "cleanup_scope",
            "worktree_removed",
            "issue_closed",
            "task_archived",
            "task_completed",
            "task_status_changed",
        ):
            if artifact_payload.get(field) != event_payload.get(field):
                issues.append(f"Local cleanup artifact and event payloads disagree on {field}")
                break

    warnings.extend(issues)
    return {
        "available": available and not issues,
        "artifact_recorded": bool(artifacts),
        "event_recorded": bool(events),
        "artifact_kind": "local_cleanup",
        "event_type": "local_cleanup_completed",
        "artifact_path": str(artifact_path) if artifact_path is not None else None,
        "cleanup_scope": evidence.get("cleanup_scope"),
        "branch": evidence.get("branch") or evidence.get("local_branch"),
        "local_branch": evidence.get("local_branch"),
        "worktree_removed": evidence.get("worktree_removed") is True,
        "local_branch_deleted": evidence.get("local_branch_deleted") is True,
        "issue_closed": evidence.get("issue_closed") is True,
        "task_archived": evidence.get("task_archived") is True,
        "task_completed": evidence.get("task_completed") is True,
        "task_status_changed": evidence.get("task_status_changed") is True,
        "task_status": evidence.get("task_status"),
        "requires_human_confirmation": evidence.get("requires_human_confirmation"),
        "confirmation_flag": evidence.get("confirmation_flag"),
        "warnings": _dedupe_preserve_order(warnings or ([] if available else ["Local cleanup evidence is missing"])),
    }


def _read_remote_branch_cleanup_evidence(store: TaskMirrorStore, task_key: str) -> dict[str, Any]:
    artifacts = [artifact for artifact in store.list_task_artifacts(task_key) if artifact.artifact_type == "remote_branch_cleanup"]
    events = [event for event in store.list_task_events(task_key) if event.event_type == "remote_branch_cleanup_completed"]
    warnings: list[str] = []

    artifact_payload: dict[str, Any] | None = None
    artifact_path: Path | None = None
    if artifacts:
        artifact_path = artifacts[-1].path
        try:
            artifact_payload = _load_json_object(artifact_path)
        except OSError as exc:
            warnings.append(f"Could not read remote branch cleanup artifact: {exc}")
        except json.JSONDecodeError as exc:
            warnings.append(f"Remote branch cleanup artifact is not valid JSON: {exc}")

    event_payload = _latest_event_payload(events)
    evidence = artifact_payload or event_payload or {}
    available = bool(artifacts and events and isinstance(evidence, dict) and not warnings)

    issues: list[str] = []
    if not artifacts:
        issues.append("Remote branch cleanup artifact record is missing")
    if not events:
        issues.append("Remote branch cleanup event is missing")
    if evidence.get("cleanup_scope") != "remote_branch":
        issues.append("Remote branch cleanup evidence must indicate cleanup_scope remote_branch")
    if evidence.get("remote_branch_deleted") is not True:
        issues.append("Remote branch cleanup evidence must indicate remote_branch_deleted true")
    if evidence.get("issue_closed") is not False:
        issues.append("Remote branch cleanup evidence must not indicate issue_closed true")
    if evidence.get("task_archived") is not False:
        issues.append("Remote branch cleanup evidence must not indicate task_archived true")
    if evidence.get("task_completed") is not False:
        issues.append("Remote branch cleanup evidence must not indicate task_completed true")
    if evidence.get("task_status_changed") is not False:
        issues.append("Remote branch cleanup evidence must not indicate task_status_changed true")

    if artifacts and events:
        artifact_payload = artifact_payload or {}
        event_payload = event_payload or {}
        for field in (
            "cleanup_scope",
            "remote_branch_deleted",
            "issue_closed",
            "task_archived",
            "task_completed",
            "task_status_changed",
        ):
            if artifact_payload.get(field) != event_payload.get(field):
                issues.append(f"Remote branch cleanup artifact and event payloads disagree on {field}")
                break

    warnings.extend(issues)
    return {
        "available": available and not issues,
        "artifact_recorded": bool(artifacts),
        "event_recorded": bool(events),
        "artifact_kind": "remote_branch_cleanup",
        "event_type": "remote_branch_cleanup_completed",
        "artifact_path": str(artifact_path) if artifact_path is not None else None,
        "cleanup_scope": evidence.get("cleanup_scope"),
        "remote": evidence.get("remote"),
        "branch": evidence.get("branch"),
        "remote_branch_deleted": evidence.get("remote_branch_deleted") is True,
        "remote_branch_exists_before": evidence.get("remote_branch_exists_before"),
        "remote_branch_exists_after": evidence.get("remote_branch_exists_after"),
        "remote_branch_delete_attempted": evidence.get("remote_branch_delete_attempted"),
        "remote_branch_delete_error": evidence.get("remote_branch_delete_error"),
        "issue_closed": evidence.get("issue_closed") is True,
        "task_archived": evidence.get("task_archived") is True,
        "task_completed": evidence.get("task_completed") is True,
        "task_status_changed": evidence.get("task_status_changed") is True,
        "requires_human_confirmation": evidence.get("requires_human_confirmation"),
        "confirmation_flag": evidence.get("confirmation_flag"),
        "task_status": evidence.get("task_status"),
        "warnings": _dedupe_preserve_order(warnings or ([] if available else ["Remote branch cleanup evidence is missing"])),
    }


def _read_pr_status(
    *,
    request: TaskCloseoutConfirmRequest,
    draft_pr: dict[str, Any],
    runner: Runner | None,
) -> dict[str, Any]:
    if request.offline_pr_json is not None:
        payload = _load_json_object(request.offline_pr_json)
    else:
        selector = _resolve_pr_selector(draft_pr)
        completed = _run_command(
            [
                "gh",
                "pr",
                "view",
                selector,
                "--repo",
                request.repo,
                "--json",
                ",".join(PR_JSON_FIELDS),
            ],
            cwd=request.repo_path,
            runner=runner,
        )
        if completed.returncode != 0:
            raise TaskCloseoutConfirmError(
                f"gh pr view failed with {completed.returncode}: {completed.stderr.strip()}"
            )
        payload = _parse_json_object(completed.stdout, source="gh pr view")

    normalized = _normalize_pr_payload(payload)
    warnings: list[str] = []
    if normalized["is_draft"] is True:
        warnings.append("GitHub PR is still marked draft")
    if normalized["merged"] is not True:
        warnings.append("GitHub PR is not merged")
    if normalized["state"] not in {"MERGED"}:
        warnings.append("GitHub PR state is not MERGED")
    if draft_pr.get("pr_number") is not None and normalized.get("number") not in {None, draft_pr.get("pr_number")}:
        warnings.append("GitHub PR number does not match draft PR evidence")
    if draft_pr.get("pr_url") and normalized.get("url") not in {None, draft_pr.get("pr_url")}:
        warnings.append("GitHub PR URL does not match draft PR evidence")
    if draft_pr.get("head_branch") and normalized.get("head_ref_name") not in {None, draft_pr.get("head_branch")}:
        warnings.append("GitHub PR headRefName does not match draft PR evidence")
    if draft_pr.get("base_branch") and normalized.get("base_ref_name") not in {None, draft_pr.get("base_branch")}:
        warnings.append("GitHub PR baseRefName does not match draft PR evidence")
    if normalized["merged_at"] is None and normalized["state"] == "MERGED":
        warnings.append("GitHub PR mergedAt is missing")
    if normalized["merged"] and normalized["merge_commit"] is None:
        warnings.append("GitHub PR mergeCommit is unavailable; mergedAt was used as merge evidence")

    normalized["warnings"] = warnings
    normalized["available"] = True
    return normalized


def _inspect_remote_branch(
    *,
    request: TaskCloseoutConfirmRequest,
    branch: str,
    runner: Runner | None,
) -> dict[str, Any]:
    completed = _run_command(
        ["git", "ls-remote", "--heads", request.remote, branch],
        cwd=request.repo_path,
        runner=runner,
    )
    warnings: list[str] = []
    exists_after: bool | None = None
    if completed.returncode != 0:
        warnings.append(
            f"Could not inspect remote branch existence: {completed.stderr.strip() or 'git ls-remote failed'}"
        )
    else:
        exists_after = bool(completed.stdout.strip())
    if exists_after:
        warnings.append("Remote branch still exists")
    return {
        "available": True,
        "remote": request.remote,
        "branch": branch,
        "exists_after": exists_after,
        "warnings": warnings,
    }


def _readiness_issues(
    *,
    request: TaskCloseoutConfirmRequest,
    task: TaskRecord,
    draft_pr: dict[str, Any],
    pr: dict[str, Any],
    local_cleanup: dict[str, Any],
    remote_branch_cleanup: dict[str, Any],
    remote_branch: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    if task.repo_path.resolve() != request.repo_path.resolve():
        issues.append(
            f"Provided repo_path {request.repo_path} does not match task repo_path {task.repo_path}"
        )
    if task.status not in ELIGIBLE_MUTATION_STATUSES and task.status not in TERMINAL_STATUSES:
        issues.append(
            f"Task {request.task_key} must be waiting_approval before closeout; current status: {task.status}"
        )
    if task.status in TERMINAL_STATUSES and task.status not in {"completed", "done"}:
        issues.append(f"Task {request.task_key} is already terminal with status {task.status}")

    if draft_pr["available"] is not True:
        issues.extend(draft_pr["warnings"])
    if pr["available"] is not True:
        issues.extend(pr["warnings"])
    if local_cleanup["available"] is not True:
        issues.extend(local_cleanup["warnings"])
    if remote_branch_cleanup["available"] is not True:
        issues.extend(remote_branch_cleanup["warnings"])

    if draft_pr.get("repo") and draft_pr["repo"] != request.repo:
        issues.append("Draft PR evidence repo does not match the requested repo")
    if pr.get("number") is None:
        issues.append("GitHub PR number is missing")
    if pr.get("url") is None:
        issues.append("GitHub PR URL is missing")
    if pr.get("merged") is not True:
        issues.append("GitHub PR is not merged")
    if pr.get("is_draft") is True:
        issues.append("GitHub PR is still draft")

    if local_cleanup.get("cleanup_scope") != "local":
        issues.append("Local cleanup evidence is not scoped to local cleanup")
    if local_cleanup.get("issue_closed") is True:
        issues.append("Local cleanup evidence must not indicate issue_closed true")
    if local_cleanup.get("task_archived") is True:
        issues.append("Local cleanup evidence must not indicate task_archived true")
    if local_cleanup.get("task_completed") is True:
        issues.append("Local cleanup evidence must not indicate task_completed true")
    if local_cleanup.get("task_status_changed") is True:
        issues.append("Local cleanup evidence must not indicate task_status_changed true")

    if remote_branch_cleanup.get("cleanup_scope") != "remote_branch":
        issues.append("Remote branch cleanup evidence is not scoped to remote_branch cleanup")
    if remote_branch_cleanup.get("remote_branch_deleted") is not True:
        issues.append("Remote branch cleanup evidence must indicate remote_branch_deleted true")
    if remote_branch_cleanup.get("issue_closed") is True:
        issues.append("Remote branch cleanup evidence must not indicate issue_closed true")
    if remote_branch_cleanup.get("task_archived") is True:
        issues.append("Remote branch cleanup evidence must not indicate task_archived true")
    if remote_branch_cleanup.get("task_completed") is True:
        issues.append("Remote branch cleanup evidence must not indicate task_completed true")
    if remote_branch_cleanup.get("task_status_changed") is True:
        issues.append("Remote branch cleanup evidence must not indicate task_status_changed true")

    if remote_branch.get("exists_after") is True:
        issues.append("Remote branch still exists")

    return _dedupe_preserve_order(issues)


def _already_completed_result(
    *,
    request: TaskCloseoutConfirmRequest,
    task: TaskRecord,
    pr: dict[str, Any],
    draft_pr: dict[str, Any],
    local_cleanup: dict[str, Any],
    remote_branch_cleanup: dict[str, Any],
    warnings: list[str],
) -> TaskCloseoutConfirmResult:
    return TaskCloseoutConfirmResult(
        ok=True,
        status="already_completed",
        task_key=request.task_key,
        task_status=task.status,
        previous_task_status=task.status,
        new_task_status=task.status,
        repo=request.repo,
        pr=pr,
        draft_pr=draft_pr,
        local_cleanup=local_cleanup,
        remote_branch_cleanup=remote_branch_cleanup,
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_KIND,
            "artifact_path": None,
            "cleanup_scope": "task_closeout",
            "requires_human_confirmation": True,
            "confirmation_flag": "--confirm-task-closeout",
            "task_closeout_performed": False,
        },
        next_allowed_actions=[
            "no further local closeout action is required",
        ],
        actions_not_performed=[
            "GitHub issue close",
            "branch deletion",
            "worktree removal",
            "PR merge",
            "PR approval",
            "GitHub mutation",
        ],
        summary={
            "task_closeout_performed": False,
            "task_status_changed": False,
            "issue_closed": False,
            "github_mutated": False,
            "task_archived": task.status == "done",
            "task_completed": True,
            "cleanup_performed": False,
            "requires_human_review": False,
            "next_phase": None,
        },
        safety=_safety_block(
            human_confirmation_confirmed=False,
            task_status_changed=False,
            db_written=False,
            task_closeout_performed=False,
            task_completed=True,
            task_archived=task.status == "done",
        ),
        warnings=_dedupe_preserve_order(warnings),
        blocking_warnings=[],
        performed=False,
        dry_run=False,
        confirmation_required=False,
        artifact_recorded=False,
        event_recorded=False,
        task_status_changed=False,
        db_written=False,
        task_closeout_performed=False,
        closeout_ready=True,
        error=None,
    )


def _blocked_result(
    *,
    request: TaskCloseoutConfirmRequest,
    task: TaskRecord,
    pr: dict[str, Any],
    draft_pr: dict[str, Any],
    local_cleanup: dict[str, Any],
    remote_branch_cleanup: dict[str, Any],
    remote_branch: dict[str, Any],
    warnings: list[str],
    error: str,
) -> TaskCloseoutConfirmResult:
    return TaskCloseoutConfirmResult(
        ok=False,
        status="blocked",
        task_key=request.task_key,
        task_status=task.status,
        previous_task_status=task.status,
        new_task_status=None,
        repo=request.repo,
        pr=pr,
        draft_pr=draft_pr,
        local_cleanup=local_cleanup,
        remote_branch_cleanup=remote_branch_cleanup,
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_KIND,
            "artifact_path": None,
            "cleanup_scope": "task_closeout",
            "requires_human_confirmation": True,
            "confirmation_flag": "--confirm-task-closeout",
            "task_closeout_performed": False,
        },
        next_allowed_actions=[
            "resolve blocking warnings",
            "rerun explicit task closeout confirm once evidence is complete",
        ],
        actions_not_performed=[
            "GitHub issue close",
            "branch deletion",
            "worktree removal",
            "PR merge",
            "PR approval",
            "GitHub mutation",
            "task status update",
            "task closeout evidence recording",
        ],
        summary={
            "task_closeout_performed": False,
            "task_status_changed": False,
            "issue_closed": False,
            "github_mutated": False,
            "task_archived": False,
            "task_completed": False,
            "cleanup_performed": False,
            "requires_human_review": True,
            "next_phase": None,
        },
        safety=_safety_block(
            human_confirmation_confirmed=False,
            task_status_changed=False,
            db_written=False,
            task_closeout_performed=False,
            task_completed=False,
            task_archived=False,
        ),
        warnings=_dedupe_preserve_order([*warnings, error]),
        blocking_warnings=_dedupe_preserve_order([error, *warnings]),
        performed=False,
        dry_run=request.dry_run,
        confirmation_required=not request.dry_run,
        artifact_recorded=False,
        event_recorded=False,
        task_status_changed=False,
        db_written=False,
        task_closeout_performed=False,
        closeout_ready=False,
        error=error,
    )


def _dry_run_result(
    *,
    request: TaskCloseoutConfirmRequest,
    task: TaskRecord,
    pr: dict[str, Any],
    draft_pr: dict[str, Any],
    local_cleanup: dict[str, Any],
    remote_branch_cleanup: dict[str, Any],
    remote_branch: dict[str, Any],
    warnings: list[str],
) -> TaskCloseoutConfirmResult:
    target_status = request.target_status
    return TaskCloseoutConfirmResult(
        ok=True,
        status="dry_run",
        task_key=request.task_key,
        task_status=task.status,
        previous_task_status=task.status,
        new_task_status=target_status,
        repo=request.repo,
        pr=pr,
        draft_pr=draft_pr,
        local_cleanup=local_cleanup,
        remote_branch_cleanup=remote_branch_cleanup,
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_KIND,
            "artifact_path": None,
            "cleanup_scope": "task_closeout",
            "requires_human_confirmation": True,
            "confirmation_flag": "--confirm-task-closeout",
            "task_closeout_performed": False,
        },
        next_allowed_actions=[
            "run explicit task closeout confirm with --confirm-task-closeout",
        ],
        actions_not_performed=[
            "GitHub issue close",
            "branch deletion",
            "worktree removal",
            "PR merge",
            "PR approval",
            "GitHub mutation",
            "task status update",
            "task closeout evidence recording",
        ],
        summary={
            "task_closeout_performed": False,
            "task_status_changed": False,
            "issue_closed": False,
            "github_mutated": False,
            "task_archived": target_status == "done",
            "task_completed": True,
            "cleanup_performed": False,
            "requires_human_review": True,
            "next_phase": None,
        },
        safety=_safety_block(
            human_confirmation_confirmed=False,
            task_status_changed=False,
            db_written=False,
            task_closeout_performed=False,
            task_completed=True,
            task_archived=target_status == "done",
        ),
        warnings=_dedupe_preserve_order(warnings),
        blocking_warnings=[],
        performed=False,
        dry_run=True,
        confirmation_required=True,
        artifact_recorded=False,
        event_recorded=False,
        task_status_changed=False,
        db_written=False,
        task_closeout_performed=False,
        closeout_ready=True,
        error=None,
    )


def _success_result(
    *,
    request: TaskCloseoutConfirmRequest,
    task: TaskRecord,
    previous_task_status: str,
    new_task_status: str,
    pr: dict[str, Any],
    draft_pr: dict[str, Any],
    local_cleanup: dict[str, Any],
    remote_branch_cleanup: dict[str, Any],
    remote_branch: dict[str, Any],
    warnings: list[str],
    artifact_path: Path | None,
) -> TaskCloseoutConfirmResult:
    task_archived = new_task_status == "done"
    return TaskCloseoutConfirmResult(
        ok=True,
        status="task_closeout_completed",
        task_key=request.task_key,
        task_status=task.status,
        previous_task_status=previous_task_status,
        new_task_status=new_task_status,
        repo=request.repo,
        pr=pr,
        draft_pr=draft_pr,
        local_cleanup=local_cleanup,
        remote_branch_cleanup=remote_branch_cleanup,
        evidence={
            "artifact_recorded": True,
            "event_recorded": True,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_KIND,
            "artifact_path": str(artifact_path) if artifact_path is not None else None,
            "cleanup_scope": "task_closeout",
            "requires_human_confirmation": True,
            "confirmation_flag": "--confirm-task-closeout",
            "task_closeout_performed": True,
        },
        next_allowed_actions=[
            "manual GitHub issue close in a later phase if desired",
            "retain the completed local task record for review",
        ],
        actions_not_performed=[
            "GitHub issue close",
            "branch deletion",
            "worktree removal",
            "PR merge",
            "PR approval",
            "GitHub mutation",
        ],
        summary={
            "task_closeout_performed": True,
            "task_status_changed": previous_task_status != new_task_status,
            "issue_closed": False,
            "github_mutated": False,
            "task_archived": task_archived,
            "task_completed": True,
            "cleanup_performed": False,
            "requires_human_review": False,
            "next_phase": None,
        },
        safety=_safety_block(
            human_confirmation_confirmed=True,
            task_status_changed=previous_task_status != new_task_status,
            db_written=True,
            task_closeout_performed=True,
            task_completed=True,
            task_archived=task_archived,
        ),
        warnings=_dedupe_preserve_order(warnings),
        blocking_warnings=[],
        performed=True,
        dry_run=False,
        confirmation_required=True,
        artifact_recorded=True,
        event_recorded=True,
        task_status_changed=previous_task_status != new_task_status,
        db_written=True,
        task_closeout_performed=True,
        closeout_ready=True,
        error=None,
    )


def _task_closeout_evidence(
    *,
    task_key: str,
    previous_task_status: str,
    new_task_status: str,
    repo: str,
    pr: dict[str, Any],
    draft_pr: dict[str, Any],
    local_cleanup: dict[str, Any],
    remote_branch_cleanup: dict[str, Any],
) -> dict[str, Any]:
    task_completed = new_task_status in TERMINAL_STATUSES
    task_archived = new_task_status == "done"
    return {
        "schema_version": "1",
        "artifact_type": ARTIFACT_KIND,
        "kind": EVENT_TYPE,
        "task_key": task_key,
        "repo": repo,
        "previous_task_status": previous_task_status,
        "new_task_status": new_task_status,
        "pr_number": pr.get("number"),
        "pr_url": pr.get("url"),
        "merge_commit": pr.get("merge_commit"),
        "merged_at": pr.get("merged_at"),
        "draft_pr_verified": draft_pr.get("verified") is True,
        "local_cleanup_verified": local_cleanup.get("available") is True,
        "remote_branch_cleanup_verified": remote_branch_cleanup.get("available") is True,
        "issue_closed": False,
        "github_issue_mutated": False,
        "local_branch_deleted": False,
        "remote_branch_deleted": False,
        "worktree_removed": False,
        "task_status_changed": previous_task_status != new_task_status,
        "task_completed": task_completed,
        "task_archived": task_archived,
        "cleanup_scope": "task_closeout",
        "requires_human_confirmation": True,
        "confirmation_flag": "--confirm-task-closeout",
        "pr": pr,
        "draft_pr": draft_pr,
        "local_cleanup": local_cleanup,
        "remote_branch_cleanup": remote_branch_cleanup,
        "recorded_at": utc_now_iso(),
        "safety": {
            "human_confirmation_required": True,
            "human_confirmation_confirmed": True,
            "task_status_changed": previous_task_status != new_task_status,
            "db_written": True,
            "task_closeout_performed": True,
            "github_mutated": False,
            "issue_closed": False,
            "local_branch_deleted": False,
            "remote_branch_deleted": False,
            "worktree_removed": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "task_completed": task_completed,
            "task_archived": task_archived,
            "background_worker_started": False,
            "webhook_started": False,
            "polling_loop_started": False,
        },
    }


def _record_task_closeout_evidence(
    *,
    store: TaskMirrorStore,
    task: TaskRecord,
    artifact_root: Path | None,
    artifact_payload: dict[str, Any],
) -> Path:
    output_root = _resolve_closeout_artifact_root(task, artifact_root)
    artifact_path = output_root / task.task_key / "task_closeout.json"
    atomic_write_json(artifact_path, artifact_payload, sort_keys=True)
    store.record_task_artifact(task.task_key, ARTIFACT_KIND, artifact_path)
    store.record_task_event(
        task.task_key,
        EVENT_TYPE,
        SOURCE,
        message="Task closeout completed",
        payload=artifact_payload,
    )
    return artifact_path


def _resolve_closeout_artifact_root(task: TaskRecord, artifact_root: Path | None) -> Path:
    if artifact_root is not None:
        return artifact_root / ARTIFACT_KIND
    if task.artifact_dir is not None:
        return task.artifact_dir.resolve().parent / ARTIFACT_KIND
    return task.repo_path / ".agent-taskflow" / "artifacts" / ARTIFACT_KIND


def _task_closeout_evidence_exists(store: TaskMirrorStore, task_key: str) -> bool:
    artifacts = [artifact for artifact in store.list_task_artifacts(task_key) if artifact.artifact_type == ARTIFACT_KIND]
    events = [event for event in store.list_task_events(task_key) if event.event_type == EVENT_TYPE]
    return bool(artifacts and events)


def _inspect_remote_branch(
    *,
    request: TaskCloseoutConfirmRequest,
    branch: str,
    runner: Runner | None,
) -> dict[str, Any]:
    completed = _run_command(
        ["git", "ls-remote", "--heads", request.remote, branch],
        cwd=request.repo_path,
        runner=runner,
    )
    warnings: list[str] = []
    exists_after: bool | None = None
    if completed.returncode != 0:
        warnings.append(
            f"Could not inspect remote branch existence: {completed.stderr.strip() or 'git ls-remote failed'}"
        )
    else:
        exists_after = bool(completed.stdout.strip())
    if exists_after:
        warnings.append("Remote branch still exists")
    return {
        "available": True,
        "remote": request.remote,
        "branch": branch,
        "exists_after": exists_after,
        "warnings": warnings,
    }


def _resolve_branch_name(
    *,
    draft_pr: dict[str, Any],
    local_cleanup: dict[str, Any],
    remote_branch_cleanup: dict[str, Any],
) -> str:
    branch = (
        draft_pr.get("head_branch")
        or local_cleanup.get("branch")
        or local_cleanup.get("local_branch")
        or remote_branch_cleanup.get("branch")
    )
    if not branch or not str(branch).strip():
        raise TaskCloseoutConfirmError("Unable to resolve the task branch from evidence")
    return str(branch)


def _normalize_pr_payload(payload: dict[str, Any]) -> dict[str, Any]:
    merge_commit = payload.get("mergeCommit")
    normalized_merge_commit: str | None
    if isinstance(merge_commit, dict):
        normalized_merge_commit = merge_commit.get("oid") if isinstance(merge_commit.get("oid"), str) else None
    elif isinstance(merge_commit, str):
        normalized_merge_commit = merge_commit
    else:
        normalized_merge_commit = None

    state = str(payload.get("state") or "").strip().upper() or None
    merged = state == "MERGED" or payload.get("mergedAt") is not None
    return {
        "available": True,
        "number": payload.get("number"),
        "url": payload.get("url"),
        "state": state,
        "is_draft": bool(payload.get("isDraft")) if payload.get("isDraft") is not None else None,
        "merged_at": payload.get("mergedAt"),
        "merge_commit": normalized_merge_commit,
        "head_ref_name": payload.get("headRefName"),
        "base_ref_name": payload.get("baseRefName"),
        "title": payload.get("title"),
        "merged": merged,
        "warnings": [],
    }


def _resolve_pr_selector(draft_pr: dict[str, Any]) -> str:
    if draft_pr.get("pr_url"):
        return str(draft_pr["pr_url"])
    if draft_pr.get("pr_number") is not None:
        return str(draft_pr["pr_number"])
    raise TaskCloseoutConfirmError("Draft PR evidence does not include a PR selector")


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise OSError(f"Could not read JSON file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise json.JSONDecodeError("JSON object required", doc="", pos=0)
    return payload


def _parse_json_object(text: str, *, source: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TaskCloseoutConfirmError(f"{source} returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TaskCloseoutConfirmError(f"{source} did not contain a JSON object")
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
    return payload if isinstance(payload, dict) else {}


def _run_command(
    args: list[str],
    *,
    cwd: Path | None,
    runner: Runner | None,
) -> CompletedProcessLike:
    if runner is not None:
        return runner(
            args,
            cwd=cwd,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    return subprocess.run(
        args,
        cwd=cwd,
        shell=False,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _normalize_repo(repo: str) -> str:
    normalized = repo.strip()
    if not normalized:
        raise ValueError("repo must not be empty")
    if normalized.startswith("-") or any(ch.isspace() for ch in normalized):
        raise ValueError("repo must be a simple owner/name string")
    if normalized.count("/") != 1:
        raise ValueError("repo must be an owner/name string")
    return normalized


def _normalize_target_status(status: str) -> str:
    normalized = validate_task_status(status)
    if normalized not in TERMINAL_STATUSES:
        raise ValueError("target_status must be one of: completed, done")
    return normalized


def _not_found_result(
    *,
    request: TaskCloseoutConfirmRequest,
    error: str,
) -> TaskCloseoutConfirmResult:
    empty_pr = _empty_pr()
    empty_draft_pr = _empty_draft_pr()
    empty_local_cleanup = _empty_local_cleanup()
    empty_remote_branch_cleanup = _empty_remote_branch_cleanup()
    return TaskCloseoutConfirmResult(
        ok=False,
        status="not_found",
        task_key=request.task_key,
        task_status=None,
        previous_task_status=None,
        new_task_status=None,
        repo=request.repo,
        pr=empty_pr,
        draft_pr=empty_draft_pr,
        local_cleanup=empty_local_cleanup,
        remote_branch_cleanup=empty_remote_branch_cleanup,
        evidence={
            "artifact_recorded": False,
            "event_recorded": False,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_KIND,
            "artifact_path": None,
            "cleanup_scope": "task_closeout",
            "requires_human_confirmation": True,
            "confirmation_flag": "--confirm-task-closeout",
            "task_closeout_performed": False,
        },
        next_allowed_actions=["resolve the missing task record and retry"],
        actions_not_performed=[
            "GitHub issue close",
            "branch deletion",
            "worktree removal",
            "PR merge",
            "PR approval",
            "GitHub mutation",
            "task status update",
            "task closeout evidence recording",
        ],
        summary={
            "task_closeout_performed": False,
            "task_status_changed": False,
            "issue_closed": False,
            "github_mutated": False,
            "task_archived": False,
            "task_completed": False,
            "cleanup_performed": False,
            "requires_human_review": True,
            "next_phase": None,
        },
        safety=_safety_block(
            human_confirmation_confirmed=False,
            task_status_changed=False,
            db_written=False,
            task_closeout_performed=False,
            task_completed=False,
            task_archived=False,
        ),
        warnings=[error],
        blocking_warnings=[error],
        performed=False,
        dry_run=request.dry_run,
        confirmation_required=not request.dry_run,
        artifact_recorded=False,
        event_recorded=False,
        task_status_changed=False,
        db_written=False,
        task_closeout_performed=False,
        closeout_ready=False,
        error=error,
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
        "merged": False,
        "warnings": [],
    }


def _empty_draft_pr() -> dict[str, Any]:
    return {
        "available": False,
        "artifact_recorded": False,
        "event_recorded": False,
        "artifact_kind": "draft_pr",
        "event_type": "draft_pr_created",
        "artifact_path": None,
        "repo": None,
        "pr_number": None,
        "pr_url": None,
        "base_branch": None,
        "head_branch": None,
        "verified": False,
        "pr_created": False,
        "draft_pr_created": False,
        "merged": False,
        "issue_closed": False,
        "requires_human_confirmation": None,
        "warnings": ["Draft PR evidence is missing"],
    }


def _empty_local_cleanup() -> dict[str, Any]:
    return {
        "available": False,
        "artifact_recorded": False,
        "event_recorded": False,
        "artifact_kind": "local_cleanup",
        "event_type": "local_cleanup_completed",
        "artifact_path": None,
        "cleanup_scope": None,
        "worktree_removed": False,
        "local_branch_deleted": False,
        "issue_closed": False,
        "task_archived": False,
        "task_completed": False,
        "task_status_changed": False,
        "task_status": None,
        "requires_human_confirmation": None,
        "confirmation_flag": None,
        "warnings": ["Local cleanup evidence is missing"],
    }


def _empty_remote_branch_cleanup() -> dict[str, Any]:
    return {
        "available": False,
        "artifact_recorded": False,
        "event_recorded": False,
        "artifact_kind": "remote_branch_cleanup",
        "event_type": "remote_branch_cleanup_completed",
        "artifact_path": None,
        "cleanup_scope": None,
        "remote": None,
        "branch": None,
        "remote_branch_deleted": False,
        "remote_branch_exists_before": None,
        "remote_branch_exists_after": None,
        "remote_branch_delete_attempted": None,
        "remote_branch_delete_error": None,
        "issue_closed": False,
        "task_archived": False,
        "task_completed": False,
        "task_status_changed": False,
        "requires_human_confirmation": None,
        "confirmation_flag": None,
        "task_status": None,
        "warnings": ["Remote branch cleanup evidence is missing"],
    }


def _empty_remote_branch_state() -> dict[str, Any]:
    return {
        "available": False,
        "remote": None,
        "branch": None,
        "exists_after": None,
        "warnings": [],
    }


def _safety_block(
    *,
    human_confirmation_confirmed: bool,
    task_status_changed: bool,
    db_written: bool,
    task_closeout_performed: bool,
    task_completed: bool,
    task_archived: bool,
) -> dict[str, Any]:
    return {
        "human_confirmation_required": True,
        "human_confirmation_confirmed": human_confirmation_confirmed,
        "task_status_changed": task_status_changed,
        "db_written": db_written,
        "task_closeout_performed": task_closeout_performed,
        "github_mutated": False,
        "issue_closed": False,
        "local_branch_deleted": False,
        "remote_branch_deleted": False,
        "worktree_removed": False,
        "merged": False,
        "approved": False,
        "cleanup_performed": False,
        "task_completed": task_completed,
        "task_archived": task_archived,
        "background_worker_started": False,
        "webhook_started": False,
        "polling_loop_started": False,
    }
