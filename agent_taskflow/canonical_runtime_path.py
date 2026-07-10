"""Canonical explicit-token runtime admission path.

The existing runtime implementations already persist all executor boundaries
through ``TaskMirrorStore``. This module supplies a claim-aware store adapter and
installs it at the two direct execution roots: ``Dispatcher`` and
``run_approved_task``. Delegating entrypoints such as queued handoff and the
scheduler therefore cross the same admission boundary without maintaining a
second ownership implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
import threading
from types import ModuleType
from typing import Any, Mapping
from uuid import uuid4

from agent_taskflow.canonical_runtime_schema import (
    CANONICAL_RUNTIME_ADMISSION_MIGRATION,
    migrate_canonical_runtime_admission,
)
from agent_taskflow.runtime_admission import (
    DEFAULT_LEASE_TTL_SECONDS,
    RuntimeAdmissionError,
    RuntimeAdmissionStore as _PR3RuntimeAdmissionStore,
    RuntimeClaim,
)
from agent_taskflow.store import TaskMirrorStore as _LegacyTaskMirrorStore
from agent_taskflow.tasks import normalize_task_key


class CanonicalRuntimeAdmissionStore(_PR3RuntimeAdmissionStore):
    """Runtime admission API whose initialization preserves PR-4 guards."""

    def init_db(self) -> None:
        migrate_canonical_runtime_admission(self.db_path)


@dataclass
class _ClaimState:
    claim: RuntimeClaim
    admission: CanonicalRuntimeAdmissionStore
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    heartbeat_error: BaseException | None = None


class CanonicalRuntimeTaskStore(_LegacyTaskMirrorStore):
    """Task mirror adapter carrying explicit runtime ownership credentials.

    ``update_task_status(..., 'preparing')`` becomes the single claim boundary.
    Subsequent executor and validator evidence is authenticated with the same
    in-memory owner/token pair. The raw token is never written to SQLite or task
    events.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        heartbeat_interval_seconds: float | None = None,
    ) -> None:
        super().__init__(db_path)
        ttl = int(lease_ttl_seconds)
        if ttl < 1:
            raise ValueError("lease_ttl_seconds must be >= 1")
        interval = (
            max(0.2, min(60.0, ttl / 3.0))
            if heartbeat_interval_seconds is None
            else float(heartbeat_interval_seconds)
        )
        if interval <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        self.lease_ttl_seconds = ttl
        self.heartbeat_interval_seconds = interval
        self._runtime_claims: dict[str, _ClaimState] = {}
        self._runtime_claims_lock = threading.RLock()

    def init_db(self) -> None:
        migrate_canonical_runtime_admission(self.db_path)

    def _state_for(self, task_key: str) -> _ClaimState | None:
        normalized = normalize_task_key(task_key)
        with self._runtime_claims_lock:
            return self._runtime_claims.get(normalized)

    def _require_state(self, task_key: str) -> _ClaimState:
        state = self._state_for(task_key)
        if state is None:
            raise RuntimeAdmissionError(
                f"Task {normalize_task_key(task_key)} has no canonical runtime claim"
            )
        if state.heartbeat_error is not None:
            raise RuntimeAdmissionError(
                "Canonical runtime heartbeat failed: "
                f"{state.heartbeat_error.__class__.__name__}: "
                f"{state.heartbeat_error}"
            )
        return state

    def _heartbeat_loop(self, task_key: str, state: _ClaimState) -> None:
        while not state.stop_event.wait(self.heartbeat_interval_seconds):
            try:
                state.admission.heartbeat(
                    state.claim.attempt_id,
                    owner_id=state.claim.owner_id,
                    lease_token=state.claim.lease_token,
                    ttl_seconds=self.lease_ttl_seconds,
                )
            except BaseException as exc:  # pragma: no cover - defensive daemon path.
                state.heartbeat_error = exc
                state.stop_event.set()
                return

    def _start_supervisor(self, task_key: str, state: _ClaimState) -> None:
        thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(task_key, state),
            name=f"runtime-heartbeat-{normalize_task_key(task_key)}",
            daemon=True,
        )
        state.thread = thread
        thread.start()

    @staticmethod
    def _stop_supervisor(state: _ClaimState) -> None:
        state.stop_event.set()
        thread = state.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _heartbeat(self, task_key: str) -> _ClaimState:
        state = self._require_state(task_key)
        try:
            state.admission.heartbeat(
                state.claim.attempt_id,
                owner_id=state.claim.owner_id,
                lease_token=state.claim.lease_token,
                ttl_seconds=self.lease_ttl_seconds,
            )
        except BaseException as exc:
            state.heartbeat_error = exc
            raise
        return state

    def _claim(
        self,
        task_key: str,
        *,
        source: str,
        message: str | None,
        expected_current_status: str | None,
    ) -> None:
        normalized = normalize_task_key(task_key)
        self.init_db()
        with self._runtime_claims_lock:
            if normalized in self._runtime_claims:
                raise RuntimeAdmissionError(
                    f"Task {normalized} already has a claim in this runtime store"
                )

        task = self.get_task(normalized)
        if task is None:
            raise KeyError(f"Task not found: {normalized}")
        if expected_current_status is not None and task.status != expected_current_status:
            raise ValueError(
                f"Task {normalized} status is {task.status!r}; "
                f"expected {expected_current_status!r}"
            )
        worktree = self.get_task_worktree(normalized)
        owner_id = f"{source}:{uuid4().hex}"
        admission = CanonicalRuntimeAdmissionStore(self.db_path)
        claim = admission.claim(
            normalized,
            owner_id=owner_id,
            ttl_seconds=self.lease_ttl_seconds,
            executor=task.executor,
            model=task.model,
            worktree_path=worktree.worktree_path if worktree is not None else None,
            artifact_root=task.artifact_dir,
            reason_code="canonical_runtime_pickup_claimed",
            metadata={
                "entrypoint_source": source,
                "message": message,
                "canonical_migration": CANONICAL_RUNTIME_ADMISSION_MIGRATION,
            },
        )
        state = _ClaimState(claim=claim, admission=admission)
        with self._runtime_claims_lock:
            self._runtime_claims[normalized] = state
        self._start_supervisor(normalized, state)

    @staticmethod
    def _terminal_attempt_status(task_status: str) -> tuple[str, str | None, str | None]:
        if task_status == "waiting_approval":
            return "waiting_approval", "completed", "passed"
        if task_status == "completed":
            return "completed", "completed", "passed"
        if task_status == "canceled":
            return "canceled", "canceled", None
        return "blocked", "blocked", None

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
                f"Task {normalized} status is {task.status!r}; "
                f"expected {expected_current_status!r}"
            )

        self._stop_supervisor(state)
        attempt_status, execution_result, validation_result = (
            self._terminal_attempt_status(status)
        )
        reason_code = {
            "waiting_approval": "canonical_runtime_waiting_approval",
            "completed": "canonical_runtime_completed",
            "canceled": "canonical_runtime_canceled",
            "blocked": "canonical_runtime_blocked",
        }.get(status, "canonical_runtime_released")
        try:
            state.admission.release(
                state.claim.attempt_id,
                owner_id=state.claim.owner_id,
                lease_token=state.claim.lease_token,
                attempt_status=attempt_status,
                task_status=status,
                reason_code=reason_code,
                execution_result=execution_result,
                validation_result=validation_result,
                metadata={
                    "message": message,
                    "blocked_reason": blocked_reason,
                    "runtime_lease_id": state.claim.lease_id,
                },
            )
        finally:
            with self._runtime_claims_lock:
                self._runtime_claims.pop(normalized, None)

        if status == "blocked" and (blocked_reason or message):
            super().update_task_status(
                normalized,
                "blocked",
                message=message,
                source=state.claim.owner_id,
                blocked_reason=blocked_reason or message,
            )

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
            self._claim(
                normalized,
                source=source,
                message=message,
                expected_current_status=expected_current_status,
            )
            return

        state = self._state_for(normalized)
        if status in {"blocked", "waiting_approval", "canceled", "completed"}:
            if state is None:
                super().update_task_status(
                    normalized,
                    status,
                    message=message,
                    source=source,
                    blocked_reason=blocked_reason,
                    expected_current_status=expected_current_status,
                )
                return
            self._release(
                normalized,
                status=status,
                message=message,
                blocked_reason=blocked_reason,
                expected_current_status=expected_current_status,
            )
            return

        if status in {"implementing", "validating"}:
            self._heartbeat(normalized)
        elif state is not None:
            self._heartbeat(normalized)

        super().update_task_status(
            normalized,
            status,
            message=message,
            source=source,
            blocked_reason=blocked_reason,
            expected_current_status=expected_current_status,
        )

    def create_executor_run(
        self,
        task_key: str,
        executor: str,
        *,
        model: str | None = None,
        prompt_path: str | Path | None = None,
    ) -> str:
        normalized = normalize_task_key(task_key)
        state = self._heartbeat(normalized)
        state.admission.assert_executor_start_allowed(normalized)
        run_id = f"run-{uuid4().hex}"
        self.record_task_event(
            normalized,
            "note",
            "canonical_runtime_admission",
            message=f"Executor {executor} started",
            payload={
                "kind": "executor_run_started",
                "run_id": run_id,
                "executor": executor,
                "model": model,
                "prompt_path": str(prompt_path) if prompt_path else None,
                "runtime_attempt_id": state.claim.attempt_id,
                "runtime_lease_id": state.claim.lease_id,
                "runtime_owner_id": state.claim.owner_id,
            },
        )
        return run_id

    def finish_executor_run(
        self,
        task_key: str,
        run_id: str,
        *,
        executor: str,
        status: str,
        exit_code: int | None = None,
        summary: str | None = None,
        log_path: str | Path | None = None,
        artifacts: dict[str, str | Path] | None = None,
    ) -> None:
        self._heartbeat(task_key)
        super().finish_executor_run(
            task_key,
            run_id,
            executor=executor,
            status=status,
            exit_code=exit_code,
            summary=summary,
            log_path=log_path,
            artifacts=artifacts,
        )
        self._heartbeat(task_key)

    def record_validation_result(
        self,
        task_key: str,
        validator: str,
        *,
        status: str,
        exit_code: int | None = None,
        summary: str | None = None,
        log_path: str | Path | None = None,
        artifacts: dict[str, str | Path] | None = None,
    ) -> None:
        self._heartbeat(task_key)
        super().record_validation_result(
            task_key,
            validator,
            status=status,
            exit_code=exit_code,
            summary=summary,
            log_path=log_path,
            artifacts=artifacts,
        )
        self._heartbeat(task_key)

    def runtime_claim(self, task_key: str) -> RuntimeClaim | None:
        state = self._state_for(task_key)
        return state.claim if state is not None else None

    def shutdown_runtime_supervisors(self) -> None:
        with self._runtime_claims_lock:
            states = list(self._runtime_claims.values())
        for state in states:
            self._stop_supervisor(state)


