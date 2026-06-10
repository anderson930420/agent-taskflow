"""Scheduled, locked one-task tick for GitHub Issue automation.

This module wraps the existing one-shot GitHub Issue automation in a shared
non-overlap lock so cron, systemd timers, and manual automation invocations use
the same advisory lock path. It is one tick only: no daemon, scheduler loop,
background worker, or multi-task batch is started here. Human review and human
merge remain external final gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent_taskflow.approved_task_runner import ApprovedTaskRunRequest, run_approved_task
from agent_taskflow.dispatcher import DEFAULT_VALIDATORS
from agent_taskflow.execution_engine_contract import ExecutionEngine
from agent_taskflow.github_issue_discovery import IssueListFetcher
from agent_taskflow.github_issue_ingestion import IssueFetcher
from agent_taskflow.github_issue_one_task_automation import (
    GitHubIssueOneTaskAutomationError,
    GitHubIssueOneTaskAutomationRequest,
    run_github_issue_one_task_automation,
)
from agent_taskflow.github_issue_one_task_lock import (
    NonOverlapLock,
    default_github_issue_one_task_lock_path,
)
from agent_taskflow.models import require_absolute_path
from agent_taskflow.scheduler_execution_engine_opt_in import (
    route_scheduler_tick_through_execution_engine,
)


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
    publish_after_execution: bool = False

    executor: str | None = None
    validators: tuple[str, ...] = DEFAULT_VALIDATORS
    worktree_root: Path | None = None
    approved_task_preflight: bool = True
    command: tuple[str, ...] | None = None

    # Executor profile metadata threaded down to ingestion and the approved
    # task runner so a confirmed tick can drive a real executor profile, not
    # only noop.
    model: str | None = None
    provider: str | None = None
    tools: tuple[str, ...] | None = None
    pi_bin: str | None = None

    # P5-d opt-in: route the one selected confirmed task through the
    # ExecutionEngine facade for runtime evidence. Off by default. The legacy
    # scheduler tick path is unchanged unless this is explicitly enabled, and it
    # is only valid in confirmed mode (see __post_init__). The active cron path
    # never sets this flag.
    use_execution_engine: bool = False

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

        if self.worktree_root is not None:
            object.__setattr__(
                self,
                "worktree_root",
                require_absolute_path(self.worktree_root, "worktree_root"),
            )

        if self.confirmed:
            object.__setattr__(self, "dry_run", False)
        elif not self.dry_run:
            raise ValueError("confirmed mode requires confirmed=True")

        if self.use_execution_engine and not self.confirmed:
            raise ValueError(
                "use_execution_engine requires confirmed mode: the scheduler "
                "ExecutionEngine opt-in path is execution-only and confirmed-"
                "mode only; a dry-run tick cannot enable --use-execution-engine"
            )

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
        object.__setattr__(
            self,
            "validators",
            _normalize_validators(self.validators),
        )

        lock_path = self.lock_path or default_lock_path()
        lock_path = Path(lock_path).expanduser()
        if not lock_path.is_absolute():
            raise ValueError("lock_path must be an absolute path")
        object.__setattr__(self, "lock_path", lock_path)

        for field_name in (
            "operator",
            "operator_note",
            "base_branch",
            "executor",
            "model",
            "provider",
            "pi_bin",
        ):
            value = getattr(self, field_name)
            if value is None:
                continue
            stripped = str(value).strip()
            object.__setattr__(self, field_name, stripped or None)

        if self.tools is not None:
            object.__setattr__(self, "tools", _normalize_executor_tools(self.tools))

        remote = str(self.remote or "").strip()
        if not remote:
            raise ValueError("remote must not be empty")
        object.__setattr__(self, "remote", remote)

        command = self.command
        if command is not None:
            normalized_command = tuple(
                str(part).strip() for part in command if str(part).strip()
            )
            if not normalized_command:
                raise ValueError("command must not be empty when provided")
            object.__setattr__(self, "command", normalized_command)


def default_lock_path() -> Path:
    """Return the shared non-overlap lock path for GitHub Issue automation."""

    return default_github_issue_one_task_lock_path()


def run_github_issue_one_task_scheduler_tick(
    request: GitHubIssueOneTaskSchedulerTickRequest,
    *,
    discovery_fetcher: IssueListFetcher | None = None,
    ingestion_fetcher: IssueFetcher | None = None,
    approved_task_runner_fn: Callable[..., dict[str, Any]] | None = None,
    branch_push_fn: Callable[..., dict[str, Any]] | None = None,
    draft_pr_fn: Callable[..., dict[str, Any]] | None = None,
    execution_engine: ExecutionEngine | None = None,
) -> dict[str, Any]:
    """Run one locked scheduler tick and stop.

    The legacy path is the default. When ``request.use_execution_engine`` is set
    (confirmed mode only), the one selected task is additionally routed through
    the ExecutionEngine facade for runtime evidence, and an ``execution_engine``
    opt-in evidence block is attached to the returned payload. The
    ``execution_engine`` argument injects the engine facade for tests; it is
    unused unless the opt-in is enabled, and the default facade is supplied by
    the dedicated opt-in helper module.
    """

    runner_fn = approved_task_runner_fn or _configured_approved_task_runner_fn(request)

    lock = NonOverlapLock(request.lock_path)
    try:
        acquired = lock.acquire(blocking=not request.fail_if_locked)
    except OSError as exc:
        return _maybe_attach_execution_engine(
            request,
            _failure_response(
                request,
                status="lock_failed",
                reasons=[str(exc)],
                lock_acquired=False,
                lock_contended=False,
                automation=None,
            ),
            execution_engine=execution_engine,
        )

    if not acquired:
        return _maybe_attach_execution_engine(
            request,
            _locked_response(request),
            execution_engine=execution_engine,
        )

    automation: dict[str, Any] | None = None
    automation_error: str | None = None
    try:
        try:
            automation = run_github_issue_one_task_automation(
                _automation_request(request),
                discovery_fetcher=discovery_fetcher,
                ingestion_fetcher=ingestion_fetcher,
                approved_task_runner_fn=runner_fn,
                branch_push_fn=branch_push_fn,
                draft_pr_fn=draft_pr_fn,
            )
        except (GitHubIssueOneTaskAutomationError, ValueError) as exc:
            automation_error = str(exc)
    finally:
        lock.release()

    if automation_error is not None:
        return _maybe_attach_execution_engine(
            request,
            _failure_response(
                request,
                status="automation_error",
                reasons=[automation_error],
                lock_acquired=True,
                lock_contended=False,
                lock_released=True,
                automation_called=True,
                automation=automation,
            ),
            execution_engine=execution_engine,
        )

    if automation is None:
        return _maybe_attach_execution_engine(
            request,
            _failure_response(
                request,
                status="automation_error",
                reasons=["automation returned no result"],
                lock_acquired=True,
                lock_contended=False,
                lock_released=True,
                automation_called=True,
                automation=None,
            ),
            execution_engine=execution_engine,
        )

    return _maybe_attach_execution_engine(
        request,
        _automation_response(
            request,
            automation=automation,
            lock_released=True,
        ),
        execution_engine=execution_engine,
    )


def _maybe_attach_execution_engine(
    request: GitHubIssueOneTaskSchedulerTickRequest,
    response: dict[str, Any],
    *,
    execution_engine: ExecutionEngine | None,
) -> dict[str, Any]:
    """Attach the P5-d ``execution_engine`` opt-in evidence block, if enabled.

    Off by default: when ``use_execution_engine`` is not set, the legacy
    response is returned unchanged. When enabled, the one selected confirmed
    task is routed through the ExecutionEngine facade for runtime evidence only;
    the engine result never changes the legacy ``ok`` / ``status`` / publication
    / safety decision and never publishes, merges, approves, cleans up, or
    deletes a branch or worktree.
    """

    if not request.use_execution_engine:
        return response
    response["execution_engine"] = route_scheduler_tick_through_execution_engine(
        request,
        response,
        engine=execution_engine,
    )
    return response


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
            publish_after_execution=request.publish_after_execution,
            lock_path=request.lock_path,
            fail_if_locked=request.fail_if_locked,
            model=request.model,
            provider=request.provider,
            tools=request.tools,
            pi_bin=request.pi_bin,
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
        publish_after_execution=request.publish_after_execution,
        lock_path=request.lock_path,
        fail_if_locked=request.fail_if_locked,
        model=request.model,
        provider=request.provider,
        tools=request.tools,
        pi_bin=request.pi_bin,
    )


def _configured_approved_task_runner_fn(
    request: GitHubIssueOneTaskSchedulerTickRequest,
) -> Callable[..., dict[str, Any]] | None:
    """Return an injected runner wrapper when scheduler runner config exists."""

    if request.executor is None:
        return None

    def _runner(**kwargs: Any) -> dict[str, Any]:
        task_key = str(kwargs.get("task_key") or "").strip()
        if not task_key:
            return {
                "ok": False,
                "status": "blocked",
                "phase": "scheduler_runner_config",
                "error": "approved runner wrapper requires task_key",
                "safety": {
                    "executor_started": False,
                    "validators_started": False,
                    "github_mutated": False,
                },
            }
        result = run_approved_task(
            ApprovedTaskRunRequest(
                task_key=task_key,
                executor=request.executor or "",
                repo_path=request.local_repo_path,
                db_path=request.db_path,
                artifact_root=request.artifact_root,
                worktree_root=request.worktree_root,
                base_branch=request.base_branch or "main",
                validators=request.validators,
                confirm_approved_task=True,
                dry_run=False,
                preflight=request.approved_task_preflight,
                command=request.command,
                model=request.model,
                provider=request.provider,
                tools=request.tools,
                pi_bin=request.pi_bin,
            )
        )
        return result.to_dict()

    return _runner


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
        "runner_config": _runner_config_payload(request),
        "publication_config": _publication_config_payload(request),
        "automation": automation,
        "selected_task_key": automation.get("selected_task_key"),
        "ingestion_failure_registry": automation.get("ingestion_failure_registry"),
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
        "runner_config": _runner_config_payload(request),
        "publication_config": _publication_config_payload(request),
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
        "runner_config": _runner_config_payload(request),
        "publication_config": _publication_config_payload(request),
        "automation": automation,
        "selected_task_key": (
            automation.get("selected_task_key") if automation else None
        ),
        "ingestion_failure_registry": (
            automation.get("ingestion_failure_registry") if automation else None
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


def _runner_config_payload(request: GitHubIssueOneTaskSchedulerTickRequest) -> dict[str, Any]:
    return {
        "configured": request.executor is not None,
        "executor": request.executor,
        "validators": list(request.validators),
        "worktree_root": str(request.worktree_root) if request.worktree_root else None,
        "base_branch": request.base_branch or "main",
        "preflight": request.approved_task_preflight,
        "command": list(request.command) if request.command else None,
        "model": request.model,
        "provider": request.provider,
        "tools": list(request.tools) if request.tools else None,
        "pi_bin": request.pi_bin,
    }


def _publication_config_payload(
    request: GitHubIssueOneTaskSchedulerTickRequest,
) -> dict[str, Any]:
    """Describe whether this tick is execution-only or publication-enabled.

    The scheduler confirmed tick defaults to execution-only: it stops after the
    one-task pipeline reaches ``waiting_approval`` and never publishes. Branch
    push and draft PR creation remain the separate explicit task-to-draft-PR
    workflow unless ``publish_after_execution`` is opted in.
    """

    return {
        "publish_after_execution": request.publish_after_execution,
        "mode": (
            "publication" if request.publish_after_execution else "execution_only"
        ),
        "next_operator_action": (
            None
            if request.publish_after_execution
            else (
                "run explicit task-to-draft-pr publication workflow if "
                "publication is desired"
            )
        ),
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
        "runner_configured": request.executor is not None,
        "publish_after_execution": request.publish_after_execution,
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


def _normalize_executor_tools(tools: tuple[str, ...]) -> tuple[str, ...] | None:
    normalized: list[str] = []
    seen: set[str] = set()
    for tool in tools:
        value = str(tool or "").strip()
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return tuple(normalized) or None


def _normalize_validators(validators: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(
        str(value).strip().lower() for value in validators if str(value).strip()
    )
    return normalized or DEFAULT_VALIDATORS


def _unique_strings(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        unique.append(value)
        seen.add(value)
    return unique
