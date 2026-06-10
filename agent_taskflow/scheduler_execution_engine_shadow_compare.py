"""P5-c: scheduler ExecutionEngine shadow / compare summary only.

This module provides a **pure, behavior-free** compare layer for the staged
scheduler-to-ExecutionEngine migration plan defined by the P5-a boundary
document (``docs/scheduler-execution-engine-migration-boundary.md``). It takes a
legacy scheduler tick payload and an engine-shaped ``ExecutionEngineRequest``
produced by the P5-b request builder
(``agent_taskflow/scheduler_execution_engine_request_builder.py``) and produces a
diagnostic comparison summary — and nothing else.

The compare layer does not execute anything. It does not call
``ExecutionEngine.execute``, the approved task runner, an executor, or a
validator. It does not wire into the scheduler tick runtime, does not modify the
active crontab, and does not read or write the DB or GitHub. It creates no
directories and no artifacts, runs no subprocesses, and never touches the
filesystem: path inputs are read as values only.

Mismatches reported here are **diagnostic only** and carry no authority.
Deterministic validators and human review gates remain the validation and
approval authority, exactly as the P5-a boundary requires. A future P5-d stage
may use this compare layer before enabling an opt-in execution path.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from agent_taskflow.execution_engine_contract import (
    REQUEST_SOURCE_SCHEDULED_TICK,
    ExecutionEngineRequest,
    to_json_dict,
)


SCHEDULER_EXECUTION_ENGINE_SHADOW_COMPARE_SCHEMA_VERSION = (
    "scheduler_execution_engine_shadow_compare.v1"
)
SCHEDULER_EXECUTION_ENGINE_SHADOW_COMPARE_SOURCE = (
    "scheduler_execution_engine_shadow_compare"
)


_MISSING = object()


def _nested_get(
    payload: Mapping[str, Any],
    path: tuple[str, ...],
    default: Any = None,
) -> Any:
    """Safely walk a dict-like payload by ``path``; return ``default`` if absent.

    This reads values only. It never touches the filesystem and never raises on
    a missing or non-mapping intermediate node.
    """

    current: Any = payload
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return default
        current = current[key]
    return current


def _first_present(
    payload: Mapping[str, Any],
    paths: tuple[tuple[str, ...], ...],
) -> tuple[Any, bool]:
    """Return ``(value, True)`` for the first present path, else ``(None, False)``."""

    for path in paths:
        value = _nested_get(payload, path, _MISSING)
        if value is not _MISSING:
            return value, True
    return None, False


@dataclass(frozen=True)
class SchedulerExecutionEngineShadowCompareInput:
    """A legacy scheduler tick payload paired with an engine-shaped request.

    Construction copies the dict-like inputs defensively and requires the engine
    request to carry the scheduled-tick source. It performs no filesystem, DB,
    GitHub, or runtime access.
    """

    legacy_scheduler_tick: Mapping[str, Any]
    engine_request: ExecutionEngineRequest
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.legacy_scheduler_tick, Mapping):
            raise TypeError("legacy_scheduler_tick must be a mapping")
        if not isinstance(self.engine_request, ExecutionEngineRequest):
            raise TypeError(
                "engine_request must be an ExecutionEngineRequest"
            )
        if self.engine_request.source != REQUEST_SOURCE_SCHEDULED_TICK:
            raise ValueError(
                "engine_request.source must be"
                f" {REQUEST_SOURCE_SCHEDULED_TICK!r}: the shadow compare layer"
                " only compares scheduler-tick requests"
            )
        # Defensive deep copies so later mutation of the caller's dicts (or any
        # nested container inside them) cannot mutate this input or a result.
        object.__setattr__(
            self,
            "legacy_scheduler_tick",
            MappingProxyType(copy.deepcopy(dict(self.legacy_scheduler_tick))),
        )
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(copy.deepcopy(dict(self.metadata))),
        )


@dataclass(frozen=True)
class SchedulerExecutionEngineShadowCompareResult:
    """Diagnostic comparison of a legacy tick against an engine-shaped request.

    The result is JSON-compatible (or convertible via
    :func:`scheduler_execution_engine_shadow_compare_to_json_dict`). It carries
    no authority: mismatches are diagnostic only.
    """

    ok: bool
    schema_version: str
    source: str
    legacy_status: str | None
    legacy_selected_task_key: str | None
    engine_task_key: str
    engine_source: str
    matched: bool
    mismatches: tuple[str, ...]
    warnings: tuple[str, ...]
    summary: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "mismatches", tuple(self.mismatches))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(
            self,
            "summary",
            MappingProxyType(dict(self.summary)),
        )


def compare_scheduler_tick_to_engine_request(
    input: SchedulerExecutionEngineShadowCompareInput,
) -> SchedulerExecutionEngineShadowCompareResult:
    """Compare a legacy scheduler tick payload to an engine-shaped request.

    Pure inspection only: this reads the two values and returns a diagnostic
    summary. It executes nothing, wires no scheduler runtime, touches no
    filesystem, DB, GitHub, cron, or subprocess, and mutates no state.
    """

    legacy = input.legacy_scheduler_tick
    request = input.engine_request
    engine_metadata: Mapping[str, Any] = request.metadata

    mismatches: list[str] = []
    warnings: list[str] = []
    summary: dict[str, Any] = {}

    # --- A. Task identity ----------------------------------------------------
    engine_task_key = request.task_key
    legacy_selected = _nested_get(legacy, ("selected_task_key",), _MISSING)
    if legacy_selected is _MISSING or legacy_selected is None:
        nested = _nested_get(legacy, ("automation", "selected_task_key"), _MISSING)
        legacy_selected = None if nested is _MISSING else nested
    legacy_selected_task_key = (
        None if legacy_selected is None else str(legacy_selected)
    )
    summary["legacy_selected_task_key"] = legacy_selected_task_key
    summary["engine_task_key"] = engine_task_key
    if legacy_selected_task_key is None:
        warnings.append(
            "legacy selected_task_key absent; task identity not compared"
        )
    elif legacy_selected_task_key != engine_task_key:
        mismatches.append(
            "task_key mismatch: legacy"
            f" {legacy_selected_task_key!r} != engine {engine_task_key!r}"
        )

    # --- B. Scheduler / source identity --------------------------------------
    # The engine source is enforced to be the scheduled-tick source at input
    # construction. Legacy source is recorded but not required to match: the
    # legacy source marker is ``github_issue_one_task_scheduler_tick``.
    engine_source = request.source
    legacy_source = _nested_get(legacy, ("source",))
    summary["engine_source"] = engine_source
    summary["legacy_source"] = legacy_source

    # --- C. Repo / project ---------------------------------------------------
    legacy_repo = _nested_get(legacy, ("repo",), _MISSING)
    engine_project = request.project
    summary["legacy_repo"] = None if legacy_repo is _MISSING else legacy_repo
    summary["engine_project"] = engine_project
    if legacy_repo is _MISSING or legacy_repo is None:
        warnings.append("legacy repo absent; repo/project not compared")
    elif str(legacy_repo) != str(engine_project):
        mismatches.append(
            "repo/project mismatch: legacy"
            f" {str(legacy_repo)!r} != engine {str(engine_project)!r}"
        )

    # --- D. Execution-only publication ---------------------------------------
    engine_publish = engine_metadata.get("publish_after_execution", _MISSING)
    engine_mode = engine_metadata.get("mode", _MISSING)
    engine_execution_only = engine_metadata.get("execution_only", _MISSING)
    summary["engine_publication"] = {
        "publish_after_execution": (
            None if engine_publish is _MISSING else engine_publish
        ),
        "mode": None if engine_mode is _MISSING else engine_mode,
        "execution_only": (
            None if engine_execution_only is _MISSING else engine_execution_only
        ),
    }
    if engine_publish is not False:
        mismatches.append(
            "engine metadata publish_after_execution must be False:"
            f" {None if engine_publish is _MISSING else engine_publish!r}"
        )
    if engine_mode != "execution_only":
        mismatches.append(
            'engine metadata mode must be "execution_only":'
            f" {None if engine_mode is _MISSING else engine_mode!r}"
        )
    if engine_execution_only is not True:
        mismatches.append(
            "engine metadata execution_only must be True:"
            f" {None if engine_execution_only is _MISSING else engine_execution_only!r}"
        )

    legacy_publish, publish_found = _first_present(
        legacy,
        (
            ("publication", "publish_after_execution"),
            ("publication_config", "publish_after_execution"),
            ("automation", "publication", "publish_after_execution"),
            ("automation", "publication_config", "publish_after_execution"),
        ),
    )
    legacy_pub_mode, mode_found = _first_present(
        legacy,
        (
            ("publication", "mode"),
            ("publication_config", "mode"),
            ("automation", "publication", "mode"),
            ("automation", "publication_config", "mode"),
        ),
    )
    summary["legacy_publication"] = {
        "publish_after_execution": legacy_publish if publish_found else None,
        "mode": legacy_pub_mode if mode_found else None,
        "found": publish_found or mode_found,
    }
    if not publish_found and not mode_found:
        warnings.append(
            "legacy publication markers absent;"
            " execution-only publication not verified"
        )
    else:
        if publish_found and legacy_publish is not False:
            mismatches.append(
                "legacy publish_after_execution must be False:"
                f" {legacy_publish!r}"
            )
        if mode_found and legacy_pub_mode != "execution_only":
            mismatches.append(
                'legacy publication mode must be "execution_only":'
                f" {legacy_pub_mode!r}"
            )

    # --- E. Safety -----------------------------------------------------------
    engine_one_task_only = engine_metadata.get("one_task_only", _MISSING)
    engine_scheduler_tick = engine_metadata.get("scheduler_tick", _MISSING)
    summary["engine_safety"] = {
        "one_task_only": (
            None if engine_one_task_only is _MISSING else engine_one_task_only
        ),
        "scheduler_tick": (
            None if engine_scheduler_tick is _MISSING else engine_scheduler_tick
        ),
    }
    if engine_one_task_only is not True:
        mismatches.append(
            "engine metadata one_task_only must be True:"
            f" {None if engine_one_task_only is _MISSING else engine_one_task_only!r}"
        )
    if engine_scheduler_tick is not True:
        mismatches.append(
            "engine metadata scheduler_tick must be True:"
            f" {None if engine_scheduler_tick is _MISSING else engine_scheduler_tick!r}"
        )

    legacy_safety = _nested_get(legacy, ("safety",), _MISSING)
    if legacy_safety is _MISSING or not isinstance(legacy_safety, Mapping):
        summary["legacy_safety"] = None
        warnings.append(
            "legacy safety markers absent; scheduler safety not verified"
        )
    else:
        recorded: dict[str, Any] = {}
        for marker in (
            "one_task_only",
            "scheduler_loop_started",
            "background_worker_started",
            "multi_task_batch_started",
            "github_mutated",
            "approved",
            "merged",
        ):
            if marker in legacy_safety:
                recorded[marker] = legacy_safety[marker]
        summary["legacy_safety"] = recorded
        # A scheduler-tick execution-only request must not cross any of these
        # governance boundaries.
        for marker, label in (
            ("scheduler_loop_started", "scheduler loop"),
            ("background_worker_started", "background worker"),
            ("multi_task_batch_started", "multi-task batch"),
            ("github_mutated", "GitHub mutation"),
            ("approved", "approval"),
            ("merged", "merge"),
        ):
            if marker in legacy_safety and bool(legacy_safety[marker]):
                mismatches.append(
                    f"legacy safety indicates {label}: {marker}=True"
                )
        if "one_task_only" in legacy_safety and not bool(
            legacy_safety["one_task_only"]
        ):
            warnings.append(
                "legacy safety one_task_only is not True:"
                f" {legacy_safety['one_task_only']!r}"
            )

    # --- F. Executor / validator / workspace observability -------------------
    workspace = request.workspace
    summary["engine_executor"] = request.executor_profile.executor
    summary["engine_model"] = request.executor_profile.model
    summary["engine_validators"] = list(request.validator_profile.validators)
    summary["engine_workspace"] = {
        "repo_path": str(workspace.repo_path),
        "artifact_dir": str(workspace.artifact_dir),
        "worktree_root": (
            str(workspace.worktree_root)
            if workspace.worktree_root is not None
            else None
        ),
        "task_worktree_path": (
            str(workspace.task_worktree_path)
            if workspace.task_worktree_path is not None
            else None
        ),
    }
    legacy_runner, runner_found = _first_present(
        legacy,
        (
            ("runner_config",),
            ("runner",),
            ("automation", "runner_config"),
            ("automation", "runner"),
            ("observability", "runner"),
        ),
    )
    if not runner_found or not isinstance(legacy_runner, Mapping):
        summary["legacy_runner"] = None
        warnings.append(
            "legacy runner config absent; executor/validator observability"
            " not compared"
        )
    else:
        legacy_executor = legacy_runner.get("executor")
        legacy_validators = legacy_runner.get("validators")
        summary["legacy_runner"] = {
            "executor": legacy_executor,
            "validators": legacy_validators,
        }
        if (
            legacy_executor is not None
            and str(legacy_executor) != str(request.executor_profile.executor)
        ):
            warnings.append(
                "legacy/engine executor differ: legacy"
                f" {legacy_executor!r} != engine"
                f" {request.executor_profile.executor!r}"
            )
        if legacy_validators is not None and list(legacy_validators) != list(
            request.validator_profile.validators
        ):
            warnings.append(
                "legacy/engine validators differ: legacy"
                f" {list(legacy_validators)!r} != engine"
                f" {list(request.validator_profile.validators)!r}"
            )

    legacy_status_value = _nested_get(legacy, ("status",), _MISSING)
    legacy_status = (
        None
        if legacy_status_value is _MISSING or legacy_status_value is None
        else str(legacy_status_value)
    )

    matched = not mismatches
    return SchedulerExecutionEngineShadowCompareResult(
        ok=matched,
        schema_version=SCHEDULER_EXECUTION_ENGINE_SHADOW_COMPARE_SCHEMA_VERSION,
        source=SCHEDULER_EXECUTION_ENGINE_SHADOW_COMPARE_SOURCE,
        legacy_status=legacy_status,
        legacy_selected_task_key=legacy_selected_task_key,
        engine_task_key=engine_task_key,
        engine_source=engine_source,
        matched=matched,
        mismatches=tuple(mismatches),
        warnings=tuple(warnings),
        summary=summary,
    )


def scheduler_execution_engine_shadow_compare_to_json_dict(
    result: SchedulerExecutionEngineShadowCompareResult,
) -> dict[str, Any]:
    """Return the compare result as a JSON-compatible dict via the contract codec."""

    payload = to_json_dict(result)
    if not isinstance(payload, dict):
        raise TypeError(
            "SchedulerExecutionEngineShadowCompareResult did not serialize to a"
            f" dict: {type(payload).__name__}"
        )
    return payload


__all__ = [
    "SCHEDULER_EXECUTION_ENGINE_SHADOW_COMPARE_SCHEMA_VERSION",
    "SCHEDULER_EXECUTION_ENGINE_SHADOW_COMPARE_SOURCE",
    "SchedulerExecutionEngineShadowCompareInput",
    "SchedulerExecutionEngineShadowCompareResult",
    "compare_scheduler_tick_to_engine_request",
    "scheduler_execution_engine_shadow_compare_to_json_dict",
]
