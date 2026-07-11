"""Install PR-9 managed validator process bindings on canonical runtime paths."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import agent_taskflow.attempt_scoped_runtime_path as attempt_path
import agent_taskflow.canonical_runtime_path as canonical_path
from agent_taskflow.executor_launch import ExecutorLaunchBinding
from agent_taskflow.reset_runtime_path import ResetLineageRuntimeTaskStore
from agent_taskflow.validator_process_schema import migrate_validator_process_lifecycle


class ValidatorProcessRuntimeTaskStore(ResetLineageRuntimeTaskStore):
    """Final runtime store layer with managed validator process groups."""

    def init_db(self) -> None:
        migrate_validator_process_lifecycle(self.db_path)

    def bind_validator_context(self, context: Any) -> Any:
        bound = super().bind_validator_context(context)
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

    def classify_validator_result(self, task_key: str, result: Any) -> None:
        summary = (result.summary or "").lower()
        if (
            "validator process-group exit could not be verified" in summary
            or "verified_exit=false" in summary
        ):
            self._set_outcome(
                task_key,
                attempt_status="execution_aborted",
                reason_code="validator_process_exit_unverified",
                execution_result="completed",
                validation_result="aborted",
                metadata={
                    "validator": result.validator,
                    "exit_code": result.exit_code,
                    "summary": result.summary,
                },
            )
            return
        if "validator leader exited with live descendants" in summary:
            self._set_outcome(
                task_key,
                attempt_status="execution_aborted",
                reason_code="validator_descendant_cleanup",
                execution_result="completed",
                validation_result="aborted",
                metadata={
                    "validator": result.validator,
                    "exit_code": result.exit_code,
                    "summary": result.summary,
                },
            )
            return
        super().classify_validator_result(task_key, result)


def install_validator_process_runtime_path(
    *,
    dispatcher_module: ModuleType,
    approved_task_runner_module: ModuleType,
) -> None:
    """Make managed validator launch the final canonical runtime-store layer."""
    if getattr(canonical_path, "__validator_process_runtime_installed__", False):
        return

    def validator_canonicalize_store(
        store: Any | None,
        db_path: str | Path | None,
    ) -> ValidatorProcessRuntimeTaskStore:
        if isinstance(store, ValidatorProcessRuntimeTaskStore):
            return store
        resolved_path = getattr(store, "db_path", None) if store is not None else db_path
        return ValidatorProcessRuntimeTaskStore(resolved_path)

    def validator_attempt_store_for_request(
        store: Any | None,
        request: Any,
    ) -> ValidatorProcessRuntimeTaskStore:
        if isinstance(store, ValidatorProcessRuntimeTaskStore):
            return store
        resolved_path = getattr(store, "db_path", None) if store is not None else request.db_path
        return ValidatorProcessRuntimeTaskStore(resolved_path)

    canonical_path._canonicalize_store = validator_canonicalize_store
    attempt_path._attempt_store_for_request = validator_attempt_store_for_request
    canonical_path.__validator_process_runtime_installed__ = True


__all__ = [
    "ValidatorProcessRuntimeTaskStore",
    "install_validator_process_runtime_path",
]
