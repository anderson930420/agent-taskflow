"""Typed, behavior-free contracts for a future ExecutionEngine.

This module defines values exchanged at the execution boundary. It does not
instantiate an engine, call runtime code, touch the filesystem, or perform any
orchestration action.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol, runtime_checkable


REQUEST_SOURCE_RUNTIME_HANDOFF = "runtime_handoff"
REQUEST_SOURCE_APPROVED_TASK = "approved_task"
REQUEST_SOURCE_MANUAL = "manual"
REQUEST_SOURCE_SCHEDULED_TICK = "scheduled_tick"
REQUEST_SOURCES = (
    REQUEST_SOURCE_RUNTIME_HANDOFF,
    REQUEST_SOURCE_APPROVED_TASK,
    REQUEST_SOURCE_MANUAL,
    REQUEST_SOURCE_SCHEDULED_TICK,
)
ExecutionEngineRequestSource = Literal[
    "runtime_handoff",
    "approved_task",
    "manual",
    "scheduled_tick",
]

EXECUTION_STATUS_NOT_STARTED = "not_started"
EXECUTION_STATUS_PREFLIGHT_FAILED = "preflight_failed"
EXECUTION_STATUS_EXECUTOR_FAILED = "executor_failed"
EXECUTION_STATUS_VALIDATOR_FAILED = "validator_failed"
EXECUTION_STATUS_BLOCKED = "blocked"
EXECUTION_STATUS_WAITING_APPROVAL = "waiting_approval"
EXECUTION_STATUS_COMPLETED = "completed"
EXECUTION_STATUS_DRY_RUN = "dry_run"
EXECUTION_STATUSES = (
    EXECUTION_STATUS_NOT_STARTED,
    EXECUTION_STATUS_PREFLIGHT_FAILED,
    EXECUTION_STATUS_EXECUTOR_FAILED,
    EXECUTION_STATUS_VALIDATOR_FAILED,
    EXECUTION_STATUS_BLOCKED,
    EXECUTION_STATUS_WAITING_APPROVAL,
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_DRY_RUN,
)
ExecutionEngineStatus = Literal[
    "not_started",
    "preflight_failed",
    "executor_failed",
    "validator_failed",
    "blocked",
    "waiting_approval",
    "completed",
    "dry_run",
]

STEP_STATUS_SKIPPED = "skipped"
STEP_STATUS_PASSED = "passed"
STEP_STATUS_FAILED = "failed"
STEP_STATUS_BLOCKED = "blocked"
STEP_STATUS_COMPLETED = "completed"
STEP_STATUSES = (
    STEP_STATUS_SKIPPED,
    STEP_STATUS_PASSED,
    STEP_STATUS_FAILED,
    STEP_STATUS_BLOCKED,
    STEP_STATUS_COMPLETED,
)
ExecutionEngineStepStatus = Literal[
    "skipped",
    "passed",
    "failed",
    "blocked",
    "completed",
]


def _require_non_empty(value: str, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _require_absolute_path(value: str | Path, field_name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{field_name} must be absolute: {value}")
    return path


def _normalize_string_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(values, str):
        return (values,)
    return tuple(values)


def _copy_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class ExecutionEngineExecutorProfile:
    """Executor selection inputs for one future engine invocation."""

    executor: str
    model: str | None = None
    provider: str | None = None
    tools: tuple[str, ...] = ()
    pi_bin: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "executor",
            _require_non_empty(self.executor, "executor"),
        )
        object.__setattr__(self, "tools", _normalize_string_tuple(self.tools))


@dataclass(frozen=True)
class ExecutionEngineValidatorProfile:
    """Deterministic validators requested for one execution."""

    validators: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "validators",
            _normalize_string_tuple(self.validators),
        )


@dataclass(frozen=True)
class ExecutionEngineWorkspaceProfile:
    """Workspace and artifact locations supplied to the future engine."""

    repo_path: Path
    artifact_dir: Path
    worktree_root: Path | None = None
    task_worktree_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "repo_path",
            _require_absolute_path(self.repo_path, "repo_path"),
        )
        object.__setattr__(
            self,
            "artifact_dir",
            _require_absolute_path(self.artifact_dir, "artifact_dir"),
        )
        if self.worktree_root is not None:
            object.__setattr__(self, "worktree_root", Path(self.worktree_root))
        if self.task_worktree_path is not None:
            object.__setattr__(
                self,
                "task_worktree_path",
                Path(self.task_worktree_path),
            )


@dataclass(frozen=True)
class ExecutionEngineArtifactRef:
    """Reference to proof-of-work produced or consumed by execution."""

    artifact_type: str
    path: Path
    description: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))


@dataclass(frozen=True)
class ExecutionEngineStepResult:
    """Result summary for one future engine step."""

    name: str
    status: str
    summary: str | None = None
    artifacts: tuple[ExecutionEngineArtifactRef, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "metadata", _copy_mapping(self.metadata))


@dataclass(frozen=True)
class ExecutionEngineSafety:
    """Explicit evidence that execution did not cross governance boundaries."""

    human_review_required: bool = True
    approved: bool = False
    merged: bool = False
    github_mutated: bool = False
    issue_closed: bool = False
    branch_pushed: bool = False
    branch_deleted: bool = False
    worktree_deleted: bool = False
    cleanup_performed: bool = False
    cron_modified: bool = False
    daemon_started: bool = False
    webhook_started: bool = False
    background_worker_started: bool = False
    scheduler_loop_started: bool = False
    multi_task_batch_started: bool = False
    executor_started: bool = False
    validator_started: bool = False
    one_task_only: bool = True
    execution_only: bool = True


@dataclass(frozen=True, kw_only=True)
class ExecutionEngineRequest:
    """Input contract for one future ExecutionEngine invocation."""

    task_key: str
    project: str | None = None
    source: str = REQUEST_SOURCE_MANUAL
    dry_run: bool = True
    preflight: bool = True
    executor_profile: ExecutionEngineExecutorProfile
    validator_profile: ExecutionEngineValidatorProfile
    workspace: ExecutionEngineWorkspaceProfile
    runtime_handoff_path: Path | None = None
    verifier_report_path: Path | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "task_key",
            _require_non_empty(self.task_key, "task_key"),
        )
        if self.runtime_handoff_path is not None:
            object.__setattr__(
                self,
                "runtime_handoff_path",
                Path(self.runtime_handoff_path),
            )
        if self.verifier_report_path is not None:
            object.__setattr__(
                self,
                "verifier_report_path",
                Path(self.verifier_report_path),
            )
        object.__setattr__(self, "metadata", _copy_mapping(self.metadata))


@dataclass(frozen=True)
class ExecutionEngineResult:
    """Output contract returned by a future ExecutionEngine implementation."""

    ok: bool
    task_key: str
    status: str
    summary: str | None = None
    next_operator_action: str | None = None
    safety: ExecutionEngineSafety = field(default_factory=ExecutionEngineSafety)
    steps: tuple[ExecutionEngineStepResult, ...] = ()
    artifacts: tuple[ExecutionEngineArtifactRef, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "task_key",
            _require_non_empty(self.task_key, "task_key"),
        )
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "metadata", _copy_mapping(self.metadata))


@runtime_checkable
class ExecutionEngine(Protocol):
    """Structural contract for future execution engine implementations."""

    def execute(self, request: ExecutionEngineRequest) -> ExecutionEngineResult:
        ...


def to_json_dict(
    value: Any,
) -> dict[str, Any] | list[Any] | str | int | float | bool | None:
    """Return a recursively JSON-compatible copy of a contract value."""

    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: to_json_dict(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): to_json_dict(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_json_dict(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Value is not JSON serializable by this contract: {value!r}")


__all__ = [
    "EXECUTION_STATUSES",
    "EXECUTION_STATUS_BLOCKED",
    "EXECUTION_STATUS_COMPLETED",
    "EXECUTION_STATUS_DRY_RUN",
    "EXECUTION_STATUS_EXECUTOR_FAILED",
    "EXECUTION_STATUS_NOT_STARTED",
    "EXECUTION_STATUS_PREFLIGHT_FAILED",
    "EXECUTION_STATUS_VALIDATOR_FAILED",
    "EXECUTION_STATUS_WAITING_APPROVAL",
    "ExecutionEngine",
    "ExecutionEngineArtifactRef",
    "ExecutionEngineExecutorProfile",
    "ExecutionEngineRequest",
    "ExecutionEngineRequestSource",
    "ExecutionEngineResult",
    "ExecutionEngineSafety",
    "ExecutionEngineStatus",
    "ExecutionEngineStepResult",
    "ExecutionEngineStepStatus",
    "ExecutionEngineValidatorProfile",
    "ExecutionEngineWorkspaceProfile",
    "REQUEST_SOURCES",
    "REQUEST_SOURCE_APPROVED_TASK",
    "REQUEST_SOURCE_MANUAL",
    "REQUEST_SOURCE_RUNTIME_HANDOFF",
    "REQUEST_SOURCE_SCHEDULED_TICK",
    "STEP_STATUSES",
    "STEP_STATUS_BLOCKED",
    "STEP_STATUS_COMPLETED",
    "STEP_STATUS_FAILED",
    "STEP_STATUS_PASSED",
    "STEP_STATUS_SKIPPED",
    "to_json_dict",
]
