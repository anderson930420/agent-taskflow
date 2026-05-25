"""Read-only scheduler proposal evidence readback.

This module exposes scheduler proposal evidence that was already recorded in
the local mirror. It never creates proposals, confirmations, verifier reports,
handoffs, runtime execution, executor runs, validator runs, approvals, merges,
cleanup records, background workers, or GitHub mutations.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_taskflow.scheduler_proposals import (
    PROPOSAL_ARTIFACT_TYPE,
    PROPOSAL_EVENT_TYPE,
    SCHEMA_VERSION as PROPOSAL_SCHEMA_VERSION,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key


READBACK_SCHEMA_VERSION = "scheduler_proposal_readback.v1"
READBACK_MODE = "read_only"
READBACK_NOTE = (
    "Scheduler proposal readback is read-only and is not execution permission. "
    "Proposal is not confirmation; human/operator confirmation remains required."
)

READBACK_SAFETY_FLAGS: dict[str, bool] = {
    "read_only": True,
    "proposal_created": False,
    "confirmation_created": False,
    "verifier_report_created": False,
    "handoff_created": False,
    "runtime_started": False,
    "approved_task_runner_called": False,
    "executor_started": False,
    "validators_started": False,
    "github_mutated": False,
    "approved": False,
    "merged": False,
    "cleanup_performed": False,
    "background_worker_started": False,
    "not_execution_permission": True,
    "not_confirmation": True,
    "not_handoff": True,
    "not_runtime": True,
}

READBACK_ITEM_SAFETY_FLAGS: dict[str, bool] = dict(READBACK_SAFETY_FLAGS)


class SchedulerProposalReadbackError(RuntimeError):
    """Raised when scheduler proposal readback cannot query stored evidence."""


def list_scheduler_proposal_readbacks(
    store: TaskMirrorStore,
    *,
    task_key: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Return read-only scheduler proposal evidence across known tasks."""

    normalized_task_key = _normalize_optional_task_key(task_key)
    _validate_limit(limit)

    try:
        task_keys = (
            [normalized_task_key]
            if normalized_task_key is not None
            else [task.task_key for task in store.list_tasks()]
        )
        items = [
            item
            for key in task_keys
            for item in _read_task_scheduler_proposal_items(store, key)
        ]
    except Exception as exc:
        raise SchedulerProposalReadbackError(
            f"could not read scheduler proposal evidence: {exc}"
        ) from exc

    items.sort(key=_sort_key)
    if limit is not None:
        items = items[:limit]

    return {
        "ok": True,
        "schema_version": READBACK_SCHEMA_VERSION,
        "mode": READBACK_MODE,
        "readback_note": READBACK_NOTE,
        "filters": {
            "task_key": normalized_task_key,
            "limit": limit,
        },
        "items": items,
        "count": len(items),
        "safety": dict(READBACK_SAFETY_FLAGS),
    }


