"""Install PR-6 lifecycle transitions and cooperative controls on runtime paths."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any

import agent_taskflow.attempt_scoped_runtime_path as attempt_path
import agent_taskflow.canonical_runtime_path as canonical_path
from agent_taskflow.attempt_scoped_runtime_path import AttemptScopedRuntimeTaskStore
from agent_taskflow.executors.base import ExecutorResult
from agent_taskflow.lifecycle_control import (
    RuntimeControlStore,
    RuntimeKillRequested,
    task_status_for_attempt,
    validate_attempt_transition,
    validate_reason_code,
)
from agent_taskflow.lifecycle_control_schema import migrate_lifecycle_control
from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import connect
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.validators.base import ValidatorResult


@dataclass(frozen=True)
class _TerminalOutcome:
    attempt_status: str
    reason_code: str
    execution_result: str | None
    validation_result: str | None
    metadata: dict[str, Any]


class LifecycleRuntimeAdmissionStore(canonical_path.CanonicalRuntimeAdmissionStore):
    """Token admission with graph validation and admission control checks."""

    def init_db(self) -> None:
        migrate_lifecycle_control(self.db_path)

    def claim(self, task_key: str, **kwargs: Any):
        RuntimeControlStore(self.db_path).assert_admission_allowed(task_key)
        return super().claim(task_key, **kwargs)

    def transition(
        self,
        attempt_id: str,
        *,
        owner_id: str,
        lease_token: str,
        attempt_status: str,
        reason_code: str,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Atomically advance the active Attempt and its projected task status."""
        self.init_db()
        reason = validate_reason_code(reason_code)
        now = utc_now_iso()
        with connect(self.db_path) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT runtime_leases.*, attempts.status AS attempt_status,
                       tasks.task_key, tasks.status AS task_status
                FROM runtime_leases
                JOIN attempts ON attempts.attempt_id = runtime_leases.attempt_id
                JOIN tasks ON tasks.task_id = runtime_leases.task_id
                WHERE runtime_leases.attempt_id = ?
                  AND runtime_leases.is_active = 1
                """,
                (attempt_id,),
            ).fetchone()
            row = self._verify_owned_lease(
                row,
                owner_id=owner_id,
                lease_token=lease_token,
                now=now,
                allow_expired=False,
            )
            source, target = validate_attempt_transition(
                row["attempt_status"], attempt_status
            )
            projected_task_status = task_status_for_attempt(target)
            conn.execute(
                """
                UPDATE attempts
                SET status = ?, updated_at = ?
                WHERE attempt_id = ? AND is_active = 1 AND status = ?
                """,
                (target, now, attempt_id, source),
            )
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, blocked_reason = NULL,
                    updated_at = ?, last_synced_at = ?
                WHERE task_id = ? AND active_attempt_id = ?
                """,
                (
                    projected_task_status,
                    now,
                    now,
                    row["task_id"],
                    attempt_id,
                ),
            )
            self._insert_lifecycle_event(
                conn,
                task_id=row["task_id"],
                attempt_id=attempt_id,
                from_status=source,
                to_status=target,
                reason_code=reason,
                actor=owner_id,
                timestamp=now,
                metadata={
                    "task_status": projected_task_status,
                    "lease_id": row["lease_id"],
                    "message": message,
                    **(metadata or {}),
                },
            )
            self._insert_status_event(
                conn,
                task_key=row["task_key"],
                status=projected_task_status,
                source=owner_id,
                message=message or f"Runtime transitioned to {target}",
                created_at=now,
            )

    def release(self, attempt_id: str, **kwargs: Any):
        reason = validate_reason_code(kwargs["reason_code"])
        target = kwargs["attempt_status"]
        self.init_db()
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT status FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Attempt not found: {attempt_id}")
        validate_attempt_transition(row["status"], target)
        kwargs["reason_code"] = reason
        return super().release(attempt_id, **kwargs)


class _LifecycleExecutorProxy:
    def __init__(self, executor: Any, store: "LifecycleRuntimeTaskStore") -> None:
        self._executor = executor
        self._store = store
        self.name = getattr(executor, "name", executor.__class__.__name__)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._executor, name)

    def run(self, context: Any) -> ExecutorResult:
        if self._store.kill_requested(context.task_key):
            self._store.mark_operator_kill(context.task_key, phase="before_executor")
            return ExecutorResult(
                executor=self.name,
                status="blocked",
                summary="Operator kill requested before executor invocation.",
            )
        result = self._executor.run(context)
        self._store.classify_executor_result(context.task_key, result)
        if self._store.kill_requested(context.task_key):
            self._store.mark_operator_kill(context.task_key, phase="after_executor")
            return replace(
                result,
                status="blocked",
                summary="Operator kill requested after executor invocation.",
            )
        return result


