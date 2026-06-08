"""P4-c adapter: implement the ExecutionEngine protocol over the approved runner.

This module adapts the existing one-shot ``approved_task_runner.run_approved_task``
to the P4-b ``ExecutionEngine`` protocol. It only translates between the
``ExecutionEngineRequest`` / ``ExecutionEngineResult`` contract and the existing
``ApprovedTaskRunRequest`` / ``ApprovedTaskRunResult`` shapes.

The adapter performs no orchestration of its own. It does not approve, merge,
clean up, archive, close out, publish a PR, delete a branch or worktree, start a
daemon/webhook/background worker, run a scheduler loop, or batch multiple tasks.
Whatever ``run_approved_task`` does when called is the only behavior; the adapter
adds none. P4-c also does not wire this adapter into any scheduler or automation
runtime path: it is imported by tests and docs only.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_taskflow.approved_task_runner import (
    ApprovedTaskRunRequest,
    run_approved_task,
)
from agent_taskflow.execution_engine_contract import (
    EXECUTION_STATUS_BLOCKED,
    STEP_STATUS_BLOCKED,
    STEP_STATUS_COMPLETED,
    STEP_STATUS_FAILED,
    STEP_STATUS_PASSED,
    STEP_STATUS_SKIPPED,
    ExecutionEngineArtifactRef,
    ExecutionEngineRequest,
    ExecutionEngineResult,
    ExecutionEngineSafety,
    ExecutionEngineStepResult,
)


_MISSING = object()


def _read(source: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a mapping or an attribute holder, safely."""

    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _json_safe(value: Any) -> Any:
    """Return a JSON-compatible copy of ``value`` without ever raising."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _bool(value: Any) -> bool:
    return bool(value)


class ApprovedTaskRunnerExecutionEngineAdapter:
    """Implement ``ExecutionEngine`` by delegating to ``run_approved_task``."""

    def execute(self, request: ExecutionEngineRequest) -> ExecutionEngineResult:
        approved_request = self._build_approved_request(request)
        try:
            result = run_approved_task(approved_request)
        except Exception as exc:  # noqa: BLE001 - surfaced as a blocked result.
            return self._adapter_failure_result(request, exc)
        return self._map_result(request, result)

    # -- request mapping ---------------------------------------------------

    @staticmethod
    def _build_approved_request(
        request: ExecutionEngineRequest,
    ) -> ApprovedTaskRunRequest:
        executor_profile = request.executor_profile
        validator_profile = request.validator_profile
        workspace = request.workspace
        return ApprovedTaskRunRequest(
            task_key=request.task_key,
            executor=executor_profile.executor,
            repo_path=workspace.repo_path,
            artifact_root=workspace.artifact_dir,
            worktree_root=workspace.worktree_root,
            validators=validator_profile.validators,
            dry_run=request.dry_run,
            preflight=request.preflight,
            model=executor_profile.model,
            provider=executor_profile.provider,
            tools=executor_profile.tools,
            pi_bin=executor_profile.pi_bin,
        )

    # -- result mapping ----------------------------------------------------

    def _map_result(
        self,
        request: ExecutionEngineRequest,
        result: Any,
    ) -> ExecutionEngineResult:
        ok = _bool(_read(result, "ok", False))
        status = (
            _read(result, "status", None)
            or _read(result, "task_status", None)
            or EXECUTION_STATUS_BLOCKED
        )
        return ExecutionEngineResult(
            ok=ok,
            task_key=request.task_key,
            status=str(status),
            summary=self._map_summary(request, result, status),
            next_operator_action=self._map_next_action(result),
            safety=self._map_safety(result),
            steps=self._map_steps(result),
            artifacts=self._map_artifacts(result),
            metadata=self._map_metadata(result),
        )

    @staticmethod
    def _map_summary(
        request: ExecutionEngineRequest,
        result: Any,
        status: Any,
    ) -> str:
        summary = _read(result, "summary", None)
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
        return (
            f"Approved task runner returned status={status} for "
            f"{request.task_key}."
        )

    @staticmethod
    def _map_next_action(result: Any) -> str | None:
        actions = _read(result, "next_allowed_actions", None)
        if isinstance(actions, (list, tuple)) and actions:
            first = actions[0]
            if first is not None:
                return str(first)
        return None

    @staticmethod
    def _map_safety(result: Any) -> ExecutionEngineSafety:
        payload = _read(result, "safety", None)
        if not isinstance(payload, Mapping):
            return ExecutionEngineSafety()

        kwargs: dict[str, bool] = {}
        if "executor_started" in payload:
            kwargs["executor_started"] = _bool(payload["executor_started"])
        # The approved runner exposes the plural ``validators_started`` field.
        if "validators_started" in payload:
            kwargs["validator_started"] = _bool(payload["validators_started"])
        if "validator_started" in payload:
            kwargs["validator_started"] = _bool(payload["validator_started"])

        # Preserve any explicitly present governance evidence. Anything absent
        # stays at the conservative ExecutionEngineSafety default.
        passthrough = (
            "approved",
            "merged",
            "github_mutated",
            "issue_closed",
            "branch_pushed",
            "branch_deleted",
            "worktree_deleted",
            "cleanup_performed",
            "cron_modified",
            "daemon_started",
            "webhook_started",
            "background_worker_started",
            "scheduler_loop_started",
            "multi_task_batch_started",
        )
        for name in passthrough:
            if name in payload:
                kwargs[name] = _bool(payload[name])
        return ExecutionEngineSafety(**kwargs)

    def _map_steps(self, result: Any) -> tuple[ExecutionEngineStepResult, ...]:
        steps: list[ExecutionEngineStepResult] = []

        preflight = _read(result, "preflight", _MISSING)
        if preflight is not _MISSING and preflight is not None:
            steps.append(self._preflight_step(preflight))

        workspace = _read(result, "workspace", _MISSING)
        if workspace is not _MISSING and workspace is not None:
            steps.append(self._workspace_step(workspace))

        executor_run = _read(result, "executor_run", _MISSING)
        if executor_run is not _MISSING and executor_run is not None:
            steps.append(self._executor_step(executor_run))

        validators = _read(result, "validators", _MISSING)
        if validators is not _MISSING and validators:
            steps.append(self._validators_step(validators))

        status = _read(result, "status", None) or _read(
            result, "task_status", None
        )
        if status:
            ok = _bool(_read(result, "ok", False))
            steps.append(
                ExecutionEngineStepResult(
                    name="status_transition",
                    status=STEP_STATUS_COMPLETED if ok else STEP_STATUS_BLOCKED,
                    summary=f"final status: {status}",
                )
            )
        return tuple(steps)

    @staticmethod
    def _preflight_step(payload: Any) -> ExecutionEngineStepResult:
        ran = _bool(_read(payload, "ran", False))
        ok = _read(payload, "ok", None)
        if not ran:
            status = STEP_STATUS_SKIPPED
        elif ok is True:
            status = STEP_STATUS_PASSED
        elif ok is False:
            status = STEP_STATUS_FAILED
        else:
            status = STEP_STATUS_SKIPPED
        return ExecutionEngineStepResult(
            name="preflight",
            status=status,
            summary=_optional_str(_read(payload, "status", None)),
        )

    @staticmethod
    def _workspace_step(payload: Any) -> ExecutionEngineStepResult:
        prepared = _bool(_read(payload, "prepared", False))
        return ExecutionEngineStepResult(
            name="workspace",
            status=STEP_STATUS_PASSED if prepared else STEP_STATUS_BLOCKED,
            summary=_optional_str(_read(payload, "summary", None)),
        )

    @staticmethod
    def _executor_step(payload: Any) -> ExecutionEngineStepResult:
        started = _bool(_read(payload, "started", False))
        ok = _read(payload, "ok", None)
        if not started:
            status = STEP_STATUS_SKIPPED
        elif ok is True:
            status = STEP_STATUS_PASSED
        elif ok is False:
            status = STEP_STATUS_FAILED
        else:
            status = STEP_STATUS_SKIPPED
        return ExecutionEngineStepResult(
            name="executor",
            status=status,
            summary=_optional_str(_read(payload, "summary", None)),
        )

    @staticmethod
    def _validators_step(payload: Any) -> ExecutionEngineStepResult:
        items = list(payload) if isinstance(payload, (list, tuple)) else []
        failed = any(
            str(_read(item, "status", "")).lower() in {"failed", "blocked"}
            for item in items
        )
        all_passed = bool(items) and all(
            _bool(_read(item, "ok", False))
            or str(_read(item, "status", "")).lower() in {"passed", "completed"}
            for item in items
        )
        if failed:
            status = STEP_STATUS_FAILED
        elif all_passed:
            status = STEP_STATUS_PASSED
        else:
            status = STEP_STATUS_SKIPPED
        return ExecutionEngineStepResult(
            name="validators",
            status=status,
            summary=f"{len(items)} validator result(s)",
        )

    def _map_artifacts(
        self, result: Any
    ) -> tuple[ExecutionEngineArtifactRef, ...]:
        raw = _read(result, "artifacts", None)
        refs: list[ExecutionEngineArtifactRef] = []
        if isinstance(raw, Mapping):
            for artifact_type, path in raw.items():
                ref = self._artifact_ref(artifact_type, path)
                if ref is not None:
                    refs.append(ref)
        elif isinstance(raw, (list, tuple)):
            for item in raw:
                ref = self._artifact_ref_from_item(item)
                if ref is not None:
                    refs.append(ref)
        return tuple(refs)

    def _artifact_ref_from_item(
        self, item: Any
    ) -> ExecutionEngineArtifactRef | None:
        if isinstance(item, Mapping):
            artifact_type = item.get("artifact_type", item.get("kind"))
            path = item.get("path")
            description = item.get("description")
        else:
            artifact_type = getattr(
                item, "artifact_type", getattr(item, "kind", None)
            )
            path = getattr(item, "path", None)
            description = getattr(item, "description", None)
        return self._artifact_ref(artifact_type, path, description)

    @staticmethod
    def _artifact_ref(
        artifact_type: Any,
        path: Any,
        description: Any = None,
    ) -> ExecutionEngineArtifactRef | None:
        if path is None:
            return None
        path_str = str(path).strip()
        if not path_str:
            return None
        type_str = str(artifact_type).strip() if artifact_type is not None else ""
        if not type_str:
            type_str = "artifact"
        description_str = (
            str(description).strip()
            if description is not None and str(description).strip()
            else None
        )
        try:
            return ExecutionEngineArtifactRef(
                artifact_type=type_str,
                path=Path(path_str),
                description=description_str,
            )
        except Exception:  # noqa: BLE001 - skip malformed artifacts.
            return None

    @staticmethod
    def _map_metadata(result: Any) -> dict[str, Any]:
        metadata: dict[str, Any] = {"adapter": "approved_task_runner"}
        for source_key, dest_key in (
            ("ok", "runner_ok"),
            ("status", "runner_status"),
            ("phase", "runner_phase"),
            ("executor", "runner_executor"),
            ("dry_run", "runner_dry_run"),
            ("error", "runner_error"),
            ("summary", "runner_summary"),
        ):
            value = _read(result, source_key, _MISSING)
            if value is _MISSING:
                continue
            metadata[dest_key] = _json_safe(value)
        return metadata

    # -- error handling ----------------------------------------------------

    @staticmethod
    def _adapter_failure_result(
        request: ExecutionEngineRequest,
        exc: Exception,
    ) -> ExecutionEngineResult:
        message = f"{exc.__class__.__name__}: {exc}"
        return ExecutionEngineResult(
            ok=False,
            task_key=request.task_key,
            status=EXECUTION_STATUS_BLOCKED,
            summary=(
                "ApprovedTaskRunnerExecutionEngineAdapter failed to delegate to "
                f"run_approved_task: {message}"
            ),
            next_operator_action=None,
            safety=ExecutionEngineSafety(),
            steps=(
                ExecutionEngineStepResult(
                    name="approved_task_runner",
                    status=STEP_STATUS_FAILED,
                    summary=message,
                ),
            ),
            artifacts=(),
            metadata={
                "adapter": "approved_task_runner",
                "error_type": exc.__class__.__name__,
                "error_message": str(exc),
            },
        )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = ["ApprovedTaskRunnerExecutionEngineAdapter"]
