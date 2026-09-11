"""Retry a stopped Ticket (SPEC §33.3; V1 Step 5, ruling 27c).

A Ticket that ended ``failed`` or ``needs_decision`` returns to ``created``
(display ``ready``). Its next claim creates the new Attempt, which runs in the
same worktree on the same branch (ruling 26). This is the Ticket half of
``scripts/reset_task_status.py``; legacy tasks keep the PR-8
``blocked -> queued`` reset lineage untouched.

No reset-lineage row is written: ``reset_lineages`` only admits
``blocked -> queued`` by SQL ``CHECK``, and the next ordinary claim already
creates the next Attempt. The retry is one ``BEGIN IMMEDIATE`` compare-and-set
on the Ticket row that also writes a ``status_changed`` event and a ``note``
event holding the full audit record, plus a JSON audit artifact.
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.models import utc_now_iso
from agent_taskflow.reset_lineage import ACTIVE_EXECUTOR_PROCESS_STATES
from agent_taskflow.store import TaskMirrorStore, connect
from agent_taskflow.ticket_dependencies import (
    COMPLETED_BLOCKER_STATUSES,
    is_dependency_blocked_reason,
)
from agent_taskflow.ticket_lifecycle import (
    TICKET_FAILED_STATUS,
    TICKET_NEEDS_DECISION_STATUS,
)

TICKET_RETRY_FROM_STATUSES = (TICKET_FAILED_STATUS, TICKET_NEEDS_DECISION_STATUS)
TICKET_RETRY_TO_STATUS = "created"
TICKET_RETRY_KIND = "ticket_retry_reset"
TICKET_RETRY_ARTIFACT_TYPE = "other"
# Attempt-resource states whose runtime process is not known to have ended.
_LIVE_RESOURCE_STATES = ("allocated", "active", "reap_blocked_live_pid")


class TicketRetryError(RuntimeError):
    """Raised when a Ticket cannot be retried safely. Nothing is written."""


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def dependency_blocked_ticket_reason(db_path: str | Path, task_key: str) -> str | None:
    """Return why a dependency-blocked Ticket must not be reset, or None."""
    with closing(sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)) as conn:
        if "blocked_by" not in _columns(conn, "tasks"):
            return None
        row = conn.execute(
            "SELECT status, blocked_by, blocked_reason FROM tasks WHERE task_key = ?",
            (task_key,),
        ).fetchone()
    if row is None or row[0] != "blocked" or not row[1]:
        return None
    if not is_dependency_blocked_reason(row[2]):
        return None
    return (
        f"Ticket {task_key} is waiting for its blocker {row[1]}; it is not a failure to "
        "retry. Remove or replace the dependency with scripts/ticket_dependency.py instead."
    )


def _result(
    request: Any,
    *,
    mutated: bool,
    dry_run: bool,
    retry_id: str | None,
    request_id: str | None,
    old_attempt_id: str | None,
    expected_generation: int,
    committed_generation: int | None,
    next_attempt_number: int,
    idempotent_replay: bool,
    audit_artifact_path: Path | None = None,
    artifact_error: str | None = None,
) -> dict[str, Any]:
    return {
        "task_key": request.task_key,
        "from_status": request.from_status,
        "to_status": TICKET_RETRY_TO_STATUS,
        "reason": request.reason,
        "dry_run": dry_run,
        "operator_confirmed": bool(request.confirm_reset) if dry_run else True,
        "mutated": mutated,
        "audit_artifact_path": audit_artifact_path,
        "artifact_error": artifact_error,
        "reset_id": retry_id,
        "request_id": request_id,
        "old_attempt_id": old_attempt_id,
        "new_attempt_id": None,
        "expected_reset_generation": expected_generation,
        "committed_reset_generation": committed_generation,
        "next_attempt_number": next_attempt_number,
        "idempotent_replay": idempotent_replay,
    }


def _replay(request: Any, store: TaskMirrorStore) -> dict[str, Any] | None:
    for event in store.list_task_events(request.task_key):
        try:
            payload = json.loads(event.payload_json) if event.payload_json else {}
        except ValueError:
            continue
        if payload.get("kind") != TICKET_RETRY_KIND:
            continue
        if payload.get("request_id") != request.request_id:
            continue
        if payload.get("reason") != request.reason or payload.get("actor") != request.actor:
            raise TicketRetryError("request_id already belongs to a different reset request")
        if payload.get("from_status") != request.from_status:
            raise TicketRetryError("request_id already belongs to a different reset request")
        return _result(
            request,
            mutated=False,
            dry_run=False,
            retry_id=payload.get("reset_id"),
            request_id=request.request_id,
            old_attempt_id=payload.get("old_attempt_id"),
            expected_generation=int(payload.get("expected_reset_generation") or 0),
            committed_generation=payload.get("committed_reset_generation"),
            next_attempt_number=int(payload.get("next_attempt_number") or 1),
            idempotent_replay=True,
            audit_artifact_path=(
                Path(payload["audit_artifact_path"]) if payload.get("audit_artifact_path") else None
            ),
        )
    return None


def retry_ticket(request: Any, *, store: TaskMirrorStore) -> dict[str, Any]:
    """Preview or perform one ``failed``/``needs_decision`` -> ``created`` retry.

    ``request`` is a :class:`~agent_taskflow.task_status_reset.TaskStatusResetRequest`.
    Returns the fields of a ``TaskStatusResetResult``. Raises
    :class:`TicketRetryError` before any write on every refusal.
    """
    if request.from_status not in TICKET_RETRY_FROM_STATUSES:
        raise TicketRetryError(f"A Ticket is retried only from {TICKET_RETRY_FROM_STATUSES!r}")
    if request.request_id is not None:
        replay = _replay(request, store)
        if replay is not None:
            return replay

    retry_id = f"ticket-retry-{uuid4().hex}"
    request_id = request.request_id or f"ticket-retry-request-{uuid4().hex}"
    now = utc_now_iso()
    with closing(connect(store.db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            tables = _tables(conn)
            task_columns = _columns(conn, "tasks")
            has_generation = "reset_generation" in task_columns
            row = conn.execute(
                f"""
                SELECT task_key, task_id, status, active_attempt_id, blocked_by,
                       artifact_dir{', reset_generation' if has_generation else ''}
                FROM tasks WHERE task_key = ?
                """,
                (request.task_key,),
            ).fetchone()
            if row is None:
                raise TicketRetryError(f"Task not found: {request.task_key}")
            if row["status"] != request.from_status:
                raise TicketRetryError(
                    f"Task {request.task_key} status is {row['status']!r}; "
                    f"expected {request.from_status!r}"
                )
            if row["active_attempt_id"]:
                raise TicketRetryError(
                    f"Task {request.task_key} still has active runtime ownership"
                )
            if "runtime_leases" in tables and row["task_id"]:
                live = conn.execute(
                    "SELECT 1 FROM runtime_leases WHERE task_id = ? AND is_active = 1",
                    (row["task_id"],),
                ).fetchone()
                if live is not None:
                    raise TicketRetryError(
                        f"Task {request.task_key} still has an active runtime lease"
                    )
            # The next Attempt shares this worktree (ruling 26), so a process a
            # previous Attempt may still run must be ruled out first: the same
            # guard as the legacy reset, plus Attempt resources whose PID is not
            # known to be dead.
            if "executor_processes" in tables and row["task_id"]:
                placeholders = ",".join("?" for _ in ACTIVE_EXECUTOR_PROCESS_STATES)
                process = conn.execute(
                    f"""
                    SELECT process_id FROM executor_processes
                    WHERE task_id = ? AND state IN ({placeholders}) LIMIT 1
                    """,
                    (row["task_id"], *ACTIVE_EXECUTOR_PROCESS_STATES),
                ).fetchone()
                if process is not None:
                    raise TicketRetryError(
                        f"Task {request.task_key} still has an active executor process "
                        f"{process['process_id']}; terminate it before retrying"
                    )
            if "attempt_resources" in tables:
                resource = conn.execute(
                    f"""
                    SELECT attempt_id, status FROM attempt_resources
                    WHERE task_key = ? AND status IN ({",".join("?" for _ in _LIVE_RESOURCE_STATES)})
                    LIMIT 1
                    """,
                    (request.task_key, *_LIVE_RESOURCE_STATES),
                ).fetchone()
                if resource is not None:
                    raise TicketRetryError(
                        f"Task {request.task_key} Attempt {resource['attempt_id']} resources are "
                        f"{resource['status']!r}; its runtime process may still be alive. Run "
                        "scripts/reap_stale_runtime.py (or terminate the process) before retrying"
                    )
            latest = None
            if "attempts" in tables and row["task_id"]:
                latest = conn.execute(
                    """
                    SELECT attempt_id, attempt_number, is_active FROM attempts
                    WHERE task_id = ? ORDER BY attempt_number DESC LIMIT 1
                    """,
                    (row["task_id"],),
                ).fetchone()
            if latest is not None and latest["is_active"]:
                raise TicketRetryError(
                    f"Task {request.task_key} latest Attempt is still active"
                )
            old_attempt_id = latest["attempt_id"] if latest is not None else None
            next_attempt_number = (int(latest["attempt_number"]) if latest is not None else 0) + 1
            if (
                request.expected_old_attempt_id is not None
                and old_attempt_id != request.expected_old_attempt_id
            ):
                raise TicketRetryError(
                    f"Task {request.task_key} latest Attempt is {old_attempt_id!r}; "
                    f"expected {request.expected_old_attempt_id!r}"
                )
            generation = int(row["reset_generation"] or 0) if has_generation else 0
            if (
                request.expected_reset_generation is not None
                and generation != request.expected_reset_generation
            ):
                raise TicketRetryError(
                    f"Task {request.task_key} reset generation is {generation}; "
                    f"expected {request.expected_reset_generation}"
                )
            blocker = row["blocked_by"]
            if blocker:
                blocker_row = conn.execute(
                    "SELECT status FROM tasks WHERE task_key = ?", (blocker,)
                ).fetchone()
                blocker_status = blocker_row["status"] if blocker_row is not None else "missing"
                if blocker_status not in COMPLETED_BLOCKER_STATUSES:
                    raise TicketRetryError(
                        f"Ticket {request.task_key} is blocked_by {blocker} (status "
                        f"{blocker_status!r}), an unreleased dependency. Remove or replace "
                        "the dependency with scripts/ticket_dependency.py first."
                    )
            if request.dry_run:
                conn.rollback()
                return _result(
                    request,
                    mutated=False,
                    dry_run=True,
                    retry_id=None,
                    request_id=request.request_id,
                    old_attempt_id=old_attempt_id,
                    expected_generation=generation,
                    committed_generation=None,
                    next_attempt_number=next_attempt_number,
                    idempotent_replay=False,
                )
            if not request.confirm_reset:
                raise TicketRetryError("Reset requires --confirm-reset unless --dry-run is used")

            committed = generation + 1
            generation_sql = ", reset_generation = reset_generation + 1" if has_generation else ""
            updated = conn.execute(
                f"""
                UPDATE tasks
                SET status = ?, blocked_reason = NULL, updated_at = ?, last_synced_at = ?
                    {generation_sql}
                WHERE task_key = ? AND status = ? AND active_attempt_id IS NULL
                """,
                (TICKET_RETRY_TO_STATUS, now, now, request.task_key, request.from_status),
            ).rowcount
            if updated != 1:
                raise TicketRetryError(
                    f"Task {request.task_key} changed while it was being retried"
                )
            # After a claim `tasks.artifact_dir` names the last Attempt's root,
            # which is immutable evidence; write beside it, under the Ticket's
            # artifact base, as the legacy reset audit does.
            base = (
                conn.execute(
                    """
                    SELECT artifact_base_root FROM attempt_resources
                    WHERE task_key = ? ORDER BY attempt_number DESC LIMIT 1
                    """,
                    (request.task_key,),
                ).fetchone()
                if "attempt_resources" in tables
                else None
            )
            artifact_dir = (
                Path(base[0])
                if base is not None and base[0]
                else (Path(row["artifact_dir"]) if row["artifact_dir"] else None)
            )
            artifact_path = (
                artifact_dir / "reset-audit" / f"{retry_id}.json" if artifact_dir else None
            )
            audit = {
                "kind": TICKET_RETRY_KIND,
                "task_key": request.task_key,
                "from_status": request.from_status,
                "to_status": TICKET_RETRY_TO_STATUS,
                "reason": request.reason,
                "actor": request.actor,
                "reset_id": retry_id,
                "request_id": request_id,
                "old_attempt_id": old_attempt_id,
                "next_attempt_number": next_attempt_number,
                "expected_reset_generation": generation,
                "committed_reset_generation": committed,
                "same_worktree": True,
                "worktree_cleaned": False,
                "dry_run": False,
                "operator_confirmed": True,
                "not_approval": True,
                "not_merge": True,
                "not_cleanup": True,
                "not_validation_authority": True,
                "audit_artifact_path": str(artifact_path) if artifact_path else None,
            }
            conn.execute(
                """
                INSERT INTO task_events(task_key, event_type, source, message, payload_json, created_at)
                VALUES (?, 'status_changed', ?, ?, ?, ?)
                """,
                (
                    request.task_key,
                    request.actor,
                    f"Operator retry from {request.from_status}: {request.reason}",
                    json.dumps({"status": TICKET_RETRY_TO_STATUS, "blocked_reason": None}, sort_keys=True),
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO task_events(task_key, event_type, source, message, payload_json, created_at)
                VALUES (?, 'note', ?, ?, ?, ?)
                """,
                (
                    request.task_key,
                    request.actor,
                    "Operator-confirmed Ticket retry recorded (SPEC §33.3)",
                    json.dumps(audit, sort_keys=True),
                    now,
                ),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    artifact_error: str | None = None
    if artifact_path is not None:
        try:
            atomic_write_json(artifact_path, audit, sort_keys=True)
            store.record_task_artifact(request.task_key, TICKET_RETRY_ARTIFACT_TYPE, artifact_path)
        except OSError as exc:
            artifact_error = f"{exc.__class__.__name__}: {exc}"
            artifact_path = None
    return _result(
        request,
        mutated=True,
        dry_run=False,
        retry_id=retry_id,
        request_id=request_id,
        old_attempt_id=old_attempt_id,
        expected_generation=generation,
        committed_generation=committed,
        next_attempt_number=next_attempt_number,
        idempotent_replay=False,
        audit_artifact_path=artifact_path,
        artifact_error=artifact_error,
    )


__all__ = [
    "TICKET_RETRY_FROM_STATUSES",
    "TICKET_RETRY_KIND",
    "TICKET_RETRY_TO_STATUS",
    "TicketRetryError",
    "dependency_blocked_ticket_reason",
    "retry_ticket",
]
