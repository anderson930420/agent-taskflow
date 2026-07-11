"""Atomic blocked-to-queued reset with old/new Attempt lineage binding."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from agent_taskflow.attempt_models import require_non_empty
from agent_taskflow.models import require_absolute_path, utc_now_iso
from agent_taskflow.reset_lineage_schema import migrate_reset_lineage
from agent_taskflow.store import connect, default_db_path
from agent_taskflow.tasks import normalize_task_key

ACTIVE_EXECUTOR_PROCESS_STATES = (
    "allocated",
    "running",
    "term_sent",
    "kill_sent",
)


class ResetLineageError(RuntimeError):
    """Base error for retry reservation and lineage persistence."""


class ResetCompareAndSetError(ResetLineageError):
    """Raised when the blocked task no longer matches the expected reset input."""


@dataclass(frozen=True)
class ResetLineageRecord:
    reset_id: str
    request_id: str
    task_id: str
    task_key: str
    old_attempt_id: str | None
    new_attempt_id: str
    expected_generation: int
    committed_generation: int
    from_status: str
    to_status: str
    reason: str
    actor: str
    state: str
    created_at: str
    claimed_at: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ResetPreview:
    task_key: str
    current_status: str
    current_generation: int
    old_attempt_id: str | None
    old_attempt_status: str | None
    next_attempt_number: int
    active_attempt_id: str | None


def _decode_metadata(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _row_to_lineage(row: sqlite3.Row) -> ResetLineageRecord:
    return ResetLineageRecord(
        reset_id=row["reset_id"],
        request_id=row["request_id"],
        task_id=row["task_id"],
        task_key=row["task_key"],
        old_attempt_id=row["old_attempt_id"],
        new_attempt_id=row["new_attempt_id"],
        expected_generation=int(row["expected_generation"]),
        committed_generation=int(row["committed_generation"]),
        from_status=row["from_status"],
        to_status=row["to_status"],
        reason=row["reason"],
        actor=row["actor"],
        state=row["state"],
        created_at=row["created_at"],
        claimed_at=row["claimed_at"],
        metadata=_decode_metadata(row["metadata_json"]),
    )


class ResetLineageStore:
    """Persist and atomically reserve the next retry Attempt.

    A successful reset creates the next Attempt in ``created`` state and binds it
    to the task before the task becomes ``queued``. Runtime admission must adopt
    that exact Attempt; it must not invent a second retry identity.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = (
            default_db_path()
            if db_path is None
            else require_absolute_path(db_path, "db_path")
        )

    def init_db(self) -> None:
        migrate_reset_lineage(self.db_path)

    @staticmethod
    def _insert_event(
        conn: sqlite3.Connection,
        *,
        reset_id: str,
        task_id: str,
        old_attempt_id: str | None,
        new_attempt_id: str,
        event_type: str,
        reason_code: str,
        actor: str,
        timestamp: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO reset_lineage_events(
                reset_id, task_id, old_attempt_id, new_attempt_id,
                event_type, reason_code, actor, timestamp, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reset_id,
                task_id,
                old_attempt_id,
                new_attempt_id,
                event_type,
                require_non_empty(reason_code, "reason_code"),
                require_non_empty(actor, "actor"),
                timestamp,
                json.dumps(metadata or {}, sort_keys=True),
            ),
        )

    def get(self, reset_id: str) -> ResetLineageRecord | None:
        self.init_db()
        with closing(connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM reset_lineages WHERE reset_id = ?",
                (require_non_empty(reset_id, "reset_id"),),
            ).fetchone()
        return _row_to_lineage(row) if row is not None else None

    def get_by_request_id(self, request_id: str) -> ResetLineageRecord | None:
        self.init_db()
        with closing(connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM reset_lineages WHERE request_id = ?",
                (require_non_empty(request_id, "request_id"),),
            ).fetchone()
        return _row_to_lineage(row) if row is not None else None

    def latest_for_task(self, task_key: str) -> ResetLineageRecord | None:
        self.init_db()
        normalized = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn:
            row = conn.execute(
                """
                SELECT reset_lineages.*
                FROM reset_lineages
                JOIN tasks ON tasks.task_id = reset_lineages.task_id
                WHERE tasks.task_key = ?
                ORDER BY reset_lineages.committed_generation DESC
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
        return _row_to_lineage(row) if row is not None else None

    def preview(self, task_key: str) -> ResetPreview:
        self.init_db()
        normalized = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn:
            task = conn.execute(
                """
                SELECT task_id, status, active_attempt_id, reset_generation
                FROM tasks WHERE task_key = ?
                """,
                (normalized,),
            ).fetchone()
            if task is None:
                raise ResetLineageError(f"Task not found: {normalized}")
            old = conn.execute(
                """
                SELECT attempt_id, status, attempt_number, is_active
                FROM attempts
                WHERE task_id = ?
                ORDER BY attempt_number DESC
                LIMIT 1
                """,
                (task["task_id"],),
            ).fetchone()
            next_number = 1 if old is None else int(old["attempt_number"]) + 1
        return ResetPreview(
            task_key=normalized,
            current_status=task["status"],
            current_generation=int(task["reset_generation"]),
            old_attempt_id=old["attempt_id"] if old is not None else None,
            old_attempt_status=old["status"] if old is not None else None,
            next_attempt_number=next_number,
            active_attempt_id=task["active_attempt_id"],
        )

    def _record_compare_and_set_rejection(
        self,
        *,
        task_key: str,
        request_id: str,
        actor: str,
        expected_generation: int | None,
        expected_old_attempt_id: str | None,
        error: str,
    ) -> None:
        normalized = normalize_task_key(task_key)
        now = utc_now_iso()
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT reset_lineages.*, tasks.status AS task_status,
                       tasks.reset_generation AS observed_generation
                FROM reset_lineages
                JOIN tasks ON tasks.task_id = reset_lineages.task_id
                WHERE tasks.task_key = ?
                ORDER BY reset_lineages.committed_generation DESC
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
            if row is None:
                return
            self._insert_event(
                conn,
                reset_id=row["reset_id"],
                task_id=row["task_id"],
                old_attempt_id=row["old_attempt_id"],
                new_attempt_id=row["new_attempt_id"],
                event_type="compare_and_set_rejected",
                reason_code="reset_compare_and_set_rejected",
                actor=actor,
                timestamp=now,
                metadata={
                    "request_id": request_id,
                    "expected_generation": expected_generation,
                    "expected_old_attempt_id": expected_old_attempt_id,
                    "observed_generation": row["observed_generation"],
                    "observed_task_status": row["task_status"],
                    "error": error,
                },
            )

    def reserve_retry(
        self,
        task_key: str,
        *,
        reason: str,
        actor: str,
        request_id: str | None = None,
        expected_generation: int | None = None,
        expected_old_attempt_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[ResetLineageRecord, bool]:
        """Atomically create one retry Attempt and move blocked -> queued.

        Returns ``(record, idempotent_replay)``. Different concurrent request IDs
        compete through a generation/status compare-and-set; exactly one can
        reserve the next Attempt.
        """
        self.init_db()
        normalized = normalize_task_key(task_key)
        normalized_reason = require_non_empty(reason, "reason")
        normalized_actor = require_non_empty(actor, "actor")
        normalized_request = require_non_empty(
            request_id or f"reset-request-{uuid4().hex}",
            "request_id",
        )
        reset_id = f"reset-{uuid4().hex}"
        new_attempt_id = f"attempt-{uuid4().hex}"
        now = utc_now_iso()

        try:
            with closing(connect(self.db_path)) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT * FROM reset_lineages WHERE request_id = ?",
                    (normalized_request,),
                ).fetchone()
                if existing is not None:
                    record = _row_to_lineage(existing)
                    if (
                        record.task_key != normalized
                        or record.reason != normalized_reason
                        or record.actor != normalized_actor
                    ):
                        raise ResetLineageError(
                            "request_id already belongs to a different reset request"
                        )
                    return record, True

                task = conn.execute(
                    """
                    SELECT task_id, task_key, status, active_attempt_id,
                           reset_generation, executor, model, artifact_dir
                    FROM tasks WHERE task_key = ?
                    """,
                    (normalized,),
                ).fetchone()
                if task is None:
                    raise ResetLineageError(f"Task not found: {normalized}")
                observed_generation = int(task["reset_generation"])
                expected = (
                    observed_generation
                    if expected_generation is None
                    else int(expected_generation)
                )
                if expected < 0:
                    raise ValueError("expected_generation must be >= 0")
                if task["status"] != "blocked":
                    raise ResetCompareAndSetError(
                        f"Task {normalized} status is {task['status']!r}; expected 'blocked'"
                    )
                if observed_generation != expected:
                    raise ResetCompareAndSetError(
                        f"Task {normalized} reset generation is {observed_generation}; "
                        f"expected {expected}"
                    )
                if task["active_attempt_id"] is not None:
                    raise ResetCompareAndSetError(
                        f"Task {normalized} still has active Attempt "
                        f"{task['active_attempt_id']}"
                    )

                active_attempt = conn.execute(
                    "SELECT attempt_id FROM attempts WHERE task_id = ? AND is_active = 1",
                    (task["task_id"],),
                ).fetchone()
                active_lease = conn.execute(
                    "SELECT lease_id FROM runtime_leases WHERE task_id = ? AND is_active = 1",
                    (task["task_id"],),
                ).fetchone()
                placeholders = ",".join("?" for _ in ACTIVE_EXECUTOR_PROCESS_STATES)
                active_process = conn.execute(
                    f"""
                    SELECT process_id FROM executor_processes
                    WHERE task_id = ? AND state IN ({placeholders})
                    LIMIT 1
                    """,
                    (task["task_id"], *ACTIVE_EXECUTOR_PROCESS_STATES),
                ).fetchone()
                if active_attempt is not None or active_lease is not None or active_process is not None:
                    raise ResetCompareAndSetError(
                        f"Task {normalized} still has active runtime ownership"
                    )

                old = conn.execute(
                    """
                    SELECT attempt_id, attempt_number, status, is_active, ended_at
                    FROM attempts
                    WHERE task_id = ?
                    ORDER BY attempt_number DESC
                    LIMIT 1
                    """,
                    (task["task_id"],),
                ).fetchone()
                old_attempt_id = old["attempt_id"] if old is not None else None
                if old is not None and bool(old["is_active"]):
                    raise ResetCompareAndSetError(
                        f"Latest Attempt is still active: {old_attempt_id}"
                    )
                if (
                    expected_old_attempt_id is not None
                    and old_attempt_id != expected_old_attempt_id
                ):
                    raise ResetCompareAndSetError(
                        f"Task {normalized} latest Attempt is {old_attempt_id!r}; "
                        f"expected {expected_old_attempt_id!r}"
                    )
                next_number = 1 if old is None else int(old["attempt_number"]) + 1
                committed_generation = observed_generation + 1

                conn.execute(
                    """
                    INSERT INTO attempts(
                        attempt_id, task_id, attempt_number, status, is_active,
                        is_legacy, executor, model, base_commit, policy_version,
                        config_snapshot_hash, prompt_template_version,
                        permission_profile, worktree_path, artifact_root,
                        started_at, ended_at, execution_result, validation_result,
                        merge_recommendation, created_at, updated_at
                    ) VALUES (?, ?, ?, 'created', 1, 0, ?, ?, NULL, NULL, NULL,
                              NULL, NULL, NULL, ?, NULL, NULL, NULL, NULL, NULL,
                              ?, ?)
                    """,
                    (
                        new_attempt_id,
                        task["task_id"],
                        next_number,
                        task["executor"],
                        task["model"],
                        task["artifact_dir"],
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO reset_lineage_suppressions(
                        task_id, reset_id, new_attempt_id, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (task["task_id"], reset_id, new_attempt_id, now),
                )
                cursor = conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'queued', active_attempt_id = ?,
                        blocked_reason = NULL, reset_generation = reset_generation + 1,
                        updated_at = ?, last_synced_at = ?
                    WHERE task_id = ?
                      AND status = 'blocked'
                      AND active_attempt_id IS NULL
                      AND reset_generation = ?
                    """,
                    (
                        new_attempt_id,
                        now,
                        now,
                        task["task_id"],
                        expected,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ResetCompareAndSetError(
                        f"Task {normalized} changed during reset compare-and-set"
                    )

                lineage_metadata = {
                    "old_attempt_status": old["status"] if old is not None else None,
                    "old_attempt_ended_at": old["ended_at"] if old is not None else None,
                    "new_attempt_number": next_number,
                    **dict(metadata or {}),
                }
                conn.execute(
                    """
                    INSERT INTO reset_lineages(
                        reset_id, request_id, task_id, task_key,
                        old_attempt_id, new_attempt_id, expected_generation,
                        committed_generation, from_status, to_status, reason,
                        actor, state, created_at, claimed_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'blocked', 'queued', ?, ?,
                              'reserved', ?, NULL, ?)
                    """,
                    (
                        reset_id,
                        normalized_request,
                        task["task_id"],
                        normalized,
                        old_attempt_id,
                        new_attempt_id,
                        expected,
                        committed_generation,
                        normalized_reason,
                        normalized_actor,
                        now,
                        json.dumps(lineage_metadata, sort_keys=True),
                    ),
                )
                self._insert_event(
                    conn,
                    reset_id=reset_id,
                    task_id=task["task_id"],
                    old_attempt_id=old_attempt_id,
                    new_attempt_id=new_attempt_id,
                    event_type="reserved",
                    reason_code="reset_retry_attempt_reserved",
                    actor=normalized_actor,
                    timestamp=now,
                    metadata={
                        "request_id": normalized_request,
                        "expected_generation": expected,
                        "committed_generation": committed_generation,
                        **lineage_metadata,
                    },
                )
                conn.execute(
                    """
                    INSERT INTO lifecycle_events(
                        task_id, attempt_id, from_status, to_status,
                        reason_code, actor, timestamp, metadata_json
                    ) VALUES (?, ?, NULL, 'created', ?, ?, ?, ?)
                    """,
                    (
                        task["task_id"],
                        new_attempt_id,
                        "reset_retry_attempt_reserved",
                        normalized_actor,
                        now,
                        json.dumps(
                            {
                                "reset_id": reset_id,
                                "request_id": normalized_request,
                                "old_attempt_id": old_attempt_id,
                                "committed_generation": committed_generation,
                            },
                            sort_keys=True,
                        ),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO task_events(
                        task_key, event_type, source, message,
                        payload_json, created_at
                    ) VALUES (?, 'status_changed', ?, ?, ?, ?)
                    """,
                    (
                        normalized,
                        normalized_actor,
                        "Operator reset reserved a new retry Attempt",
                        json.dumps(
                            {
                                "status": "queued",
                                "blocked_reason": None,
                                "kind": "reset_lineage_reserved",
                                "reset_id": reset_id,
                                "old_attempt_id": old_attempt_id,
                                "new_attempt_id": new_attempt_id,
                                "reset_generation": committed_generation,
                            },
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                conn.execute(
                    "DELETE FROM reset_lineage_suppressions WHERE task_id = ?",
                    (task["task_id"],),
                )

            record = self.get(reset_id)
            assert record is not None
            return record, False
        except ResetCompareAndSetError as exc:
            self._record_compare_and_set_rejection(
                task_key=normalized,
                request_id=normalized_request,
                actor=normalized_actor,
                expected_generation=expected_generation,
                expected_old_attempt_id=expected_old_attempt_id,
                error=str(exc),
            )
            raise

    def mark_claimed(
        self,
        reset_id: str,
        *,
        actor: str,
        lease_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> ResetLineageRecord:
        self.init_db()
        now = utc_now_iso()
        normalized_actor = require_non_empty(actor, "actor")
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM reset_lineages WHERE reset_id = ?",
                (require_non_empty(reset_id, "reset_id"),),
            ).fetchone()
            if row is None:
                raise KeyError(f"Reset lineage not found: {reset_id}")
            if row["state"] == "claimed":
                return _row_to_lineage(row)
            if row["state"] != "reserved":
                raise ResetLineageError(
                    f"Reset lineage {reset_id} is not claimable from {row['state']}"
                )
            cursor = conn.execute(
                """
                UPDATE reset_lineages
                SET state = 'claimed', claimed_at = ?
                WHERE reset_id = ? AND state = 'reserved'
                """,
                (now, reset_id),
            )
            if cursor.rowcount != 1:
                raise ResetCompareAndSetError(
                    f"Reset lineage changed while claiming: {reset_id}"
                )
            self._insert_event(
                conn,
                reset_id=reset_id,
                task_id=row["task_id"],
                old_attempt_id=row["old_attempt_id"],
                new_attempt_id=row["new_attempt_id"],
                event_type="claimed",
                reason_code="reset_retry_attempt_claimed",
                actor=normalized_actor,
                timestamp=now,
                metadata={"lease_id": lease_id, **dict(metadata or {})},
            )
        record = self.get(reset_id)
        assert record is not None
        return record

    def append_artifact_failure(
        self,
        reset_id: str,
        *,
        actor: str,
        error: str,
    ) -> None:
        self.init_db()
        now = utc_now_iso()
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM reset_lineages WHERE reset_id = ?",
                (reset_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Reset lineage not found: {reset_id}")
            self._insert_event(
                conn,
                reset_id=reset_id,
                task_id=row["task_id"],
                old_attempt_id=row["old_attempt_id"],
                new_attempt_id=row["new_attempt_id"],
                event_type="artifact_failed",
                reason_code="reset_audit_artifact_write_failed",
                actor=actor,
                timestamp=now,
                metadata={"error": error},
            )

    def audit_artifact_path(self, record: ResetLineageRecord) -> Path | None:
        self.init_db()
        with closing(connect(self.db_path)) as conn:
            row = conn.execute(
                """
                SELECT artifact_base_root
                FROM attempt_resources
                WHERE task_key = ?
                ORDER BY attempt_number DESC
                LIMIT 1
                """,
                (record.task_key,),
            ).fetchone()
            if row is not None:
                base = Path(row["artifact_base_root"])
            else:
                task = conn.execute(
                    "SELECT artifact_dir FROM tasks WHERE task_id = ?",
                    (record.task_id,),
                ).fetchone()
                if task is None or task["artifact_dir"] is None:
                    return None
                base = Path(task["artifact_dir"])
        return base / "reset-audit" / f"{record.reset_id}.json"


__all__ = [
    "ResetCompareAndSetError",
    "ResetLineageError",
    "ResetLineageRecord",
    "ResetLineageStore",
    "ResetPreview",
]
