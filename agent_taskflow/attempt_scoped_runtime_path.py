"""Install Attempt-scoped resources on the canonical runtime admission path."""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import wraps
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

import agent_taskflow.canonical_runtime_path as canonical_path
from agent_taskflow.attempt_resources import (
    AttemptResourceError,
    AttemptResourceHandle,
    AttemptResourceManager,
    AttemptResourceRecord,
)
from agent_taskflow.attempt_resources_schema import migrate_attempt_resources
from agent_taskflow.runtime_admission import RuntimeAdmissionError


@dataclass(frozen=True)
class _AttemptResourceConfig:
    base_branch: str = "main"
    worktree_root: Path | None = None
    artifact_base_root: Path | None = None


@dataclass
class _AttemptResourceState:
    handle: AttemptResourceHandle


class AttemptScopedRuntimeTaskStore(canonical_path.CanonicalRuntimeTaskStore):
    """Canonical token store with immutable resources for each Attempt.

    Resource configuration is staged before the wrapped runtime starts, but the
    claim is intentionally delayed until the runtime's canonical transition to
    ``preparing``. This preserves selection, effective-task persistence, preflight,
    and legacy governance ordering while ensuring no executor boundary can run
    before Attempt resources exist.

    Existing runners retain local ``TaskRecord`` and ``TaskWorktreeRecord``
    instances across the preparing transition. Those dataclasses are frozen, so
    this adapter deliberately updates the already-returned instances with
    ``object.__setattr__`` when the Attempt is activated. This keeps the old public
    runner API while making all later mission-contract, executor, validator, and
    artifact operations use the Attempt-scoped paths.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        lease_ttl_seconds: int = canonical_path.DEFAULT_LEASE_TTL_SECONDS,
        heartbeat_interval_seconds: float | None = None,
    ) -> None:
        super().__init__(
            db_path,
            lease_ttl_seconds=lease_ttl_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        )
        self._attempt_resources = AttemptResourceManager(self.db_path)
        self._attempt_resource_states: dict[str, _AttemptResourceState] = {}
        self._attempt_resource_configs: dict[str, _AttemptResourceConfig] = {}
        self._task_objects: dict[str, Any] = {}
        self._worktree_objects: dict[str, Any] = {}

    def init_db(self) -> None:
        migrate_attempt_resources(self.db_path)

    def get_task(self, task_key: str):
        task = super().get_task(task_key)
        if task is not None:
            self._task_objects[task.task_key] = task
        return task

    def upsert_task(self, record: Any, *, preserve_existing_status: bool = True) -> None:
        super().upsert_task(record, preserve_existing_status=preserve_existing_status)
        self._task_objects[record.task_key] = record

    def get_task_worktree(self, task_key: str):
        worktree = super().get_task_worktree(task_key)
        if worktree is not None:
            self._worktree_objects[worktree.task_key] = worktree
        return worktree

    def upsert_task_worktree(self, record: Any) -> None:
        cached = self._worktree_objects.get(record.task_key)
        if cached is not None and cached is not record:
            for field_name in (
                "repo_path",
                "worktree_path",
                "branch",
                "base_branch",
                "base_sha",
                "status",
            ):
                object.__setattr__(cached, field_name, getattr(record, field_name))
            record = cached
        super().upsert_task_worktree(record)
        self._worktree_objects[record.task_key] = record

    def configure_attempt_resources(
        self,
        task_key: str,
        *,
        base_branch: str = "main",
        worktree_root: str | Path | None = None,
        artifact_base_root: str | Path | None = None,
    ) -> None:
        """Stage immutable resource inputs without claiming the task yet."""
        normalized = canonical_path.normalize_task_key(task_key)
        branch = base_branch.strip()
        if not branch:
            raise ValueError("base_branch must not be empty")
        self._attempt_resource_configs[normalized] = _AttemptResourceConfig(
            base_branch=branch,
            worktree_root=(Path(worktree_root) if worktree_root is not None else None),
            artifact_base_root=(
                Path(artifact_base_root) if artifact_base_root is not None else None
            ),
        )

    def attempt_resource(self, task_key: str) -> AttemptResourceRecord | None:
        normalized = canonical_path.normalize_task_key(task_key)
        state = self._attempt_resource_states.get(normalized)
        if state is not None:
            return self._attempt_resources.get(state.handle.record.attempt_id) or state.handle.record
        claim = self.runtime_claim(normalized)
        return self._attempt_resources.get(claim.attempt_id) if claim is not None else None

    def _bind_task_artifact_root(self, record: AttemptResourceRecord) -> None:
        cached = self._task_objects.get(record.task_key)
        if cached is not None:
            object.__setattr__(cached, "artifact_dir", record.artifact_root)

    def _bind_workspace_result(self, result: Any) -> None:
        cached = self._worktree_objects.get(result.task_key)
        if cached is None:
            return
        object.__setattr__(cached, "repo_path", result.repo_path)
        object.__setattr__(cached, "worktree_path", result.worktree_path)
        object.__setattr__(cached, "branch", result.branch)
        object.__setattr__(cached, "base_branch", result.base_branch)
        object.__setattr__(cached, "base_sha", result.base_sha)
        object.__setattr__(cached, "status", "active")

    def preclaim_runtime(
        self,
        task_key: str,
        *,
        source: str,
        message: str | None = None,
        base_branch: str = "main",
        worktree_root: str | Path | None = None,
        artifact_base_root: str | Path | None = None,
    ) -> AttemptResourceRecord:
        """Claim once and allocate paths, lock, PID, manifest, and input snapshot."""
        normalized = canonical_path.normalize_task_key(task_key)
        existing = self._attempt_resource_states.get(normalized)
        if existing is not None:
            return self._attempt_resources.get(existing.handle.record.attempt_id) or existing.handle.record

        task = super().get_task(normalized)
        if task is None:
            raise KeyError(f"Task not found: {normalized}")
        original_status = task.status
        super()._claim(
            normalized,
            source=source,
            message=message,
            expected_current_status=original_status,
        )
        claim_state = self._state_for(normalized)
        assert claim_state is not None
        try:
            handle = self._attempt_resources.allocate(
                claim_state.claim,
                task,
                base_branch=base_branch,
                worktree_root=worktree_root,
                artifact_base_root=artifact_base_root,
            )
        except BaseException as exc:
            self._stop_supervisor(claim_state)
            try:
                claim_state.admission.release(
                    claim_state.claim.attempt_id,
                    owner_id=claim_state.claim.owner_id,
                    lease_token=claim_state.claim.lease_token,
                    attempt_status="execution_aborted",
                    task_status="blocked",
                    reason_code="attempt_resource_allocation_failed",
                    execution_result="resource_allocation_failed",
                    metadata={"error": f"{exc.__class__.__name__}: {exc}"},
                )
            finally:
                with self._runtime_claims_lock:
                    self._runtime_claims.pop(normalized, None)
            raise

        self._attempt_resource_states[normalized] = _AttemptResourceState(handle=handle)
        self._bind_task_artifact_root(handle.record)
        return handle.record

    def prepare_attempt_workspace(self, task_key: str):
        normalized = canonical_path.normalize_task_key(task_key)
        state = self._attempt_resource_states.get(normalized)
        if state is None:
            raise RuntimeAdmissionError(
                f"Task {normalized} has no Attempt-scoped resource allocation"
            )
        result = self._attempt_resources.provision_workspace(state.handle, store=self)
        if result.ok:
            self._bind_task_artifact_root(state.handle.record)
            self._bind_workspace_result(result)
        return result

    def _heartbeat(self, task_key: str):
        state = super()._heartbeat(task_key)
        self._attempt_resources.heartbeat(state.claim.attempt_id)
        return state

    def _configuration_for_preparing(self, task_key: str) -> _AttemptResourceConfig:
        configured = self._attempt_resource_configs.pop(task_key, None)
        if configured is not None:
            return configured
        task = super().get_task(task_key)
        if task is None:
            raise KeyError(f"Task not found: {task_key}")
        previous = super().get_task_worktree(task_key)
        return _AttemptResourceConfig(
            base_branch=previous.base_branch if previous is not None else "main",
            worktree_root=(previous.worktree_path.parent if previous is not None else None),
            artifact_base_root=task.artifact_dir,
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
        normalized = canonical_path.normalize_task_key(task_key)
        resource_state = self._attempt_resource_states.get(normalized)

        if status == "preparing":
            if resource_state is None:
                config = self._configuration_for_preparing(normalized)
                self.preclaim_runtime(
                    normalized,
                    source=source,
                    message=message,
                    base_branch=config.base_branch,
                    worktree_root=config.worktree_root,
                    artifact_base_root=config.artifact_base_root,
                )
                resource_state = self._attempt_resource_states[normalized]
                workspace = self.prepare_attempt_workspace(normalized)
                if not workspace.ok:
                    raise AttemptResourceError(workspace.summary)
            self._heartbeat(normalized)
            return

        terminal = status in {"blocked", "waiting_approval", "canceled", "completed"}
        if terminal and resource_state is not None:
            handle = resource_state.handle
            super().update_task_status(
                normalized,
                status,
                message=message,
                source=source,
                blocked_reason=blocked_reason,
                expected_current_status=expected_current_status,
            )
            self._attempt_resource_states.pop(normalized, None)
            self._attempt_resources.release(handle, reason=f"task_status:{status}")

            latest = super().get_task(normalized)
            if latest is not None and latest.artifact_dir != handle.record.artifact_base_root:
                restored = replace(latest, artifact_dir=handle.record.artifact_base_root)
                super().upsert_task(restored, preserve_existing_status=True)
                cached = self._task_objects.get(normalized)
                if cached is not None:
                    object.__setattr__(cached, "artifact_dir", handle.record.artifact_base_root)
            return

        super().update_task_status(
            normalized,
            status,
            message=message,
            source=source,
            blocked_reason=blocked_reason,
            expected_current_status=expected_current_status,
        )

    def shutdown_runtime_supervisors(self) -> None:
        super().shutdown_runtime_supervisors()
        # Do not release locks or PID evidence here. An unowned process shutdown is
        # crash evidence; the deterministic reaper decides whether it is stale.


def _attempt_store_for_request(store: Any | None, request: Any) -> AttemptScopedRuntimeTaskStore:
    if isinstance(store, AttemptScopedRuntimeTaskStore):
        return store
    resolved_path = getattr(store, "db_path", None) if store is not None else request.db_path
    return AttemptScopedRuntimeTaskStore(resolved_path)


def install_attempt_scoped_runtime_path(
    *,
    dispatcher_module: ModuleType,
    approved_task_runner_module: ModuleType,
) -> None:
    """Layer Attempt resources over the already-installed canonical token path."""
    if getattr(approved_task_runner_module.run_approved_task, "__attempt_scoped_runtime__", False):
        return

    canonical_path.CanonicalRuntimeTaskStore = AttemptScopedRuntimeTaskStore

    original_prepare = approved_task_runner_module.prepare_task_workspace

    def attempt_prepare_task_workspace(request: Any, *, store: Any | None = None):
        if isinstance(store, AttemptScopedRuntimeTaskStore) and store.runtime_claim(request.task_key):
            return store.prepare_attempt_workspace(request.task_key)
        return original_prepare(request, store=store)

    approved_task_runner_module.prepare_task_workspace = attempt_prepare_task_workspace

    canonical_run = approved_task_runner_module.run_approved_task

    @wraps(canonical_run)
    def attempt_run_approved_task(
        request: Any,
        *,
        store: Any | None = None,
        executor_registry: Mapping[str, Any] | None = None,
        validator_registry: Mapping[str, Any] | None = None,
        preflight_runner: Any = approved_task_runner_module.run_preflight,
    ) -> Any:
        attempt_store = _attempt_store_for_request(store, request)
        if not request.dry_run and request.confirm_approved_task:
            try:
                task = approved_task_runner_module._load_task(attempt_store, request)
            except (OSError, ValueError):
                task = None
            if task is not None and task.status == approved_task_runner_module.TASK_QUEUE_STATUS:
                artifact_base = approved_task_runner_module._effective_artifact_dir(task, request)
                if artifact_base is not None:
                    attempt_store.configure_attempt_resources(
                        task.task_key,
                        base_branch=request.base_branch,
                        worktree_root=request.worktree_root,
                        artifact_base_root=artifact_base,
                    )
        return canonical_run(
            request,
            store=attempt_store,
            executor_registry=executor_registry,
            validator_registry=validator_registry,
            preflight_runner=preflight_runner,
        )

    attempt_run_approved_task.__attempt_scoped_runtime__ = True
    approved_task_runner_module.run_approved_task = attempt_run_approved_task

    canonical_dispatcher = dispatcher_module.Dispatcher

    class AttemptScopedDispatcher(canonical_dispatcher):
        """Dispatcher that stages Attempt resources before canonical preparing."""

        __attempt_scoped_runtime__ = True

        def dispatch_task(
            self,
            task_key: str,
            *,
            executor_name: str | None = None,
            model: str | None = None,
            dry_run: bool = False,
        ):
            if not dry_run:
                try:
                    task = self.store.get_task(task_key)
                    previous = (
                        self.store.get_task_worktree(task.task_key)
                        if task is not None
                        else None
                    )
                except (OSError, ValueError):
                    task = None
                    previous = None
                if task is not None and task.status in {"queued", "blocked"}:
                    self.store.configure_attempt_resources(
                        task.task_key,
                        base_branch=previous.base_branch if previous is not None else "main",
                        worktree_root=(
                            previous.worktree_path.parent if previous is not None else None
                        ),
                        artifact_base_root=task.artifact_dir,
                    )
                elif (
                    task is not None
                    and task.status == "preparing"
                    and self.store.runtime_claim(task.task_key) is None
                ):
                    reason = (
                        "Preparing task cannot be resumed without its in-memory owner token; "
                        "run the stale lease/resource reaper before retry"
                    )
                    return dispatcher_module.DispatcherResult(
                        task_key=task.task_key,
                        status="blocked",
                        summary=reason,
                        blocked_reason=reason,
                    )
            try:
                return super().dispatch_task(
                    task_key,
                    executor_name=executor_name,
                    model=model,
                    dry_run=dry_run,
                )
            except (AttemptResourceError, RuntimeAdmissionError, OSError) as exc:
                normalized = canonical_path.normalize_task_key(task_key)
                reason = f"Attempt resource preparation failed: {exc}"
                if self.store.runtime_claim(normalized) is not None:
                    self.store.update_task_status(
                        normalized,
                        "blocked",
                        source="dispatcher",
                        message=reason,
                        blocked_reason=reason,
                    )
                return dispatcher_module.DispatcherResult(
                    task_key=normalized,
                    status="blocked",
                    summary=reason,
                    blocked_reason=reason,
                )

    AttemptScopedDispatcher.__name__ = "Dispatcher"
    AttemptScopedDispatcher.__qualname__ = "Dispatcher"
    AttemptScopedDispatcher.__module__ = dispatcher_module.__name__
    dispatcher_module.Dispatcher = AttemptScopedDispatcher


__all__ = [
    "AttemptScopedRuntimeTaskStore",
    "install_attempt_scoped_runtime_path",
]
