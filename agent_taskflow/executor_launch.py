"""Managed executor launch, process-group termination, and verified exit.

PR-7 deliberately isolates only canonical Attempt executor launches. Legacy
plain ``ExecutorContext`` callers retain their existing synchronous subprocess
behavior until a canonical runtime injects an ``ExecutorLaunchBinding``.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import time
from typing import Any, Mapping, Sequence, TextIO
from uuid import uuid4

from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.executor_process_schema import (
    ACTIVE_PROCESS_STATES,
    migrate_executor_process_lifecycle,
)
from agent_taskflow.lifecycle_control import RuntimeControlStore
from agent_taskflow.models import require_absolute_path, utc_now_iso
from agent_taskflow.store import connect, default_db_path
from agent_taskflow.tasks import normalize_task_key

_ROLE_REASON_SUFFIXES = frozenset(
    {
        "launch_allocated",
        "launch_preflight_failed",
        "process_start_failed",
        "process_started",
        "process_exited",
        "timeout",
        "descendant_cleanup",
        "process_sigterm_sent",
        "process_sigkill_sent",
        "process_exit_verified",
        "process_exit_unverified",
        "process_identity_mismatch",
    }
)
PROCESS_REASON_CODES = frozenset(
    {f"{role}_{suffix}" for role in ("executor", "validator") for suffix in _ROLE_REASON_SUFFIXES}
    | {"operator_kill_requested"}
)


def _validate_process_role(process_role: str) -> str:
    normalized = str(process_role).strip().lower()
    if normalized not in {"executor", "validator"}:
        raise ValueError(f"Invalid process_role: {process_role!r}")
    return normalized


def _role_reason(process_role: str, suffix: str) -> str:
    role = _validate_process_role(process_role)
    reason = f"{role}_{suffix}"
    if reason not in PROCESS_REASON_CODES:
        raise ValueError(f"Unknown runtime process reason_code: {reason!r}")
    return reason


def _role_label(process_role: str) -> str:
    return "validator" if _validate_process_role(process_role) == "validator" else "executor"

_DEAD_PROCESS_STATES = frozenset({"Z", "X", "x"})
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


class ExecutorLaunchError(RuntimeError):
    """Base error for managed executor launch failures."""


class ExecutorLaunchPreflightError(ExecutorLaunchError):
    """Raised only by callers that require an exception on preflight failure."""


class ProcessIdentityError(ExecutorLaunchError):
    """Raised when a stored PID/PGID cannot be proven to be the recorded process."""


@dataclass(frozen=True)
class ExecutorLaunchBinding:
    """Runtime ownership injected into an executor context by the canonical store."""

    db_path: Path
    attempt_id: str
    task_id: str
    task_key: str
    lease_id: str
    owner_id: str
    worktree_path: Path
    artifact_root: Path
    control_poll_seconds: float = 0.2

    def __post_init__(self) -> None:
        object.__setattr__(self, "db_path", require_absolute_path(self.db_path, "db_path"))
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        object.__setattr__(
            self,
            "worktree_path",
            require_absolute_path(self.worktree_path, "worktree_path"),
        )
        object.__setattr__(
            self,
            "artifact_root",
            require_absolute_path(self.artifact_root, "artifact_root"),
        )
        for field_name in ("attempt_id", "task_id", "lease_id", "owner_id"):
            value = str(getattr(self, field_name)).strip()
            if not value:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, value)
        interval = float(self.control_poll_seconds)
        if interval <= 0 or interval > 5:
            raise ValueError("control_poll_seconds must be in (0, 5]")
        object.__setattr__(self, "control_poll_seconds", interval)


@dataclass(frozen=True)
class ExecutorLaunchSpec:
    """Immutable, redacted description of one process-group launch."""

    executor_name: str
    argv: tuple[str, ...]
    cwd: Path
    artifact_dir: Path
    timeout_seconds: int | None
    stdin_mode: str
    combined_output: bool
    process_role: str = "executor"
    environment_mode: str = "inherit_with_overrides"
    environment_keys: tuple[str, ...] = ()
    redacted_arg_indexes: tuple[int, ...] = ()
    terminate_grace_seconds: float = 2.0
    kill_wait_seconds: float = 3.0

    def __post_init__(self) -> None:
        name = self.executor_name.strip()
        if not name:
            raise ValueError("executor_name must not be empty")
        object.__setattr__(self, "executor_name", name)
        argv = tuple(self.argv)
        if not argv or any(not isinstance(part, str) or not part for part in argv):
            raise ValueError("argv must be a non-empty tuple of non-empty strings")
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "process_role", _validate_process_role(self.process_role))
        object.__setattr__(self, "cwd", require_absolute_path(self.cwd, "cwd"))
        object.__setattr__(
            self,
            "artifact_dir",
            require_absolute_path(self.artifact_dir, "artifact_dir"),
        )
        if self.timeout_seconds is not None and int(self.timeout_seconds) <= 0:
            raise ValueError("timeout_seconds must be positive when provided")
        if self.stdin_mode not in {"devnull", "text"}:
            raise ValueError("stdin_mode must be devnull or text")
        if self.environment_mode != "inherit_with_overrides":
            raise ValueError("unsupported environment_mode")
        redacted = tuple(sorted(set(int(index) for index in self.redacted_arg_indexes)))
        if any(index < 0 or index >= len(argv) for index in redacted):
            raise ValueError("redacted_arg_indexes contains an out-of-range index")
        object.__setattr__(self, "redacted_arg_indexes", redacted)
        object.__setattr__(
            self,
            "environment_keys",
            tuple(sorted(set(str(key) for key in self.environment_keys))),
        )
        if float(self.terminate_grace_seconds) <= 0:
            raise ValueError("terminate_grace_seconds must be positive")
        if float(self.kill_wait_seconds) <= 0:
            raise ValueError("kill_wait_seconds must be positive")

    def redacted_argv(self) -> list[str]:
        hidden = set(self.redacted_arg_indexes)
        return ["<redacted>" if index in hidden else part for index, part in enumerate(self.argv)]

    def to_artifact(self, binding: ExecutorLaunchBinding) -> dict[str, Any]:
        redacted = self.redacted_argv()
        digest = hashlib.sha256(
            json.dumps(redacted, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return {
            "schema_version": (
                "validator_launch_spec.v1"
                if self.process_role == "validator"
                else "executor_launch_spec.v1"
            ),
            "process_role": self.process_role,
            "task_key": binding.task_key,
            "task_id": binding.task_id,
            "attempt_id": binding.attempt_id,
            "lease_id": binding.lease_id,
            "owner_id": binding.owner_id,
            "executor_name": self.executor_name,
            "argv": redacted,
            "argv_sha256": digest,
            "cwd": str(self.cwd),
            "artifact_dir": str(self.artifact_dir),
            "timeout_seconds": self.timeout_seconds,
            "stdin_mode": self.stdin_mode,
            "combined_output": self.combined_output,
            "shell": False,
            "start_new_session": True,
            "close_fds": True,
            "environment_mode": self.environment_mode,
            "environment_keys": list(self.environment_keys),
            "environment_values_logged": False,
            "network_isolation": False,
            "filesystem_isolation": "exact_attempt_worktree_and_artifact_binding",
            "terminate_grace_seconds": self.terminate_grace_seconds,
            "kill_wait_seconds": self.kill_wait_seconds,
        }


@dataclass(frozen=True)
class ExecutorLaunchPreflightResult:
    ok: bool
    blocking_errors: tuple[str, ...]
    warnings: tuple[str, ...]
    resolved_executable: str | None


@dataclass(frozen=True)
class ProcStat:
    pid: int
    state: str
    pgrp: int
    session_id: int
    start_ticks: int

    @property
    def live(self) -> bool:
        # Linux may add or expose less-common live states (for example K).
        # Fail closed: only documented dead/zombie states count as exited.
        return self.state not in _DEAD_PROCESS_STATES


@dataclass(frozen=True)
class ProcessGroupSnapshot:
    pgid: int
    session_id: int
    members: tuple[ProcStat, ...]

    @property
    def live_members(self) -> tuple[ProcStat, ...]:
        return tuple(member for member in self.members if member.live)

    @property
    def verified_exited(self) -> bool:
        return not self.live_members


@dataclass(frozen=True)
class ExecutorProcessRecord:
    process_id: str
    attempt_id: str
    task_id: str
    task_key: str
    lease_id: str
    owner_id: str
    executor_name: str
    process_role: str
    pid: int | None
    pgid: int | None
    session_id: int | None
    leader_start_ticks: int | None
    state: str
    started_at: str | None
    term_sent_at: str | None
    kill_sent_at: str | None
    exited_at: str | None
    exit_code: int | None
    termination_reason: str | None
    verified_exit: bool
    launch_spec_path: Path
    pid_manifest_path: Path
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ManagedProcessResult:
    process_id: str
    exit_code: int | None
    timed_out: bool
    kill_requested: bool
    start_error: str | None
    preflight_errors: tuple[str, ...]
    term_sent: bool
    kill_sent: bool
    verified_exit: bool
    termination_reason: str | None
    launch_spec_path: Path
    pid_manifest_path: Path
    stdout_path: Path
    stderr_path: Path | None


def _validate_process_reason(reason_code: str) -> str:
    normalized = str(reason_code).strip()
    if normalized not in PROCESS_REASON_CODES:
        raise ValueError(f"Unknown runtime process reason_code: {reason_code!r}")
    return normalized


def _safe_name(value: str) -> str:
    normalized = _SAFE_NAME.sub("-", value.strip()).strip(".-")
    return normalized or "executor"


def _row_to_record(row: sqlite3.Row) -> ExecutorProcessRecord:
    return ExecutorProcessRecord(
        process_id=row["process_id"],
        attempt_id=row["attempt_id"],
        task_id=row["task_id"],
        task_key=row["task_key"],
        lease_id=row["lease_id"],
        owner_id=row["owner_id"],
        executor_name=row["executor_name"],
        process_role=(row["process_role"] if "process_role" in row.keys() else "executor"),
        pid=row["pid"],
        pgid=row["pgid"],
        session_id=row["session_id"],
        leader_start_ticks=row["leader_start_ticks"],
        state=row["state"],
        started_at=row["started_at"],
        term_sent_at=row["term_sent_at"],
        kill_sent_at=row["kill_sent_at"],
        exited_at=row["exited_at"],
        exit_code=row["exit_code"],
        termination_reason=row["termination_reason"],
        verified_exit=bool(row["verified_exit"]),
        launch_spec_path=Path(row["launch_spec_path"]),
        pid_manifest_path=Path(row["pid_manifest_path"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ExecutorProcessStore:
    """Persistence and append-only audit operations for one executor process group."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = default_db_path() if db_path is None else require_absolute_path(db_path, "db_path")

    def init_db(self) -> None:
        migrate_executor_process_lifecycle(self.db_path)

    def get(self, process_id: str) -> ExecutorProcessRecord | None:
        self.init_db()
        with closing(connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM executor_processes WHERE process_id = ?",
                (process_id,),
            ).fetchone()
        return _row_to_record(row) if row is not None else None

    def active_for_attempt(self, attempt_id: str) -> ExecutorProcessRecord | None:
        self.init_db()
        with closing(connect(self.db_path)) as conn:
            placeholders = ",".join("?" for _ in ACTIVE_PROCESS_STATES)
            row = conn.execute(
                f"""
                SELECT * FROM executor_processes
                WHERE attempt_id = ? AND state IN ({placeholders})
                ORDER BY created_at DESC LIMIT 1
                """,
                (attempt_id, *ACTIVE_PROCESS_STATES),
            ).fetchone()
        return _row_to_record(row) if row is not None else None

    def list_active(self) -> list[ExecutorProcessRecord]:
        self.init_db()
        with closing(connect(self.db_path)) as conn:
            placeholders = ",".join("?" for _ in ACTIVE_PROCESS_STATES)
            rows = conn.execute(
                f"""
                SELECT * FROM executor_processes
                WHERE state IN ({placeholders})
                ORDER BY created_at
                """,
                ACTIVE_PROCESS_STATES,
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def create(
        self,
        *,
        process_id: str,
        binding: ExecutorLaunchBinding,
        executor_name: str,
        process_role: str,
        state: str,
        launch_spec_path: Path,
        pid_manifest_path: Path,
        reason_code: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ExecutorProcessRecord:
        self.init_db()
        reason = _validate_process_reason(reason_code)
        now = utc_now_iso()
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO executor_processes(
                    process_id, attempt_id, task_id, task_key, lease_id, owner_id,
                    executor_name, process_role, state, launch_spec_path, pid_manifest_path,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    process_id,
                    binding.attempt_id,
                    binding.task_id,
                    binding.task_key,
                    binding.lease_id,
                    binding.owner_id,
                    executor_name,
                    _validate_process_role(process_role),
                    state,
                    str(launch_spec_path),
                    str(pid_manifest_path),
                    now,
                    now,
                ),
            )
            self._insert_event(
                conn,
                process_id=process_id,
                attempt_id=binding.attempt_id,
                from_state=None,
                to_state=state,
                reason_code=reason,
                actor=binding.owner_id,
                timestamp=now,
                metadata=dict(metadata or {}),
            )
        record = self.get(process_id)
        assert record is not None
        return record

    def mark_running(
        self,
        process_id: str,
        *,
        pid: int,
        pgid: int,
        session_id: int,
        leader_start_ticks: int,
        actor: str,
    ) -> ExecutorProcessRecord:
        now = utc_now_iso()
        record = self.get(process_id)
        if record is None:
            raise KeyError(f"Runtime process not found: {process_id}")
        return self._transition(
            process_id,
            to_state="running",
            reason_code=_role_reason(record.process_role, "process_started"),
            actor=actor,
            updates={
                "pid": pid,
                "pgid": pgid,
                "session_id": session_id,
                "leader_start_ticks": leader_start_ticks,
                "started_at": now,
            },
            metadata={
                "pid": pid,
                "pgid": pgid,
                "session_id": session_id,
                "leader_start_ticks": leader_start_ticks,
            },
        )

    def mark_start_failed(
        self,
        process_id: str,
        *,
        actor: str,
        error: str,
    ) -> ExecutorProcessRecord:
        record = self.get(process_id)
        if record is None:
            raise KeyError(f"Runtime process not found: {process_id}")
        return self._transition(
            process_id,
            to_state="start_failed",
            reason_code=_role_reason(record.process_role, "process_start_failed"),
            actor=actor,
            updates={"termination_reason": "executor_process_start_failed"},
            metadata={"error": error},
        )

    def mark_signal(
        self,
        process_id: str,
        *,
        signal_name: str,
        actor: str,
        termination_reason: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ExecutorProcessRecord:
        if signal_name not in {"SIGTERM", "SIGKILL"}:
            raise ValueError("signal_name must be SIGTERM or SIGKILL")
        now = utc_now_iso()
        return self._transition(
            process_id,
            to_state="term_sent" if signal_name == "SIGTERM" else "kill_sent",
            reason_code=_role_reason(
                (self.get(process_id) or (_ for _ in ()).throw(
                    KeyError(f"Runtime process not found: {process_id}")
                )).process_role,
                "process_sigterm_sent" if signal_name == "SIGTERM" else "process_sigkill_sent",
            ),
            actor=actor,
            updates={
                "term_sent_at" if signal_name == "SIGTERM" else "kill_sent_at": now,
                "termination_reason": termination_reason,
            },
            metadata={"signal": signal_name, **dict(metadata or {})},
        )

    def finalize(
        self,
        process_id: str,
        *,
        actor: str,
        exit_code: int | None,
        verified_exit: bool,
        termination_reason: str | None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ExecutorProcessRecord:
        self.init_db()
        now = utc_now_iso()
        target = "exited" if verified_exit else "exit_unverified"
        existing = self.get(process_id)
        if existing is None:
            raise KeyError(f"Runtime process not found: {process_id}")
        reason = _role_reason(
            existing.process_role,
            "process_exit_verified" if verified_exit else "process_exit_unverified",
        )
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM executor_processes WHERE process_id = ?",
                (process_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Executor process not found: {process_id}")
            current = row["state"]
            event_metadata = {
                "exit_code": exit_code,
                "verified_exit": verified_exit,
                "termination_reason": termination_reason,
                **dict(metadata or {}),
            }
            if current == "exited":
                conn.execute(
                    """
                    UPDATE executor_processes
                    SET exit_code = COALESCE(?, exit_code),
                        termination_reason = COALESCE(?, termination_reason),
                        verified_exit = 1, updated_at = ?
                    WHERE process_id = ?
                    """,
                    (exit_code, termination_reason, now, process_id),
                )
            elif current == "exit_unverified" and verified_exit:
                cursor = conn.execute(
                    """
                    UPDATE executor_processes
                    SET state = 'exited', exited_at = COALESCE(exited_at, ?),
                        exit_code = COALESCE(?, exit_code),
                        termination_reason = COALESCE(?, termination_reason),
                        verified_exit = 1, updated_at = ?
                    WHERE process_id = ? AND state = 'exit_unverified'
                    """,
                    (now, exit_code, termination_reason, now, process_id),
                )
                if cursor.rowcount != 1:
                    raise ExecutorLaunchError(
                        f"Executor process state changed concurrently: {process_id}"
                    )
                self._insert_event(
                    conn,
                    process_id=process_id,
                    attempt_id=row["attempt_id"],
                    from_state="exit_unverified",
                    to_state="exited",
                    reason_code=reason,
                    actor=actor,
                    timestamp=now,
                    metadata=event_metadata,
                )
            elif current == "exit_unverified":
                conn.execute(
                    """
                    UPDATE executor_processes
                    SET exit_code = COALESCE(?, exit_code),
                        termination_reason = COALESCE(?, termination_reason),
                        updated_at = ?
                    WHERE process_id = ?
                    """,
                    (exit_code, termination_reason, now, process_id),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE executor_processes
                    SET state = ?, exited_at = ?, exit_code = ?,
                        termination_reason = ?, verified_exit = ?, updated_at = ?
                    WHERE process_id = ? AND state = ?
                    """,
                    (
                        target,
                        now,
                        exit_code,
                        termination_reason,
                        int(verified_exit),
                        now,
                        process_id,
                        current,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ExecutorLaunchError(
                        f"Executor process state changed concurrently: {process_id}"
                    )
                self._insert_event(
                    conn,
                    process_id=process_id,
                    attempt_id=row["attempt_id"],
                    from_state=current,
                    to_state=target,
                    reason_code=reason,
                    actor=actor,
                    timestamp=now,
                    metadata=event_metadata,
                )
        record = self.get(process_id)
        assert record is not None
        return record

    def record_identity_mismatch(
        self,
        process_id: str,
        *,
        actor: str,
        metadata: Mapping[str, Any],
    ) -> None:
        self.init_db()
        now = utc_now_iso()
        with closing(connect(self.db_path)) as conn, conn:
            row = conn.execute(
                "SELECT attempt_id, state FROM executor_processes WHERE process_id = ?",
                (process_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Executor process not found: {process_id}")
            self._insert_event(
                conn,
                process_id=process_id,
                attempt_id=row["attempt_id"],
                from_state=row["state"],
                to_state=row["state"],
                reason_code=_role_reason(
                    (self.get(process_id) or (_ for _ in ()).throw(
                        KeyError(f"Runtime process not found: {process_id}")
                    )).process_role,
                    "process_identity_mismatch",
                ),
                actor=actor,
                timestamp=now,
                metadata=dict(metadata),
            )

    def _transition(
        self,
        process_id: str,
        *,
        to_state: str,
        reason_code: str,
        actor: str,
        updates: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> ExecutorProcessRecord:
        self.init_db()
        reason = _validate_process_reason(reason_code)
        now = utc_now_iso()
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT attempt_id, state FROM executor_processes WHERE process_id = ?",
                (process_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Executor process not found: {process_id}")
            current = row["state"]
            assignments = ["state = ?", "updated_at = ?"]
            values: list[Any] = [to_state, now]
            for key, value in updates.items():
                if key not in {
                    "pid",
                    "pgid",
                    "session_id",
                    "leader_start_ticks",
                    "started_at",
                    "term_sent_at",
                    "kill_sent_at",
                    "termination_reason",
                }:
                    raise ValueError(f"Unsupported executor process update field: {key}")
                assignments.append(f"{key} = ?")
                values.append(value)
            values.extend([process_id, current])
            cursor = conn.execute(
                f"""
                UPDATE executor_processes SET {', '.join(assignments)}
                WHERE process_id = ? AND state = ?
                """,
                values,
            )
            if cursor.rowcount != 1:
                raise ExecutorLaunchError(
                    f"Executor process state changed concurrently: {process_id}"
                )
            self._insert_event(
                conn,
                process_id=process_id,
                attempt_id=row["attempt_id"],
                from_state=current,
                to_state=to_state,
                reason_code=reason,
                actor=actor,
                timestamp=now,
                metadata=dict(metadata),
            )
        record = self.get(process_id)
        assert record is not None
        return record

    @staticmethod
    def _insert_event(
        conn: sqlite3.Connection,
        *,
        process_id: str,
        attempt_id: str,
        from_state: str | None,
        to_state: str,
        reason_code: str,
        actor: str,
        timestamp: str,
        metadata: Mapping[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO executor_process_events(
                process_id, attempt_id, from_state, to_state, reason_code,
                actor, timestamp, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                process_id,
                attempt_id,
                from_state,
                to_state,
                _validate_process_reason(reason_code),
                actor,
                timestamp,
                json.dumps(dict(metadata), sort_keys=True),
            ),
        )


def check_executor_launch_preflight(
    binding: ExecutorLaunchBinding,
    spec: ExecutorLaunchSpec,
    *,
    run_env: Mapping[str, str] | None = None,
) -> ExecutorLaunchPreflightResult:
    """Validate ownership, exact paths, executable, and Linux process controls."""
    errors: list[str] = []
    warnings: list[str] = []
    role_label = _role_label(spec.process_role)
    if os.name != "posix" or not hasattr(os, "killpg"):
        errors.append(f"managed {role_label} process groups require POSIX os.killpg support")
    if not Path("/proc/self/stat").is_file():
        errors.append(f"managed {role_label} identity verification requires Linux /proc")
    if spec.cwd.resolve() != binding.worktree_path.resolve():
        errors.append("launch cwd does not match the active Attempt worktree")
    if spec.artifact_dir.resolve() != binding.artifact_root.resolve():
        errors.append("launch artifact_dir does not match the active Attempt artifact root")
    if not spec.cwd.is_dir():
        errors.append(f"launch cwd is not a directory: {spec.cwd}")
    if not spec.artifact_dir.is_dir():
        errors.append(f"launch artifact_dir is not a directory: {spec.artifact_dir}")

    executable = spec.argv[0]
    resolved_executable: str | None
    if Path(executable).is_absolute():
        candidate = Path(executable)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            errors.append(f"{role_label} binary is not an executable file: {candidate}")
            resolved_executable = None
        else:
            resolved_executable = str(candidate.resolve())
    else:
        search_path = (run_env or os.environ).get("PATH")
        resolved_executable = shutil.which(executable, path=search_path)
        if resolved_executable is None:
            errors.append(f"{role_label} binary was not found on PATH: {executable}")

    migrate_executor_process_lifecycle(binding.db_path)
    with closing(connect(binding.db_path)) as conn:
        row = conn.execute(
            """
            SELECT attempts.is_active, attempts.task_id, tasks.task_key,
                   runtime_leases.lease_id, runtime_leases.owner_id,
                   runtime_leases.is_active AS lease_active,
                   attempt_resources.worktree_path,
                   attempt_resources.artifact_root,
                   attempt_resources.status AS resource_status
            FROM attempts
            JOIN tasks ON tasks.task_id = attempts.task_id
            JOIN runtime_leases ON runtime_leases.attempt_id = attempts.attempt_id
            JOIN attempt_resources ON attempt_resources.attempt_id = attempts.attempt_id
            WHERE attempts.attempt_id = ?
            """,
            (binding.attempt_id,),
        ).fetchone()
        if row is None:
            errors.append("active Attempt ownership/resource record was not found")
        else:
            if not row["is_active"] or not row["lease_active"]:
                errors.append("Attempt or runtime lease is not active")
            if row["task_id"] != binding.task_id:
                errors.append("launch task_id does not match active Attempt")
            if row["task_key"] != binding.task_key:
                errors.append("launch task_key does not match active Attempt")
            if row["lease_id"] != binding.lease_id:
                errors.append("launch lease_id does not match active lease")
            if row["owner_id"] != binding.owner_id:
                errors.append("launch owner_id does not match active lease")
            if Path(row["worktree_path"]).resolve() != binding.worktree_path.resolve():
                errors.append("persisted Attempt worktree does not match launch binding")
            if Path(row["artifact_root"]).resolve() != binding.artifact_root.resolve():
                errors.append("persisted Attempt artifact root does not match launch binding")
            if row["resource_status"] != "active":
                errors.append("Attempt resource is not active")
        placeholders = ",".join("?" for _ in ACTIVE_PROCESS_STATES)
        active = conn.execute(
            f"""
            SELECT process_id FROM executor_processes
            WHERE attempt_id = ? AND state IN ({placeholders})
            LIMIT 1
            """,
            (binding.attempt_id, *ACTIVE_PROCESS_STATES),
        ).fetchone()
        if active is not None:
            errors.append(
                f"Attempt already has an active managed process: {active['process_id']}"
            )

    if spec.environment_mode == "inherit_with_overrides":
        warnings.append(
            f"environment inheritance is retained for {role_label} credentials; values are not persisted"
        )
    warnings.append("managed process launch does not provide network or container isolation")
    return ExecutorLaunchPreflightResult(
        ok=not errors,
        blocking_errors=tuple(errors),
        warnings=tuple(warnings),
        resolved_executable=resolved_executable,
    )


def _read_proc_stat(pid: int) -> ProcStat | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    fields = raw[close + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        return ProcStat(
            pid=pid,
            state=fields[0],
            pgrp=int(fields[2]),
            session_id=int(fields[3]),
            start_ticks=int(fields[19]),
        )
    except (ValueError, IndexError):
        return None


def inspect_process_group(pgid: int, session_id: int) -> ProcessGroupSnapshot:
    members: list[ProcStat] = []
    proc_root = Path("/proc")
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        stat = _read_proc_stat(int(entry.name))
        if stat is not None and stat.pgrp == pgid and stat.session_id == session_id:
            members.append(stat)
    return ProcessGroupSnapshot(
        pgid=pgid,
        session_id=session_id,
        members=tuple(sorted(members, key=lambda item: item.pid)),
    )


def _verify_record_identity(
    store: ExecutorProcessStore,
    record: ExecutorProcessRecord,
    *,
    actor: str,
) -> ProcessGroupSnapshot:
    if record.pid is None or record.pgid is None or record.session_id is None:
        raise ProcessIdentityError("executor process record has no PID/PGID/session identity")
    snapshot = inspect_process_group(record.pgid, record.session_id)
    leader = _read_proc_stat(record.pid)
    if leader is not None and record.leader_start_ticks is not None:
        if (
            leader.start_ticks != record.leader_start_ticks
            or leader.pgrp != record.pgid
            or leader.session_id != record.session_id
        ):
            metadata = {
                "stored_pid": record.pid,
                "stored_pgid": record.pgid,
                "stored_session_id": record.session_id,
                "stored_start_ticks": record.leader_start_ticks,
                "observed": asdict(leader),
            }
            store.record_identity_mismatch(
                record.process_id,
                actor=actor,
                metadata=metadata,
            )
            raise ProcessIdentityError(
                f"executor process identity mismatch for {record.process_id}"
            )
    return snapshot


def _wait_group_exit(pgid: int, session_id: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if inspect_process_group(pgid, session_id).verified_exited:
            return True
        time.sleep(0.05)
    return inspect_process_group(pgid, session_id).verified_exited


def terminate_registered_process(
    store: ExecutorProcessStore,
    record: ExecutorProcessRecord,
    *,
    actor: str,
    termination_reason: str,
    terminate_grace_seconds: float = 2.0,
    kill_wait_seconds: float = 3.0,
) -> ExecutorProcessRecord:
    """Signal one proven process group, escalate, and persist verified exit.

    The operation is intentionally idempotent. The in-process launcher and an
    external operator may observe the same kill request; each transition reloads
    the current state and tolerates the other actor winning the compare-and-set.
    """
    current = store.get(record.process_id)
    if current is None:
        raise KeyError(f"Executor process not found: {record.process_id}")
    if current.state == "exited":
        return current
    if current.state == "exit_unverified":
        if current.pgid is None or current.session_id is None:
            return current
        verified = inspect_process_group(
            current.pgid, current.session_id
        ).verified_exited
        return store.finalize(
            current.process_id,
            actor=actor,
            exit_code=current.exit_code,
            verified_exit=verified,
            termination_reason=termination_reason,
            metadata={"reconciled_after_unverified_exit": True},
        )
    if current.state not in ACTIVE_PROCESS_STATES:
        return current

    snapshot = _verify_record_identity(store, current, actor=actor)
    if snapshot.verified_exited:
        return store.finalize(
            current.process_id,
            actor=actor,
            exit_code=current.exit_code,
            verified_exit=True,
            termination_reason=termination_reason,
            metadata={"already_exited": True},
        )
    assert current.pgid is not None and current.session_id is not None
    pgid = current.pgid
    session_id = current.session_id

    if current.state == "allocated":
        raise ProcessIdentityError(
            "executor process is allocated but has no proven running identity"
        )
    if current.state == "running":
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            current = store.mark_signal(
                current.process_id,
                signal_name="SIGTERM",
                actor=actor,
                termination_reason=termination_reason,
                metadata={
                    "live_member_pids": [item.pid for item in snapshot.live_members]
                },
            )
        except ExecutorLaunchError:
            current = store.get(current.process_id) or current

    if _wait_group_exit(pgid, session_id, terminate_grace_seconds):
        return store.finalize(
            current.process_id,
            actor=actor,
            exit_code=current.exit_code,
            verified_exit=True,
            termination_reason=termination_reason,
            metadata={"escalated_to_sigkill": False},
        )

    current = store.get(current.process_id) or current
    if current.state == "exited":
        return current
    if current.state == "exit_unverified":
        return store.finalize(
            current.process_id,
            actor=actor,
            exit_code=current.exit_code,
            verified_exit=inspect_process_group(pgid, session_id).verified_exited,
            termination_reason=termination_reason,
            metadata={"reconciled_during_escalation": True},
        )
    if current.state == "term_sent":
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            current = store.mark_signal(
                current.process_id,
                signal_name="SIGKILL",
                actor=actor,
                termination_reason=termination_reason,
                metadata={
                    "live_member_pids": [
                        item.pid
                        for item in inspect_process_group(pgid, session_id).live_members
                    ]
                },
            )
        except ExecutorLaunchError:
            current = store.get(current.process_id) or current

    verified = _wait_group_exit(pgid, session_id, kill_wait_seconds)
    current = store.get(current.process_id) or current
    return store.finalize(
        current.process_id,
        actor=actor,
        exit_code=current.exit_code,
        verified_exit=verified,
        termination_reason=termination_reason,
        metadata={"escalated_to_sigkill": True},
    )


def _write_preamble(handle: TextIO, preamble: str | None) -> None:
    if preamble:
        handle.write(preamble)
        if not preamble.endswith("\n"):
            handle.write("\n")
        handle.flush()


def run_managed_process(
    binding: ExecutorLaunchBinding,
    spec: ExecutorLaunchSpec,
    *,
    stdout_path: str | Path,
    stderr_path: str | Path | None = None,
    stdin_text: str | None = None,
    run_env: Mapping[str, str] | None = None,
    preamble: str | None = None,
) -> ManagedProcessResult:
    """Launch a new session, poll controls, terminate the group, and verify exit."""
    stdout = require_absolute_path(stdout_path, "stdout_path")
    stderr = (
        require_absolute_path(stderr_path, "stderr_path")
        if stderr_path is not None
        else None
    )
    for path in (stdout, stderr):
        if path is None:
            continue
        try:
            path.resolve().relative_to(binding.artifact_root.resolve())
        except ValueError as exc:
            raise ExecutorLaunchError(
                f"executor output must stay inside Attempt artifact root: {path}"
            ) from exc
    binding.artifact_root.mkdir(parents=True, exist_ok=True)
    safe_executor = _safe_name(spec.executor_name)
    role_label = _role_label(spec.process_role)
    launch_spec_path = binding.artifact_root / f"{role_label}-launch-spec-{safe_executor}.json"
    pid_manifest_path = binding.artifact_root / f"{role_label}-process-{safe_executor}.pid.json"
    process_id = f"process-{uuid4().hex}"
    spec_payload = spec.to_artifact(binding)
    atomic_write_json(launch_spec_path, spec_payload, sort_keys=True)
    manifest_base = {
        "schema_version": f"{role_label}_process_pid.v1",
        "process_role": spec.process_role,
        "process_id": process_id,
        "attempt_id": binding.attempt_id,
        "task_key": binding.task_key,
        "lease_id": binding.lease_id,
        "owner_id": binding.owner_id,
        "executor_name": spec.executor_name,
        "pid": None,
        "pgid": None,
        "session_id": None,
        "leader_start_ticks": None,
        "state": "pending_preflight",
        "created_at": utc_now_iso(),
    }
    atomic_write_json(pid_manifest_path, manifest_base, sort_keys=True)

    store = ExecutorProcessStore(binding.db_path)
    preflight = check_executor_launch_preflight(binding, spec, run_env=run_env)
    if not preflight.ok:
        store.create(
            process_id=process_id,
            binding=binding,
            executor_name=spec.executor_name,
            process_role=spec.process_role,
            state="preflight_failed",
            launch_spec_path=launch_spec_path,
            pid_manifest_path=pid_manifest_path,
            reason_code=_role_reason(spec.process_role, "launch_preflight_failed"),
            metadata={
                "blocking_errors": list(preflight.blocking_errors),
                "warnings": list(preflight.warnings),
            },
        )
        atomic_write_json(
            pid_manifest_path,
            {
                **manifest_base,
                "state": "preflight_failed",
                "blocking_errors": list(preflight.blocking_errors),
            },
            sort_keys=True,
        )
        return ManagedProcessResult(
            process_id=process_id,
            exit_code=None,
            timed_out=False,
            kill_requested=False,
            start_error="; ".join(preflight.blocking_errors),
            preflight_errors=preflight.blocking_errors,
            term_sent=False,
            kill_sent=False,
            verified_exit=True,
            termination_reason=_role_reason(spec.process_role, "launch_preflight_failed"),
            launch_spec_path=launch_spec_path,
            pid_manifest_path=pid_manifest_path,
            stdout_path=stdout,
            stderr_path=stderr,
        )

    store.create(
        process_id=process_id,
        binding=binding,
        executor_name=spec.executor_name,
        process_role=spec.process_role,
        state="allocated",
        launch_spec_path=launch_spec_path,
        pid_manifest_path=pid_manifest_path,
        reason_code=_role_reason(spec.process_role, "launch_allocated"),
        metadata={
            "resolved_executable": preflight.resolved_executable,
            "warnings": list(preflight.warnings),
        },
    )

    stdout.parent.mkdir(parents=True, exist_ok=True)
    if stderr is not None:
        stderr.parent.mkdir(parents=True, exist_ok=True)
    stdout_handle: TextIO | None = None
    stderr_handle: TextIO | None = None
    process: subprocess.Popen[str] | None = None
    timed_out = False
    kill_requested = False
    termination_reason: str | None = None
    try:
        stdout_handle = stdout.open("w", encoding="utf-8")
        _write_preamble(stdout_handle, preamble)
        if spec.combined_output:
            stderr_target: Any = subprocess.STDOUT
        else:
            if stderr is None:
                raise ExecutorLaunchError("separate stderr output requires stderr_path")
            stderr_handle = stderr.open("w", encoding="utf-8")
            stderr_target = stderr_handle
        try:
            process = subprocess.Popen(
                list(spec.argv),
                cwd=spec.cwd,
                stdin=(subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL),
                stdout=stdout_handle,
                stderr=stderr_target,
                env=dict(run_env) if run_env is not None else None,
                text=True,
                shell=False,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            store.mark_start_failed(
                process_id,
                actor=binding.owner_id,
                error=f"{exc.__class__.__name__}: {exc}",
            )
            atomic_write_json(
                pid_manifest_path,
                {
                    **manifest_base,
                    "state": "start_failed",
                    "error": f"{exc.__class__.__name__}: {exc}",
                },
                sort_keys=True,
            )
            return ManagedProcessResult(
                process_id=process_id,
                exit_code=None,
                timed_out=False,
                kill_requested=False,
                start_error=str(exc),
                preflight_errors=(),
                term_sent=False,
                kill_sent=False,
                verified_exit=True,
                termination_reason=_role_reason(spec.process_role, "process_start_failed"),
                launch_spec_path=launch_spec_path,
                pid_manifest_path=pid_manifest_path,
                stdout_path=stdout,
                stderr_path=stderr,
            )

        pid = process.pid
        stat = _read_proc_stat(pid)
        if stat is None:
            try:
                process.terminate()
            finally:
                process.wait(timeout=spec.kill_wait_seconds)
            error = "could not read executor leader identity from /proc"
            store.mark_start_failed(process_id, actor=binding.owner_id, error=error)
            return ManagedProcessResult(
                process_id=process_id,
                exit_code=process.returncode,
                timed_out=False,
                kill_requested=False,
                start_error=error,
                preflight_errors=(),
                term_sent=False,
                kill_sent=False,
                verified_exit=True,
                termination_reason=_role_reason(spec.process_role, "process_start_failed"),
                launch_spec_path=launch_spec_path,
                pid_manifest_path=pid_manifest_path,
                stdout_path=stdout,
                stderr_path=stderr,
            )
        if stat.pgrp != pid or stat.session_id != pid:
            try:
                process.terminate()
            finally:
                process.wait(timeout=spec.kill_wait_seconds)
            error = (
                "executor launch did not create an isolated session/process group: "
                f"pid={pid} pgid={stat.pgrp} sid={stat.session_id}"
            )
            store.mark_start_failed(process_id, actor=binding.owner_id, error=error)
            return ManagedProcessResult(
                process_id=process_id,
                exit_code=process.returncode,
                timed_out=False,
                kill_requested=False,
                start_error=error,
                preflight_errors=(),
                term_sent=False,
                kill_sent=False,
                verified_exit=True,
                termination_reason="executor_process_start_failed",
                launch_spec_path=launch_spec_path,
                pid_manifest_path=pid_manifest_path,
                stdout_path=stdout,
                stderr_path=stderr,
            )

        store.mark_running(
            process_id,
            pid=pid,
            pgid=stat.pgrp,
            session_id=stat.session_id,
            leader_start_ticks=stat.start_ticks,
            actor=binding.owner_id,
        )
        atomic_write_json(
            pid_manifest_path,
            {
                "schema_version": f"{role_label}_process_pid.v1",
                "process_role": spec.process_role,
                "process_id": process_id,
                "attempt_id": binding.attempt_id,
                "task_key": binding.task_key,
                "lease_id": binding.lease_id,
                "owner_id": binding.owner_id,
                "executor_name": spec.executor_name,
                "pid": pid,
                "pgid": stat.pgrp,
                "session_id": stat.session_id,
                "leader_start_ticks": stat.start_ticks,
                "started_at": utc_now_iso(),
            },
            sort_keys=True,
        )
        if process.stdin is not None:
            try:
                process.stdin.write(stdin_text or "")
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        deadline = (
            time.monotonic() + spec.timeout_seconds
            if spec.timeout_seconds is not None
            else None
        )
        controls = RuntimeControlStore(binding.db_path)
        while process.poll() is None:
            control = controls.effective_control(
                task_key=binding.task_key,
                attempt_id=binding.attempt_id,
            )
            if control.kill_requested:
                kill_requested = True
                termination_reason = "operator_kill_requested"
                current = store.get(process_id)
                assert current is not None
                terminate_registered_process(
                    store,
                    current,
                    actor=binding.owner_id,
                    termination_reason=termination_reason,
                    terminate_grace_seconds=spec.terminate_grace_seconds,
                    kill_wait_seconds=spec.kill_wait_seconds,
                )
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                termination_reason = _role_reason(spec.process_role, "timeout")
                current = store.get(process_id)
                assert current is not None
                terminate_registered_process(
                    store,
                    current,
                    actor=binding.owner_id,
                    termination_reason=termination_reason,
                    terminate_grace_seconds=spec.terminate_grace_seconds,
                    kill_wait_seconds=spec.kill_wait_seconds,
                )
                break
            time.sleep(binding.control_poll_seconds)

        try:
            exit_code = process.wait(timeout=spec.kill_wait_seconds)
        except subprocess.TimeoutExpired:
            current = store.get(process_id)
            assert current is not None
            termination_reason = termination_reason or _role_reason(
                spec.process_role, "descendant_cleanup"
            )
            terminate_registered_process(
                store,
                current,
                actor=binding.owner_id,
                termination_reason=termination_reason,
                terminate_grace_seconds=spec.terminate_grace_seconds,
                kill_wait_seconds=spec.kill_wait_seconds,
            )
            exit_code = process.wait(timeout=spec.kill_wait_seconds)

        current = store.get(process_id)
        assert current is not None
        if current.pgid is not None and current.session_id is not None:
            snapshot = inspect_process_group(current.pgid, current.session_id)
            if not snapshot.verified_exited:
                termination_reason = termination_reason or _role_reason(
                    spec.process_role, "descendant_cleanup"
                )
                current = terminate_registered_process(
                    store,
                    current,
                    actor=binding.owner_id,
                    termination_reason=termination_reason,
                    terminate_grace_seconds=spec.terminate_grace_seconds,
                    kill_wait_seconds=spec.kill_wait_seconds,
                )
            else:
                current = store.finalize(
                    process_id,
                    actor=binding.owner_id,
                    exit_code=exit_code,
                    verified_exit=True,
                    termination_reason=termination_reason,
                    metadata={"leader_reaped": True},
                )
        else:
            current = store.finalize(
                process_id,
                actor=binding.owner_id,
                exit_code=exit_code,
                verified_exit=False,
                termination_reason=termination_reason,
            )
        current = store.finalize(
            process_id,
            actor=binding.owner_id,
            exit_code=exit_code,
            verified_exit=current.verified_exit,
            termination_reason=termination_reason,
            metadata={"leader_reaped": True},
        )
        return ManagedProcessResult(
            process_id=process_id,
            exit_code=exit_code,
            timed_out=timed_out,
            kill_requested=kill_requested,
            start_error=None,
            preflight_errors=(),
            term_sent=current.term_sent_at is not None,
            kill_sent=current.kill_sent_at is not None,
            verified_exit=current.verified_exit,
            termination_reason=current.termination_reason,
            launch_spec_path=launch_spec_path,
            pid_manifest_path=pid_manifest_path,
            stdout_path=stdout,
            stderr_path=stderr,
        )
    finally:
        if stdout_handle is not None:
            stdout_handle.close()
        if stderr_handle is not None:
            stderr_handle.close()


__all__ = [
    "ExecutorLaunchBinding",
    "ExecutorLaunchError",
    "ExecutorLaunchPreflightError",
    "ExecutorLaunchPreflightResult",
    "ExecutorLaunchSpec",
    "ExecutorProcessRecord",
    "ExecutorProcessStore",
    "ManagedProcessResult",
    "PROCESS_REASON_CODES",
    "ProcessIdentityError",
    "check_executor_launch_preflight",
    "inspect_process_group",
    "run_managed_process",
    "terminate_registered_process",
]
