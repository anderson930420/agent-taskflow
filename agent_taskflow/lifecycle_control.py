"""PR-6 lifecycle graph, reason-code taxonomy, and persisted control switches."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any

from agent_taskflow.attempt_models import require_non_empty, validate_attempt_status
from agent_taskflow.lifecycle_control_schema import (
    ATTEMPT_TRANSITIONS,
    LIFECYCLE_CONTROL_MIGRATION,
    migrate_lifecycle_control,
)
from agent_taskflow.models import require_absolute_path, utc_now_iso, validate_task_status
from agent_taskflow.store import connect, default_db_path
from agent_taskflow.tasks import normalize_task_key

CONTROL_MODES = frozenset({"running", "paused", "kill_requested"})
CONTROL_SCOPES = frozenset({"global", "task", "attempt"})
ACTIVE_ATTEMPT_STATUSES = frozenset({"created", "preparing", "implementing", "validating"})
TERMINAL_ATTEMPT_STATUSES = frozenset(
    {
        "waiting_approval",
        "validation_failed",
        "execution_timeout",
        "execution_aborted",
        "blocked",
        "completed",
        "failed",
        "canceled",
    }
)

# Canonical machine-readable reason codes. Free-form explanation belongs in
# metadata/message fields, never in this identifier.
RUNTIME_REASON_CODES = frozenset(
    {
        "runtime_preparing",
        "runtime_implementing",
        "runtime_validating",
        "runtime_waiting_approval",
        "runtime_completed",
        "runtime_canceled",
        "executor_failed",
        "executor_timeout",
        "executor_aborted",
        "executor_blocked",
        "validator_failed",
        "validator_timeout",
        "validator_blocked",
        "operator_pause_requested",
        "operator_pause_cleared",
        "operator_kill_requested",
        "operator_kill_cleared",
        "runtime_lease_expired",
        "runtime_internal_error",
        "runtime_governance_blocked",
        "attempt_resource_allocation_failed",
    }
)

TASK_STATUS_BY_ATTEMPT_STATUS = {
    "preparing": "preparing",
    "implementing": "implementing",
    "validating": "validating",
    "waiting_approval": "waiting_approval",
    "validation_failed": "blocked",
    "execution_timeout": "blocked",
    "execution_aborted": "blocked",
    "blocked": "blocked",
    "failed": "blocked",
    "completed": "completed",
    "canceled": "canceled",
}


class LifecycleTransitionError(RuntimeError):
    """Raised when a requested status edge is not in the canonical graph."""


class RuntimeControlError(RuntimeError):
    """Base error for persisted pause/kill controls."""


class RuntimePausedError(RuntimeControlError):
    """Raised when admission is paused for the requested scope."""


class RuntimeKillRequested(RuntimeControlError):
    """Raised internally when a cooperative runtime boundary observes kill."""


@dataclass(frozen=True)
class RuntimeControlRecord:
    scope_kind: str
    scope_id: str
    mode: str
    reason_code: str
    requested_by: str
    requested_at: str
    generation: int
    metadata: dict[str, Any]


@dataclass(frozen=True)
class EffectiveRuntimeControl:
    mode: str
    matched_controls: tuple[RuntimeControlRecord, ...]

    @property
    def is_paused(self) -> bool:
        return self.mode == "paused"

    @property
    def kill_requested(self) -> bool:
        return self.mode == "kill_requested"


def validate_reason_code(reason_code: str) -> str:
    normalized = require_non_empty(reason_code, "reason_code")
    if normalized not in RUNTIME_REASON_CODES:
        raise ValueError(f"Unknown lifecycle reason_code: {reason_code!r}")
    return normalized


def validate_attempt_transition(from_status: str, to_status: str) -> tuple[str, str]:
    source = validate_attempt_status(from_status)
    target = validate_attempt_status(to_status)
    if source == target:
        return source, target
    if (source, target) not in ATTEMPT_TRANSITIONS:
        raise LifecycleTransitionError(
            f"Illegal Attempt lifecycle transition: {source} -> {target}"
        )
    return source, target


def task_status_for_attempt(attempt_status: str) -> str:
    normalized = validate_attempt_status(attempt_status)
    try:
        return TASK_STATUS_BY_ATTEMPT_STATUS[normalized]
    except KeyError as exc:
        raise LifecycleTransitionError(
            f"Attempt status has no canonical task projection: {normalized}"
        ) from exc


def _normalize_scope(scope_kind: str, scope_id: str | None) -> tuple[str, str]:
    kind = require_non_empty(scope_kind, "scope_kind").lower()
    if kind not in CONTROL_SCOPES:
        raise ValueError(f"Invalid runtime control scope: {scope_kind!r}")
    if kind == "global":
        return kind, "*"
    raw = require_non_empty(scope_id or "", "scope_id")
    return (kind, normalize_task_key(raw)) if kind == "task" else (kind, raw)


def _row_to_control(row: sqlite3.Row) -> RuntimeControlRecord:
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except json.JSONDecodeError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return RuntimeControlRecord(
        scope_kind=row["scope_kind"],
        scope_id=row["scope_id"],
        mode=row["mode"],
        reason_code=row["reason_code"],
        requested_by=row["requested_by"],
        requested_at=row["requested_at"],
        generation=int(row["generation"]),
        metadata=metadata,
    )


class RuntimeControlStore:
    """Persisted admission pause and cooperative kill controls.

    Pause is admission-only: it denies new claims but does not suspend an active
    process. Kill is cooperative: active runtimes observe it at executor,
    validator, heartbeat, or status boundaries and close as execution_aborted.
    PR-6 intentionally does not send OS signals or terminate process groups.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = (
            default_db_path()
            if db_path is None
            else require_absolute_path(db_path, "db_path")
        )

    def init_db(self) -> None:
        migrate_lifecycle_control(self.db_path)

    def set_control(
        self,
        mode: str,
        *,
        scope_kind: str = "global",
        scope_id: str | None = None,
        actor: str,
        reason_code: str,
        metadata: dict[str, Any] | None = None,
    ) -> RuntimeControlRecord:
        normalized_mode = require_non_empty(mode, "mode").lower()
        if normalized_mode not in CONTROL_MODES:
            raise ValueError(f"Invalid runtime control mode: {mode!r}")
        kind, identifier = _normalize_scope(scope_kind, scope_id)
        normalized_actor = require_non_empty(actor, "actor")
        normalized_reason = validate_reason_code(reason_code)
        now = utc_now_iso()
        self.init_db()
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                """
                SELECT * FROM runtime_controls
                WHERE scope_kind = ? AND scope_id = ?
                """,
                (kind, identifier),
            ).fetchone()
            generation = 1 if previous is None else int(previous["generation"]) + 1
            from_mode = previous["mode"] if previous is not None else None
            conn.execute(
                """
                INSERT INTO runtime_controls(
                    scope_kind, scope_id, mode, reason_code, requested_by,
                    requested_at, generation, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_kind, scope_id) DO UPDATE SET
                    mode = excluded.mode,
                    reason_code = excluded.reason_code,
                    requested_by = excluded.requested_by,
                    requested_at = excluded.requested_at,
                    generation = excluded.generation,
                    metadata_json = excluded.metadata_json
                """,
                (
                    kind,
                    identifier,
                    normalized_mode,
                    normalized_reason,
                    normalized_actor,
                    now,
                    generation,
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )
            conn.execute(
                """
                INSERT INTO runtime_control_events(
                    scope_kind, scope_id, from_mode, to_mode, reason_code,
                    actor, generation, timestamp, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    identifier,
                    from_mode,
                    normalized_mode,
                    normalized_reason,
                    normalized_actor,
                    generation,
                    now,
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )
        record = self.get_control(scope_kind=kind, scope_id=identifier)
        assert record is not None
        return record

    def pause(
        self,
        *,
        scope_kind: str = "global",
        scope_id: str | None = None,
        actor: str,
        metadata: dict[str, Any] | None = None,
    ) -> RuntimeControlRecord:
        return self.set_control(
            "paused",
            scope_kind=scope_kind,
            scope_id=scope_id,
            actor=actor,
            reason_code="operator_pause_requested",
            metadata=metadata,
        )

    def request_kill(
        self,
        *,
        scope_kind: str = "global",
        scope_id: str | None = None,
        actor: str,
        metadata: dict[str, Any] | None = None,
    ) -> RuntimeControlRecord:
        return self.set_control(
            "kill_requested",
            scope_kind=scope_kind,
            scope_id=scope_id,
            actor=actor,
            reason_code="operator_kill_requested",
            metadata=metadata,
        )

    def clear(
        self,
        *,
        scope_kind: str = "global",
        scope_id: str | None = None,
        actor: str,
        metadata: dict[str, Any] | None = None,
    ) -> RuntimeControlRecord:
        kind, identifier = _normalize_scope(scope_kind, scope_id)
        previous = self.get_control(scope_kind=kind, scope_id=identifier)
        reason = (
            "operator_kill_cleared"
            if previous is not None and previous.mode == "kill_requested"
            else "operator_pause_cleared"
        )
        return self.set_control(
            "running",
            scope_kind=kind,
            scope_id=identifier,
            actor=actor,
            reason_code=reason,
            metadata=metadata,
        )

    def get_control(
        self,
        *,
        scope_kind: str = "global",
        scope_id: str | None = None,
    ) -> RuntimeControlRecord | None:
        kind, identifier = _normalize_scope(scope_kind, scope_id)
        self.init_db()
        with closing(connect(self.db_path)) as conn:
            row = conn.execute(
                """
                SELECT * FROM runtime_controls
                WHERE scope_kind = ? AND scope_id = ?
                """,
                (kind, identifier),
            ).fetchone()
        return _row_to_control(row) if row is not None else None

    def effective_control(
        self,
        *,
        task_key: str | None = None,
        attempt_id: str | None = None,
    ) -> EffectiveRuntimeControl:
        self.init_db()
        scopes: list[tuple[str, str]] = [("global", "*")]
        if task_key is not None:
            scopes.append(("task", normalize_task_key(task_key)))
        if attempt_id is not None:
            scopes.append(("attempt", require_non_empty(attempt_id, "attempt_id")))
        controls: list[RuntimeControlRecord] = []
        with closing(connect(self.db_path)) as conn:
            for kind, identifier in scopes:
                row = conn.execute(
                    """
                    SELECT * FROM runtime_controls
                    WHERE scope_kind = ? AND scope_id = ?
                    """,
                    (kind, identifier),
                ).fetchone()
                if row is not None:
                    controls.append(_row_to_control(row))
        active = tuple(record for record in controls if record.mode != "running")
        mode = (
            "kill_requested"
            if any(record.mode == "kill_requested" for record in active)
            else ("paused" if any(record.mode == "paused" for record in active) else "running")
        )
        return EffectiveRuntimeControl(mode=mode, matched_controls=active)

    def assert_admission_allowed(self, task_key: str) -> None:
        control = self.effective_control(task_key=task_key)
        if control.kill_requested:
            raise RuntimeKillRequested(
                f"Runtime admission denied by kill switch for {normalize_task_key(task_key)}"
            )
        if control.is_paused:
            raise RuntimePausedError(
                f"Runtime admission paused for {normalize_task_key(task_key)}"
            )

    def assert_not_killed(self, task_key: str, attempt_id: str | None = None) -> None:
        control = self.effective_control(task_key=task_key, attempt_id=attempt_id)
        if control.kill_requested:
            raise RuntimeKillRequested(
                f"Operator kill requested for {normalize_task_key(task_key)}"
            )


__all__ = [
    "ACTIVE_ATTEMPT_STATUSES",
    "CONTROL_MODES",
    "CONTROL_SCOPES",
    "EffectiveRuntimeControl",
    "LIFECYCLE_CONTROL_MIGRATION",
    "LifecycleTransitionError",
    "RUNTIME_REASON_CODES",
    "RuntimeControlError",
    "RuntimeControlRecord",
    "RuntimeControlStore",
    "RuntimeKillRequested",
    "RuntimePausedError",
    "TASK_STATUS_BY_ATTEMPT_STATUS",
    "TERMINAL_ATTEMPT_STATUSES",
    "migrate_lifecycle_control",
    "task_status_for_attempt",
    "validate_attempt_transition",
    "validate_reason_code",
]
