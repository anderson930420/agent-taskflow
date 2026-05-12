"""JSON-safe serializers for the Agent Taskflow read-only API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agent_taskflow.models import TaskArtifactRecord, TaskRecord



class CreateTaskRequest(BaseModel):
    """Request body for creating a local mirrored task record."""

    task_key: str
    project: str
    repo_path: str
    worktree_path: str
    artifact_dir: str
    executor: str | None = None
    model: str | None = None
    validator: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    title: str | None = None
    board: str | None = None
    hermes_task_id: str | None = None
    branch: str | None = None
    base_branch: str | None = "main"


class StartTaskRequest(BaseModel):
    """Request body for dispatching a task through the dispatcher abstraction."""

    validators: list[str] | None = None
    executor: str | None = None
    model: str | None = None
    dry_run: bool = False


class ValidateTaskRequest(BaseModel):
    """Request body reserved for future validation-only dispatch."""

    validators: list[str] | None = None


class ApprovalRequest(BaseModel):
    """Request body for accepting a waiting task after human review."""

    decided_by: str
    notes: str | None = None


class RejectRequest(BaseModel):
    """Request body for rejecting a task after human review."""

    decided_by: str
    notes: str | None = None


class BlockTaskRequest(BaseModel):
    """Request body for manually blocking a task."""

    blocked_reason: str


class ActionResponse(BaseModel):
    """Stable action response envelope."""

    ok: bool
    action: str
    task_key: str | None = None
    status: str | None = None
    message: str
    item: dict[str, Any] | None = None


def action_response(
    *,
    ok: bool,
    action: str,
    message: str,
    task_key: str | None = None,
    status: str | None = None,
    item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return json_safe(
        {
            "ok": ok,
            "action": action,
            "task_key": task_key,
            "status": status,
            "message": message,
            "item": item or {},
        }
    )


def dispatcher_result_to_dict(result: Any) -> dict[str, Any]:
    return json_safe(
        {
            "task_key": result.task_key,
            "status": result.status,
            "summary": result.summary,
            "executor_status": result.executor_status,
            "validator_statuses": result.validator_statuses,
            "blocked_reason": result.blocked_reason,
        }
    )


_SENSITIVE_KEYS = {
    "env",
    "environment",
    "secret",
    "secrets",
    "token",
    "api_key",
    "access_token",
    "refresh_token",
    "password",
    "authorization",
}


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    if normalized in {"task_key"}:
        return False
    return normalized in _SENSITIVE_KEYS or normalized.endswith("_token")


def json_safe(value: Any) -> Any:
    """Convert internal values into JSON-safe values.

    Path values become strings. Sensitive dictionary keys are omitted.
    """
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(key): json_safe(item)
            for key, item in value.items()
            if not _is_sensitive_key(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def project_to_dict(record: dict[str, Any]) -> dict[str, Any]:
    return json_safe(record)


def task_to_dict(record: TaskRecord) -> dict[str, Any]:
    return json_safe(
        {
            "task_key": record.task_key,
            "project": record.project,
            "board": record.board,
            "hermes_task_id": record.hermes_task_id,
            "title": record.title,
            "status": record.status,
            "repo_path": record.repo_path,
            "artifact_dir": record.artifact_dir,
            "executor": record.executor,
            "model": record.model,
            "provider": record.provider,
            "tools": record.tools,
            "pi_bin": record.pi_bin,
            "blocked_reason": record.blocked_reason,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "last_synced_at": record.last_synced_at,
        }
    )


def artifact_to_dict(record: TaskArtifactRecord) -> dict[str, Any]:
    return json_safe(
        {
            "task_key": record.task_key,
            "artifact_type": record.artifact_type,
            "path": record.path,
            "created_at": record.created_at,
        }
    )


def executor_run_to_dict(record: dict[str, Any]) -> dict[str, Any]:
    return json_safe(record)


def validation_result_to_dict(record: dict[str, Any]) -> dict[str, Any]:
    return json_safe(record)


def approval_decision_to_dict(record: dict[str, Any]) -> dict[str, Any]:
    return json_safe(record)


def list_response(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": items, "count": len(items)}


def detail_response(item: dict[str, Any]) -> dict[str, Any]:
    return {"item": item}
