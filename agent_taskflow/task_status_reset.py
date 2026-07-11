"""Operator-confirmed, audited reset of a blocked mirrored task.

PR-8 turns ``blocked -> queued`` into an atomic retry reservation. The reset
creates and binds the next Attempt identity, records old/new lineage, and leaves
execution, validation, approval, merge, and cleanup to their own authorities.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.reset_lineage import (
    ResetCompareAndSetError,
    ResetLineageError,
    ResetLineageRecord,
    ResetLineageStore,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key

RESET_FROM_STATUS = "blocked"
RESET_TO_STATUS = "queued"
RESET_SOURCE = "reset_task_status_cli"
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
    actor: str = RESET_SOURCE
    request_id: str | None = None
    expected_reset_generation: int | None = None
    expected_old_attempt_id: str | None = None

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

        normalized_actor = self.actor.strip()
        if not normalized_actor:
            raise ValueError("actor must not be empty")
        object.__setattr__(self, "actor", normalized_actor)

        if self.request_id is not None:
            normalized_request = self.request_id.strip()
            if not normalized_request:
                raise ValueError("request_id must not be empty")
            object.__setattr__(self, "request_id", normalized_request)

        if self.expected_reset_generation is not None:
            generation = int(self.expected_reset_generation)
            if generation < 0:
                raise ValueError("expected_reset_generation must be >= 0")
            object.__setattr__(self, "expected_reset_generation", generation)

        if self.expected_old_attempt_id is not None:
            old_attempt_id = self.expected_old_attempt_id.strip()
            if not old_attempt_id:
                raise ValueError("expected_old_attempt_id must not be empty")
            object.__setattr__(self, "expected_old_attempt_id", old_attempt_id)

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
    artifact_error: str | None
    reset_id: str | None
    request_id: str | None
    old_attempt_id: str | None
    new_attempt_id: str | None
    expected_reset_generation: int
    committed_reset_generation: int | None
    next_attempt_number: int
    idempotent_replay: bool

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
            "artifact_error": self.artifact_error,
            "reset_id": self.reset_id,
            "request_id": self.request_id,
            "old_attempt_id": self.old_attempt_id,
            "new_attempt_id": self.new_attempt_id,
            "expected_reset_generation": self.expected_reset_generation,
            "committed_reset_generation": self.committed_reset_generation,
            "next_attempt_number": self.next_attempt_number,
            "idempotent_replay": self.idempotent_replay,
        }


def _audit_payload(
    request: TaskStatusResetRequest,
    lineage: ResetLineageRecord,
) -> dict[str, Any]:
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
        "reset_id": lineage.reset_id,
        "request_id": lineage.request_id,
        "old_attempt_id": lineage.old_attempt_id,
        "new_attempt_id": lineage.new_attempt_id,
        "expected_reset_generation": lineage.expected_generation,
        "committed_reset_generation": lineage.committed_generation,
        "reset_lineage_state": lineage.state,
        "new_attempt_number": lineage.metadata.get("new_attempt_number"),
    }


def reset_task_status(
    request: TaskStatusResetRequest,
    *,
    store: TaskMirrorStore | None = None,
) -> TaskStatusResetResult:
    """Preview or perform one atomic ``blocked`` to ``queued`` retry reset."""

    current_store = store or TaskMirrorStore(request.db_path)
    lineage_store = ResetLineageStore(current_store.db_path)
    preview = lineage_store.preview(request.task_key)
    if preview.current_status != request.from_status:
        raise TaskStatusResetError(
            f"Task {request.task_key} status is {preview.current_status!r}; "
            f"expected {request.from_status!r}"
        )
    if (
        request.expected_reset_generation is not None
        and preview.current_generation != request.expected_reset_generation
    ):
        raise TaskStatusResetError(
            f"Task {request.task_key} reset generation is "
            f"{preview.current_generation}; expected "
            f"{request.expected_reset_generation}"
        )
    if (
        request.expected_old_attempt_id is not None
        and preview.old_attempt_id != request.expected_old_attempt_id
    ):
        raise TaskStatusResetError(
            f"Task {request.task_key} latest Attempt is "
            f"{preview.old_attempt_id!r}; expected "
            f"{request.expected_old_attempt_id!r}"
        )

    expected_generation = (
        preview.current_generation
        if request.expected_reset_generation is None
        else request.expected_reset_generation
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
            artifact_error=None,
            reset_id=None,
            request_id=request.request_id,
            old_attempt_id=preview.old_attempt_id,
            new_attempt_id=None,
            expected_reset_generation=expected_generation,
            committed_reset_generation=None,
            next_attempt_number=preview.next_attempt_number,
            idempotent_replay=False,
        )

    if not request.confirm_reset:
        raise TaskStatusResetError(
            "Reset requires --confirm-reset unless --dry-run is used"
        )

    try:
        lineage, replay = lineage_store.reserve_retry(
            request.task_key,
            reason=request.reason,
            actor=request.actor,
            request_id=request.request_id,
            expected_generation=expected_generation,
            expected_old_attempt_id=request.expected_old_attempt_id,
            metadata={
                "not_approval": True,
                "not_merge": True,
                "not_cleanup": True,
                "not_validation_authority": True,
            },
        )
    except (ResetLineageError, ResetCompareAndSetError, KeyError, ValueError) as exc:
        raise TaskStatusResetError(str(exc)) from exc

    payload = _audit_payload(request, lineage)
    if not replay:
        current_store.record_task_event(
            request.task_key,
            "note",
            RESET_SOURCE,
            message="Operator-confirmed retry reset lineage recorded",
            payload=payload,
        )

    artifact_path = lineage_store.audit_artifact_path(lineage)
    artifact_error: str | None = None
    if artifact_path is not None:
        try:
            if not artifact_path.exists():
                atomic_write_json(artifact_path, payload, sort_keys=True)
            if not replay:
                current_store.record_task_artifact(
                    request.task_key,
                    RESET_ARTIFACT_TYPE,
                    artifact_path,
                )
        except OSError as exc:
            artifact_error = f"{exc.__class__.__name__}: {exc}"
            lineage_store.append_artifact_failure(
                lineage.reset_id,
                actor=request.actor,
                error=artifact_error,
            )
            artifact_path = None

    return TaskStatusResetResult(
        task_key=request.task_key,
        from_status=request.from_status,
        to_status=request.to_status,
        reason=request.reason,
        dry_run=False,
        operator_confirmed=True,
        mutated=not replay,
        audit_artifact_path=artifact_path,
        artifact_error=artifact_error,
        reset_id=lineage.reset_id,
        request_id=lineage.request_id,
        old_attempt_id=lineage.old_attempt_id,
        new_attempt_id=lineage.new_attempt_id,
        expected_reset_generation=lineage.expected_generation,
        committed_reset_generation=lineage.committed_generation,
        next_attempt_number=int(lineage.metadata.get("new_attempt_number", 1)),
        idempotent_replay=replay,
    )
