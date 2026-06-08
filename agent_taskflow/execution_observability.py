"""P4-e unified execution summary / observability record.

This module defines a single, JSON-safe *observability* shape and read-only
normalizers that convert the several execution-result payloads the system already
produces into that one shape:

1. :class:`~agent_taskflow.execution_engine_contract.ExecutionEngineResult`
   (the P4-b/P4-c/P4-d engine facade output).
2. ``approved_task_runner`` result payloads (mapping- or attribute-shaped).
3. Scheduler tick JSON payloads (the real cron path's tick output).

The goal of P4-e is to make it easier for future Mission Control / CLI
observability to reason over execution records *without changing runtime
behavior*. It does not migrate the live scheduler or cron path onto the engine
facade; it only normalizes payloads that already exist.

This module is strictly read-only. It does not read or write files, touch the
DB, call git or GitHub, or run executors or validators. It does not import the
scheduler tick, cron, Mission Control, or DB modules. The summarizers only
inspect values that are handed to them and return new dataclasses; they never
mutate the source payloads.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any


# -- summary sources -------------------------------------------------------

SUMMARY_SOURCE_MANUAL_ENGINE_FACADE = "manual_engine_facade"
SUMMARY_SOURCE_APPROVED_TASK_RUNNER = "approved_task_runner"
SUMMARY_SOURCE_SCHEDULER_TICK = "scheduler_tick"
SUMMARY_SOURCE_UNKNOWN = "unknown"
SUMMARY_SOURCES = (
    SUMMARY_SOURCE_MANUAL_ENGINE_FACADE,
    SUMMARY_SOURCE_APPROVED_TASK_RUNNER,
    SUMMARY_SOURCE_SCHEDULER_TICK,
    SUMMARY_SOURCE_UNKNOWN,
)

# -- summary schema version ------------------------------------------------

EXECUTION_OBSERVABILITY_SUMMARY_SCHEMA_VERSION = "execution_observability_summary.v1"

# Result-type discriminators recorded in ``metadata["result_type"]``.
RESULT_TYPE_EXECUTION_ENGINE_RESULT = "ExecutionEngineResult"
RESULT_TYPE_APPROVED_TASK_RUNNER_PAYLOAD = "approved_task_runner_payload"
RESULT_TYPE_SCHEDULER_TICK_PAYLOAD = "scheduler_tick_payload"


_MISSING = object()

# Canonical safety field names shared by :class:`ExecutionObservedSafety` and the
# engine contract :class:`ExecutionEngineSafety`. Conservative defaults live on
# the dataclass; these names drive payload mapping.
_SAFETY_FIELDS = (
    "human_review_required",
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
    "executor_started",
    "validator_started",
    "one_task_only",
    "execution_only",
)

# Source-payload aliases for safety flags whose key differs from the canonical
# observed field name. Only applied when the canonical field was not present.
_SAFETY_ALIASES = {
    # approved_task_runner uses these plural / verbose spellings.
    "human_approval_required": "human_review_required",
    "validators_started": "validator_started",
}


def _read(source: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a mapping or an attribute holder, safely."""

    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _has(source: Any, key: str) -> bool:
    """Return whether ``key`` is present on a mapping or attribute holder."""

    if isinstance(source, Mapping):
        return key in source
    return hasattr(source, key)


def _opt_str(value: Any) -> str | None:
    """Return a stripped string for non-empty values, otherwise ``None``."""

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _str_tuple(value: Any) -> tuple[str, ...]:
    """Coerce a string / sequence / mapping-of-names into a tuple of names."""

    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, Mapping):
        value = list(value.keys())
    if isinstance(value, (list, tuple)):
        names: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                name = item.get("name") or item.get("validator") or item.get("id")
            elif isinstance(item, str):
                name = item
            else:
                name = getattr(item, "name", None)
            text = _opt_str(name)
            if text:
                names.append(text)
        return tuple(names)
    return ()


# -- dataclasses -----------------------------------------------------------


@dataclass(frozen=True)
class ExecutionObserverProfile:
    """Executor / validator selection observed for one execution record."""

    executor: str | None = None
    model: str | None = None
    provider: str | None = None
    tools: tuple[str, ...] = ()
    validators: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "validators", tuple(self.validators))


@dataclass(frozen=True)
class ExecutionObservedStep:
    """One observed execution step."""

    name: str
    status: str
    summary: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class ExecutionObservedArtifact:
    """One observed proof-of-work artifact reference."""

    artifact_type: str
    path: str
    description: str | None = None


@dataclass(frozen=True)
class ExecutionObservedSafety:
    """High-level governance flags observed for one execution record.

    Defaults are conservative: human review is required and nothing destructive
    or expansive has happened.
    """

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


