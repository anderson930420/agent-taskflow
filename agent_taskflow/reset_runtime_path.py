"""Install PR-8 reset-reserved Attempt adoption on canonical runtime paths."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
from types import ModuleType
from typing import Any
from uuid import uuid4

import agent_taskflow.attempt_scoped_runtime_path as attempt_path
import agent_taskflow.canonical_runtime_path as canonical_path
from agent_taskflow.executor_process_runtime_path import ExecutorProcessRuntimeTaskStore
from agent_taskflow.lifecycle_control import RuntimeControlStore
from agent_taskflow.models import require_absolute_path, utc_now_iso
from agent_taskflow.reset_lineage import ResetLineageStore
from agent_taskflow.reset_lineage_schema import migrate_reset_lineage
from agent_taskflow.runtime_admission import (
    DEFAULT_LEASE_TTL_SECONDS,
    ActiveAttemptExistsError,
    RuntimeClaim,
)
from agent_taskflow.store import connect
from agent_taskflow.tasks import normalize_task_key


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


class ResetAwareRuntimeAdmissionStore(canonical_path.CanonicalRuntimeAdmissionStore):
    """Canonical admission that consumes one reset-reserved Attempt exactly once."""

    def init_db(self) -> None:
        migrate_reset_lineage(self.db_path)

    def _try_claim_reserved_retry(
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
    ) -> RuntimeClaim | None:
        normalized = normalize_task_key(task_key)
        normalized_owner = owner_id.strip()
        if not normalized_owner:
            raise ValueError("owner_id must not be empty")
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
        lease_id = f"lease-{uuid4().hex}"
        token = secrets.token_urlsafe(32)
        fingerprint = _token_hash(token)

        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT tasks.task_id, tasks.task_key, tasks.status AS task_status,
                       tasks.active_attempt_id, tasks.executor AS task_executor,
                       tasks.model AS task_model, tasks.artifact_dir,
                       attempts.attempt_id, attempts.attempt_number,
                       attempts.status AS attempt_status, attempts.is_active,
                       reset_lineages.reset_id, reset_lineages.old_attempt_id,
                       reset_lineages.committed_generation,
                       reset_lineages.state AS reset_state
                FROM tasks
                JOIN attempts
                  ON attempts.attempt_id = tasks.active_attempt_id
                 AND attempts.task_id = tasks.task_id
                JOIN reset_lineages
                  ON reset_lineages.new_attempt_id = attempts.attempt_id
                 AND reset_lineages.task_id = tasks.task_id
                WHERE tasks.task_key = ?
                  AND tasks.status = 'queued'
                  AND attempts.status = 'created'
                  AND attempts.is_active = 1
                  AND reset_lineages.state = 'reserved'
                """,
                (normalized,),
            ).fetchone()
            if row is None:
                return None

            active_lease = conn.execute(
                """
                SELECT lease_id FROM runtime_leases
                WHERE task_id = ? AND is_active = 1
                """,
                (row["task_id"],),
            ).fetchone()
            if active_lease is not None:
                raise ActiveAttemptExistsError(
                    f"Task {normalized} already has active runtime lease "
                    f"{active_lease['lease_id']}"
                )

            conn.execute(
                """
                INSERT INTO runtime_claim_suppressions(task_id, operation, created_at)
                VALUES (?, 'explicit_claim', ?)
                """,
                (row["task_id"], now),
            )
            cursor = conn.execute(
                """
                UPDATE attempts
                SET status = 'preparing',
                    executor = COALESCE(?, executor),
                    model = COALESCE(?, model),
                    base_commit = COALESCE(?, base_commit),
                    policy_version = COALESCE(?, policy_version),
                    config_snapshot_hash = COALESCE(?, config_snapshot_hash),
                    prompt_template_version = COALESCE(?, prompt_template_version),
                    permission_profile = COALESCE(?, permission_profile),
                    worktree_path = COALESCE(?, worktree_path),
                    artifact_root = COALESCE(?, artifact_root),
                    started_at = COALESCE(started_at, ?),
                    updated_at = ?
                WHERE attempt_id = ? AND status = 'created' AND is_active = 1
                """,
                (
                    executor or row["task_executor"],
                    model or row["task_model"],
                    base_commit,
                    policy_version,
                    config_snapshot_hash,
                    prompt_template_version,
                    permission_profile,
                    str(normalized_worktree) if normalized_worktree else None,
                    (
                        str(normalized_artifact)
                        if normalized_artifact
                        else row["artifact_dir"]
                    ),
                    now,
                    now,
                    row["attempt_id"],
                ),
            )
            if cursor.rowcount != 1:
                raise ActiveAttemptExistsError(
                    f"Reset-reserved Attempt changed while claiming: {row['attempt_id']}"
                )
            conn.execute(
                """
                INSERT INTO runtime_leases(
                    lease_id, task_id, attempt_id, owner_id, token_hash,
                    auth_mode, ttl_seconds, acquired_at, heartbeat_at,
                    expires_at, released_at, release_reason, is_active
                ) VALUES (?, ?, ?, ?, ?, 'token', ?, ?, ?, ?, NULL, NULL, 1)
                """,
                (
                    lease_id,
                    row["task_id"],
                    row["attempt_id"],
                    normalized_owner,
                    fingerprint,
                    ttl,
                    now,
                    now,
                    expiry,
                ),
            )
            task_cursor = conn.execute(
                """
                UPDATE tasks
                SET status = 'preparing', blocked_reason = NULL,
                    updated_at = ?, last_synced_at = ?
                WHERE task_id = ?
                  AND status = 'queued'
                  AND active_attempt_id = ?
                """,
                (now, now, row["task_id"], row["attempt_id"]),
            )
            if task_cursor.rowcount != 1:
                raise ActiveAttemptExistsError(
                    f"Task {normalized} changed while adopting reset Attempt"
                )
            lineage_cursor = conn.execute(
                """
                UPDATE reset_lineages
                SET state = 'claimed', claimed_at = ?
                WHERE reset_id = ? AND state = 'reserved'
                """,
                (now, row["reset_id"]),
            )
            if lineage_cursor.rowcount != 1:
                raise ActiveAttemptExistsError(
                    f"Reset lineage changed while claiming: {row['reset_id']}"
                )
            self._insert_lifecycle_event(
                conn,
                task_id=row["task_id"],
                attempt_id=row["attempt_id"],
                from_status="created",
                to_status="preparing",
                reason_code=reason_code,
                actor=normalized_owner,
                timestamp=now,
                metadata={
                    "lease_id": lease_id,
                    "auth_mode": "token",
                    "reset_id": row["reset_id"],
                    "old_attempt_id": row["old_attempt_id"],
                    "reset_generation": row["committed_generation"],
                    **(metadata or {}),
                },
            )
            self._insert_status_event(
                conn,
                task_key=normalized,
                status="preparing",
                source=normalized_owner,
                message="Runtime admission adopted reset-reserved Attempt",
                created_at=now,
            )
            ResetLineageStore._insert_event(
                conn,
                reset_id=row["reset_id"],
                task_id=row["task_id"],
                old_attempt_id=row["old_attempt_id"],
                new_attempt_id=row["attempt_id"],
                event_type="claimed",
                reason_code="reset_retry_attempt_claimed",
                actor=normalized_owner,
                timestamp=now,
                metadata={
                    "lease_id": lease_id,
                    "entrypoint_reason_code": reason_code,
                },
            )
            conn.execute(
                "DELETE FROM runtime_claim_suppressions WHERE task_id = ?",
                (row["task_id"],),
            )

        return RuntimeClaim(
            task_key=normalized,
            task_id=row["task_id"],
            attempt_id=row["attempt_id"],
            attempt_number=int(row["attempt_number"]),
            lease_id=lease_id,
            owner_id=normalized_owner,
            lease_token=token,
            acquired_at=now,
            heartbeat_at=now,
            expires_at=expiry,
        )

    def claim(self, task_key: str, **kwargs: Any) -> RuntimeClaim:
        RuntimeControlStore(self.db_path).assert_admission_allowed(task_key)
        self.init_db()
        reserved = self._try_claim_reserved_retry(task_key, **kwargs)
        if reserved is not None:
            return reserved
        try:
            return super().claim(task_key, **kwargs)
        except ActiveAttemptExistsError:
            # A reset may have committed between the initial lookup and the
            # ordinary claim transaction. Retry adoption once, then fail closed.
            reserved = self._try_claim_reserved_retry(task_key, **kwargs)
            if reserved is not None:
                return reserved
            raise


