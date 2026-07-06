"""Operator-confirmed, audited reset of a blocked mirrored task.

This module only permits the local mirror transition ``blocked`` to ``queued``.
It does not invoke executors or validators and has no workspace, approval,
merge, or cleanup authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key


RESET_FROM_STATUS = "blocked"
RESET_TO_STATUS = "queued"
RESET_SOURCE = "reset_task_status_cli"
RESET_ARTIFACT_NAME = "task-status-reset.json"
RESET_ARTIFACT_TYPE = "other"


class TaskStatusResetError(RuntimeError):
    """Raised when a task reset cannot proceed safely."""


@dataclass(frozen=True)
class TaskStatusResetRequest:
    """Validated request for the single supported task status reset."""

    task_key: str
    from_status: str
    reason: str
    to_status: str = RESET_TO_STATUS
    db_path: Path | None = None
    confirm_reset: bool = False
    dry_run: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))

        normalized_from = self.from_status.strip().lower()
        if normalized_from != RESET_FROM_STATUS:
            raise ValueError(
                f"from_status must be {RESET_FROM_STATUS!r}, got {self.from_status!r}"
            )
        object.__setattr__(self, "from_status", normalized_from)

        normalized_to = self.to_status.strip().lower()
        if normalized_to != RESET_TO_STATUS:
            raise ValueError(
                f"to_status must be {RESET_TO_STATUS!r}, got {self.to_status!r}"
            )
        object.__setattr__(self, "to_status", normalized_to)

        normalized_reason = self.reason.strip()
        if not normalized_reason:
            raise ValueError("reason must not be empty")
        object.__setattr__(self, "reason", normalized_reason)

        if self.db_path is not None:
            object.__setattr__(
                self,
                "db_path",
                Path(self.db_path).expanduser().resolve(),
            )


@dataclass(frozen=True)
class TaskStatusResetResult:
    """Structured reset or dry-run result for CLI reporting and tests."""

    task_key: str
    from_status: str
    to_status: str
    reason: str
    dry_run: bool
    operator_confirmed: bool
    mutated: bool
    audit_artifact_path: Path | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "reason": self.reason,
            "dry_run": self.dry_run,
            "operator_confirmed": self.operator_confirmed,
            "mutated": self.mutated,
            "audit_artifact_path": (
                str(self.audit_artifact_path)
                if self.audit_artifact_path is not None
                else None
            ),
        }


def _audit_payload(request: TaskStatusResetRequest) -> dict[str, Any]:
    return {
        "kind": "task_status_reset",
        "task_key": request.task_key,
        "from_status": request.from_status,
        "to_status": request.to_status,
        "reason": request.reason,
        "dry_run": False,
        "operator_confirmed": True,
        "not_approval": True,
        "not_merge": True,
        "not_cleanup": True,
        "not_validation_authority": True,
    }


def reset_task_status(
    request: TaskStatusResetRequest,
    *,
    store: TaskMirrorStore | None = None,
) -> TaskStatusResetResult:
    """Preview or perform the audited ``blocked`` to ``queued`` reset."""

    current_store = store or TaskMirrorStore(request.db_path)
    task = current_store.get_task(request.task_key)
    if task is None:
        raise TaskStatusResetError(f"Task not found: {request.task_key}")
    if task.status != request.from_status:
        raise TaskStatusResetError(
            f"Task {request.task_key} status is {task.status!r}; "
            f"expected {request.from_status!r}"
        )

    if request.dry_run:
        return TaskStatusResetResult(
            task_key=request.task_key,
            from_status=request.from_status,
            to_status=request.to_status,
            reason=request.reason,
            dry_run=True,
            operator_confirmed=request.confirm_reset,
            mutated=False,
            audit_artifact_path=None,
        )

    if not request.confirm_reset:
        raise TaskStatusResetError(
            "Reset requires --confirm-reset unless --dry-run is used"
        )

    payload = _audit_payload(request)
    try:
        current_store.update_task_status(
            request.task_key,
            request.to_status,
            source=RESET_SOURCE,
            message=(
                f"Operator reset task status from {request.from_status} "
                f"to {request.to_status}: {request.reason}"
            ),
            expected_current_status=request.from_status,
        )
    except (KeyError, ValueError) as exc:
        raise TaskStatusResetError(str(exc)) from exc

    current_store.record_task_event(
        request.task_key,
        "note",
        RESET_SOURCE,
        message="Operator-confirmed task status reset recorded",
        payload=payload,
    )

    artifact_path: Path | None = None
    if task.artifact_dir is not None:
        artifact_path = task.artifact_dir / RESET_ARTIFACT_NAME
        atomic_write_json(artifact_path, payload, sort_keys=True)
        current_store.record_task_artifact(
            request.task_key,
            RESET_ARTIFACT_TYPE,
            artifact_path,
        )

    return TaskStatusResetResult(
        task_key=request.task_key,
        from_status=request.from_status,
        to_status=request.to_status,
        reason=request.reason,
        dry_run=False,
        operator_confirmed=True,
        mutated=True,
        audit_artifact_path=artifact_path,
    )