@dataclass(frozen=True)
class UnifiedExecutionSummary:
    """One normalized, JSON-safe observability record for an execution."""

    schema_version: str
    source: str
    ok: bool
    task_key: str | None = None
    status: str | None = None
    raw_status: str | None = None
    dry_run: bool | None = None
    mode: str | None = None
    publication_mode: str | None = None
    next_operator_action: str | None = None
    profile: ExecutionObserverProfile = field(default_factory=ExecutionObserverProfile)
    safety: ExecutionObservedSafety = field(default_factory=ExecutionObservedSafety)
    steps: tuple[ExecutionObservedStep, ...] = ()
    artifacts: tuple[ExecutionObservedArtifact, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


# -- JSON-safe serialization ----------------------------------------------


def to_observability_dict(value: Any) -> Any:
    """Return a recursively JSON-safe copy of an observability value.

    ``Path`` becomes ``str``; dataclasses become dicts; tuples/lists become
    lists; mappings become dicts; primitives pass through. Anything else is
    coerced to ``str`` so normalization never raises. The source object is never
    mutated.
    """

    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: to_observability_dict(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): to_observability_dict(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_observability_dict(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# -- shared mapping helpers ------------------------------------------------


def _observed_safety(source: Any) -> ExecutionObservedSafety:
    """Map a safety payload (mapping or object) onto observed safety flags.

    Only flags explicitly present in the source override the conservative
    defaults; the canonical field name wins over an alias when both appear.
    """

    if source is None:
        return ExecutionObservedSafety()

    kwargs: dict[str, bool] = {}
    for name in _SAFETY_FIELDS:
        if _has(source, name):
            kwargs[name] = bool(_read(source, name))
    for alias, dest in _SAFETY_ALIASES.items():
        if dest not in kwargs and _has(source, alias):
            kwargs[dest] = bool(_read(source, alias))
    return ExecutionObservedSafety(**kwargs)


def _observed_artifacts(raw: Any) -> tuple[ExecutionObservedArtifact, ...]:
    """Map a list- or mapping-shaped artifact payload onto observed artifacts."""

    refs: list[ExecutionObservedArtifact] = []
    if isinstance(raw, Mapping):
        for artifact_type, value in raw.items():
            if isinstance(value, Mapping):
                ref = _artifact_from_item(
                    value, default_type=str(artifact_type)
                )
            else:
                ref = _artifact_ref(artifact_type, value)
            if ref is not None:
                refs.append(ref)
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            ref = _artifact_from_item(item)
            if ref is not None:
                refs.append(ref)
    return tuple(refs)


def _artifact_from_item(
    item: Any, *, default_type: str | None = None
) -> ExecutionObservedArtifact | None:
    artifact_type = (
        _read(item, "artifact_type")
        or _read(item, "kind")
        or _read(item, "type")
        or default_type
    )
    path = _read(item, "path")
    description = _read(item, "description") or _read(item, "summary")
    return _artifact_ref(artifact_type, path, description)


def _artifact_ref(
    artifact_type: Any, path: Any, description: Any = None
) -> ExecutionObservedArtifact | None:
    if path is None:
        return None
    path_str = str(path).strip()
    if not path_str:
        return None
    type_str = _opt_str(artifact_type) or "artifact"
    return ExecutionObservedArtifact(
        artifact_type=type_str,
        path=path_str,
        description=_opt_str(description),
    )


def _observed_step(name: str, source: Any) -> ExecutionObservedStep:
    """Build one observed step from a section payload (boolean-gated status)."""

    status = _derive_step_status(source)
    return ExecutionObservedStep(
        name=name,
        status=status,
        summary=_opt_str(_read(source, "summary") or _read(source, "status")),
        metadata=_mapping_metadata(source),
    )


def _derive_step_status(source: Any) -> str:
    """Derive a step status from common boolean gates, conservatively."""

    if not isinstance(source, Mapping):
        return "unknown"
    gate_keys = ("ran", "started", "prepared")
    has_gate = any(key in source for key in gate_keys)
    ran = any(bool(source.get(key)) for key in gate_keys)
    ok = source.get("ok")
    if ok is False:
        return "failed"
    if has_gate and not ran:
        return "skipped"
    if ok is True or ran:
        return "passed"
    if "status" in source or "ok" in source:
        return "completed"
    return "unknown"


def _mapping_metadata(source: Any) -> dict[str, Any]:
    if isinstance(source, Mapping):
        return {str(key): to_observability_dict(value) for key, value in source.items()}
    return {}


def _profile(
    *,
    executor: Any = None,
    model: Any = None,
    provider: Any = None,
    tools: Any = None,
    validators: Any = None,
) -> ExecutionObserverProfile:
    return ExecutionObserverProfile(
        executor=_opt_str(executor),
        model=_opt_str(model),
        provider=_opt_str(provider),
        tools=_str_tuple(tools),
        validators=_str_tuple(validators),
    )


# -- summarizers -----------------------------------------------------------


def summarize_execution_engine_result(
    result: Any,
    source: str = SUMMARY_SOURCE_MANUAL_ENGINE_FACADE,
) -> UnifiedExecutionSummary:
    """Normalize an :class:`ExecutionEngineResult` into a unified summary."""

    status = _opt_str(_read(result, "status"))
    metadata: dict[str, Any] = {
        "result_type": RESULT_TYPE_EXECUTION_ENGINE_RESULT,
    }
    summary_text = _opt_str(_read(result, "summary"))
    if summary_text is not None:
        metadata["summary"] = summary_text
    result_metadata = _read(result, "metadata")
    if isinstance(result_metadata, Mapping) and result_metadata:
        metadata["result_metadata"] = to_observability_dict(result_metadata)

    dry_run: bool | None = None
    if isinstance(result_metadata, Mapping):
        runner_dry_run = result_metadata.get("runner_dry_run")
        if isinstance(runner_dry_run, bool):
            dry_run = runner_dry_run

    steps = tuple(
        ExecutionObservedStep(
            name=str(_read(step, "name", "step")),
            status=str(_read(step, "status", "unknown")),
            summary=_opt_str(_read(step, "summary")),
            metadata=_mapping_metadata(_read(step, "metadata") or {}),
        )
        for step in (_read(result, "steps") or ())
    )

    return UnifiedExecutionSummary(
        schema_version=EXECUTION_OBSERVABILITY_SUMMARY_SCHEMA_VERSION,
        source=source,
        ok=bool(_read(result, "ok", False)),
        task_key=_opt_str(_read(result, "task_key")),
        status=status,
        raw_status=status,
        dry_run=dry_run,
        next_operator_action=_opt_str(_read(result, "next_operator_action")),
        safety=_observed_safety(_read(result, "safety")),
        steps=steps,
        artifacts=_observed_artifacts(_read(result, "artifacts")),
        metadata=metadata,
    )


def summarize_approved_task_runner_payload(
    payload: Mapping[str, Any] | object,
    source: str = SUMMARY_SOURCE_APPROVED_TASK_RUNNER,
) -> UnifiedExecutionSummary:
    """Normalize an ``approved_task_runner`` result payload.

    Supports both mapping-like and attribute-like payloads.
    """

    status = _opt_str(_read(payload, "status") or _read(payload, "task_status"))
    metadata: dict[str, Any] = {
        "result_type": RESULT_TYPE_APPROVED_TASK_RUNNER_PAYLOAD,
    }
    for source_key, dest_key in (
        ("phase", "phase"),
        ("error", "error"),
    ):
        value = _read(payload, source_key, _MISSING)
        if value is not _MISSING and value is not None:
            metadata[dest_key] = to_observability_dict(value)

    dry_run_value = _read(payload, "dry_run", _MISSING)
    dry_run = bool(dry_run_value) if dry_run_value is not _MISSING else None

    return UnifiedExecutionSummary(
        schema_version=EXECUTION_OBSERVABILITY_SUMMARY_SCHEMA_VERSION,
        source=source,
        ok=bool(_read(payload, "ok", False)),
        task_key=_opt_str(_read(payload, "task_key")),
        status=status,
        raw_status=status,
        dry_run=dry_run,
        next_operator_action=_first_next_action(payload),
        profile=_profile(
            executor=_read(payload, "executor"),
            model=_read(payload, "model"),
            provider=_read(payload, "provider"),
            tools=_read(payload, "tools"),
            validators=_read(payload, "validators"),
        ),
        safety=_observed_safety(_read(payload, "safety")),
        steps=_approved_task_runner_steps(payload),
        artifacts=_observed_artifacts(_read(payload, "artifacts")),
        metadata=metadata,
    )


def summarize_scheduler_tick_payload(
    payload: Mapping[str, Any] | object,
) -> UnifiedExecutionSummary:
    """Normalize a scheduler tick JSON payload into a unified summary.

    The ``source`` is always ``scheduler_tick``.
    """

    status = _opt_str(_read(payload, "status"))
    runner_config = _read(payload, "runner_config")
    publication_config = _read(payload, "publication_config")

    metadata: dict[str, Any] = {
        "result_type": RESULT_TYPE_SCHEDULER_TICK_PAYLOAD,
    }
    for source_key in ("repo", "selected_issue", "lock", "reasons"):
        value = _read(payload, source_key, _MISSING)
        if value is not _MISSING and value is not None:
            metadata[source_key] = to_observability_dict(value)
    if isinstance(runner_config, Mapping):
        for source_key in ("worktree_root", "base_branch", "command", "configured"):
            if source_key in runner_config:
                metadata[f"runner_{source_key}"] = to_observability_dict(
                    runner_config[source_key]
                )

    publication_mode = None
    publication_only: bool | None = None
    next_action = _opt_str(_read(payload, "next_operator_action"))
    if isinstance(publication_config, Mapping):
        publication_mode = _opt_str(publication_config.get("mode"))
        if "publish_after_execution" in publication_config:
            publishes = bool(publication_config["publish_after_execution"])
            publication_only = not publishes
            metadata["publish_after_execution"] = publishes
        next_action = next_action or _opt_str(
            publication_config.get("next_operator_action")
        )

    safety = _observed_safety(_read(payload, "safety"))
    if publication_only is not None:
        # Scheduler ticks are execution-only unless publication is opted in.
        safety = ExecutionObservedSafety(
            **{
                **{f.name: getattr(safety, f.name) for f in fields(safety)},
                "execution_only": publication_only,
            }
        )

    return UnifiedExecutionSummary(
        schema_version=EXECUTION_OBSERVABILITY_SUMMARY_SCHEMA_VERSION,
        source=SUMMARY_SOURCE_SCHEDULER_TICK,
        ok=bool(_read(payload, "ok", False)),
        task_key=_opt_str(_read(payload, "selected_task_key")),
        status=status,
        raw_status=status,
        dry_run=_scheduler_dry_run(payload),
        mode=_opt_str(_read(payload, "mode")),
        publication_mode=publication_mode,
        next_operator_action=next_action,
        profile=_profile(
            executor=_read(runner_config, "executor"),
            model=_read(runner_config, "model"),
            provider=_read(runner_config, "provider"),
            tools=_read(runner_config, "tools"),
            validators=_read(runner_config, "validators"),
        ),
        safety=safety,
        metadata=metadata,
    )


def _first_next_action(payload: Any) -> str | None:
    actions = _read(payload, "next_allowed_actions")
    if isinstance(actions, (list, tuple)) and actions:
        return _opt_str(actions[0])
    return _opt_str(_read(payload, "next_operator_action"))


def _scheduler_dry_run(payload: Any) -> bool | None:
    safety = _read(payload, "safety")
    if isinstance(safety, Mapping) and "dry_run" in safety:
        return bool(safety["dry_run"])
    value = _read(payload, "dry_run", _MISSING)
    return bool(value) if value is not _MISSING else None


def _approved_task_runner_steps(payload: Any) -> tuple[ExecutionObservedStep, ...]:
    steps: list[ExecutionObservedStep] = []
    for name, key in (
        ("preflight", "preflight"),
        ("workspace", "workspace"),
        ("executor", "executor_run"),
    ):
        section = _read(payload, key, _MISSING)
        if section is not _MISSING and isinstance(section, Mapping):
            steps.append(_observed_step(name, section))

    validators = _read(payload, "validators", _MISSING)
    if isinstance(validators, (list, tuple)) and validators:
        steps.append(_validators_step(validators))

    status = _opt_str(_read(payload, "status") or _read(payload, "task_status"))
    if status:
        ok = bool(_read(payload, "ok", False))
        steps.append(
            ExecutionObservedStep(
                name="status_transition",
                status="completed" if ok else "blocked",
                summary=f"final status: {status}",
            )
        )
    return tuple(steps)


def _validators_step(items: Any) -> ExecutionObservedStep:
    results = list(items)
    failed = any(
        str(_read(item, "status", "")).lower() in {"failed", "blocked"}
        or _read(item, "ok") is False
        for item in results
    )
    if failed:
        status = "failed"
    elif results:
        status = "passed"
    else:
        status = "skipped"
    return ExecutionObservedStep(
        name="validators",
        status=status,
        summary=f"{len(results)} validator result(s)",
        metadata={"results": to_observability_dict(results)},
    )


__all__ = [
    "EXECUTION_OBSERVABILITY_SUMMARY_SCHEMA_VERSION",
    "ExecutionObservedArtifact",
    "ExecutionObservedSafety",
    "ExecutionObservedStep",
    "ExecutionObserverProfile",
    "RESULT_TYPE_APPROVED_TASK_RUNNER_PAYLOAD",
    "RESULT_TYPE_EXECUTION_ENGINE_RESULT",
    "RESULT_TYPE_SCHEDULER_TICK_PAYLOAD",
    "SUMMARY_SOURCES",
    "SUMMARY_SOURCE_APPROVED_TASK_RUNNER",
    "SUMMARY_SOURCE_MANUAL_ENGINE_FACADE",
    "SUMMARY_SOURCE_SCHEDULER_TICK",
    "SUMMARY_SOURCE_UNKNOWN",
    "UnifiedExecutionSummary",
    "summarize_approved_task_runner_payload",
    "summarize_execution_engine_result",
    "summarize_scheduler_tick_payload",
    "to_observability_dict",
]