class ResetLineageRuntimeTaskStore(ExecutorProcessRuntimeTaskStore):
    """Final runtime layer with PR-8 retry-reservation migration."""

    def init_db(self) -> None:
        migrate_reset_lineage(self.db_path)


def install_reset_runtime_path(
    *,
    dispatcher_module: ModuleType,
    approved_task_runner_module: ModuleType,
) -> None:
    """Make reset-reserved Attempt adoption the final canonical runtime layer."""
    if getattr(canonical_path, "__reset_runtime_installed__", False):
        return

    canonical_path.CanonicalRuntimeAdmissionStore = ResetAwareRuntimeAdmissionStore

    def reset_canonicalize_store(
        store: Any | None,
        db_path: str | Path | None,
    ) -> ResetLineageRuntimeTaskStore:
        if isinstance(store, ResetLineageRuntimeTaskStore):
            return store
        resolved_path = getattr(store, "db_path", None) if store is not None else db_path
        return ResetLineageRuntimeTaskStore(resolved_path)

    def reset_attempt_store_for_request(
        store: Any | None,
        request: Any,
    ) -> ResetLineageRuntimeTaskStore:
        if isinstance(store, ResetLineageRuntimeTaskStore):
            return store
        resolved_path = getattr(store, "db_path", None) if store is not None else request.db_path
        return ResetLineageRuntimeTaskStore(resolved_path)

    canonical_path._canonicalize_store = reset_canonicalize_store
    attempt_path._attempt_store_for_request = reset_attempt_store_for_request
    canonical_path.__reset_runtime_installed__ = True


__all__ = [
    "ResetAwareRuntimeAdmissionStore",
    "ResetLineageRuntimeTaskStore",
    "install_reset_runtime_path",
]
