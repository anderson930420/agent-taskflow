"""Install PR-7 managed executor process bindings on canonical runtime paths."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import agent_taskflow.attempt_scoped_runtime_path as attempt_path
import agent_taskflow.canonical_runtime_path as canonical_path
from agent_taskflow.executor_launch import ExecutorLaunchBinding
from agent_taskflow.executor_process_schema import migrate_executor_process_lifecycle
from agent_taskflow.lifecycle_runtime_path import LifecycleRuntimeTaskStore


class ExecutorProcessRuntimeTaskStore(LifecycleRuntimeTaskStore):
    """Final runtime store layer that binds executor launches to one Attempt."""

    def init_db(self) -> None:
        migrate_executor_process_lifecycle(self.db_path)

    def bind_executor_context(self, context: Any) -> Any:
        bound = super().bind_executor_context(context)
        claim = self.runtime_claim(bound.task_key)
        resource = self.attempt_resource(bound.task_key)
        if claim is None or resource is None:
            return bound
        launch_binding = ExecutorLaunchBinding(
            db_path=self.db_path,
            attempt_id=claim.attempt_id,
            task_id=claim.task_id,
            task_key=claim.task_key,
            lease_id=claim.lease_id,
            owner_id=claim.owner_id,
            worktree_path=resource.worktree_path,
            artifact_root=resource.artifact_root,
        )
        return replace(bound, launch_binding=launch_binding)

    def classify_executor_result(self, task_key: str, result: Any) -> None:
        summary = (result.summary or "").lower()
        if (
            "process-group exit could not be verified" in summary
            or "verified_exit=false" in summary
        ):
            self._set_outcome(
                task_key,
                attempt_status="execution_aborted",
                reason_code="executor_process_exit_unverified",
                execution_result="aborted",
                validation_result=None,
                metadata={
                    "executor": result.executor,
                    "exit_code": result.exit_code,
                    "summary": result.summary,
                },
            )
            return
        if "leader exited with live descendants" in summary:
            self._set_outcome(
                task_key,
                attempt_status="execution_aborted",
                reason_code="executor_descendant_cleanup",
                execution_result="aborted",
                validation_result=None,
                metadata={
                    "executor": result.executor,
                    "exit_code": result.exit_code,
                    "summary": result.summary,
                },
            )
            return
        super().classify_executor_result(task_key, result)


def install_executor_process_runtime_path(
    *,
    dispatcher_module: ModuleType,
    approved_task_runner_module: ModuleType,
) -> None:
    """Make managed process launch the final canonical executor-store layer."""
    if getattr(canonical_path, "__executor_process_runtime_installed__", False):
        return

    def process_canonicalize_store(
        store: Any | None,
        db_path: str | Path | None,
    ) -> ExecutorProcessRuntimeTaskStore:
        if isinstance(store, ExecutorProcessRuntimeTaskStore):
            return store
        resolved_path = getattr(store, "db_path", None) if store is not None else db_path
        return ExecutorProcessRuntimeTaskStore(resolved_path)

    def process_attempt_store_for_request(
        store: Any | None,
        request: Any,
    ) -> ExecutorProcessRuntimeTaskStore:
        if isinstance(store, ExecutorProcessRuntimeTaskStore):
            return store
        resolved_path = getattr(store, "db_path", None) if store is not None else request.db_path
        return ExecutorProcessRuntimeTaskStore(resolved_path)

    canonical_path._canonicalize_store = process_canonicalize_store
    attempt_path._attempt_store_for_request = process_attempt_store_for_request
    canonical_path.__executor_process_runtime_installed__ = True


__all__ = [
    "ExecutorProcessRuntimeTaskStore",
    "install_executor_process_runtime_path",
]