def _canonicalize_store(
    store: Any | None,
    db_path: str | Path | None,
) -> CanonicalRuntimeTaskStore:
    if isinstance(store, CanonicalRuntimeTaskStore):
        return store
    resolved_path = getattr(store, "db_path", None) if store is not None else db_path
    return CanonicalRuntimeTaskStore(resolved_path)


def install_canonical_runtime_path(
    *,
    dispatcher_module: ModuleType,
    approved_task_runner_module: ModuleType,
    runtime_admission_module: ModuleType,
) -> None:
    """Install one claim-aware path at every direct executor entrypoint."""
    if getattr(approved_task_runner_module.run_approved_task, "__canonical_runtime__", False):
        return

    # Keep the PR-3 RuntimeAdmissionStore import stable for compatibility tests
    # and read-only tooling. Canonical entrypoints instantiate the explicit
    # subclass directly; they do not globally replace the legacy API symbol.
    _ = runtime_admission_module

    original_run_approved_task = approved_task_runner_module.run_approved_task

    @wraps(original_run_approved_task)
    def canonical_run_approved_task(
        request: Any,
        *,
        store: Any | None = None,
        executor_registry: Mapping[str, Any] | None = None,
        validator_registry: Mapping[str, Any] | None = None,
        preflight_runner: Any = approved_task_runner_module.run_preflight,
    ) -> Any:
        canonical_store = _canonicalize_store(store, request.db_path)
        return original_run_approved_task(
            request,
            store=canonical_store,
            executor_registry=executor_registry,
            validator_registry=validator_registry,
            preflight_runner=preflight_runner,
        )

    canonical_run_approved_task.__canonical_runtime__ = True
    canonical_run_approved_task.__canonical_original__ = original_run_approved_task
    approved_task_runner_module.run_approved_task = canonical_run_approved_task

    original_dispatcher = dispatcher_module.Dispatcher

    class CanonicalDispatcher(original_dispatcher):
        """Dispatcher whose store always carries explicit runtime ownership."""

        __canonical_runtime__ = True

        def __init__(
            self,
            store: Any | None = None,
            *,
            db_path: str | Path | None = None,
            **kwargs: Any,
        ) -> None:
            if store is not None and db_path is not None:
                raise ValueError("Provide either store or db_path, not both")
            canonical_store = _canonicalize_store(store, db_path)
            super().__init__(store=canonical_store, **kwargs)

    CanonicalDispatcher.__name__ = "Dispatcher"
    CanonicalDispatcher.__qualname__ = "Dispatcher"
    CanonicalDispatcher.__module__ = dispatcher_module.__name__
    dispatcher_module.Dispatcher = CanonicalDispatcher


__all__ = [
    "CANONICAL_RUNTIME_ADMISSION_MIGRATION",
    "CanonicalRuntimeAdmissionStore",
    "CanonicalRuntimeTaskStore",
    "install_canonical_runtime_path",
    "migrate_canonical_runtime_admission",
]
