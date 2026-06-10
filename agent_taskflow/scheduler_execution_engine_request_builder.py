"""P5-b: scheduler ExecutionEngine request-builder contract only.

This module maps scheduler-selected confirmed one-task work onto the P4-b
``ExecutionEngineRequest`` contract. It is pure and behavior-free: it builds a
request value and nothing else. It does not execute the request, does not wire
into the scheduler tick runtime, and does not call an executor, a validator,
the approved task runner, or ``ExecutionEngine.execute``.

The builder never reads or writes the DB, never reads or writes GitHub, never
creates directories or artifacts, never touches the active crontab, never
mutates scheduler state or files, and never runs subprocesses. Path inputs are
validated for shape only; they are not required to exist and the filesystem is
never touched.

A future P5-c stage may use this builder for shadow/compare summaries only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from agent_taskflow.execution_engine_contract import (
    REQUEST_SOURCE_SCHEDULED_TICK,
    ExecutionEngineExecutorProfile,
    ExecutionEngineRequest,
    ExecutionEngineValidatorProfile,
    ExecutionEngineWorkspaceProfile,
    to_json_dict,
)


SCHEDULER_EXECUTION_ENGINE_REQUEST_BUILDER_SCHEMA_VERSION = (
    "scheduler_execution_engine_request_builder.v1"
)
SCHEDULER_EXECUTION_ENGINE_REQUEST_BUILDER_SOURCE = (
    "scheduler_execution_engine_request_builder"
)


def _require_non_empty(value: str, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _require_owner_name_repo(value: str) -> str:
    repo = str(value or "").strip()
    parts = repo.split("/")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise ValueError("repo must be in owner/name form")
    return repo


def _require_absolute_path(value: str | Path, field_name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{field_name} must be absolute: {value}")
    return path


def _normalize_string_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(values, str):
        return (values,)
    return tuple(values)


def _strip_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


@dataclass(frozen=True)
class SchedulerExecutionEngineRequestBuildInput:
    """One scheduler-selected confirmed task, ready to map onto a request.

    Construction validates shape only. Paths are not required to exist and
    the filesystem is never touched.
    """

    task_key: str
    repo: str
    local_repo_path: Path
    artifact_dir: Path
    executor: str
    model: str | None = None
    provider: str | None = None
    tools: tuple[str, ...] = ()
    pi_bin: str | None = None
    validators: tuple[str, ...] = ()
    worktree_root: Path | None = None
    task_worktree_path: Path | None = None
    dry_run: bool = True
    confirmed: bool = False
    preflight: bool = True
    publish_after_execution: bool = False
    execution_only: bool = True
    operator: str | None = None
    operator_note: str | None = None
    selected_issue_number: int | None = None
    selected_candidate_key: str | None = None
    runtime_handoff_path: Path | None = None
    verifier_report_path: Path | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "task_key",
            _require_non_empty(self.task_key, "task_key"),
        )
        object.__setattr__(self, "repo", _require_owner_name_repo(self.repo))
        object.__setattr__(
            self,
            "local_repo_path",
            _require_absolute_path(self.local_repo_path, "local_repo_path"),
        )
        object.__setattr__(
            self,
            "artifact_dir",
            _require_absolute_path(self.artifact_dir, "artifact_dir"),
        )
        object.__setattr__(
            self,
            "executor",
            _require_non_empty(self.executor, "executor"),
        )
        object.__setattr__(self, "tools", _normalize_string_tuple(self.tools))
        object.__setattr__(
            self,
            "validators",
            _normalize_string_tuple(self.validators),
        )
        for name in (
            "worktree_root",
            "task_worktree_path",
            "runtime_handoff_path",
            "verifier_report_path",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, Path(value))
        for name in ("operator", "operator_note", "selected_candidate_key"):
            object.__setattr__(self, name, _strip_to_none(getattr(self, name)))
        if self.publish_after_execution:
            raise ValueError(
                "publish_after_execution must be False: the scheduler request"
                " builder is execution-only and never publishes"
            )
        if not self.execution_only:
            raise ValueError(
                "execution_only must be True: the scheduler request builder"
                " only builds execution-only requests"
            )
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(dict(self.metadata)),
        )


def build_scheduler_execution_engine_request(
    input: SchedulerExecutionEngineRequestBuildInput,
) -> ExecutionEngineRequest:
    """Map one scheduler-selected confirmed task to an ExecutionEngineRequest.

    Pure value mapping only: no execution, no scheduler wiring, no filesystem,
    DB, GitHub, cron, or subprocess access.
    """

    metadata: dict[str, Any] = {
        "schema_version": (
            SCHEDULER_EXECUTION_ENGINE_REQUEST_BUILDER_SCHEMA_VERSION
        ),
        "builder_source": SCHEDULER_EXECUTION_ENGINE_REQUEST_BUILDER_SOURCE,
        "repo": input.repo,
        "confirmed": input.confirmed,
        "publish_after_execution": False,
        "mode": "execution_only",
        "execution_only": True,
        "one_task_only": True,
        "scheduler_tick": True,
    }
    if input.selected_issue_number is not None:
        metadata["selected_issue_number"] = input.selected_issue_number
    if input.selected_candidate_key is not None:
        metadata["selected_candidate_key"] = input.selected_candidate_key
    if input.operator is not None:
        metadata["operator"] = input.operator
    if input.operator_note is not None:
        metadata["operator_note"] = input.operator_note
    # Deep, JSON-compatible copy so later mutation of the caller's metadata
    # (or nested containers inside it) cannot mutate the built request.
    metadata["caller_metadata"] = to_json_dict(dict(input.metadata))

    return ExecutionEngineRequest(
        task_key=input.task_key,
        project=input.repo,
        source=REQUEST_SOURCE_SCHEDULED_TICK,
        dry_run=input.dry_run,
        preflight=input.preflight,
        executor_profile=ExecutionEngineExecutorProfile(
            executor=input.executor,
            model=input.model,
            provider=input.provider,
            tools=input.tools,
            pi_bin=input.pi_bin,
        ),
        validator_profile=ExecutionEngineValidatorProfile(
            validators=input.validators,
        ),
        workspace=ExecutionEngineWorkspaceProfile(
            repo_path=input.local_repo_path,
            artifact_dir=input.artifact_dir,
            worktree_root=input.worktree_root,
            task_worktree_path=input.task_worktree_path,
        ),
        runtime_handoff_path=input.runtime_handoff_path,
        verifier_report_path=input.verifier_report_path,
        metadata=metadata,
    )


def scheduler_execution_engine_request_to_json_dict(
    request: ExecutionEngineRequest,
) -> dict[str, Any]:
    """Return the request as a JSON-compatible dict via the contract codec."""

    payload = to_json_dict(request)
    if not isinstance(payload, dict):
        raise TypeError(
            "ExecutionEngineRequest did not serialize to a dict:"
            f" {type(payload).__name__}"
        )
    return payload


__all__ = [
    "SCHEDULER_EXECUTION_ENGINE_REQUEST_BUILDER_SCHEMA_VERSION",
    "SCHEDULER_EXECUTION_ENGINE_REQUEST_BUILDER_SOURCE",
    "SchedulerExecutionEngineRequestBuildInput",
    "build_scheduler_execution_engine_request",
    "scheduler_execution_engine_request_to_json_dict",
]
