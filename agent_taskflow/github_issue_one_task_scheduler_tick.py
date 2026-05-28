"""Scheduled, locked one-task tick for GitHub Issue automation.

This module wraps the existing one-shot GitHub Issue automation in a
non-overlap lock so cron or a systemd timer can call it safely. It is one
tick only: no daemon, scheduler loop, background worker, or multi-task batch
is started here. Human review and human merge remain external final gates.
"""

from __future__ import annotations

import errno
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import fcntl

from agent_taskflow.github_issue_discovery import IssueListFetcher
from agent_taskflow.github_issue_ingestion import IssueFetcher
from agent_taskflow.github_issue_one_task_automation import (
    GitHubIssueOneTaskAutomationError,
    GitHubIssueOneTaskAutomationRequest,
    run_github_issue_one_task_automation,
)
from agent_taskflow.models import require_absolute_path


GITHUB_ISSUE_ONE_TASK_SCHEDULER_TICK_SCHEMA_VERSION = (
    "github_issue_one_task_scheduler_tick.v1"
)
GITHUB_ISSUE_ONE_TASK_SCHEDULER_TICK_SOURCE = (
    "github_issue_one_task_scheduler_tick"
)


class GitHubIssueOneTaskSchedulerTickError(RuntimeError):
    """Raised when the scheduled GitHub Issue one-task tick cannot proceed."""


@dataclass(frozen=True)
class GitHubIssueOneTaskSchedulerTickRequest:
    """Inputs for one scheduled, locked GitHub Issue one-task tick."""

    repo: str
    db_path: Path
    local_repo_path: Path
    artifact_root: Path

    dry_run: bool = True
    confirmed: bool = False
    issue_limit: int = 100
    include_labels: tuple[str, ...] = ()
    exclude_labels: tuple[str, ...] = ()
    lock_path: Path | None = None
    fail_if_locked: bool = True
    operator: str | None = None
    operator_note: str | None = None
    remote: str = "origin"
    base_branch: str | None = None
    draft: bool = True

    def __post_init__(self) -> None:
        repo = str(self.repo or "").strip()
        if "/" not in repo or repo.startswith("/") or repo.endswith("/"):
            raise ValueError("repo must be in owner/name form")
        object.__setattr__(self, "repo", repo)

        object.__setattr__(
            self,
            "db_path",
            require_absolute_path(self.db_path, "db_path"),
        )

        local_repo_path = require_absolute_path(
            self.local_repo_path,
            "local_repo_path",
        )
        if not local_repo_path.is_dir():
            raise ValueError(
                f"local_repo_path must be an existing directory: {local_repo_path}"
            )
        object.__setattr__(self, "local_repo_path", local_repo_path)

        object.__setattr__(
            self,
            "artifact_root",
            require_absolute_path(self.artifact_root, "artifact_root"),
        )

        if self.confirmed:
            object.__setattr__(self, "dry_run", False)
        elif not self.dry_run:
            raise ValueError("confirmed mode requires confirmed=True")

        if self.issue_limit <= 0:
            raise ValueError("issue_limit must be positive")

        object.__setattr__(
            self,
            "include_labels",
            _normalize_labels(self.include_labels),
        )
        object.__setattr__(
            self,
            "exclude_labels",
            _normalize_labels(self.exclude_labels),
        )

        lock_path = self.lock_path or default_lock_path()
        lock_path = Path(lock_path).expanduser()
        if not lock_path.is_absolute():
            raise ValueError("lock_path must be an absolute path")
        object.__setattr__(self, "lock_path", lock_path)

        for field_name in ("operator", "operator_note", "base_branch"):
            value = getattr(self, field_name)
            if value is None:
                continue
            stripped = str(value).strip()
            object.__setattr__(self, field_name, stripped or None)

        remote = str(self.remote or "").strip()
        if not remote:
            raise ValueError("remote must not be empty")
        object.__setattr__(self, "remote", remote)


def default_lock_path() -> Path:
    """Return the default non-overlap lock path for scheduled ticks."""

    return (
        Path.home()
        / ".agent-taskflow"
        / "github_issue_one_task_scheduler_tick.lock"
    )


