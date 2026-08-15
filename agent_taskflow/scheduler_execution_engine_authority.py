"""Authoritative ExecutionEngine binding for confirmed scheduler execution.

The scheduler's existing proposal, confirmation, verifier, and runtime-handoff
chain remains intact.  At the one execution callback, this module constructs a
Level 2 ``ExecutionEngineRequest`` and invokes exactly one engine.  It never
falls back to the legacy scheduler runner after an engine error.

The approved-task runner may remain behind the default engine adapter as an
implementation detail.  It is not a competing scheduler authority: its return
value is accepted only through ``ExecutionEngineResult`` and a successful Level
2 result must be bound to the canonical Attempt created by that invocation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agent_taskflow.execution_engine_approved_task_adapter import (
    ApprovedTaskRunnerExecutionEngineAdapter,
)
from agent_taskflow.execution_engine_contract import (
    EXECUTION_STATUS_BLOCKED,
    ExecutionEngine,
    ExecutionEngineRequest,
    ExecutionEngineResult,
    ExecutionEngineSafety,
    to_json_dict,
)
from agent_taskflow.level2_execution_authority import (
    Level2ExecutionAuthorityError,
    ensure_level2_task_identity,
    list_canonical_attempt_ids,
    verify_canonical_attempt,
)
from agent_taskflow.scheduler_execution_engine_opt_in import (
    build_scheduler_tick_execution_engine_request,
)
from agent_taskflow.scheduler_execution_engine_shadow_compare import (
    SchedulerExecutionEngineShadowCompareInput,
    compare_scheduler_tick_to_engine_request,
    scheduler_execution_engine_shadow_compare_to_json_dict,
)


SCHEDULER_EXECUTION_ENGINE_AUTHORITY_SCHEMA_VERSION = (
    "scheduler_execution_engine_authority.v1"
)
SCHEDULER_EXECUTION_ENGINE_AUTHORITY_SOURCE = (
    "scheduler_execution_engine_authority"
)
EFFECTIVE_AUTHORITY_EXECUTION_ENGINE = "execution_engine"


@dataclass(frozen=True)
class DirectRuntimeHandoffAuthorityRequest:
    """Engine-authority inputs for a canonical caller outside the tick.

    ``SchedulerExecutionEngineAuthority`` reads its scheduler request purely by
    attribute. A caller that reaches the same runtime handoff without a
    scheduler tick — the one-task automation path, and local golden-path
    smokes — supplies this typed value instead of an ad-hoc namespace, so the
    fields the request builder consumes stay explicit and validated.
    """

    repo: str
    db_path: Path
    local_repo_path: Path
    artifact_root: Path
    executor: str | None = None
    model: str | None = None
    provider: str | None = None
    tools: tuple[str, ...] = ()
    pi_bin: str | None = None
    command: tuple[str, ...] | None = None
    validators: tuple[str, ...] = ()
    worktree_root: Path | None = None
    base_branch: str = "main"
    approved_task_preflight: bool = False
    operator: str | None = None
    operator_note: str | None = None
    use_execution_engine: bool = True

    def __post_init__(self) -> None:
        for name in ("db_path", "local_repo_path", "artifact_root"):
            value = Path(getattr(self, name)).expanduser()
            if not value.is_absolute():
                raise ValueError(f"{name} must be absolute: {value}")
            object.__setattr__(self, name, value)
        if self.worktree_root is not None:
            object.__setattr__(self, "worktree_root", Path(self.worktree_root))
        object.__setattr__(self, "tools", tuple(self.tools or ()))
        object.__setattr__(self, "validators", tuple(self.validators or ()))
        base_branch = str(self.base_branch or "").strip() or "main"
        object.__setattr__(self, "base_branch", base_branch)


class SchedulerExecutionEngineAuthority:
    """Stateful one-tick bridge from runtime handoff to one engine call."""

    def __init__(
        self,
        scheduler_request: Any,
        *,
        engine: ExecutionEngine | None = None,
        approved_task_runner_fn: Callable[..., Any] | None = None,
    ) -> None:
        self.scheduler_request = scheduler_request
        self.request: ExecutionEngineRequest | None = None
        self.result: ExecutionEngineResult | None = None
        self.error: str | None = None
        self.invocation_count = 0
        self.legacy_scheduler_authority_invoked = False
        self._attempt_ids_before: set[str] | None = None

        if engine is not None:
            self.engine = engine
        elif approved_task_runner_fn is None:
            self.engine = ApprovedTaskRunnerExecutionEngineAdapter()
        else:
            self.engine = ApprovedTaskRunnerExecutionEngineAdapter(
                approved_task_runner=self._runner_delegate(
                    approved_task_runner_fn
                )
            )

    @staticmethod
    def _runner_delegate(
        runner: Callable[..., Any],
    ) -> Callable[..., Any]:
        """Adapt the historical injected test hook below engine authority.

        ``store`` is the canonical runtime store the engine reserved the
        Attempt through. An injected runner that persists runtime evidence must
        use it, otherwise its executor start carries no runtime claim.
        """

        def delegate(request: Any, *, store: Any = None) -> Any:
            return runner(
                task_key=request.task_key,
                approved_task_request=request,
                db_path=request.db_path,
                artifact_root=request.artifact_root,
                store=store,
            )

        return delegate

    def execute_from_runtime_handoff(self, **runtime: Any) -> dict[str, Any]:
        """Invoke the engine once and return the runtime helper's result shape."""

        task_key = str(runtime.get("task_key") or "").strip()
        if not task_key:
            return self._blocked_runner_payload(
                "runtime handoff did not provide task_key"
            )
        if self.invocation_count:
            return self._blocked_runner_payload(
                "ExecutionEngine authority may be invoked only once per tick"
            )

        request = build_scheduler_tick_execution_engine_request(
            self.scheduler_request,
            task_key=task_key,
        )
        handoff = runtime.get("handoff")
        handoff_view = handoff if isinstance(handoff, Mapping) else {}
        runtime_handoff_path = self._optional_path(
            handoff_view.get("handoff_artifact_path")
        )
        verifier_report_path = self._optional_path(
            handoff_view.get("verifier_report_artifact_path")
        )
        metadata = {
            **dict(request.metadata),
            "runtime_execution_id": runtime.get("runtime_execution_id"),
            "handoff_id": runtime.get("handoff_id"),
        }
        request = replace(
            request,
            runtime_handoff_path=runtime_handoff_path,
            verifier_report_path=verifier_report_path,
            metadata=metadata,
        )
        self.request = request
        try:
            if request.lifecycle_db_path is None:
                raise Level2ExecutionAuthorityError(
                    "Level 2 authority requires lifecycle_db_path"
                )
            ensure_level2_task_identity(
                request.lifecycle_db_path,
                task_key,
            )
            self._attempt_ids_before = list_canonical_attempt_ids(
                request.lifecycle_db_path,
                task_key,
            )
        except Level2ExecutionAuthorityError as exc:
            self.error = str(exc)
            self.result = self._blocked_result(task_key, self.error)
            return self._result_to_runner_payload(self.result)
        self.invocation_count += 1

        try:
            result = self.engine.execute(request)
        except Exception as exc:  # noqa: BLE001 - deterministic fail-closed path.
            self.error = f"{exc.__class__.__name__}: {exc}"
            self.result = self._blocked_result(task_key, self.error)
            return self._result_to_runner_payload(self.result)

        if not isinstance(result, ExecutionEngineResult):
            self.error = (
                "engine returned a non-ExecutionEngineResult value: "
                f"{type(result).__name__}"
            )
            self.result = self._blocked_result(task_key, self.error)
            return self._result_to_runner_payload(self.result)
        if result.task_key != task_key:
            self.error = (
                f"engine result task_key mismatch: {result.task_key!r} != "
                f"{task_key!r}"
            )
            self.result = self._blocked_result(task_key, self.error)
            return self._result_to_runner_payload(self.result)
        if result.ok and not self._canonical_attempt_bound(result):
            self.error = (
                "successful Level 2 engine result is missing canonical Attempt "
                "binding"
            )
            self.result = self._blocked_result(task_key, self.error)
            return self._result_to_runner_payload(self.result)

        if result.ok:
            try:
                attempt_id = str(result.metadata["canonical_attempt_id"])
                assert request.lifecycle_db_path is not None
                verification = verify_canonical_attempt(
                    db_path=request.lifecycle_db_path,
                    task_key=task_key,
                    attempt_id=attempt_id,
                    preexisting_attempt_ids=self._attempt_ids_before,
                )
                after_ids = list_canonical_attempt_ids(
                    request.lifecycle_db_path,
                    task_key,
                )
                new_ids = after_ids - (self._attempt_ids_before or set())
                if new_ids != {attempt_id}:
                    raise Level2ExecutionAuthorityError(
                        "authoritative execution must create exactly the canonical "
                        f"Attempt it returns; created={sorted(new_ids)!r}, "
                        f"returned={attempt_id!r}"
                    )
            except (KeyError, Level2ExecutionAuthorityError) as exc:
                self.error = f"canonical Attempt store verification failed: {exc}"
                self.result = self._blocked_result(task_key, self.error)
                return self._result_to_runner_payload(self.result)
            result = replace(
                result,
                metadata={
                    **dict(result.metadata),
                    "canonical_attempt_bound": True,
                    "canonical_attempt_id": verification.attempt.attempt_id,
                    "canonical_attempt_store_verified": True,
                    "canonical_attempt_task_verified": True,
                    "canonical_attempt_terminal_verified": True,
                    "canonical_attempt_execution_association_verified": True,
                    "canonical_attempt_downstream_valid": True,
                },
            )

        self.result = result
        return self._result_to_runner_payload(result)

    def evidence(self, tick_payload: Mapping[str, Any]) -> dict[str, Any]:
        """Return JSON-safe authority evidence without changing the decision."""

        request_json = (
            to_json_dict(self.request) if self.request is not None else None
        )
        result_json = to_json_dict(self.result) if self.result is not None else None
        compare_json = None
        if self.request is not None:
            compare_json = scheduler_execution_engine_shadow_compare_to_json_dict(
                compare_scheduler_tick_to_engine_request(
                    SchedulerExecutionEngineShadowCompareInput(
                        legacy_scheduler_tick=tick_payload,
                        engine_request=self.request,
                    )
                )
            )
        result = self.result
        canonical_attempt_id = (
            result.metadata.get("canonical_attempt_id")
            if result is not None
            else None
        )
        return {
            "schema_version": SCHEDULER_EXECUTION_ENGINE_AUTHORITY_SCHEMA_VERSION,
            "source": SCHEDULER_EXECUTION_ENGINE_AUTHORITY_SOURCE,
            "configured": True,
            "effective_authority": EFFECTIVE_AUTHORITY_EXECUTION_ENGINE,
            "engine_authority": True,
            "engine_result_accepted_as_authority": bool(result and result.ok),
            "legacy_fallback_allowed": False,
            "legacy_scheduler_authority_invoked": False,
            "use_execution_engine_compat_flag": bool(
                self.scheduler_request.use_execution_engine
            ),
            "executed": self.invocation_count == 1,
            "engine_invocation_count": self.invocation_count,
            "engine": self.engine.__class__.__name__,
            "ok": bool(result and result.ok),
            "status": result.status if result is not None else "not_executed",
            "error": self.error,
            "canonical_attempt_bound": bool(
                result and self._canonical_attempt_bound(result)
            ),
            "canonical_attempt_id": canonical_attempt_id,
            "canonical_attempt_store_verified": bool(
                result
                and result.metadata.get("canonical_attempt_store_verified") is True
            ),
            "canonical_attempt_execution_association_verified": bool(
                result
                and result.metadata.get(
                    "canonical_attempt_execution_association_verified"
                )
                is True
            ),
            "request": request_json,
            "result": result_json,
            "shadow_compare": compare_json,
            "shadow_result_can_override_authority": False,
            "safety": {
                "approval_authority": False,
                "approved": False,
                "merged": False,
                "cleanup_performed": False,
                "human_review_required": True,
                "execution_only": True,
            },
        }

    @staticmethod
    def _canonical_attempt_bound(result: ExecutionEngineResult) -> bool:
        attempt_id = result.metadata.get("canonical_attempt_id")
        return (
            result.metadata.get("canonical_attempt_bound") is True
            and isinstance(attempt_id, str)
            and bool(attempt_id.strip())
        )

    @staticmethod
    def _optional_path(value: Any) -> Path | None:
        if value is None or not str(value).strip():
            return None
        return Path(str(value))

    @staticmethod
    def _blocked_result(task_key: str, message: str) -> ExecutionEngineResult:
        return ExecutionEngineResult(
            ok=False,
            task_key=task_key,
            status=EXECUTION_STATUS_BLOCKED,
            summary=f"ExecutionEngine failed closed: {message}",
            safety=ExecutionEngineSafety(),
            metadata={
                "execution_authority": EFFECTIVE_AUTHORITY_EXECUTION_ENGINE,
                "legacy_fallback_allowed": False,
                "canonical_attempt_bound": False,
                "authority_error": message,
            },
        )

    def _blocked_runner_payload(self, message: str) -> dict[str, Any]:
        self.error = message
        return self._result_to_runner_payload(
            self._blocked_result("UNKNOWN", message)
        )

    @staticmethod
    def _result_to_runner_payload(
        result: ExecutionEngineResult,
    ) -> dict[str, Any]:
        safety = to_json_dict(result.safety)
        assert isinstance(safety, dict)
        safety["validators_started"] = bool(safety.get("validator_started"))
        return {
            "ok": result.ok,
            "status": result.status,
            "phase": "execution_engine",
            "task_key": result.task_key,
            "error": None if result.ok else result.summary,
            "summary": {
                "execution_authority": EFFECTIVE_AUTHORITY_EXECUTION_ENGINE,
                "legacy_fallback_allowed": False,
                "canonical_attempt_bound": result.metadata.get(
                    "canonical_attempt_bound"
                )
                is True,
                "canonical_attempt_id": result.metadata.get(
                    "canonical_attempt_id"
                ),
                "canonical_attempt_store_verified": result.metadata.get(
                    "canonical_attempt_store_verified"
                )
                is True,
                "canonical_attempt_execution_association_verified": (
                    result.metadata.get(
                        "canonical_attempt_execution_association_verified"
                    )
                    is True
                ),
                "engine_status": result.status,
                "engine_summary": result.summary,
            },
            "safety": safety,
            "execution_engine_result": to_json_dict(result),
        }


__all__ = [
    "DirectRuntimeHandoffAuthorityRequest",
    "EFFECTIVE_AUTHORITY_EXECUTION_ENGINE",
    "SCHEDULER_EXECUTION_ENGINE_AUTHORITY_SCHEMA_VERSION",
    "SCHEDULER_EXECUTION_ENGINE_AUTHORITY_SOURCE",
    "SchedulerExecutionEngineAuthority",
]
