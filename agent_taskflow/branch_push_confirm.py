"""Explicit branch push confirmation from waiting-approval handoff evidence.

This module can perform a dry-run validation and, only when explicitly
confirmed, publish the task branch with ``git push origin HEAD:<branch>``.
It does not create pull requests, merge, approve, clean up, delete branches,
delete worktrees, prepare workspaces, dispatch executors, or run validators.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shlex
import subprocess
from typing import Any, Callable, Protocol

from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.models import utc_now_iso
from agent_taskflow.pr_handoff_package import (
    PrHandoffPackageRequest,
    PrHandoffPackageResult,
    create_pr_handoff_package,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.worktree import ensure_absolute_path


ARTIFACT_TYPE = "branch_push"
EVENT_TYPE = "branch_push_completed"
SOURCE = "branch_push_confirm"
DEFAULT_REMOTE = "origin"
PROTECTED_BRANCHES = {"main", "master", "trunk"}


class BranchPushConfirmError(RuntimeError):
    """Raised when a branch push confirmation cannot proceed safely."""


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., CompletedProcessLike]


@dataclass(frozen=True)
class BranchPushConfirmRequest:
    """Request for previewing or confirming a task branch push."""

    task_key: str
    repo_path: Path
    db_path: Path | None = None
    artifact_root: Path | None = None
    remote: str = DEFAULT_REMOTE
    branch: str | None = None
    dry_run: bool = False
    confirm_branch_push: bool = False
    allow_non_waiting: bool = False

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
        normalized_remote = self.remote.strip()
        if not normalized_remote:
            raise ValueError("remote must not be empty")
        if normalized_remote.startswith("-") or any(ch.isspace() for ch in normalized_remote):
            raise ValueError("remote must be a simple git remote name")
        object.__setattr__(self, "remote", normalized_remote)


@dataclass(frozen=True)
class BranchPushConfirmResult:
    """Structured branch push preview or confirmation result."""

    ok: bool
    status: str
    task_key: str
    task_status: str | None
    remote: str
    branch: str | None
    refspec: str | None
    source: dict[str, Any]
    workspace: dict[str, Any]
    git: dict[str, Any]
    executor: dict[str, Any]
    validation: dict[str, Any]
    evidence: dict[str, Any]
    handoff: dict[str, Any]
    next_allowed_actions: list[str]
    actions_not_performed: list[str]
    summary: dict[str, Any]
    safety: dict[str, Any]
    warnings: list[str]
    performed: bool
    dry_run_performed: bool
    dry_run_ok: bool
    push_performed: bool
    push_ok: bool
    artifact_recorded: bool
    event_recorded: bool
    branch_push_json_path: str | None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = json.loads(json.dumps(self.__dict__, sort_keys=True))
        payload.update(
            {
                "branch_pushed": self.safety.get("branch_pushed", False),
                "pr_created": self.safety.get("pr_created", False),
                "merged": self.safety.get("merged", False),
                "approved": self.safety.get("approved", False),
                "cleanup_performed": self.safety.get("cleanup_performed", False),
                "branch_deleted": self.safety.get("branch_deleted", False),
                "worktree_deleted": self.safety.get("worktree_deleted", False),
                "force_push": self.safety.get("force_push", False),
            }
        )
        return payload


def confirm_branch_push(
    request: BranchPushConfirmRequest,
    *,
    store: TaskMirrorStore | None = None,
    runner: Runner | None = None,
) -> BranchPushConfirmResult:
    """Preview or confirm a task branch push after Phase 5B readiness."""

    current_store = store or TaskMirrorStore(request.db_path)
    package_request = PrHandoffPackageRequest(
        task_key=request.task_key,
        repo_path=request.repo_path,
        db_path=request.db_path,
        artifact_root=request.artifact_root,
        dry_run=True,
        allow_non_waiting=request.allow_non_waiting,
        remote=request.remote,
    )
    handoff = create_pr_handoff_package(package_request, store=current_store)
    warnings = list(handoff.warnings)

    if not handoff.ok:
        summary_error = handoff.error or handoff.summary.get("next_phase") or "Branch push handoff is not ready"
        return _error_result(
            request=request,
            handoff=handoff,
            branch=str(request.branch or handoff.workspace.get("branch") or "").strip() or None,
            refspec=None,
            warnings=warnings,
            error=summary_error,
            dry_run_performed=False,
            dry_run_ok=False,
            push_performed=False,
            push_ok=False,
            artifact_recorded=False,
            event_recorded=False,
            performed=False,
        )

    try:
        branch_name = _resolve_branch_name(request.branch, handoff)
    except ValueError as exc:
        summary_error = str(exc)
        return _error_result(
            request=request,
            handoff=handoff,
            branch=str(handoff.workspace.get("branch") or "").strip() or None,
            refspec=None,
            warnings=warnings + [summary_error],
            error=summary_error,
            dry_run_performed=False,
            dry_run_ok=False,
            push_performed=False,
            push_ok=False,
            artifact_recorded=False,
            event_recorded=False,
            performed=False,
        )
    refspec = f"HEAD:{branch_name}"
    task_record = current_store.get_task(handoff.task_key)
    summary_error: str | None = None
    dry_run_performed = False
    dry_run_ok = False
    push_performed = False
    push_ok = False
    push_stdout = ""
    push_stderr = ""
    dry_run_stdout = ""
    dry_run_stderr = ""

    inspection_only = request.dry_run and request.allow_non_waiting
    readiness_issues = _branch_push_readiness_issues(handoff)

    if (
        (not handoff.summary.get("ready_for_branch_push_review", False) or readiness_issues)
        and not inspection_only
    ):
        warnings.extend(issue for issue in readiness_issues if issue not in warnings)
        warnings.extend(
            warning
            for warning in handoff.review_summary.get("blocking_warnings", [])
            if warning not in warnings
        )
        summary_error = (
            "Phase 5B handoff is not ready for branch push review"
            if not readiness_issues
            else "Missing branch push readiness evidence"
        )
        return _error_result(
            request=request,
            handoff=handoff,
            branch=branch_name,
            refspec=refspec,
            warnings=warnings,
            error=summary_error,
            dry_run_performed=False,
            dry_run_ok=False,
            push_performed=False,
            push_ok=False,
            artifact_recorded=False,
            event_recorded=False,
            performed=False,
        )

    current_branch = str(handoff.git.get("current_branch") or "").strip() or None
    worktree_clean = bool(handoff.git.get("worktree_clean"))
    if current_branch != branch_name:
        summary_error = f"current branch {current_branch!r} does not match task branch {branch_name!r}"
        return _error_result(
            request=request,
            handoff=handoff,
            branch=branch_name,
            refspec=refspec,
            warnings=warnings + [summary_error],
            error=summary_error,
            dry_run_performed=False,
            dry_run_ok=False,
            push_performed=False,
            push_ok=False,
            artifact_recorded=False,
            event_recorded=False,
            performed=False,
        )
    if not worktree_clean and not inspection_only:
        summary_error = "worktree must be clean before branch push confirmation"
        return _error_result(
            request=request,
            handoff=handoff,
            branch=branch_name,
            refspec=refspec,
            warnings=warnings + [summary_error],
            error=summary_error,
            dry_run_performed=False,
            dry_run_ok=False,
            push_performed=False,
            push_ok=False,
            artifact_recorded=False,
            event_recorded=False,
            performed=False,
        )

    if request.branch is not None and request.branch != branch_name:
        summary_error = (
            f"Requested branch {request.branch!r} does not match the task worktree branch {branch_name!r}"
        )
        return _error_result(
            request=request,
            handoff=handoff,
            branch=branch_name,
            refspec=refspec,
            warnings=warnings + [summary_error],
            error=summary_error,
            dry_run_performed=False,
            dry_run_ok=False,
            push_performed=False,
            push_ok=False,
            artifact_recorded=False,
            event_recorded=False,
            performed=False,
        )

    if branch_name in PROTECTED_BRANCHES and not inspection_only:
        summary_error = f"Refusing to push protected branch: {branch_name}"
        return _error_result(
            request=request,
            handoff=handoff,
            branch=branch_name,
            refspec=refspec,
            warnings=warnings + [summary_error],
            error=summary_error,
            dry_run_performed=False,
            dry_run_ok=False,
            push_performed=False,
            push_ok=False,
            artifact_recorded=False,
            event_recorded=False,
            performed=False,
        )

    base_branch = str(handoff.workspace.get("base_branch") or "").strip()
    base_sha = str(handoff.workspace.get("base_sha") or "").strip()
    if not base_branch:
        summary_error = "base_branch is required"
        return _error_result(
            request=request,
            handoff=handoff,
            branch=branch_name,
            refspec=refspec,
            warnings=warnings + [summary_error],
            error=summary_error,
            dry_run_performed=False,
            dry_run_ok=False,
            push_performed=False,
            push_ok=False,
            artifact_recorded=False,
            event_recorded=False,
            performed=False,
        )
    if not base_sha:
        summary_error = "base_sha is required"
        return _error_result(
            request=request,
            handoff=handoff,
            branch=branch_name,
            refspec=refspec,
            warnings=warnings + [summary_error],
            error=summary_error,
            dry_run_performed=False,
            dry_run_ok=False,
            push_performed=False,
            push_ok=False,
            artifact_recorded=False,
            event_recorded=False,
            performed=False,
        )

    if request.dry_run:
        dry_run_result = _run_git_push(
            runner=runner,
            cwd=Path(str(handoff.workspace["worktree_path"])),
            remote=request.remote,
            refspec=refspec,
            dry_run=True,
        )
        dry_run_performed = True
        dry_run_ok = dry_run_result["ok"]
        dry_run_stdout = dry_run_result["stdout"]
        dry_run_stderr = dry_run_result["stderr"]
        if not dry_run_ok:
            summary_error = _summarize_error("git push --dry-run failed", dry_run_result)
            return _error_result(
                request=request,
                handoff=handoff,
                branch=branch_name,
                refspec=refspec,
                warnings=warnings + [summary_error],
                error=summary_error,
                dry_run_performed=True,
                dry_run_ok=False,
                push_performed=False,
                push_ok=False,
                artifact_recorded=False,
                event_recorded=False,
                performed=False,
                dry_run_stdout=dry_run_stdout,
                dry_run_stderr=dry_run_stderr,
            )
        return _success_result(
            request=request,
            handoff=handoff,
            branch=branch_name,
            refspec=refspec,
            warnings=warnings,
            dry_run_performed=True,
            dry_run_ok=True,
            push_performed=False,
            push_ok=False,
            performed=False,
            dry_run_stdout=dry_run_stdout,
            dry_run_stderr=dry_run_stderr,
            push_stdout="",
            push_stderr="",
            artifact_recorded=False,
            event_recorded=False,
            branch_push_json_path=str(
                _branch_push_path(
                    request,
                    handoff.task_key,
                    task_record.artifact_dir if task_record else None,
                )
            ),
            status="dry_run",
            summary="Branch push dry run completed",
        )

    dry_run_result = _run_git_push(
        runner=runner,
        cwd=Path(str(handoff.workspace["worktree_path"])),
        remote=request.remote,
        refspec=refspec,
        dry_run=True,
    )
    dry_run_performed = True
    dry_run_ok = dry_run_result["ok"]
    dry_run_stdout = dry_run_result["stdout"]
    dry_run_stderr = dry_run_result["stderr"]
    if not dry_run_ok:
        summary_error = _summarize_error("git push --dry-run failed", dry_run_result)
        return _error_result(
            request=request,
            handoff=handoff,
            branch=branch_name,
            refspec=refspec,
            warnings=warnings + [summary_error],
            error=summary_error,
            dry_run_performed=True,
            dry_run_ok=False,
            push_performed=False,
            push_ok=False,
            artifact_recorded=False,
            event_recorded=False,
            performed=False,
            dry_run_stdout=dry_run_stdout,
            dry_run_stderr=dry_run_stderr,
        )

    if not request.confirm_branch_push:
        summary_error = "Actual branch push requires --confirm-branch-push"
        return _error_result(
            request=request,
            handoff=handoff,
            branch=branch_name,
            refspec=refspec,
            warnings=warnings + [summary_error],
            error=summary_error,
            dry_run_performed=True,
            dry_run_ok=True,
            push_performed=False,
            push_ok=False,
            artifact_recorded=False,
            event_recorded=False,
            performed=False,
            dry_run_stdout=dry_run_stdout,
            dry_run_stderr=dry_run_stderr,
        )

    if handoff.task_status != "waiting_approval":
        summary_error = f"Task {handoff.task_key} must be waiting_approval to push, got {handoff.task_status}"
        return _error_result(
            request=request,
            handoff=handoff,
            branch=branch_name,
            refspec=refspec,
            warnings=warnings + [summary_error],
            error=summary_error,
            dry_run_performed=True,
            dry_run_ok=True,
            push_performed=False,
            push_ok=False,
            artifact_recorded=False,
            event_recorded=False,
            performed=False,
            dry_run_stdout=dry_run_stdout,
            dry_run_stderr=dry_run_stderr,
        )

    push_result = _run_git_push(
        runner=runner,
        cwd=Path(str(handoff.workspace["worktree_path"])),
        remote=request.remote,
        refspec=refspec,
        dry_run=False,
    )
    push_performed = True
    push_ok = push_result["ok"]
    push_stdout = push_result["stdout"]
    push_stderr = push_result["stderr"]
    if not push_ok:
        summary_error = _summarize_error("git push failed", push_result)
        return _error_result(
            request=request,
            handoff=handoff,
            branch=branch_name,
            refspec=refspec,
            warnings=warnings + [summary_error],
            error=summary_error,
            dry_run_performed=True,
            dry_run_ok=True,
            push_performed=True,
            push_ok=False,
            artifact_recorded=False,
            event_recorded=False,
            performed=False,
            dry_run_stdout=dry_run_stdout,
            dry_run_stderr=dry_run_stderr,
            push_stdout=push_stdout,
            push_stderr=push_stderr,
        )

    artifact_path = _branch_push_path(
        request,
        handoff.task_key,
        task_record.artifact_dir if task_record else None,
    )
    evidence = _branch_push_evidence(
        task_key=handoff.task_key,
        task_status=handoff.task_status,
        remote=request.remote,
        branch=branch_name,
        refspec=refspec,
        worktree_path=Path(str(handoff.workspace["worktree_path"])),
        base_branch=base_branch,
        base_sha=base_sha,
        head_sha=str(handoff.git.get("head_sha") or "").strip(),
        dry_run_stdout=dry_run_stdout,
        dry_run_stderr=dry_run_stderr,
        push_stdout=push_stdout,
        push_stderr=push_stderr,
    )
    current_store.init_db()
    atomic_write_json(artifact_path, evidence, sort_keys=True)
    artifact_recorded = _record_artifact_once(current_store, handoff.task_key, artifact_path)
    event_recorded = _record_event_once(current_store, handoff.task_key, evidence, artifact_path)

    return _success_result(
        request=request,
        handoff=handoff,
        branch=branch_name,
        refspec=refspec,
        warnings=warnings,
        dry_run_performed=True,
        dry_run_ok=True,
        push_performed=True,
        push_ok=True,
        performed=True,
        dry_run_stdout=dry_run_stdout,
        dry_run_stderr=dry_run_stderr,
        push_stdout=push_stdout,
        push_stderr=push_stderr,
        artifact_recorded=artifact_recorded,
        event_recorded=event_recorded,
        branch_push_json_path=str(artifact_path),
        status="pushed",
        summary="Task branch pushed after explicit confirmation",
    )


def _resolve_branch_name(branch: str | None, handoff: PrHandoffPackageResult) -> str:
    workspace_branch = str(handoff.workspace.get("branch") or "").strip()
    if not workspace_branch:
        raise ValueError("Task worktree branch is required")
    if branch is None:
        return _validate_branch_name(workspace_branch)
    normalized = _validate_branch_name(branch)
    if normalized != workspace_branch:
        raise ValueError(
            f"Requested branch {normalized!r} does not match task worktree branch {workspace_branch!r}"
        )
    return normalized


def _validate_branch_name(branch: str) -> str:
    normalized = branch.strip()
    if not normalized:
        raise ValueError("branch must not be empty")
    if normalized.startswith("-") or any(ch.isspace() for ch in normalized):
        raise ValueError("branch must be a simple branch name")
    return normalized


def _branch_push_readiness_issues(handoff: PrHandoffPackageResult) -> list[str]:
    issues: list[str] = []

    source = handoff.source if isinstance(handoff.source, dict) else {}
    if not source.get("available"):
        issues.append("source evidence is missing")

    executor = handoff.executor if isinstance(handoff.executor, dict) else {}
    if not executor.get("available"):
        issues.append("executor evidence is missing")
    elif not executor.get("finished_ok"):
        issues.append("executor evidence did not finish successfully")

    validation = handoff.validation if isinstance(handoff.validation, dict) else {}
    if not validation.get("available"):
        issues.append("validator evidence is missing")
    elif not validation.get("all_passed"):
        issues.append("validator evidence did not pass")

    workspace = handoff.workspace if isinstance(handoff.workspace, dict) else {}
    if not workspace.get("path_exists"):
        issues.append("worktree path is missing")
    if not workspace.get("branch"):
        issues.append("worktree branch is missing")
    if not workspace.get("base_branch"):
        issues.append("base_branch is missing")
    if not workspace.get("base_sha"):
        issues.append("base_sha is missing")

    git_state = handoff.git if isinstance(handoff.git, dict) else {}
    if not git_state.get("available"):
        issues.append("git inspection failed")

    return issues


def _run_git_push(
    *,
    runner: Runner | None,
    cwd: Path,
    remote: str,
    refspec: str,
    dry_run: bool,
) -> dict[str, Any]:
    command = ["git", "push"]
    if dry_run:
        command.append("--dry-run")
    command.extend([remote, refspec])
    completed = (runner or subprocess.run)(
        command,
        cwd=cwd,
        shell=False,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    return {
        "ok": getattr(completed, "returncode", 1) == 0,
        "returncode": getattr(completed, "returncode", 1),
        "stdout": stdout,
        "stderr": stderr,
        "stdout_summary": _summarize_text(stdout),
        "stderr_summary": _summarize_text(stderr),
        "command_preview": " ".join(shlex.quote(part) for part in command),
    }


def _summarize_text(text: str, *, limit: int = 240) -> str:
    normalized = " ".join(text.strip().split())
    return normalized[:limit]


def _summarize_error(prefix: str, result: dict[str, Any]) -> str:
    stderr = result.get("stderr_summary") or result.get("stderr") or ""
    return f"{prefix} with {result.get('returncode')}: {stderr}".rstrip()


def _branch_push_path(
    request: BranchPushConfirmRequest,
    task_key: str,
    task_artifact_dir: Path | None,
) -> Path:
    if request.artifact_root is not None:
        return request.artifact_root / "branch_push" / task_key / "branch_push.json"
    if task_artifact_dir is not None:
        return task_artifact_dir / "branch_push.json"
    return Path(str(request.repo_path)) / ".agent-taskflow" / "artifacts" / task_key / "branch_push.json"


def _branch_push_evidence(
    *,
    task_key: str,
    task_status: str | None,
    remote: str,
    branch: str,
    refspec: str,
    worktree_path: Path,
    base_branch: str,
    base_sha: str,
    head_sha: str,
    dry_run_stdout: str,
    dry_run_stderr: str,
    push_stdout: str,
    push_stderr: str,
) -> dict[str, Any]:
    return {
        "kind": EVENT_TYPE,
        "artifact_type": ARTIFACT_TYPE,
        "task_key": task_key,
        "task_status": task_status,
        "remote": remote,
        "branch": branch,
        "refspec": refspec,
        "worktree_path": str(worktree_path),
        "base_branch": base_branch,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "dry_run_performed": True,
        "dry_run_ok": True,
        "push_performed": True,
        "push_ok": True,
        "dry_run_stdout_summary": _summarize_text(dry_run_stdout),
        "dry_run_stderr_summary": _summarize_text(dry_run_stderr),
        "push_stdout_summary": _summarize_text(push_stdout),
        "push_stderr_summary": _summarize_text(push_stderr),
        "pushed_at": utc_now_iso(),
        "pushed_commit_sha": head_sha,
        "branch_pushed": True,
        "pr_created": False,
        "merged": False,
        "approved": False,
        "cleanup_performed": False,
        "requires_human_confirmation": True,
        "safety": {
            "human_confirmation_required": True,
            "human_confirmation_confirmed": True,
            "task_status_changed": False,
            "workspace_prepared": False,
            "executor_started": False,
            "validators_started": False,
            "branch_pushed": True,
            "pr_created": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "branch_deleted": False,
            "worktree_deleted": False,
            "force_push": False,
            "background_worker_started": False,
        },
    }


def _record_artifact_once(store: TaskMirrorStore, task_key: str, path: Path) -> bool:
    for artifact in store.list_task_artifacts(task_key):
        if artifact.artifact_type == ARTIFACT_TYPE and artifact.path == path:
            return False
    store.record_task_artifact(task_key, ARTIFACT_TYPE, path)
    return True


def _record_event_once(
    store: TaskMirrorStore,
    task_key: str,
    payload: dict[str, Any],
    artifact_path: Path,
) -> bool:
    for event in store.list_task_events(task_key):
        if event.event_type == EVENT_TYPE and event.payload_json:
            return False
    store.record_task_event(
        task_key,
        EVENT_TYPE,
        SOURCE,
        message="Branch push confirmed and completed",
        payload={
            **payload,
            "artifact_path": str(artifact_path),
        },
    )
    return True


def _success_result(
    *,
    request: BranchPushConfirmRequest,
    handoff: PrHandoffPackageResult,
    branch: str,
    refspec: str,
    warnings: list[str],
    dry_run_performed: bool,
    dry_run_ok: bool,
    push_performed: bool,
    push_ok: bool,
    performed: bool,
    dry_run_stdout: str,
    dry_run_stderr: str,
    push_stdout: str,
    push_stderr: str,
    artifact_recorded: bool,
    event_recorded: bool,
    branch_push_json_path: str | None,
    status: str,
    summary: str,
) -> BranchPushConfirmResult:
    next_allowed_actions = (
        [
            "manual verification of pushed branch",
            "explicit draft PR creation confirm in later phase",
        ]
        if performed
        else [
            "review handoff evidence",
            "confirm the branch push explicitly",
            "explicit draft PR creation confirm in later phase",
        ]
    )
    actions_not_performed = [
        "draft PR creation",
        "PR creation",
        "merge",
        "approval",
        "cleanup",
        "branch deletion",
        "worktree deletion",
    ]
    if not performed:
        actions_not_performed.insert(0, "branch push")

    return BranchPushConfirmResult(
        ok=True,
        status=status,
        task_key=handoff.task_key,
        task_status=handoff.task_status,
        remote=request.remote,
        branch=branch,
        refspec=refspec,
        source=handoff.source,
        workspace={
            **handoff.workspace,
            "branch": branch,
            "current_branch": handoff.git.get("current_branch"),
            "branch_matches_task_branch": handoff.git.get("current_branch") == branch,
            "path_exists": handoff.workspace.get("path_exists", False),
        },
        git={
            **handoff.git,
            "remote": request.remote,
            "refspec": refspec,
            "dry_run_performed": dry_run_performed,
            "dry_run_ok": dry_run_ok,
            "push_performed": push_performed,
            "push_ok": push_ok,
            "dry_run_command_preview": _git_push_preview(request.remote, refspec, dry_run=True),
            "push_command_preview": _git_push_preview(request.remote, refspec, dry_run=False),
            "remote_url_redacted": True,
            "stdout_summary": push_stdout if push_performed else dry_run_stdout,
            "stderr_summary": push_stderr if push_performed else dry_run_stderr,
            "dry_run_stdout_summary": _summarize_text(dry_run_stdout),
            "dry_run_stderr_summary": _summarize_text(dry_run_stderr),
            "push_stdout_summary": _summarize_text(push_stdout),
            "push_stderr_summary": _summarize_text(push_stderr),
            "dry_run_stdout": dry_run_stdout,
            "dry_run_stderr": dry_run_stderr,
            "push_stdout": push_stdout,
            "push_stderr": push_stderr,
        },
        executor=handoff.executor,
        validation=handoff.validation,
        evidence={
            **handoff.evidence,
            "artifact_recorded": artifact_recorded,
            "event_recorded": event_recorded,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_TYPE,
            "artifact_path": branch_push_json_path,
        },
        handoff={
            "ready_for_branch_push_review": handoff.summary.get("ready_for_branch_push_review", False),
            "blocking_warnings": list(handoff.review_summary.get("blocking_warnings", [])),
            "ready_for_human_review": handoff.review_summary.get("ready_for_human_review", False),
            "next_phase": handoff.summary.get("next_phase"),
        },
        next_allowed_actions=next_allowed_actions,
        actions_not_performed=actions_not_performed,
        summary={
            "branch_pushed": performed,
            "pr_created": False,
            "requires_human_review": True,
            "next_phase": "explicit_draft_pr_creation_confirm" if performed else "explicit_branch_push_confirm",
        },
        safety={
            "human_confirmation_required": True,
            "human_confirmation_confirmed": performed,
            "task_status_changed": False,
            "workspace_prepared": False,
            "executor_started": False,
            "validators_started": False,
            "branch_pushed": performed,
            "pr_created": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "branch_deleted": False,
            "worktree_deleted": False,
            "force_push": False,
            "background_worker_started": False,
        },
        warnings=warnings,
        performed=performed,
        dry_run_performed=dry_run_performed,
        dry_run_ok=dry_run_ok,
        push_performed=push_performed,
        push_ok=push_ok,
        artifact_recorded=artifact_recorded,
        event_recorded=event_recorded,
        branch_push_json_path=branch_push_json_path,
        error=None,
    )


def _error_result(
    *,
    request: BranchPushConfirmRequest,
    handoff: PrHandoffPackageResult,
    branch: str | None,
    refspec: str | None,
    warnings: list[str],
    error: str,
    dry_run_performed: bool,
    dry_run_ok: bool,
    push_performed: bool,
    push_ok: bool,
    artifact_recorded: bool,
    event_recorded: bool,
    performed: bool,
    dry_run_stdout: str = "",
    dry_run_stderr: str = "",
    push_stdout: str = "",
    push_stderr: str = "",
) -> BranchPushConfirmResult:
    return BranchPushConfirmResult(
        ok=False,
        status="blocked",
        task_key=handoff.task_key,
        task_status=handoff.task_status,
        remote=request.remote,
        branch=branch,
        refspec=refspec,
        source=handoff.source,
        workspace={
            **handoff.workspace,
            "branch": branch,
            "current_branch": handoff.git.get("current_branch"),
            "branch_matches_task_branch": handoff.git.get("current_branch") == branch,
            "path_exists": handoff.workspace.get("path_exists", False),
        },
        git={
            **handoff.git,
            "remote": request.remote,
            "refspec": refspec,
            "dry_run_performed": dry_run_performed,
            "dry_run_ok": dry_run_ok,
            "push_performed": push_performed,
            "push_ok": push_ok,
            "dry_run_command_preview": _git_push_preview(request.remote, refspec or "", dry_run=True),
            "push_command_preview": _git_push_preview(request.remote, refspec or "", dry_run=False),
            "remote_url_redacted": True,
            "stdout_summary": push_stdout if push_performed else dry_run_stdout,
            "stderr_summary": push_stderr if push_performed else dry_run_stderr,
            "dry_run_stdout_summary": _summarize_text(dry_run_stdout),
            "dry_run_stderr_summary": _summarize_text(dry_run_stderr),
            "push_stdout_summary": _summarize_text(push_stdout),
            "push_stderr_summary": _summarize_text(push_stderr),
            "dry_run_stdout": dry_run_stdout,
            "dry_run_stderr": dry_run_stderr,
            "push_stdout": push_stdout,
            "push_stderr": push_stderr,
        },
        executor=handoff.executor,
        validation=handoff.validation,
        evidence={
            **handoff.evidence,
            "artifact_recorded": artifact_recorded,
            "event_recorded": event_recorded,
            "event_type": EVENT_TYPE,
            "artifact_kind": ARTIFACT_TYPE,
            "artifact_path": None,
        },
        handoff={
            "ready_for_branch_push_review": handoff.summary.get("ready_for_branch_push_review", False),
            "blocking_warnings": list(handoff.review_summary.get("blocking_warnings", [])),
            "ready_for_human_review": handoff.review_summary.get("ready_for_human_review", False),
            "next_phase": handoff.summary.get("next_phase"),
        },
        next_allowed_actions=[
            "review handoff evidence",
            "resolve blocking warnings",
            "explicit branch push dry run",
        ],
        actions_not_performed=[
            "branch push",
            "draft PR creation",
            "PR creation",
            "merge",
            "approval",
            "cleanup",
            "branch deletion",
            "worktree deletion",
        ],
        summary={
            "branch_pushed": False,
            "pr_created": False,
            "requires_human_review": True,
            "next_phase": "explicit_branch_push_confirm",
        },
        safety={
            "human_confirmation_required": True,
            "human_confirmation_confirmed": False,
            "task_status_changed": False,
            "workspace_prepared": False,
            "executor_started": False,
            "validators_started": False,
            "branch_pushed": False,
            "pr_created": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "branch_deleted": False,
            "worktree_deleted": False,
            "force_push": False,
            "background_worker_started": False,
        },
        warnings=warnings,
        performed=performed,
        dry_run_performed=dry_run_performed,
        dry_run_ok=dry_run_ok,
        push_performed=push_performed,
        push_ok=push_ok,
        artifact_recorded=artifact_recorded,
        event_recorded=event_recorded,
        branch_push_json_path=None,
        error=error,
    )


def _git_push_preview(remote: str, refspec: str, *, dry_run: bool) -> str:
    command = ["git", "push"]
    if dry_run:
        command.append("--dry-run")
    command.extend([remote, refspec])
    return " ".join(shlex.quote(part) for part in command)
