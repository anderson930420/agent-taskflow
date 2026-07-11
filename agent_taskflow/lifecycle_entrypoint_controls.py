"""Structured entrypoint handling for persisted PR-6 runtime controls."""

from __future__ import annotations

from functools import wraps
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

from agent_taskflow.lifecycle_control import (
    RuntimeControlError,
    RuntimeControlStore,
)
from agent_taskflow.tasks import normalize_task_key


def _db_path(store: Any | None, fallback: str | Path | None) -> str | Path | None:
    return getattr(store, "db_path", None) if store is not None else fallback


def install_lifecycle_entrypoint_controls(
    *,
    dispatcher_module: ModuleType,
    approved_task_runner_module: ModuleType,
) -> None:
    """Convert pause/kill admission denials into structured blocked results."""
    if getattr(
        approved_task_runner_module.run_approved_task,
        "__lifecycle_entrypoint_controls__",
        False,
    ):
        return

    current_run = approved_task_runner_module.run_approved_task

    @wraps(current_run)
    def controlled_run_approved_task(
        request: Any,
        *,
        store: Any | None = None,
        executor_registry: Mapping[str, Any] | None = None,
        validator_registry: Mapping[str, Any] | None = None,
        preflight_runner: Any = approved_task_runner_module.run_preflight,
    ) -> Any:
        if not request.dry_run and request.confirm_approved_task:
            try:
                RuntimeControlStore(_db_path(store, request.db_path)).assert_admission_allowed(
                    request.task_key
                )
            except RuntimeControlError as exc:
                return approved_task_runner_module._blocked_preview(
                    request,
                    phase="runtime_control",
                    error=str(exc),
                )
        try:
            return current_run(
                request,
                store=store,
                executor_registry=executor_registry,
                validator_registry=validator_registry,
                preflight_runner=preflight_runner,
            )
        except RuntimeControlError as exc:
            # A control may be changed between the precheck and atomic claim.
            return approved_task_runner_module._blocked_preview(
                request,
                phase="runtime_control",
                error=str(exc),
            )

    controlled_run_approved_task.__lifecycle_entrypoint_controls__ = True
    approved_task_runner_module.run_approved_task = controlled_run_approved_task

    current_dispatcher = dispatcher_module.Dispatcher

    class LifecycleControlledDispatcher(current_dispatcher):
        __lifecycle_entrypoint_controls__ = True

        def dispatch_task(
            self,
            task_key: str,
            *,
            executor_name: str | None = None,
            model: str | None = None,
            dry_run: bool = False,
        ):
            normalized = normalize_task_key(task_key)
            if not dry_run:
                try:
                    RuntimeControlStore(self.store.db_path).assert_admission_allowed(
                        normalized
                    )
                except RuntimeControlError as exc:
                    reason = str(exc)
                    return dispatcher_module.DispatcherResult(
                        task_key=normalized,
                        status="blocked",
                        summary=reason,
                        blocked_reason=reason,
                    )
            try:
                return super().dispatch_task(
                    normalized,
                    executor_name=executor_name,
                    model=model,
                    dry_run=dry_run,
                )
            except RuntimeControlError as exc:
                reason = str(exc)
                return dispatcher_module.DispatcherResult(
                    task_key=normalized,
                    status="blocked",
                    summary=reason,
                    blocked_reason=reason,
                )

    LifecycleControlledDispatcher.__name__ = "Dispatcher"
    LifecycleControlledDispatcher.__qualname__ = "Dispatcher"
    LifecycleControlledDispatcher.__module__ = dispatcher_module.__name__
    dispatcher_module.Dispatcher = LifecycleControlledDispatcher


__all__ = ["install_lifecycle_entrypoint_controls"]
