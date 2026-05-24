"""JSON-safe serializers for the Agent Taskflow read-only API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agent_taskflow.models import TaskArtifactRecord, TaskRecord

OPERATOR_CLI_DECIDED_BY = "operator_cli"
LEGACY_HUMAN_DECIDED_BY = "human"
OPERATOR_ATTESTED_DECIDED_BY_VALUES = frozenset(
    {OPERATOR_CLI_DECIDED_BY, LEGACY_HUMAN_DECIDED_BY}
)


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


class PrepareWorkspaceRequest(BaseModel):
    """Request body for explicit task workspace preparation."""

    base_branch: str = "main"
    branch: str | None = None
    worktree_root: str | None = None


class ApprovalRequest(BaseModel):
    """Request body for accepting a waiting task after operator review.

    ``decided_by`` is an operator attestation value, not an authenticated human
    identity. New API clients should use ``"operator_cli"``. The legacy
    ``"human"`` value remains accepted by the route guard for old clients and
    stored payload compatibility.
    """

    decided_by: str
    notes: str | None = None


class RejectRequest(BaseModel):
    """Request body for rejecting a task after operator review."""

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


def workspace_preparation_result_to_dict(result: Any) -> dict[str, Any]:
    return json_safe(
        {
            "task_key": result.task_key,
            "repo_path": result.repo_path,
            "worktree_path": result.worktree_path,
            "branch": result.branch,
            "base_branch": result.base_branch,
            "base_sha": result.base_sha,
            "status": result.status,
            "summary": result.summary,
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


class RuntimeAuditEventResponse(BaseModel):
    """Read-only runtime audit event surfaced from queued_task_handoff.

    Runtime audit events are observation only. They are not action evidence
    and are not validation authority. ``validation_result`` events remain
    the authoritative validator record.
    """

    id: int | None = None
    task_key: str
    created_at: str | None = None
    source: str | None = None
    message: str | None = None
    kind: str
    runtime_execution_id: str | None = None
    executor: str | None = None
    preflight_passed: bool | None = None
    package_verified: bool | None = None
    intake_runner_handoff_verified: bool | None = None
    expiration_still_valid: bool | None = None
    approved_task_runner_invoked: bool | None = None
    runner_returned: bool | None = None
    runner_ok: bool | None = None
    runner_status: str | None = None
    runner_phase: str | None = None
    final_status: str | None = None
    runner_error: str | None = None
    verifier_run_id: str | None = None
    verifier_report_path: str | None = None
    intake_runner_handoff_artifact_path: str | None = None
    proposal_hash: str | None = None
    proposal_item_id: str | None = None
    item_hash: str | None = None
    confirmation_id: str | None = None
    runtime_execution_artifact_path: str | None = None
    not_action_evidence: bool = True
    not_validation_authority: bool = True


def runtime_audit_event_to_dict(record: dict[str, Any]) -> dict[str, Any]:
    """Normalize a runtime audit event dict for JSON-safe API responses.

    Ensures the safety flags are present and true so the response always
    advertises that runtime audit evidence is not action evidence and not
    validation authority.
    """
    payload = json_safe(record)
    if not isinstance(payload, dict):
        return payload  # pragma: no cover - defensive
    payload.setdefault("not_action_evidence", True)
    payload.setdefault("not_validation_authority", True)
    # Coerce in case stored payload had explicit false; runtime audit
    # readback always presents the boundary truthfully regardless of
    # historical payload contents.
    payload["not_action_evidence"] = True
    payload["not_validation_authority"] = True
    return payload


def list_response(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": items, "count": len(items)}


def detail_response(item: dict[str, Any]) -> dict[str, Any]:
    return {"item": item}



class ArtifactPreviewResponse(BaseModel):
    """Response body for a single artifact file preview."""

    name: str
    content: str | None = None
    truncated: bool = False
    size_bytes: int = 0
    preview_reason: str | None = None


def artifact_preview_to_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Convert a build_artifact_preview result to a JSON-safe dict."""
    return json_safe(data)