class _LifecycleValidatorProxy:
    def __init__(self, validator: Any, store: "LifecycleRuntimeTaskStore") -> None:
        self._validator = validator
        self._store = store
        self.name = getattr(validator, "name", validator.__class__.__name__)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._validator, name)

    def run(self, context: Any) -> ValidatorResult:
        if self._store.kill_requested(context.task_key):
            self._store.mark_operator_kill(context.task_key, phase="before_validator")
            return ValidatorResult(
                validator=self.name,
                status="blocked",
                summary="Operator kill requested before validator invocation.",
            )
        result = self._validator.run(context)
        self._store.classify_validator_result(context.task_key, result)
        if self._store.kill_requested(context.task_key):
            self._store.mark_operator_kill(context.task_key, phase="after_validator")
            return replace(
                result,
                status="blocked",
                summary="Operator kill requested after validator invocation.",
            )
        return result


class LifecycleRuntimeTaskStore(AttemptScopedRuntimeTaskStore):
    """Attempt resource store with explicit lifecycle/outcome semantics."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.controls = RuntimeControlStore(self.db_path)
        self._terminal_outcomes: dict[str, _TerminalOutcome] = {}

    def init_db(self) -> None:
        migrate_lifecycle_control(self.db_path)

    def preclaim_runtime(self, task_key: str, **kwargs: Any):
        self.controls.assert_admission_allowed(task_key)
        self._terminal_outcomes.pop(normalize_task_key(task_key), None)
        return super().preclaim_runtime(task_key, **kwargs)

    def kill_requested(self, task_key: str) -> bool:
        claim = self.runtime_claim(task_key)
        control = self.controls.effective_control(
            task_key=task_key,
            attempt_id=claim.attempt_id if claim is not None else None,
        )
        return control.kill_requested

    def _set_outcome(
        self,
        task_key: str,
        *,
        attempt_status: str,
        reason_code: str,
        execution_result: str | None,
        validation_result: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._terminal_outcomes[normalize_task_key(task_key)] = _TerminalOutcome(
            attempt_status=attempt_status,
            reason_code=validate_reason_code(reason_code),
            execution_result=execution_result,
            validation_result=validation_result,
            metadata=dict(metadata or {}),
        )

    def mark_operator_kill(self, task_key: str, *, phase: str) -> None:
        self._set_outcome(
            task_key,
            attempt_status="execution_aborted",
            reason_code="operator_kill_requested",
            execution_result="aborted",
            validation_result=None,
            metadata={"phase": phase, "cooperative": True, "os_signal_sent": False},
        )

    @staticmethod
    def _looks_timed_out(status: str, summary: str | None) -> bool:
        normalized = status.lower()
        text = (summary or "").lower()
        return normalized in {"timeout", "timed_out"} or "timed out" in text or "timeout after" in text

    def classify_executor_result(self, task_key: str, result: ExecutorResult) -> None:
        if self._looks_timed_out(result.status, result.summary):
            self._set_outcome(
                task_key,
                attempt_status="execution_timeout",
                reason_code="executor_timeout",
                execution_result="timed_out",
                validation_result=None,
                metadata={"executor": result.executor, "exit_code": result.exit_code},
            )
        elif result.status == "failed":
            self._set_outcome(
                task_key,
                attempt_status="failed",
                reason_code="executor_failed",
                execution_result="failed",
                validation_result=None,
                metadata={"executor": result.executor, "exit_code": result.exit_code},
            )
        elif result.status == "blocked":
            self._set_outcome(
                task_key,
                attempt_status="blocked",
                reason_code="executor_blocked",
                execution_result="blocked",
                validation_result=None,
                metadata={"executor": result.executor, "exit_code": result.exit_code},
            )

    def classify_validator_result(self, task_key: str, result: ValidatorResult) -> None:
        if self._looks_timed_out(result.status, result.summary):
            self._set_outcome(
                task_key,
                attempt_status="execution_timeout",
                reason_code="validator_timeout",
                execution_result="completed",
                validation_result="timed_out",
                metadata={"validator": result.validator, "exit_code": result.exit_code},
            )
        elif result.status == "failed":
            self._set_outcome(
                task_key,
                attempt_status="validation_failed",
                reason_code="validator_failed",
                execution_result="completed",
                validation_result="failed",
                metadata={"validator": result.validator, "exit_code": result.exit_code},
            )
        elif result.status == "blocked":
            self._set_outcome(
                task_key,
                attempt_status="blocked",
                reason_code="validator_blocked",
                execution_result="completed",
                validation_result="blocked",
                metadata={"validator": result.validator, "exit_code": result.exit_code},
            )

    def wrap_executor(self, executor: Any) -> Any:
        if isinstance(executor, _LifecycleExecutorProxy):
            return executor
        return _LifecycleExecutorProxy(super().wrap_executor(executor), self)

    def wrap_validator(self, validator: Any) -> Any:
        if isinstance(validator, _LifecycleValidatorProxy):
            return validator
        return _LifecycleValidatorProxy(super().wrap_validator(validator), self)

    def update_task_status(
        self,
        task_key: str,
        status: str,
        *,
        message: str | None = None,
        source: str = "local_mirror",
        blocked_reason: str | None = None,
        expected_current_status: str | None = None,
    ) -> None:
        normalized = normalize_task_key(task_key)
        if status == "preparing":
            self.controls.assert_admission_allowed(normalized)
            return super().update_task_status(
                normalized,
                status,
                message=message,
                source=source,
                blocked_reason=blocked_reason,
                expected_current_status=expected_current_status,
            )

        if status in {"implementing", "validating"} and self.runtime_claim(normalized):
            state = self._heartbeat(normalized)
            reason_code = (
                "runtime_implementing" if status == "implementing" else "runtime_validating"
            )
            state.admission.transition(
                state.claim.attempt_id,
                owner_id=state.claim.owner_id,
                lease_token=state.claim.lease_token,
                attempt_status=status,
                reason_code=reason_code,
                message=message,
                metadata={"source": source},
            )
            return

        return super().update_task_status(
            normalized,
            status,
            message=message,
            source=source,
            blocked_reason=blocked_reason,
            expected_current_status=expected_current_status,
        )

    def _release(
        self,
        task_key: str,
        *,
        status: str,
        message: str | None,
        blocked_reason: str | None,
        expected_current_status: str | None,
    ) -> None:
        normalized = normalize_task_key(task_key)
        state = self._require_state(normalized)
        task = self.get_task(normalized)
        if task is None:
            raise KeyError(f"Task not found: {normalized}")
        if expected_current_status is not None and task.status != expected_current_status:
            raise ValueError(
                f"Task {normalized} status is {task.status!r}; expected {expected_current_status!r}"
            )

        pending = self._terminal_outcomes.pop(normalized, None)
        if status == "waiting_approval":
            outcome = _TerminalOutcome(
                "waiting_approval",
                "runtime_waiting_approval",
                "completed",
                "passed",
                {},
            )
        elif status == "completed":
            outcome = _TerminalOutcome(
                "completed", "runtime_completed", "completed", "passed", {}
            )
        elif status == "canceled":
            outcome = _TerminalOutcome(
                "canceled", "runtime_canceled", "canceled", None, {}
            )
        elif pending is not None:
            outcome = pending
        else:
            outcome = _TerminalOutcome(
                "blocked",
                "runtime_governance_blocked",
                "blocked",
                None,
                {},
            )

        self._stop_supervisor(state)
        try:
            state.admission.release(
                state.claim.attempt_id,
                owner_id=state.claim.owner_id,
                lease_token=state.claim.lease_token,
                attempt_status=outcome.attempt_status,
                task_status=status,
                reason_code=outcome.reason_code,
                execution_result=outcome.execution_result,
                validation_result=outcome.validation_result,
                metadata={
                    "message": message,
                    "blocked_reason": blocked_reason,
                    "runtime_lease_id": state.claim.lease_id,
                    **outcome.metadata,
                },
            )
        finally:
            with self._runtime_claims_lock:
                self._runtime_claims.pop(normalized, None)

        if status == "blocked" and (blocked_reason or message):
            canonical_path._LegacyTaskMirrorStore.update_task_status(
                self,
                normalized,
                "blocked",
                message=message,
                source=state.claim.owner_id,
                blocked_reason=blocked_reason or message,
            )


def install_lifecycle_runtime_path(
    *,
    dispatcher_module: ModuleType,
    approved_task_runner_module: ModuleType,
) -> None:
    """Make PR-6 lifecycle enforcement the final canonical store layer."""
    if getattr(canonical_path, "__lifecycle_runtime_installed__", False):
        return

    canonical_path.CanonicalRuntimeAdmissionStore = LifecycleRuntimeAdmissionStore

    def lifecycle_canonicalize_store(
        store: Any | None,
        db_path: str | Path | None,
    ) -> LifecycleRuntimeTaskStore:
        if isinstance(store, LifecycleRuntimeTaskStore):
            return store
        resolved_path = getattr(store, "db_path", None) if store is not None else db_path
        return LifecycleRuntimeTaskStore(resolved_path)

    def lifecycle_attempt_store_for_request(
        store: Any | None,
        request: Any,
    ) -> LifecycleRuntimeTaskStore:
        if isinstance(store, LifecycleRuntimeTaskStore):
            return store
        resolved_path = getattr(store, "db_path", None) if store is not None else request.db_path
        return LifecycleRuntimeTaskStore(resolved_path)

    canonical_path._canonicalize_store = lifecycle_canonicalize_store
    attempt_path._attempt_store_for_request = lifecycle_attempt_store_for_request
    canonical_path.__lifecycle_runtime_installed__ = True


__all__ = [
    "LifecycleRuntimeAdmissionStore",
    "LifecycleRuntimeTaskStore",
    "RuntimeKillRequested",
    "install_lifecycle_runtime_path",
]
