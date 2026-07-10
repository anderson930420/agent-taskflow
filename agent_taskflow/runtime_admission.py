"""Atomic runtime pickup, execution ownership, lease, and heartbeat APIs."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
from typing import Any
from uuid import uuid4

from agent_taskflow.attempt_models import (
    ActiveAttemptExistsError,
    default_task_id,
    require_non_empty,
    validate_attempt_status,
)
from agent_taskflow.models import require_absolute_path, utc_now_iso, validate_task_status
from agent_taskflow.runtime_admission_schema import (
    DEFAULT_LEASE_TTL_SECONDS,
    RUNTIME_ADMISSION_MIGRATION,
    migrate_runtime_admission,
)
from agent_taskflow.store import connect, default_db_path
from agent_taskflow.tasks import normalize_task_key

__all__ = [
    "DEFAULT_LEASE_TTL_SECONDS",
    "RUNTIME_ADMISSION_MIGRATION",
    "LeaseExpiredError",
    "LeaseOwnershipError",
    "RuntimeAdmissionError",
    "RuntimeAdmissionStore",
    "RuntimeClaim",
    "RuntimeLeaseRecord",
    "migrate_runtime_admission",
]


class RuntimeAdmissionError(RuntimeError):
    """Base error for runtime admission and ownership failures."""


class LeaseOwnershipError(RuntimeAdmissionError):
    """Raised when an owner/token pair does not own the active lease."""


class LeaseExpiredError(RuntimeAdmissionError):
    """Raised when a lease is no longer live."""


@dataclass(frozen=True)
class RuntimeLeaseRecord:
    lease_id: str
    task_id: str
    attempt_id: str
    owner_id: str
    auth_mode: str
    ttl_seconds: int
    acquired_at: str
    heartbeat_at: str
    expires_at: str
    released_at: str | None
    release_reason: str | None
    is_active: bool


@dataclass(frozen=True)
class RuntimeClaim:
    task_key: str
    task_id: str
    attempt_id: str
    attempt_number: int
    lease_id: str
    owner_id: str
    lease_token: str
    acquired_at: str
    heartbeat_at: str
    expires_at: str


def _row_to_lease(row: sqlite3.Row) -> RuntimeLeaseRecord:
    return RuntimeLeaseRecord(
        lease_id=row["lease_id"],
        task_id=row["task_id"],
        attempt_id=row["attempt_id"],
        owner_id=row["owner_id"],
        auth_mode=row["auth_mode"],
        ttl_seconds=row["ttl_seconds"],
        acquired_at=row["acquired_at"],
        heartbeat_at=row["heartbeat_at"],
        expires_at=row["expires_at"],
        released_at=row["released_at"],
        release_reason=row["release_reason"],
        is_active=bool(row["is_active"]),
    )


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _expires_at(now: str, ttl_seconds: int) -> str:
    return _format_utc(_parse_utc(now) + timedelta(seconds=ttl_seconds))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class RuntimeAdmissionStore:
    """SQLite-backed runtime admission boundary.

    Explicit callers receive a secret lease token. Existing runtime entrypoints
    remain protected by database triggers: their transition to ``preparing``
    creates an implicit lease, and executor-start evidence is rejected unless a
    live lease is bound to the task's active Attempt.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = (
            default_db_path()
            if db_path is None
            else require_absolute_path(db_path, "db_path")
        )

    def init_db(self) -> None:
        migrate_runtime_admission(self.db_path)

    @staticmethod
    def _ensure_task_identity(
        conn: sqlite3.Connection,
        task_key: str,
    ) -> sqlite3.Row:
        normalized = normalize_task_key(task_key)
        row = conn.execute(
            """
            SELECT task_key, task_id, task_class, status, active_attempt_id,
                   executor, model, artifact_dir
            FROM tasks
            WHERE task_key = ?
            """,
            (normalized,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Task not found: {normalized}")
        if not row["task_id"]:
            conn.execute(
                """
                UPDATE tasks
                SET task_id = ?, task_class = 'legacy', is_legacy = 1
                WHERE task_key = ?
                """,
                (default_task_id(normalized), normalized),
            )
            row = conn.execute(
                """
                SELECT task_key, task_id, task_class, status, active_attempt_id,
                       executor, model, artifact_dir
                FROM tasks
                WHERE task_key = ?
                """,
                (normalized,),
            ).fetchone()
            assert row is not None
        return row

    @staticmethod
    def _insert_lifecycle_event(
        conn: sqlite3.Connection,
        *,
        task_id: str,
        attempt_id: str,
        from_status: str | None,
        to_status: str,
        reason_code: str,
        actor: str,
        timestamp: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO lifecycle_events(
                task_id, attempt_id, from_status, to_status,
                reason_code, actor, timestamp, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                attempt_id,
                from_status,
                require_non_empty(to_status, "to_status"),
                require_non_empty(reason_code, "reason_code"),
                require_non_empty(actor, "actor"),
                timestamp,
                json.dumps(metadata or {}, sort_keys=True),
            ),
        )

    @staticmethod
    def _insert_status_event(
        conn: sqlite3.Connection,
        *,
        task_key: str,
        status: str,
        source: str,
        message: str,
        created_at: str,
        blocked_reason: str | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO task_events(
                task_key, event_type, source, message, payload_json, created_at
            )
            VALUES (?, 'status_changed', ?, ?, ?, ?)
            """,
            (
                task_key,
                source,
                message,
                json.dumps(
                    {"status": status, "blocked_reason": blocked_reason},
                    sort_keys=True,
                ),
                created_at,
            ),
        )

    def claim(
        self,
        task_key: str,
        *,
        owner_id: str,
        ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        executor: str | None = None,
        model: str | None = None,
        base_commit: str | None = None,
        policy_version: str | None = None,
        config_snapshot_hash: str | None = None,
        prompt_template_version: str | None = None,
        permission_profile: str | None = None,
        worktree_path: str | Path | None = None,
        artifact_root: str | Path | None = None,
        reason_code: str = "runtime_pickup_claimed",
        metadata: dict[str, Any] | None = None,
    ) -> RuntimeClaim:
        """Atomically create an Attempt and token-authenticated execution lease."""
        self.init_db()
        normalized_key = normalize_task_key(task_key)
        normalized_owner = require_non_empty(owner_id, "owner_id")
        ttl = int(ttl_seconds)
        if ttl < 1:
            raise ValueError("ttl_seconds must be >= 1")
        normalized_worktree = (
            require_absolute_path(worktree_path, "worktree_path")
            if worktree_path is not None
            else None
        )
        normalized_artifact = (
            require_absolute_path(artifact_root, "artifact_root")
            if artifact_root is not None
            else None
        )
        now = utc_now_iso()
        expiry = _expires_at(now, ttl)
        attempt_id = f"attempt-{uuid4().hex}"
        lease_id = f"lease-{uuid4().hex}"
        token = secrets.token_urlsafe(32)
        fingerprint = _token_hash(token)

        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            task = self._ensure_task_identity(conn, normalized_key)
            if task["status"] not in {"queued", "blocked"}:
                raise RuntimeAdmissionError(
                    f"Task {normalized_key} is not claimable from status {task['status']}"
                )
            active = conn.execute(
                """
                SELECT attempt_id FROM attempts
                WHERE task_id = ? AND is_active = 1
                """,
                (task["task_id"],),
            ).fetchone()
            active_lease = conn.execute(
                """
                SELECT lease_id FROM runtime_leases
                WHERE task_id = ? AND is_active = 1
                """,
                (task["task_id"],),
            ).fetchone()
            if task["active_attempt_id"] or active or active_lease:
                raise ActiveAttemptExistsError(
                    f"Task {normalized_key} already has active runtime ownership"
                )
            attempt_number = conn.execute(
                """
                SELECT COALESCE(MAX(attempt_number), 0) + 1
                FROM attempts WHERE task_id = ?
                """,
                (task["task_id"],),
            ).fetchone()[0]

            conn.execute(
                """
                INSERT INTO runtime_claim_suppressions(task_id, operation, created_at)
                VALUES (?, 'explicit_claim', ?)
                """,
                (task["task_id"], now),
            )
            conn.execute(
                """
                INSERT INTO attempts(
                    attempt_id, task_id, attempt_number, status, is_active,
                    is_legacy, executor, model, base_commit, policy_version,
                    config_snapshot_hash, prompt_template_version,
                    permission_profile, worktree_path, artifact_root, started_at,
                    ended_at, execution_result, validation_result,
                    merge_recommendation, created_at, updated_at
                )
                VALUES (?, ?, ?, 'preparing', 1, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    attempt_id,
                    task["task_id"],
                    attempt_number,
                    executor or task["executor"],
                    model or task["model"],
                    base_commit,
                    policy_version,
                    config_snapshot_hash,
                    prompt_template_version,
                    permission_profile,
                    str(normalized_worktree) if normalized_worktree else None,
                    str(normalized_artifact)
                    if normalized_artifact
                    else task["artifact_dir"],
                    now,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO runtime_leases(
                    lease_id, task_id, attempt_id, owner_id, token_hash,
                    auth_mode, ttl_seconds, acquired_at, heartbeat_at,
                    expires_at, released_at, release_reason, is_active
                )
                VALUES (?, ?, ?, ?, ?, 'token', ?, ?, ?, ?, NULL, NULL, 1)
                """,
                (
                    lease_id,
                    task["task_id"],
                    attempt_id,
                    normalized_owner,
                    fingerprint,
                    ttl,
                    now,
                    now,
                    expiry,
                ),
            )
            conn.execute(
                """
                UPDATE tasks
                SET active_attempt_id = ?, status = 'preparing',
                    blocked_reason = NULL, updated_at = ?, last_synced_at = ?
                WHERE task_id = ?
                """,
                (attempt_id, now, now, task["task_id"]),
            )
            self._insert_lifecycle_event(
                conn,
                task_id=task["task_id"],
                attempt_id=attempt_id,
                from_status=task["status"],
                to_status="preparing",
                reason_code=reason_code,
                actor=normalized_owner,
                timestamp=now,
                metadata={"lease_id": lease_id, "auth_mode": "token", **(metadata or {})},
            )
            self._insert_status_event(
                conn,
                task_key=normalized_key,
                status="preparing",
                source=normalized_owner,
                message="Runtime admission claimed task",
                created_at=now,
            )
            conn.execute(
                "DELETE FROM runtime_claim_suppressions WHERE task_id = ?",
                (task["task_id"],),
            )

        return RuntimeClaim(
            task_key=normalized_key,
            task_id=task["task_id"],
            attempt_id=attempt_id,
            attempt_number=attempt_number,
            lease_id=lease_id,
            owner_id=normalized_owner,
            lease_token=token,
            acquired_at=now,
            heartbeat_at=now,
            expires_at=expiry,
        )

    @staticmethod
    def _verify_owned_lease(
        row: sqlite3.Row | None,
        *,
        owner_id: str,
        lease_token: str,
        now: str,
        allow_expired: bool,
    ) -> sqlite3.Row:
        if row is None or not row["is_active"]:
            raise LeaseOwnershipError("Active runtime lease not found")
        if row["auth_mode"] != "token":
            raise LeaseOwnershipError("Implicit compatibility lease has no owner token")
        if row["owner_id"] != owner_id or not hmac.compare_digest(
            row["token_hash"], _token_hash(lease_token)
        ):
            raise LeaseOwnershipError("Runtime lease owner/token mismatch")
        if not allow_expired and _parse_utc(row["expires_at"]) <= _parse_utc(now):
            raise LeaseExpiredError(f"Runtime lease expired at {row['expires_at']}")
        return row

    def heartbeat(
        self,
        attempt_id: str,
        *,
        owner_id: str,
        lease_token: str,
        ttl_seconds: int | None = None,
    ) -> RuntimeLeaseRecord:
        """Extend a token-authenticated lease and append heartbeat evidence."""
        self.init_db()
        owner = require_non_empty(owner_id, "owner_id")
        token = require_non_empty(lease_token, "lease_token")
        now = utc_now_iso()
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT runtime_leases.*, attempts.status AS attempt_status
                FROM runtime_leases
                JOIN attempts ON attempts.attempt_id = runtime_leases.attempt_id
                WHERE runtime_leases.attempt_id = ?
                  AND runtime_leases.is_active = 1
                """,
                (attempt_id,),
            ).fetchone()
            row = self._verify_owned_lease(
                row,
                owner_id=owner,
                lease_token=token,
                now=now,
                allow_expired=False,
            )
            ttl = row["ttl_seconds"] if ttl_seconds is None else int(ttl_seconds)
            if ttl < 1:
                raise ValueError("ttl_seconds must be >= 1")
            expiry = _expires_at(now, ttl)
            conn.execute(
                """
                UPDATE runtime_leases
                SET ttl_seconds = ?, heartbeat_at = ?, expires_at = ?
                WHERE lease_id = ? AND is_active = 1
                """,
                (ttl, now, expiry, row["lease_id"]),
            )
            conn.execute(
                "UPDATE attempts SET updated_at = ? WHERE attempt_id = ?",
                (now, attempt_id),
            )
            self._insert_lifecycle_event(
                conn,
                task_id=row["task_id"],
                attempt_id=attempt_id,
                from_status=row["attempt_status"],
                to_status=row["attempt_status"],
                reason_code="runtime_lease_heartbeat",
                actor=owner,
                timestamp=now,
                metadata={"lease_id": row["lease_id"], "expires_at": expiry},
            )
        lease = self.get_lease(row["lease_id"])
        assert lease is not None
        return lease

    def release(
        self,
        attempt_id: str,
        *,
        owner_id: str,
        lease_token: str,
        attempt_status: str,
        task_status: str,
        reason_code: str,
        execution_result: str | None = None,
        validation_result: str | None = None,
        merge_recommendation: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RuntimeLeaseRecord:
        """Close a token-authenticated Attempt and release execution ownership."""
        self.init_db()
        owner = require_non_empty(owner_id, "owner_id")
        token = require_non_empty(lease_token, "lease_token")
        final_attempt_status = validate_attempt_status(attempt_status)
        final_task_status = validate_task_status(task_status)
        now = utc_now_iso()
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT runtime_leases.*, attempts.status AS attempt_status,
                       tasks.task_key
                FROM runtime_leases
                JOIN attempts ON attempts.attempt_id = runtime_leases.attempt_id
                JOIN tasks ON tasks.task_id = runtime_leases.task_id
                WHERE runtime_leases.attempt_id = ?
                  AND runtime_leases.is_active = 1
                """,
                (attempt_id,),
            ).fetchone()
            row = self._verify_owned_lease(
                row,
                owner_id=owner,
                lease_token=token,
                now=now,
                allow_expired=True,
            )
            conn.execute(
                """
                INSERT INTO runtime_claim_suppressions(task_id, operation, created_at)
                VALUES (?, 'explicit_release', ?)
                """,
                (row["task_id"], now),
            )
            self._insert_lifecycle_event(
                conn,
                task_id=row["task_id"],
                attempt_id=attempt_id,
                from_status=row["attempt_status"],
                to_status=final_attempt_status,
                reason_code=reason_code,
                actor=owner,
                timestamp=now,
                metadata={"task_status": final_task_status, **(metadata or {})},
            )
            conn.execute(
                """
                UPDATE runtime_leases
                SET is_active = 0, released_at = ?, release_reason = ?
                WHERE lease_id = ? AND is_active = 1
                """,
                (now, reason_code, row["lease_id"]),
            )
            conn.execute(
                """
                UPDATE attempts
                SET status = ?, is_active = 0, ended_at = ?,
                    execution_result = ?, validation_result = ?,
                    merge_recommendation = ?, updated_at = ?
                WHERE attempt_id = ? AND is_active = 1
                """,
                (
                    final_attempt_status,
                    now,
                    execution_result,
                    validation_result,
                    merge_recommendation,
                    now,
                    attempt_id,
                ),
            )
            blocked_reason = reason_code if final_task_status == "blocked" else None
            conn.execute(
                """
                UPDATE tasks
                SET active_attempt_id = NULL, status = ?, blocked_reason = ?,
                    updated_at = ?, last_synced_at = ?
                WHERE task_id = ? AND active_attempt_id = ?
                """,
                (
                    final_task_status,
                    blocked_reason,
                    now,
                    now,
                    row["task_id"],
                    attempt_id,
                ),
            )
            self._insert_status_event(
                conn,
                task_key=row["task_key"],
                status=final_task_status,
                source=owner,
                message=f"Runtime admission released attempt: {reason_code}",
                created_at=now,
                blocked_reason=blocked_reason,
            )
            conn.execute(
                "DELETE FROM runtime_claim_suppressions WHERE task_id = ?",
                (row["task_id"],),
            )
        lease = self.get_lease(row["lease_id"])
        assert lease is not None
        return lease

    def expire_stale_leases(self) -> list[str]:
        """Abort expired active Attempts and move their tasks to ``blocked``."""
        self.init_db()
        now = utc_now_iso()
        expired_attempts: list[str] = []
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT runtime_leases.*, attempts.status AS attempt_status,
                       tasks.task_key
                FROM runtime_leases
                JOIN attempts ON attempts.attempt_id = runtime_leases.attempt_id
                JOIN tasks ON tasks.task_id = runtime_leases.task_id
                WHERE runtime_leases.is_active = 1
                  AND julianday(runtime_leases.expires_at) <= julianday(?)
                ORDER BY runtime_leases.expires_at ASC, runtime_leases.lease_id ASC
                """,
                (now,),
            ).fetchall()
            for row in rows:
                reason = "runtime_lease_expired"
                conn.execute(
                    """
                    INSERT INTO runtime_claim_suppressions(task_id, operation, created_at)
                    VALUES (?, 'lease_reaper', ?)
                    """,
                    (row["task_id"], now),
                )
                self._insert_lifecycle_event(
                    conn,
                    task_id=row["task_id"],
                    attempt_id=row["attempt_id"],
                    from_status=row["attempt_status"],
                    to_status="execution_aborted",
                    reason_code=reason,
                    actor="runtime_lease_reaper",
                    timestamp=now,
                    metadata={
                        "lease_id": row["lease_id"],
                        "owner_id": row["owner_id"],
                        "expired_at": row["expires_at"],
                    },
                )
                conn.execute(
                    """
                    UPDATE runtime_leases
                    SET is_active = 0, released_at = ?, release_reason = ?
                    WHERE lease_id = ? AND is_active = 1
                    """,
                    (now, reason, row["lease_id"]),
                )
                conn.execute(
                    """
                    UPDATE attempts
                    SET status = 'execution_aborted', is_active = 0,
                        ended_at = ?, execution_result = 'lease_expired',
                        updated_at = ?
                    WHERE attempt_id = ? AND is_active = 1
                    """,
                    (now, now, row["attempt_id"]),
                )
                conn.execute(
                    """
                    UPDATE tasks
                    SET active_attempt_id = NULL, status = 'blocked',
                        blocked_reason = ?, updated_at = ?, last_synced_at = ?
                    WHERE task_id = ? AND active_attempt_id = ?
                    """,
                    (reason, now, now, row["task_id"], row["attempt_id"]),
                )
                self._insert_status_event(
                    conn,
                    task_key=row["task_key"],
                    status="blocked",
                    source="runtime_lease_reaper",
                    message="Expired runtime lease was reaped",
                    created_at=now,
                    blocked_reason=reason,
                )
                conn.execute(
                    "DELETE FROM runtime_claim_suppressions WHERE task_id = ?",
                    (row["task_id"],),
                )
                expired_attempts.append(row["attempt_id"])
        return expired_attempts

    def get_lease(self, lease_id: str) -> RuntimeLeaseRecord | None:
        self.init_db()
        with closing(connect(self.db_path)) as conn, conn:
            row = conn.execute(
                "SELECT * FROM runtime_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
        return _row_to_lease(row) if row is not None else None

    def get_active_lease(self, task_key: str) -> RuntimeLeaseRecord | None:
        self.init_db()
        normalized = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn, conn:
            row = conn.execute(
                """
                SELECT runtime_leases.*
                FROM runtime_leases
                JOIN tasks ON tasks.task_id = runtime_leases.task_id
                WHERE tasks.task_key = ? AND runtime_leases.is_active = 1
                """,
                (normalized,),
            ).fetchone()
        return _row_to_lease(row) if row is not None else None

    def assert_executor_start_allowed(self, task_key: str) -> RuntimeLeaseRecord:
        """Return the live lease or fail closed before executor side effects."""
        lease = self.get_active_lease(task_key)
        if lease is None:
            raise RuntimeAdmissionError(
                f"Task {normalize_task_key(task_key)} has no active runtime lease"
            )
        if _parse_utc(lease.expires_at) <= _parse_utc(utc_now_iso()):
            raise LeaseExpiredError(f"Runtime lease expired at {lease.expires_at}")
        return lease
