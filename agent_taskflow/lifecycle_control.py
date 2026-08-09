"""PR-6 lifecycle graph, reason-code taxonomy, and persisted control switches."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any

from agent_taskflow.attempt_models import require_non_empty, validate_attempt_status
from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.lifecycle_control_schema import (
    ATTEMPT_TRANSITIONS,
    LIFECYCLE_CONTROL_MIGRATION,
    migrate_lifecycle_control,
)
from agent_taskflow.models import require_absolute_path, utc_now_iso, validate_task_status
from agent_taskflow.project_class_control_schema import (
    PROJECT_CLASS_CONTROLS_MIGRATION,
    migrate_project_class_controls,
)
from agent_taskflow.store import connect, default_db_path
from agent_taskflow.tasks import normalize_task_key

CONTROL_MODES = frozenset({"running", "paused", "kill_requested"})
CONTROL_SCOPES = frozenset(
    {"global", "project", "task_class", "task", "attempt"}
)
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
        "operator_task_class_governance_disabled",
        "operator_task_class_governance_cleared",
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
class RuntimeControlEventRecord:
    event_id: int
    scope_kind: str
    scope_id: str
    from_mode: str | None
    to_mode: str
    reason_code: str
    actor: str
    generation: int
    timestamp: str
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


@dataclass(frozen=True)
class TaskClassGovernanceDecision:
    """Class-level permission only; never an auto-merge eligibility claim."""

    task_key: str
    project: str
    task_class: str
    class_control_allows_auto_merge: bool
    matched_control: RuntimeControlRecord | None
    actual_auto_merge_enabled: bool = False


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


def normalize_project_control_id(project: str) -> str:
    """Normalize a persisted project identifier without task-key semantics."""
    return require_non_empty(project, "project")


def normalize_task_class_control_id(task_class: str) -> str:
    """Normalize a persisted task-class identifier without task-key semantics."""
    return require_non_empty(task_class, "task_class")


def _normalize_scope(scope_kind: str, scope_id: str | None) -> tuple[str, str]:
    kind = require_non_empty(scope_kind, "scope_kind").lower()
    if kind not in CONTROL_SCOPES:
        raise ValueError(f"Invalid runtime control scope: {scope_kind!r}")
    if kind == "global":
        return kind, "*"
    raw = require_non_empty(scope_id or "", "scope_id")
    if kind == "project":
        return kind, normalize_project_control_id(raw)
    if kind == "task_class":
        return kind, normalize_task_class_control_id(raw)
    if kind == "task":
        return kind, normalize_task_key(raw)
    return kind, raw


def _validate_scope_mode(scope_kind: str, mode: str) -> None:
    if scope_kind == "project" and mode == "kill_requested":
        raise ValueError(
            "project controls are admission pause/clear controls, not process kills"
        )
    if scope_kind == "task_class" and mode == "paused":
        raise ValueError(
            "task_class controls are governance disable/clear controls, not admission pauses"
        )


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


def _row_to_control_event(row: sqlite3.Row) -> RuntimeControlEventRecord:
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except json.JSONDecodeError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return RuntimeControlEventRecord(
        event_id=int(row["event_id"]),
        scope_kind=row["scope_kind"],
        scope_id=row["scope_id"],
        from_mode=row["from_mode"],
        to_mode=row["to_mode"],
        reason_code=row["reason_code"],
        actor=row["actor"],
        generation=int(row["generation"]),
        timestamp=row["timestamp"],
        metadata=metadata,
    )


def _task_identity_for_controls(
    conn: sqlite3.Connection,
    task_key: str,
):
    normalized = normalize_task_key(task_key)
    identity = AttemptStore.get_task_identity_in_connection(conn, normalized)
    if identity is None:
        raise KeyError(f"Canonical Task identity not found: {normalized}")
    normalize_project_control_id(identity.project)
    normalize_task_class_control_id(identity.task_class)
    return identity


def _effective_runtime_control_in_connection(
    conn: sqlite3.Connection,
    *,
    task_key: str | None = None,
    attempt_id: str | None = None,
) -> EffectiveRuntimeControl:
    scopes: list[tuple[str, str]] = [("global", "*")]
    if task_key is not None:
        normalized_task = normalize_task_key(task_key)
        identity = AttemptStore.get_task_identity_in_connection(conn, normalized_task)
        if identity is not None:
            scopes.append(
                ("project", normalize_project_control_id(identity.project))
            )
        # Historical prechecks can run before a Task exists. They still honor
        # global/task controls; the later atomic claim requires canonical Task
        # metadata before it can admit Level 2 work.
        scopes.append(("task", normalized_task))
    if attempt_id is not None:
        scopes.append(("attempt", require_non_empty(attempt_id, "attempt_id")))
    control_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runtime_controls'"
    ).fetchone()
    if control_table is None:
        # Historical PR-3 databases predate the optional lifecycle-control
        # plane. Their existing explicit-token behavior remains compatible.
        return EffectiveRuntimeControl(mode="running", matched_controls=())
    controls: list[RuntimeControlRecord] = []
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


class RuntimeControlStore:
    """Persisted runtime controls and task-class governance controls.

    Pause is admission-only: it denies new claims but does not suspend an active
    process. Kill is cooperative: active runtimes observe it at executor,
    validator, heartbeat, or status boundaries and close as execution_aborted.
    Project controls extend pause admission semantics. Task-class controls are
    a separate governance permission and never kill active work. The control
    plane intentionally does not send OS signals or terminate process groups.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = (
            default_db_path()
            if db_path is None
            else require_absolute_path(db_path, "db_path")
        )

    def init_db(self) -> None:
        migrate_project_class_controls(self.db_path)

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
        _validate_scope_mode(kind, normalized_mode)
        normalized_actor = require_non_empty(actor, "actor")
        normalized_reason = validate_reason_code(reason_code)
        if kind == "task_class":
            expected_reason = (
                "operator_task_class_governance_disabled"
                if normalized_mode == "kill_requested"
                else "operator_task_class_governance_cleared"
            )
            if normalized_reason != expected_reason:
                raise ValueError(
                    "task_class mode changes require task-class governance reason codes"
                )
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
        kind, _ = _normalize_scope(scope_kind, scope_id)
        if kind == "task_class":
            raise ValueError(
                "task_class uses disable_task_class_governance(), not process kill"
            )
        return self.set_control(
            "kill_requested",
            scope_kind=scope_kind,
            scope_id=scope_id,
            actor=actor,
            reason_code="operator_kill_requested",
            metadata=metadata,
        )

    def disable_task_class_governance(
        self,
        task_class: str,
        *,
        actor: str,
        metadata: dict[str, Any] | None = None,
    ) -> RuntimeControlRecord:
        """Disable future class-governed automation without killing execution."""
        return self.set_control(
            "kill_requested",
            scope_kind="task_class",
            scope_id=task_class,
            actor=actor,
            reason_code="operator_task_class_governance_disabled",
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
        if kind == "task_class":
            reason = "operator_task_class_governance_cleared"
        else:
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
        with closing(connect(self.db_path)) as conn:
            return _effective_runtime_control_in_connection(
                conn,
                task_key=task_key,
                attempt_id=attempt_id,
            )

    def assert_admission_allowed(
        self,
        task_key: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        control = (
            self.effective_control(task_key=task_key)
            if connection is None
            else _effective_runtime_control_in_connection(
                connection,
                task_key=task_key,
            )
        )
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

    def task_class_governance_permitted(self, task_class: str) -> bool:
        """Return the current class-global governance permission."""
        control = self.get_control(
            scope_kind="task_class",
            scope_id=normalize_task_class_control_id(task_class),
        )
        return control is None or control.mode == "running"

    def class_control_allows_auto_merge(
        self,
        task_key: str,
    ) -> TaskClassGovernanceDecision:
        """Evaluate only the class-governance term of future eligibility."""
        self.init_db()
        with closing(connect(self.db_path)) as conn:
            identity = _task_identity_for_controls(conn, task_key)
            row = conn.execute(
                """
                SELECT * FROM runtime_controls
                WHERE scope_kind = 'task_class' AND scope_id = ?
                """,
                (normalize_task_class_control_id(identity.task_class),),
            ).fetchone()
        control = _row_to_control(row) if row is not None else None
        allowed = control is None or control.mode == "running"
        return TaskClassGovernanceDecision(
            task_key=normalize_task_key(identity.task_key),
            project=normalize_project_control_id(identity.project),
            task_class=normalize_task_class_control_id(identity.task_class),
            class_control_allows_auto_merge=allowed,
            matched_control=control,
            actual_auto_merge_enabled=False,
        )

    def list_control_events(
        self,
        *,
        scope_kind: str,
        scope_id: str | None = None,
    ) -> list[RuntimeControlEventRecord]:
        kind, identifier = _normalize_scope(scope_kind, scope_id)
        self.init_db()
        with closing(connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT * FROM runtime_control_events
                WHERE scope_kind = ? AND scope_id = ?
                ORDER BY event_id
                """,
                (kind, identifier),
            ).fetchall()
        return [_row_to_control_event(row) for row in rows]


__all__ = [
    "ACTIVE_ATTEMPT_STATUSES",
    "CONTROL_MODES",
    "CONTROL_SCOPES",
    "EffectiveRuntimeControl",
    "LIFECYCLE_CONTROL_MIGRATION",
    "PROJECT_CLASS_CONTROLS_MIGRATION",
    "LifecycleTransitionError",
    "RUNTIME_REASON_CODES",
    "RuntimeControlError",
    "RuntimeControlRecord",
    "RuntimeControlEventRecord",
    "RuntimeControlStore",
    "RuntimeKillRequested",
    "RuntimePausedError",
    "TASK_STATUS_BY_ATTEMPT_STATUS",
    "TaskClassGovernanceDecision",
    "TERMINAL_ATTEMPT_STATUSES",
    "migrate_lifecycle_control",
    "migrate_project_class_controls",
    "normalize_project_control_id",
    "normalize_task_class_control_id",
    "task_status_for_attempt",
    "validate_attempt_transition",
    "validate_reason_code",
]