def list_task_scheduler_proposal_readbacks(
    store: TaskMirrorStore,
    task_key: str,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """Return read-only scheduler proposal evidence for one task."""

    return list_scheduler_proposal_readbacks(
        store,
        task_key=task_key,
        limit=limit,
    )


def _read_task_scheduler_proposal_items(
    store: TaskMirrorStore,
    task_key: str,
) -> list[dict[str, Any]]:
    events = [
        event
        for event in store.list_task_events(task_key)
        if event.event_type == PROPOSAL_EVENT_TYPE
    ]
    artifacts = [
        artifact
        for artifact in store.list_task_artifacts(task_key)
        if artifact.artifact_type == PROPOSAL_ARTIFACT_TYPE
    ]
    artifacts_by_path = {str(artifact.path): artifact for artifact in artifacts}

    used_artifact_paths: set[str] = set()
    items: list[dict[str, Any]] = []

    for event in events:
        payload, warnings = _parse_event_payload(event.payload_json)
        artifact_path = _string_value(payload.get("artifact_path"))
        artifact = artifacts_by_path.get(artifact_path or "")
        artifact_created_at = artifact.created_at if artifact is not None else None

        artifact_payload: dict[str, Any] | None = None
        if artifact is not None:
            used_artifact_paths.add(str(artifact.path))
            artifact_payload, artifact_warnings = _read_artifact_payload(artifact.path)
            warnings.extend(artifact_warnings)
        elif artifact_path:
            artifact_payload, artifact_warnings = _read_artifact_payload(Path(artifact_path))
            warnings.append("artifact_row_missing")
            warnings.extend(artifact_warnings)

        artifact_item = _matching_artifact_item(
            artifact_payload,
            task_key=task_key,
            proposal_item_id=_string_value(payload.get("proposal_item_id")),
            item_hash=_string_value(payload.get("item_hash")),
        )

        items.append(
            _normalize_item(
                task_key=task_key,
                event_payload=payload,
                artifact_payload=artifact_payload,
                artifact_item=artifact_item,
                artifact_path=artifact_path,
                artifact_created_at=artifact_created_at,
                event_created_at=event.created_at,
                event_source=event.source,
                event_message=event.message,
                warnings=warnings,
            )
        )

    for artifact in artifacts:
        artifact_path = str(artifact.path)
        if artifact_path in used_artifact_paths:
            continue

        artifact_payload, warnings = _read_artifact_payload(artifact.path)
        artifact_items = _matching_artifact_items(artifact_payload, task_key=task_key)
        if not artifact_items:
            artifact_items = [None]

        for artifact_item in artifact_items:
            items.append(
                _normalize_item(
                    task_key=task_key,
                    event_payload={},
                    artifact_payload=artifact_payload,
                    artifact_item=artifact_item,
                    artifact_path=artifact_path,
                    artifact_created_at=artifact.created_at,
                    event_created_at=None,
                    event_source=None,
                    event_message=None,
                    warnings=list(warnings),
                )
            )

    return items


def _normalize_item(
    *,
    task_key: str,
    event_payload: Mapping[str, Any],
    artifact_payload: Mapping[str, Any] | None,
    artifact_item: Mapping[str, Any] | None,
    artifact_path: str | None,
    artifact_created_at: str | None,
    event_created_at: str | None,
    event_source: str | None,
    event_message: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    proposal_id = _first_string(
        event_payload.get("proposal_id"),
        artifact_payload.get("proposal_id") if artifact_payload else None,
    )
    proposal_hash = _first_string(
        event_payload.get("proposal_hash"),
        artifact_payload.get("proposal_hash") if artifact_payload else None,
    )
    proposal_item_id = _first_string(
        event_payload.get("proposal_item_id"),
        artifact_item.get("proposal_item_id") if artifact_item else None,
    )
    item_hash = _first_string(
        event_payload.get("item_hash"),
        artifact_item.get("item_hash") if artifact_item else None,
    )
    recommended_command_kind = _first_string(
        event_payload.get("recommended_command_kind"),
        artifact_item.get("recommended_command_kind") if artifact_item else None,
    )
    schema_version = _first_string(
        event_payload.get("schema_version"),
        artifact_payload.get("schema_version") if artifact_payload else None,
        PROPOSAL_SCHEMA_VERSION,
    )
    artifact_path = _first_string(
        artifact_path,
        artifact_payload.get("artifact_path") if artifact_payload else None,
    )

    missing_evidence = _missing_evidence(
        proposal_id=proposal_id,
        proposal_hash=proposal_hash,
        proposal_item_id=proposal_item_id,
        item_hash=item_hash,
        recommended_command_kind=recommended_command_kind,
        artifact_path=artifact_path,
        event_created_at=event_created_at,
        artifact_created_at=artifact_created_at,
    )

    return {
        "task_key": task_key,
        "proposal_id": proposal_id,
        "proposal_hash": proposal_hash,
        "proposal_item_id": proposal_item_id,
        "item_hash": item_hash,
        "recommended_command_kind": recommended_command_kind,
        "artifact_path": artifact_path,
        "artifact_created_at": artifact_created_at,
        "event_created_at": event_created_at,
        "event_source": event_source,
        "event_message": event_message,
        "schema_version": schema_version,
        "proposal_status": "recorded",
        "missing_evidence": missing_evidence,
        "readback_warnings": sorted(set(warnings)),
        "not_execution_permission": True,
        "not_confirmation": True,
        "requires_human_confirmation": True,
        "safety": dict(READBACK_ITEM_SAFETY_FLAGS),
    }


def _parse_event_payload(payload_json: str | None) -> tuple[dict[str, Any], list[str]]:
    if not payload_json:
        return {}, ["event_payload_missing"]
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return {}, ["event_payload_malformed"]
    if not isinstance(payload, dict):
        return {}, ["event_payload_not_object"]
    return payload, []


def _read_artifact_payload(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, ["artifact_file_missing"]
    except OSError:
        return None, ["artifact_file_unreadable"]

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, ["artifact_json_malformed"]

    if not isinstance(payload, dict):
        return None, ["artifact_json_not_object"]
    return payload, []


def _matching_artifact_item(
    artifact_payload: Mapping[str, Any] | None,
    *,
    task_key: str,
    proposal_item_id: str | None,
    item_hash: str | None,
) -> dict[str, Any] | None:
    for item in _matching_artifact_items(artifact_payload, task_key=task_key):
        if proposal_item_id and item.get("proposal_item_id") == proposal_item_id:
            return item
        if item_hash and item.get("item_hash") == item_hash:
            return item
    matches = _matching_artifact_items(artifact_payload, task_key=task_key)
    return matches[0] if len(matches) == 1 else None


def _matching_artifact_items(
    artifact_payload: Mapping[str, Any] | None,
    *,
    task_key: str,
) -> list[dict[str, Any]]:
    if not artifact_payload:
        return []
    raw_items = artifact_payload.get("items")
    if not isinstance(raw_items, list):
        return []
    return [
        item
        for item in raw_items
        if isinstance(item, dict) and item.get("task_key") == task_key
    ]


def _normalize_optional_task_key(task_key: str | None) -> str | None:
    if task_key is None:
        return None
    return normalize_task_key(task_key)


def _validate_limit(limit: int | None) -> None:
    if limit is not None and limit < 0:
        raise ValueError("limit must be zero or positive")


def _sort_key(item: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    created_at = _first_string(
        item.get("event_created_at"),
        item.get("artifact_created_at"),
        "",
    )
    return (
        created_at or "",
        _string_value(item.get("task_key")) or "",
        _string_value(item.get("proposal_id")) or "",
        _string_value(item.get("proposal_item_id")) or "",
        _string_value(item.get("artifact_path")) or "",
    )


def _missing_evidence(**values: str | None) -> list[str]:
    return [key for key, value in values.items() if not value]


def _first_string(*values: Any) -> str | None:
    for value in values:
        coerced = _string_value(value)
        if coerced:
            return coerced
    return None


def _string_value(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


__all__ = [
    "READBACK_ITEM_SAFETY_FLAGS",
    "READBACK_MODE",
    "READBACK_NOTE",
    "READBACK_SAFETY_FLAGS",
    "READBACK_SCHEMA_VERSION",
    "SchedulerProposalReadbackError",
    "list_scheduler_proposal_readbacks",
    "list_task_scheduler_proposal_readbacks",
]
