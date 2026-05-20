"""Explicit queued-task handoff runner.

This module is the deterministic bridge between a Phase 6E Task
Execution Package and the explicit operator-driven approved task
runner. It verifies that a queued TaskRecord has a valid execution
package (implementation_prompt.md + task_execution_package.json) and,
under explicit --confirm-handoff, hands the task off to
approved_task_runner.run_approved_task.

This module is NOT a scheduler, NOT a background loop, NOT a webhook
handler, NOT a polling daemon, and does NOT auto-pick queued tasks.
Every invocation is one explicit operator command for one explicit
task key, gated by an explicit confirmation flag. It stops at the
runner's own final status (waiting_approval on success, blocked on
failure); it never continues into PR handoff, branch push, draft PR
creation, merge, approval, or cleanup.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from agent_taskflow.approved_task_runner import (
    APPROVED_TASK_STATUS,
    ApprovedTaskRunRequest,
    ApprovedTaskRunResult,
    ApprovedTaskRunnerError,
    run_approved_task,
)
from agent_taskflow.dispatcher import DEFAULT_VALIDATORS
from agent_taskflow.executors.base import Executor
from agent_taskflow.models import TaskRecord, require_absolute_path
from agent_taskflow.store import TaskMirrorStore, default_db_path
from agent_taskflow.task_execution_package import (
    IMPLEMENTATION_PROMPT_FILENAME,
    PACKAGE_FILENAME,
    SCHEMA_VERSION,
)
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.validators.base import Validator


DEFAULT_BASE_BRANCH = "main"
TASK_QUEUE_STATUS = "queued"
RUNNER_BLOCKED_STATUS = "blocked"


ApprovedTaskRunnerCallable = Callable[..., ApprovedTaskRunResult]


class QueuedTaskHandoffError(RuntimeError):
    """Raised when the queued-task handoff cannot proceed."""


def _normalize_validators(validators: Sequence[str] | None) -> tuple[str, ...]:
    if validators is None:
        return DEFAULT_VALIDATORS
    normalized = tuple(value.strip() for value in validators if str(value).strip())
    return normalized or DEFAULT_VALIDATORS


@dataclass(frozen=True)
class QueuedTaskHandoffRequest:
    """Input for one explicit queued-task handoff."""

    task_key: str
    executor: str
    repo_path: Path
    db_path: Path | None = None
    artifact_root: Path | None = None
    worktree_root: Path | None = None
    base_branch: str = DEFAULT_BASE_BRANCH
    validators: tuple[str, ...] = DEFAULT_VALIDATORS
    command: tuple[str, ...] | None = None
    preflight: bool = True
    dry_run: bool = True
    confirm_handoff: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))

        executor = self.executor.strip().lower()
        if not executor:
            raise ValueError("executor must not be empty")
        object.__setattr__(self, "executor", executor)

        object.__setattr__(
            self,
            "repo_path",
            require_absolute_path(self.repo_path, "repo_path"),
        )

        if self.db_path is None:
            db_path = default_db_path()
        else:
            db_path = require_absolute_path(self.db_path, "db_path")
        object.__setattr__(self, "db_path", Path(db_path))

        if self.artifact_root is not None:
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

        base_branch = self.base_branch.strip()
        if not base_branch:
            raise ValueError("base_branch must not be empty")
        object.__setattr__(self, "base_branch", base_branch)

        object.__setattr__(
            self,
            "validators",
            _normalize_validators(self.validators),
        )

        if self.command is not None:
            command = tuple(part for part in self.command if str(part).strip())
            if not command:
                raise ValueError("command must not be empty when provided")
            object.__setattr__(self, "command", command)

        if self.dry_run and self.confirm_handoff:
            raise ValueError(
                "dry_run and confirm_handoff are mutually exclusive"
            )
        if not self.dry_run and not self.confirm_handoff:
            raise ValueError(
                "confirmed handoff requires confirm_handoff=True"
            )


@dataclass(frozen=True)
class QueuedTaskHandoffResult:
    """Structured result for a queued-task handoff invocation."""

    ok: bool
    status: str
    phase: str
    task_key: str
    executor: str
    dry_run: bool
    package: dict[str, Any]
    handoff: dict[str, Any]
    runner_result: dict[str, Any] | None
    safety: dict[str, Any]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "phase": self.phase,
            "task_key": self.task_key,
            "executor": self.executor,
            "dry_run": self.dry_run,
            "package": self.package,
            "handoff": self.handoff,
            "runner_result": self.runner_result,
            "safety": self.safety,
            "error": self.error,
        }


def _safety_block(
    *,
    dry_run: bool,
    package_verified: bool,
    handoff_confirmed: bool,
    runner_started: bool,
    workspace_prepared: bool = False,
    executor_started: bool = False,
    validators_started: bool = False,
    db_written: bool = False,
    artifact_written: bool = False,
) -> dict[str, Any]:
    return {
        "read_only": dry_run and not runner_started,
        "db_written": db_written,
        "artifact_written": artifact_written,
        "package_verified": package_verified,
        "handoff_confirmed": handoff_confirmed,
        "approved_task_runner_started": runner_started,
        "workspace_prepared": workspace_prepared,
        "executor_started": executor_started,
        "validators_started": validators_started,
        "branch_pushed": False,
        "pr_created": False,
        "merged": False,
        "approved": False,
        "cleanup_performed": False,
        "background_worker_started": False,
    }


def _blocked(
    request: QueuedTaskHandoffRequest,
    *,
    phase: str,
    error: str,
    package: dict[str, Any] | None = None,
) -> QueuedTaskHandoffResult:
    return QueuedTaskHandoffResult(
        ok=False,
        status="blocked",
        phase=phase,
        task_key=request.task_key,
        executor=request.executor,
        dry_run=request.dry_run,
        package=package or _empty_package_view(),
        handoff={
            "confirmed": False,
            "approved_task_runner_invoked": False,
            "executor": request.executor,
            "base_branch": request.base_branch,
            "validators": list(request.validators),
            "command": list(request.command) if request.command else None,
            "preflight": request.preflight,
        },
        runner_result=None,
        safety=_safety_block(
            dry_run=request.dry_run,
            package_verified=bool(package and package.get("verified")),
            handoff_confirmed=False,
            runner_started=False,
        ),
        error=error,
    )


def _empty_package_view() -> dict[str, Any]:
    return {
        "verified": False,
        "package_path": None,
        "implementation_prompt_path": None,
        "schema_version": None,
        "task_key": None,
        "status_before": None,
    }


def _verify_package(
    *,
    task: TaskRecord,
    request: QueuedTaskHandoffRequest,
) -> tuple[dict[str, Any] | None, str | None]:
    """Verify the on-disk Task Execution Package.

    Returns (package_view, error). Exactly one is non-None on the
    failure path; on success, error is None and package_view is the
    verified view dict.
    """

    artifact_dir = _resolve_artifact_dir(task, request)
    if artifact_dir is None:
        return None, (
            "Task has no artifact_dir and no artifact_root was supplied; "
            "cannot locate task_execution_package.json"
        )

    package_path = artifact_dir / PACKAGE_FILENAME
    prompt_path = artifact_dir / IMPLEMENTATION_PROMPT_FILENAME

    view: dict[str, Any] = {
        "verified": False,
        "package_path": str(package_path),
        "implementation_prompt_path": str(prompt_path),
        "schema_version": None,
        "task_key": None,
        "status_before": None,
    }

    if not package_path.exists():
        return view, f"Task execution package is missing: {package_path}"

    if not prompt_path.exists():
        return view, f"Implementation prompt is missing: {prompt_path}"

    try:
        raw = package_path.read_text(encoding="utf-8")
    except OSError as exc:
        return view, f"Could not read task execution package: {exc}"

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return view, f"Task execution package is not valid JSON: {exc}"

    if not isinstance(payload, dict):
        return view, "Task execution package must be a JSON object"

    schema_version = payload.get("schema_version")
    view["schema_version"] = schema_version
    if schema_version != SCHEMA_VERSION:
        return view, (
            f"Task execution package schema_version must be {SCHEMA_VERSION!r}, "
            f"got {schema_version!r}"
        )

    package_task_key = payload.get("task_key")
    view["task_key"] = package_task_key
    if package_task_key != task.task_key:
        return view, (
            f"Task execution package task_key {package_task_key!r} does not "
            f"match requested task_key {task.task_key!r}"
        )

    status_before = payload.get("status_before")
    view["status_before"] = status_before
    if status_before is not None and status_before != TASK_QUEUE_STATUS:
        return view, (
            f"Task execution package status_before must be {TASK_QUEUE_STATUS!r} "
            f"when present, got {status_before!r}"
        )

    package_prompt_path = payload.get("implementation_prompt_path")
    if package_prompt_path is not None and Path(package_prompt_path) != prompt_path:
        return view, (
            f"Task execution package implementation_prompt_path "
            f"{package_prompt_path!r} does not match expected {str(prompt_path)!r}"
        )

    package_self_path = payload.get("package_path")
    if package_self_path is not None and Path(package_self_path) != package_path:
        return view, (
            f"Task execution package package_path {package_self_path!r} "
            f"does not match expected {str(package_path)!r}"
        )

    view["verified"] = True
    return view, None


def _resolve_artifact_dir(
    task: TaskRecord,
    request: QueuedTaskHandoffRequest,
) -> Path | None:
    if task.artifact_dir is not None:
        return task.artifact_dir
    if request.artifact_root is not None:
        return request.artifact_root / task.task_key
    return None


def run_queued_task_handoff(
    request: QueuedTaskHandoffRequest,
    *,
    store: TaskMirrorStore | None = None,
    approved_task_runner: ApprovedTaskRunnerCallable = run_approved_task,
    executor_registry: Mapping[str, Executor] | None = None,
    validator_registry: Mapping[str, Validator] | None = None,
    preflight_runner=None,
) -> QueuedTaskHandoffResult:
    """Verify the execution package and, on confirm, hand off to the runner.

    The approved_task_runner callable is injectable so tests can verify
    handoff behavior without running real executors, validators, or git
    worktree commands.
    """

    current_store = store or TaskMirrorStore(request.db_path)

    task = current_store.get_task(request.task_key)
    if task is None:
        return _blocked(
            request,
            phase="selection",
            error=f"Task not found: {request.task_key}",
        )

    if task.status != TASK_QUEUE_STATUS:
        return _blocked(
            request,
            phase="selection",
            error=(
                f"Queued-task handoff requires task.status={TASK_QUEUE_STATUS!r}; "
                f"current status: {task.status!r}"
            ),
        )

    package_view, package_error = _verify_package(task=task, request=request)
    if package_error is not None:
        return _blocked(
            request,
            phase="package_verification",
            error=package_error,
            package=package_view,
        )

    assert package_view is not None
    assert package_view["verified"] is True

    handoff_meta = {
        "confirmed": bool(request.confirm_handoff),
        "approved_task_runner_invoked": False,
        "executor": request.executor,
        "base_branch": request.base_branch,
        "validators": list(request.validators),
        "command": list(request.command) if request.command else None,
        "preflight": request.preflight,
    }

    if request.dry_run:
        return QueuedTaskHandoffResult(
            ok=True,
            status="preview",
            phase="preview",
            task_key=request.task_key,
            executor=request.executor,
            dry_run=True,
            package=package_view,
            handoff=handoff_meta,
            runner_result=None,
            safety=_safety_block(
                dry_run=True,
                package_verified=True,
                handoff_confirmed=False,
                runner_started=False,
            ),
            error=None,
        )

    runner_request = ApprovedTaskRunRequest(
        task_key=request.task_key,
        executor=request.executor,
        repo_path=request.repo_path,
        db_path=request.db_path,
        artifact_root=request.artifact_root,
        worktree_root=request.worktree_root,
        base_branch=request.base_branch,
        validators=request.validators,
        confirm_approved_task=True,
        dry_run=False,
        preflight=request.preflight,
        command=request.command,
    )

    runner_kwargs: dict[str, Any] = {"store": current_store}
    if executor_registry is not None:
        runner_kwargs["executor_registry"] = executor_registry
    if validator_registry is not None:
        runner_kwargs["validator_registry"] = validator_registry
    if preflight_runner is not None:
        runner_kwargs["preflight_runner"] = preflight_runner

    try:
        runner_result = approved_task_runner(runner_request, **runner_kwargs)
    except ApprovedTaskRunnerError as exc:
        return QueuedTaskHandoffResult(
            ok=False,
            status="blocked",
            phase="runner",
            task_key=request.task_key,
            executor=request.executor,
            dry_run=False,
            package=package_view,
            handoff={**handoff_meta, "approved_task_runner_invoked": True},
            runner_result=None,
            safety=_safety_block(
                dry_run=False,
                package_verified=True,
                handoff_confirmed=True,
                runner_started=True,
            ),
            error=str(exc),
        )

    runner_dict = _runner_result_to_dict(runner_result)
    runner_safety = runner_dict.get("safety") or {}
    runner_status = runner_dict.get("status")
    ok = bool(runner_dict.get("ok")) and runner_status == APPROVED_TASK_STATUS
    status = APPROVED_TASK_STATUS if ok else "blocked"
    phase = APPROVED_TASK_STATUS if ok else "runner"

    return QueuedTaskHandoffResult(
        ok=ok,
        status=status,
        phase=phase,
        task_key=request.task_key,
        executor=request.executor,
        dry_run=False,
        package=package_view,
        handoff={**handoff_meta, "approved_task_runner_invoked": True},
        runner_result=runner_dict,
        safety=_safety_block(
            dry_run=False,
            package_verified=True,
            handoff_confirmed=True,
            runner_started=True,
            workspace_prepared=bool(runner_safety.get("workspace_prepared")),
            executor_started=bool(runner_safety.get("executor_started")),
            validators_started=bool(runner_safety.get("validators_started")),
            db_written=bool(runner_safety.get("db_written")),
            artifact_written=bool(runner_safety.get("artifact_written")),
        ),
        error=runner_dict.get("error") if not ok else None,
    )


def _runner_result_to_dict(result: Any) -> dict[str, Any]:
    if hasattr(result, "to_dict"):
        payload = result.to_dict()
    elif isinstance(result, dict):
        payload = result
    else:  # pragma: no cover - defensive shape check
        raise TypeError(
            "approved_task_runner must return an ApprovedTaskRunResult or dict"
        )
    if not isinstance(payload, dict):  # pragma: no cover - defensive shape check
        raise TypeError("approved_task_runner result.to_dict() must return a dict")
    return payload


__all__ = [
    "APPROVED_TASK_STATUS",
    "DEFAULT_BASE_BRANCH",
    "QueuedTaskHandoffError",
    "QueuedTaskHandoffRequest",
    "QueuedTaskHandoffResult",
    "run_queued_task_handoff",
]