def run_github_issue_one_task_scheduler_tick(
    request: GitHubIssueOneTaskSchedulerTickRequest,
    *,
    discovery_fetcher: IssueListFetcher | None = None,
    ingestion_fetcher: IssueFetcher | None = None,
    approved_task_runner_fn: Callable[..., dict[str, Any]] | None = None,
    branch_push_fn: Callable[..., dict[str, Any]] | None = None,
    draft_pr_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run one locked scheduler tick and stop."""

    lock = _NonOverlapLock(request.lock_path)
    try:
        acquired = lock.acquire(blocking=not request.fail_if_locked)
    except OSError as exc:
        return _failure_response(
            request,
            status="lock_failed",
            reasons=[str(exc)],
            lock_acquired=False,
            lock_contended=False,
            automation=None,
        )

    if not acquired:
        return _locked_response(request)

    automation: dict[str, Any] | None = None
    automation_error: str | None = None
    try:
        try:
            automation = run_github_issue_one_task_automation(
                _automation_request(request),
                discovery_fetcher=discovery_fetcher,
                ingestion_fetcher=ingestion_fetcher,
                approved_task_runner_fn=approved_task_runner_fn,
                branch_push_fn=branch_push_fn,
                draft_pr_fn=draft_pr_fn,
            )
        except (GitHubIssueOneTaskAutomationError, ValueError) as exc:
            automation_error = str(exc)
    finally:
        lock.release()

    if automation_error is not None:
        return _failure_response(
            request,
            status="automation_error",
            reasons=[automation_error],
            lock_acquired=True,
            lock_contended=False,
            lock_released=True,
            automation_called=True,
            automation=automation,
        )

    if automation is None:
        return _failure_response(
            request,
            status="automation_error",
            reasons=["automation returned no result"],
            lock_acquired=True,
            lock_contended=False,
            lock_released=True,
            automation_called=True,
            automation=None,
        )

    return _automation_response(
        request,
        automation=automation,
        lock_released=True,
    )


def _automation_request(
    request: GitHubIssueOneTaskSchedulerTickRequest,
) -> GitHubIssueOneTaskAutomationRequest:
    if request.confirmed:
        return GitHubIssueOneTaskAutomationRequest(
            repo=request.repo,
            db_path=request.db_path,
            local_repo_path=request.local_repo_path,
            artifact_root=request.artifact_root,
            dry_run=False,
            issue_limit=request.issue_limit,
            include_labels=request.include_labels,
            exclude_labels=request.exclude_labels,
            select_first_issue=True,
            confirm_select_first_issue=True,
            confirm_ingest_issue=True,
            confirm_run_watcher_one_task=True,
            confirm_run_one_shot_pipeline=True,
            confirm_prepare_pr=True,
            confirm_github_mutations=True,
            confirm_branch_push=True,
            confirm_draft_pr=True,
            operator=request.operator,
            operator_note=request.operator_note,
            remote=request.remote,
            base_branch=request.base_branch,
            draft=request.draft,
        )

    return GitHubIssueOneTaskAutomationRequest(
        repo=request.repo,
        db_path=request.db_path,
        local_repo_path=request.local_repo_path,
        artifact_root=request.artifact_root,
        dry_run=True,
        issue_limit=request.issue_limit,
        include_labels=request.include_labels,
        exclude_labels=request.exclude_labels,
        select_first_issue=True,
        confirm_select_first_issue=True,
        operator=request.operator,
        operator_note=request.operator_note,
        remote=request.remote,
        base_branch=request.base_branch,
        draft=request.draft,
    )


def _automation_response(
    request: GitHubIssueOneTaskSchedulerTickRequest,
    *,
    automation: dict[str, Any],
    lock_released: bool,
) -> dict[str, Any]:
    automation_ok = automation.get("ok") is True
    status = str(automation.get("status") or "automation_completed")
    return {
        "ok": automation_ok,
        "schema_version": GITHUB_ISSUE_ONE_TASK_SCHEDULER_TICK_SCHEMA_VERSION,
        "source": GITHUB_ISSUE_ONE_TASK_SCHEDULER_TICK_SOURCE,
        "status": status,
        "mode": _mode(request),
        "repo": request.repo,
        "lock": _lock_payload(
            request,
            acquired=True,
            contended=False,
            released=lock_released,
        ),
        "automation": automation,
        "selected_task_key": automation.get("selected_task_key"),
        "safety": _safety(
            request,
            lock_acquired=True,
            lock_contended=False,
            automation=automation,
        ),
    }


def _locked_response(
    request: GitHubIssueOneTaskSchedulerTickRequest,
) -> dict[str, Any]:
    return {
        "ok": True,
        "schema_version": GITHUB_ISSUE_ONE_TASK_SCHEDULER_TICK_SCHEMA_VERSION,
        "source": GITHUB_ISSUE_ONE_TASK_SCHEDULER_TICK_SOURCE,
        "status": "locked",
        "mode": _mode(request),
        "repo": request.repo,
        "lock": _lock_payload(
            request,
            acquired=False,
            contended=True,
            released=False,
        ),
        "automation": None,
        "selected_task_key": None,
        "safety": _safety(
            request,
            lock_acquired=False,
            lock_contended=True,
            automation=None,
        ),
    }


def _failure_response(
    request: GitHubIssueOneTaskSchedulerTickRequest,
    *,
    status: str,
    reasons: list[str],
    lock_acquired: bool,
    lock_contended: bool,
    lock_released: bool = False,
    automation_called: bool | None = None,
    automation: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "schema_version": GITHUB_ISSUE_ONE_TASK_SCHEDULER_TICK_SCHEMA_VERSION,
        "source": GITHUB_ISSUE_ONE_TASK_SCHEDULER_TICK_SOURCE,
        "status": status,
        "mode": _mode(request),
        "repo": request.repo,
        "lock": _lock_payload(
            request,
            acquired=lock_acquired,
            contended=lock_contended,
            released=lock_released,
        ),
        "automation": automation,
        "selected_task_key": (
            automation.get("selected_task_key") if automation else None
        ),
        "reasons": _unique_strings([reason for reason in reasons if reason]),
        "safety": _safety(
            request,
            lock_acquired=lock_acquired,
            lock_contended=lock_contended,
            automation=automation,
            automation_called=automation_called,
        ),
    }


def _lock_payload(
    request: GitHubIssueOneTaskSchedulerTickRequest,
    *,
    acquired: bool,
    contended: bool,
    released: bool,
) -> dict[str, Any]:
    return {
        "path": str(request.lock_path),
        "acquired": acquired,
        "contended": contended,
        "released": released,
        "fail_if_locked": request.fail_if_locked,
    }


def _safety(
    request: GitHubIssueOneTaskSchedulerTickRequest,
    *,
    lock_acquired: bool,
    lock_contended: bool,
    automation: dict[str, Any] | None,
    automation_called: bool | None = None,
) -> dict[str, Any]:
    automation_safety = automation.get("safety") if automation else {}
    if not isinstance(automation_safety, dict):
        automation_safety = {}
    called = automation is not None if automation_called is None else automation_called

    return {
        "scheduled_tick": True,
        "one_tick_only": True,
        "one_issue_only": True,
        "one_task_only": True,
        "lock_acquired": lock_acquired,
        "lock_contended": lock_contended,
        "dry_run": request.dry_run,
        "confirmed": request.confirmed,
        "automation_called": called,
        "discovery_called": bool(automation_safety.get("discovery_called")),
        "issue_ingested": bool(automation_safety.get("issue_ingested")),
        "watcher_called": bool(automation_safety.get("watcher_called")),
        "approved_task_runner_called": bool(
            automation_safety.get("approved_task_runner_called")
        ),
        "github_mutated": bool(automation_safety.get("github_mutated")),
        "branch_pushed": bool(automation_safety.get("branch_pushed")),
        "draft_pr_created": bool(automation_safety.get("draft_pr_created")),
        "approved": False,
        "merged": False,
        "cleanup_performed": False,
        "branch_deleted": False,
        "worktree_deleted": False,
        "scheduler_loop_started": False,
        "background_worker_started": False,
        "multi_task_batch_started": False,
        "human_review_required": True,
    }


def _mode(request: GitHubIssueOneTaskSchedulerTickRequest) -> str:
    return "confirmed" if request.confirmed else "dry_run"


class _NonOverlapLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any | None = None

    def acquire(self, *, blocking: bool) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB

        try:
            fcntl.flock(handle.fileno(), flags)
        except OSError as exc:
            handle.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise

        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def _normalize_labels(labels: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for label in labels:
        value = _normalize_label(label)
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return tuple(normalized)


def _normalize_label(label: str) -> str:
    return str(label or "").strip().lower()


def _unique_strings(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        unique.append(value)
        seen.add(value)
    return unique
