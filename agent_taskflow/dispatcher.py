"""Dispatcher state machine for Agent Taskflow.

The dispatcher advances one task through the local state mirror:

queued/blocked/preparing -> preparing -> implementing -> validating
-> waiting_approval

Failures move the task to blocked. The dispatcher never approves, merges,
pushes, cleans worktrees, or runs raw subprocesses directly. It only calls the
executor and validator abstractions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from agent_taskflow.executors.base import Executor, ExecutorContext, ExecutorResult
from agent_taskflow.executors.registry import get_executor
from agent_taskflow.governance import (
    assert_not_main_repo_write,
    assert_task_has_artifact_dir,
    assert_worktree_inside_repo_worktrees,
)
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord, require_absolute_path
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.validators.base import Validator, ValidatorContext, ValidatorResult
from agent_taskflow.validators.registry import get_validator


DEFAULT_VALIDATORS = ("pytest", "openspec")

RUNNABLE_STATUSES = {
    "queued",
    "blocked",
    "preparing",
}

SKIPPED_STATUSES = {
    "waiting_approval",
    "waiting_for_review",
    "accepted",
    "rejected",
    "cleaned",
    "completed",
    "canceled",
}


@dataclass(frozen=True)
class DispatcherResult:
    """Structured result returned by a dispatcher run."""

    task_key: str
    status: str
    summary: str
    executor_status: str | None = None
    validator_statuses: dict[str, str] = field(default_factory=dict)
    blocked_reason: str | None = None


class Dispatcher:
    """Advance a single task using store state, executors, and validators."""

    def __init__(
        self,
        store: TaskMirrorStore | None = None,
        *,
        db_path: str | Path | None = None,
        executor_registry: Mapping[str, Executor] | None = None,
        validator_registry: Mapping[str, Validator] | None = None,
        validators: Sequence[str] = DEFAULT_VALIDATORS,
        default_executor: str = "manual",
        default_model: str | None = None,
        executor_timeout_seconds: int | None = None,
        validator_timeout_seconds: int | None = None,
    ) -> None:
        if store is not None and db_path is not None:
            raise ValueError("Provide either store or db_path, not both")

        self.store = store if store is not None else TaskMirrorStore(db_path)
        self.executor_registry = dict(executor_registry or {})
        self.validator_registry = dict(validator_registry or {})
        self.validators = tuple(self._normalize_name(name, "validator") for name in validators)
        self.default_executor = self._normalize_name(default_executor, "default_executor")
        self.default_model = default_model
        self.executor_timeout_seconds = executor_timeout_seconds
        self.validator_timeout_seconds = validator_timeout_seconds

    @staticmethod
    def _normalize_name(name: str, field_name: str) -> str:
        normalized = name.strip().lower()
        if not normalized:
            raise ValueError(f"{field_name} must not be empty")
        return normalized

    def dispatch_task(
        self,
        task_key: str,
        *,
        executor_name: str | None = None,
        model: str | None = None,
        dry_run: bool = False,
    ) -> DispatcherResult:
        """Dispatch one task by key.

        dry_run validates task lookup, state, and governance only. It does not
        run executors or validators and does not mutate status.
        """

        normalized_task_key = normalize_task_key(task_key)
        try:
            task = self.store.get_task(normalized_task_key)
        except Exception as exc:
            reason = f"Task record is invalid: {exc}"
            return DispatcherResult(
                task_key=normalized_task_key,
                status="blocked",
                summary=reason,
                blocked_reason=reason,
            )

        if task is None:
            return DispatcherResult(
                task_key=normalized_task_key,
                status="blocked",
                summary=f"Task not found: {normalized_task_key}",
                blocked_reason=f"Task not found: {normalized_task_key}",
            )

        if task.status in SKIPPED_STATUSES:
            return DispatcherResult(
                task_key=task.task_key,
                status="skipped",
                summary=f"Task already in terminal/review status: {task.status}",
            )

        if task.status not in RUNNABLE_STATUSES:
            reason = f"Task status is not runnable: {task.status}"
            if not dry_run:
                self._block_task(task.task_key, reason)
            return DispatcherResult(
                task_key=task.task_key,
                status="blocked",
                summary=reason,
                blocked_reason=reason,
            )

        try:
            worktree = self.store.get_task_worktree(task.task_key)
        except Exception as exc:
            governance_error = f"Task worktree record is invalid: {exc}"
            worktree = None
        else:
            governance_error = self._validate_governance(task, worktree)
        if governance_error is not None:
            if not dry_run:
                self._block_task(task.task_key, governance_error)
            return DispatcherResult(
                task_key=task.task_key,
                status="blocked",
                summary=governance_error,
                blocked_reason=governance_error,
            )

        assert worktree is not None
        assert task.artifact_dir is not None

        selected_executor = self._selected_executor_name(task, executor_name)
        selected_model = self._selected_model(task, model)
        prompt_path = self._prompt_path(task.artifact_dir)

        if selected_executor == "opencode" and not prompt_path.is_file():
            reason = f"implementation_prompt.md is required for opencode executor: {prompt_path}"
            if not dry_run:
                self._block_task(task.task_key, reason)
            return DispatcherResult(
                task_key=task.task_key,
                status="blocked",
                summary=reason,
                blocked_reason=reason,
            )

        if dry_run:
            return DispatcherResult(
                task_key=task.task_key,
                status="skipped",
                summary="Dry run passed; executor and validators were not run.",
            )

        self.store.update_task_status(
            task.task_key,
            "preparing",
            source="dispatcher",
            message="Dispatcher preparing task",
        )

        try:
            executor = self._get_executor(
                selected_executor,
                selected_model,
                provider=task.provider,
                tools=task.tools,
                pi_bin=task.pi_bin,
            )
        except Exception as exc:
            reason = (
                f"Executor {selected_executor} is unavailable: "
                f"{exc.__class__.__name__}: {exc}"
            )
            self._block_task(task.task_key, reason)
            return DispatcherResult(
                task_key=task.task_key,
                status="blocked",
                summary=reason,
                executor_status="blocked",
                blocked_reason=reason,
            )

        executor_context = ExecutorContext(
            task_key=task.task_key,
            project=task.project,
            worktree_path=worktree.worktree_path,
            artifact_dir=task.artifact_dir,
            prompt_path=prompt_path if prompt_path.exists() else None,
            model=selected_model,
            timeout_seconds=self.executor_timeout_seconds,
        )

        self.store.update_task_status(
            task.task_key,
            "implementing",
            source="dispatcher",
            message=f"Dispatcher running executor {selected_executor}",
        )
        executor_run_id = self.store.create_executor_run(
            task.task_key,
            selected_executor,
            model=selected_model,
            prompt_path=executor_context.prompt_path,
        )

        try:
            executor_result = executor.run(executor_context)
        except Exception as exc:  # pragma: no cover - exercised by integration failures.
            reason = f"Executor {selected_executor} raised {exc.__class__.__name__}: {exc}"
            self.store.finish_executor_run(
                task.task_key,
                executor_run_id,
                executor=selected_executor,
                status="blocked",
                summary=reason,
            )
            self._block_task(task.task_key, reason)
            return DispatcherResult(
                task_key=task.task_key,
                status="blocked",
                summary=reason,
                executor_status="blocked",
                blocked_reason=reason,
            )

        self._record_executor_result(task.task_key, executor_run_id, executor_result)

        if executor_result.status in {"failed", "blocked"}:
            reason = (
                executor_result.summary
                or f"Executor {executor_result.executor} returned {executor_result.status}"
            )
            self._block_task(task.task_key, reason)
            return DispatcherResult(
                task_key=task.task_key,
                status="blocked",
                summary=reason,
                executor_status=executor_result.status,
                blocked_reason=reason,
            )

        self.store.update_task_status(
            task.task_key,
            "validating",
            source="dispatcher",
            message="Dispatcher running validators",
        )

        validator_context = ValidatorContext(
            task_key=task.task_key,
            project=task.project,
            worktree_path=worktree.worktree_path,
            artifact_dir=task.artifact_dir,
            timeout_seconds=self.validator_timeout_seconds,
        )

        validator_statuses: dict[str, str] = {}
        for validator_name in self.validators:
            try:
                validator = self._get_validator(validator_name)
                validator_result = validator.run(validator_context)
            except Exception as exc:  # pragma: no cover - exercised by integration failures.
                reason = f"Validator {validator_name} raised {exc.__class__.__name__}: {exc}"
                self.store.record_validation_result(
                    task.task_key,
                    validator_name,
                    status="blocked",
                    summary=reason,
                )
                validator_statuses[validator_name] = "blocked"
                self._block_task(task.task_key, reason)
                return DispatcherResult(
                    task_key=task.task_key,
                    status="blocked",
                    summary=reason,
                    executor_status=executor_result.status,
                    validator_statuses=validator_statuses,
                    blocked_reason=reason,
                )

            self._record_validator_result(task.task_key, validator_result)
            validator_statuses[validator_result.validator] = validator_result.status

            if validator_result.status in {"failed", "blocked"}:
                reason = (
                    validator_result.summary
                    or f"Validator {validator_result.validator} returned {validator_result.status}"
                )
                self._block_task(task.task_key, reason)
                return DispatcherResult(
                    task_key=task.task_key,
                    status="blocked",
                    summary=reason,
                    executor_status=executor_result.status,
                    validator_statuses=validator_statuses,
                    blocked_reason=reason,
                )

        self.store.update_task_status(
            task.task_key,
            "waiting_approval",
            source="dispatcher",
            message="Dispatcher completed implementation and validation",
        )

        return DispatcherResult(
            task_key=task.task_key,
            status="waiting_approval",
            summary="Task dispatched successfully and is waiting for human approval.",
            executor_status=executor_result.status,
            validator_statuses=validator_statuses,
        )

    def _selected_executor_name(
        self,
        task: TaskRecord,
        executor_name: str | None,
    ) -> str:
        raw = executor_name or getattr(task, "executor", None) or self.default_executor
        return self._normalize_name(raw, "executor")

    def _selected_model(self, task: TaskRecord, model: str | None) -> str | None:
        return model or getattr(task, "model", None) or self.default_model

    def _get_executor(
        self,
        executor_name: str,
        model: str | None,
        *,
        provider: str | None = None,
        tools: list[str] | None = None,
        pi_bin: str | None = None,
    ) -> Executor:
        if executor_name in self.executor_registry:
            return self.executor_registry[executor_name]
        # Phase 13: pass pi-specific options from task record
        return get_executor(
            executor_name,
            model=model,
            provider=provider,
            tools=tools if tools else None,
            pi_bin=pi_bin if pi_bin else "pi",
        )

    def _get_validator(self, validator_name: str) -> Validator:
        if validator_name in self.validator_registry:
            return self.validator_registry[validator_name]
        return get_validator(validator_name)

    @staticmethod
    def _prompt_path(artifact_dir: Path) -> Path:
        return artifact_dir / "implementation_prompt.md"

    def _validate_governance(
        self,
        task: TaskRecord,
        worktree: TaskWorktreeRecord | None,
    ) -> str | None:
        try:
            repo_path = require_absolute_path(task.repo_path, "repo_path")
            if task.artifact_dir is None:
                return "Task artifact_dir is required"
            artifact_dir = require_absolute_path(task.artifact_dir, "artifact_dir")
            artifact_dir.mkdir(parents=True, exist_ok=True)
            assert_task_has_artifact_dir(artifact_dir)

            if worktree is None:
                return f"Task worktree not found: {task.task_key}"
            worktree_path = require_absolute_path(
                worktree.worktree_path,
                "worktree_path",
            )
            worktree_repo_path = require_absolute_path(
                worktree.repo_path,
                "worktree.repo_path",
            )
            if worktree_repo_path != repo_path:
                return (
                    f"Task repo_path and worktree repo_path differ: "
                    f"{repo_path} != {worktree_repo_path}"
                )
            assert_not_main_repo_write(worktree_path, repo_path)
            assert_worktree_inside_repo_worktrees(worktree_path, repo_path)
        except (OSError, ValueError) as exc:
            return str(exc)

        return None

    def _record_executor_result(
        self,
        task_key: str,
        run_id: str,
        result: ExecutorResult,
    ) -> None:
        self.store.finish_executor_run(
            task_key,
            run_id,
            executor=result.executor,
            status=result.status,
            exit_code=result.exit_code,
            summary=result.summary,
            log_path=result.log_path,
            artifacts=result.artifacts,
        )

    def _record_validator_result(
        self,
        task_key: str,
        result: ValidatorResult,
    ) -> None:
        self.store.record_validation_result(
            task_key,
            result.validator,
            status=result.status,
            exit_code=result.exit_code,
            summary=result.summary,
            log_path=result.log_path,
            artifacts=result.artifacts,
        )

    def _block_task(self, task_key: str, reason: str) -> None:
        self.store.update_task_status(
            task_key,
            "blocked",
            source="dispatcher",
            message=reason,
            blocked_reason=reason,
        )


def dispatch_task(
    task_key: str,
    *,
    db_path: str | Path | None = None,
    executor_name: str | None = None,
    model: str | None = None,
    validators: Sequence[str] = DEFAULT_VALIDATORS,
    dry_run: bool = False,
) -> DispatcherResult:
    """Convenience wrapper for dispatching one task."""

    dispatcher = Dispatcher(
        db_path=db_path,
        validators=validators,
    )
    return dispatcher.dispatch_task(
        task_key,
        executor_name=executor_name,
        model=model,
        dry_run=dry_run,
    )


__all__ = [
    "DEFAULT_VALIDATORS",
    "Dispatcher",
    "DispatcherResult",
    "dispatch_task",
]
