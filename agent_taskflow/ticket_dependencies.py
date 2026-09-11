"""Ticket dependencies: ``blocked_by`` (SPEC §5, §18, §43.6-§43.8; V1 Step 5).

Step 1 stores one ``tasks.blocked_by`` key per Ticket and writes ``blocked``
when a Ticket is created with it. This module owns everything after creation:

* **Set / replace / remove** (§5.2, §18). The blocker must exist, must not be the
  Ticket itself, and must not close a cycle of any length. A refusal writes
  nothing. Setting ``blocked_by`` on a ready (``created``) Ticket moves it to
  ``blocked`` (§6's event history; ruling 28 D5).
* **Release** (§5.3, §43.7, §43.34). Only a blocker in a §12 ``completed``
  status (persisted ``cleaned``, ``completed``, ``done``) releases its
  dependents. Only a ``blocked`` row whose ``blocked_reason`` is empty or owned
  by this module is released, ``blocked -> created``, and ``blocked_by`` is
  cleared. A ``blocked_reason`` that records a failure is never released.
* **Failed / cancelled blocker** (§5.4, §43.8). The dependent goes to
  ``needs_decision``; nothing is released silently. A dependent that is running
  is never interrupted: it is left alone and stopped for a decision once its
  Attempt has ended (ruling 28 D4).

Every change writes a ``status_changed`` event (source ``ticket_dependencies``)
when the status moves, plus a ``note`` event with the full record.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any

from agent_taskflow.models import require_absolute_path, utc_now_iso
from agent_taskflow.status_vocab import persisted_statuses_for_display
from agent_taskflow.store import connect
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.ticket_lifecycle import tasks_has_ticket_columns

DEPENDENCY_SOURCE = "ticket_dependencies"
DEPENDENCY_REASON_PREFIX = "blocked_by "

# §5.3: only `completed` releases. §5.4: `failed` and `cancelled` stop.
COMPLETED_BLOCKER_STATUSES = persisted_statuses_for_display("completed")
STOPPED_BLOCKER_STATUSES = persisted_statuses_for_display("failed") | persisted_statuses_for_display(
    "cancelled"
)
RUNNING_STATUSES = frozenset({"preparing", "implementing", "validating"})
READY_STATUSES = frozenset({"created", "queued"})
# A dependent in one of these is not moved by a stopped blocker: it already
# waits for a decision, has completed or been cancelled, or was paused by the
# user. A `failed` dependent is moved: its retry needs the dependency decided.
_NOT_REDIRECTED_STATUSES = (
    frozenset({"needs_decision", "paused"})
    | COMPLETED_BLOCKER_STATUSES
    | persisted_statuses_for_display("cancelled")
)

# note-event kinds.
DEPENDENCY_SET = "ticket_dependency_set"
DEPENDENCY_REMOVED = "ticket_dependency_removed"
DEPENDENCY_RELEASED = "ticket_dependency_released"
DEPENDENCY_BLOCKER_STOPPED = "ticket_dependency_blocker_stopped"


class TicketDependencyError(ValueError):
    """Raised when a dependency change is refused. Nothing is written."""


def dependency_blocked_reason(blocker: str) -> str:
    """The ``blocked_reason`` this module writes for a dependency wait."""
    return f"{DEPENDENCY_REASON_PREFIX}{normalize_task_key(blocker)}: waiting for the blocker to complete"


def is_dependency_blocked_reason(reason: str | None) -> bool:
    """True for an empty reason (Step 1 creation) or one written by this module."""
    return reason is None or not reason.strip() or reason.startswith(DEPENDENCY_REASON_PREFIX)


@dataclass(frozen=True)
class DependencyChange:
    task_key: str
    blocked_by: str | None
    previous_blocked_by: str | None
    from_status: str
    to_status: str

    @property
    def status_changed(self) -> bool:
        return self.from_status != self.to_status

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key,
            "blocked_by": self.blocked_by,
            "previous_blocked_by": self.previous_blocked_by,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "status_changed": self.status_changed,
        }


@dataclass(frozen=True)
class DependencyMaintenanceResult:
    released: tuple[str, ...] = ()
    needs_decision: tuple[str, ...] = ()
    deferred_running: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "released": list(self.released),
            "needs_decision": list(self.needs_decision),
            "deferred_running": list(self.deferred_running),
        }


# ----------------------------------------------------------------------
# Helpers (all inside the caller's transaction)
# ----------------------------------------------------------------------


def _ticket_row(conn: sqlite3.Connection, task_key: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT task_key, status, blocked_by, blocked_reason, prompt IS NOT NULL AS is_ticket
        FROM tasks WHERE task_key = ?
        """,
        (task_key,),
    ).fetchone()
    if row is None:
        raise TicketDependencyError(f"Ticket does not exist: {task_key}")
    if not row["is_ticket"]:
        raise TicketDependencyError(f"{task_key} is not a Ticket; only Tickets carry blocked_by")
    return row


def _cycle_path(conn: sqlite3.Connection, task_key: str, blocker: str) -> list[str] | None:
    """Return ``[task, blocker, ..., task]`` if blocker's chain reaches task_key."""
    path = [task_key, blocker]
    seen = {blocker}
    current = blocker
    while True:
        row = conn.execute("SELECT blocked_by FROM tasks WHERE task_key = ?", (current,)).fetchone()
        nxt = row[0] if row is not None else None
        if not nxt:
            return None
        path.append(nxt)
        if nxt == task_key:
            return path
        if nxt in seen:  # a pre-existing cycle that does not involve task_key
            return None
        seen.add(nxt)
        current = nxt


def _last_status_source(conn: sqlite3.Connection, task_key: str) -> str | None:
    row = conn.execute(
        """
        SELECT source FROM task_events
        WHERE task_key = ? AND event_type = 'status_changed'
        ORDER BY id DESC LIMIT 1
        """,
        (task_key,),
    ).fetchone()
    return row[0] if row is not None else None


def _last_blocker_stop_was_dependency_owned(conn: sqlite3.Connection, task_key: str) -> bool:
    """True when the latest failed/cancelled-blocker stop found a dependency wait."""
    row = conn.execute(
        """
        SELECT payload_json FROM task_events
        WHERE task_key = ? AND event_type = 'note' AND source = ?
          AND json_extract(payload_json, '$.kind') = ?
        ORDER BY id DESC LIMIT 1
        """,
        (task_key, DEPENDENCY_SOURCE, DEPENDENCY_BLOCKER_STOPPED),
    ).fetchone()
    if row is None:
        return False
    return bool(json.loads(row[0]).get("dependency_owned"))


def _active_attempt_id(conn: sqlite3.Connection, task_key: str) -> str | None:
    """The row's active (or reserved) Attempt, or None; tolerant of legacy schemas."""
    if not any(r[1] == "active_attempt_id" for r in conn.execute("PRAGMA table_info(tasks)")):
        return None
    row = conn.execute(
        "SELECT active_attempt_id FROM tasks WHERE task_key = ?", (task_key,)
    ).fetchone()
    return row[0] if row is not None else None


def _dependency_owned(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """True when the row's current status was put there by a dependency.

    A `needs_decision` counts only if a stopped blocker put it there while the
    Ticket was merely waiting on its dependency (ready, or dependency-blocked);
    one that was failed or finished first still needs an operator retry.
    """
    if row["status"] == "blocked":
        return bool(row["blocked_by"]) and is_dependency_blocked_reason(row["blocked_reason"])
    if row["status"] == "needs_decision":
        return _last_status_source(
            conn, row["task_key"]
        ) == DEPENDENCY_SOURCE and _last_blocker_stop_was_dependency_owned(conn, row["task_key"])
    return False


def _write(
    conn: sqlite3.Connection,
    *,
    task_key: str,
    blocked_by: str | None,
    from_status: str,
    to_status: str,
    blocked_reason: str | None,
    kind: str,
    message: str,
    payload: dict[str, Any],
    now: str,
) -> None:
    if from_status == to_status:
        # Leave `status` alone: rewriting it would fire the runtime's status
        # triggers (a running Ticket's `preparing` pickup guard, for one).
        conn.execute(
            """
            UPDATE tasks
            SET blocked_by = ?, blocked_reason = ?, updated_at = ?, last_synced_at = ?
            WHERE task_key = ?
            """,
            (blocked_by, blocked_reason, now, now, task_key),
        )
    else:
        conn.execute(
            """
            UPDATE tasks
            SET blocked_by = ?, status = ?, blocked_reason = ?, updated_at = ?, last_synced_at = ?
            WHERE task_key = ? AND status = ?
            """,
            (blocked_by, to_status, blocked_reason, now, now, task_key, from_status),
        )
    if from_status != to_status:
        conn.execute(
            """
            INSERT INTO task_events(task_key, event_type, source, message, payload_json, created_at)
            VALUES (?, 'status_changed', ?, ?, ?, ?)
            """,
            (
                task_key,
                DEPENDENCY_SOURCE,
                message,
                json.dumps(
                    {
                        "status": to_status,
                        "blocked_reason": blocked_reason if to_status == "blocked" else None,
                    },
                    sort_keys=True,
                ),
                now,
            ),
        )
    conn.execute(
        """
        INSERT INTO task_events(task_key, event_type, source, message, payload_json, created_at)
        VALUES (?, 'note', ?, ?, ?, ?)
        """,
        (
            task_key,
            DEPENDENCY_SOURCE,
            message,
            json.dumps(
                {
                    "kind": kind,
                    "from_status": from_status,
                    "to_status": to_status,
                    "blocked_by": blocked_by,
                    **payload,
                },
                sort_keys=True,
            ),
            now,
        ),
    )


def _require_ticket_schema(conn: sqlite3.Connection) -> None:
    if not tasks_has_ticket_columns(conn):
        raise TicketDependencyError(
            "Ticket columns are not installed; run scripts/migrate_ticket_fields.py first"
        )


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def validate_blocked_by(db_path: str | Path, task_key: str, blocker: str) -> None:
    """Raise :class:`TicketDependencyError` if ``task_key blocked_by blocker`` is invalid.

    Read-only: used by the operator CLI's preview.
    """
    path = require_absolute_path(db_path, "db_path")
    normalized = normalize_task_key(task_key)
    normalized_blocker = normalize_task_key(blocker)
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        _require_ticket_schema(conn)
        _validate(conn, normalized, normalized_blocker)


def _validate(conn: sqlite3.Connection, task_key: str, blocker: str) -> sqlite3.Row:
    row = _ticket_row(conn, task_key)
    if blocker == task_key:
        raise TicketDependencyError(f"A Ticket cannot block itself: {task_key}")
    exists = conn.execute("SELECT 1 FROM tasks WHERE task_key = ?", (blocker,)).fetchone()
    if exists is None:
        raise TicketDependencyError(f"blocked_by task does not exist: {blocker}")
    cycle = _cycle_path(conn, task_key, blocker)
    if cycle is not None:
        raise TicketDependencyError(
            "Refusing a dependency cycle: " + " -> blocked_by -> ".join(cycle)
        )
    return row


def set_blocked_by(
    db_path: str | Path,
    task_key: str,
    blocker: str,
    *,
    actor: str,
    reason: str | None = None,
) -> DependencyChange:
    """Set or replace ``blocked_by``. Refused changes write nothing."""
    path = require_absolute_path(db_path, "db_path")
    normalized = normalize_task_key(task_key)
    normalized_blocker = normalize_task_key(blocker)
    now = utc_now_iso()
    with closing(connect(path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _require_ticket_schema(conn)
        row = _validate(conn, normalized, normalized_blocker)
        from_status = row["status"]
        if from_status in READY_STATUSES and _active_attempt_id(conn, normalized):
            # A legacy reset reserved a retry Attempt; blocking it would strand
            # the reservation (adoption needs `queued`).
            raise TicketDependencyError(
                f"{normalized} holds a reserved retry Attempt; start or cancel it before "
                "setting a dependency"
            )
        if from_status in READY_STATUSES or _dependency_owned(conn, row):
            to_status = "blocked"
            blocked_reason = dependency_blocked_reason(normalized_blocker)
        else:
            # A running, finished or failure-stopped Ticket keeps its status;
            # the dependency is recorded (a running one is stopped for a
            # decision only if its blocker fails; ruling 28 D4).
            to_status = from_status
            blocked_reason = row["blocked_reason"]
        _write(
            conn,
            task_key=normalized,
            blocked_by=normalized_blocker,
            from_status=from_status,
            to_status=to_status,
            blocked_reason=blocked_reason,
            kind=DEPENDENCY_SET,
            message=(
                f"{normalized} blocked_by {normalized_blocker}"
                + (f" (replacing {row['blocked_by']})" if row["blocked_by"] else "")
            ),
            payload={
                "previous_blocked_by": row["blocked_by"],
                "actor": actor,
                "reason": reason,
            },
            now=now,
        )
    return DependencyChange(
        task_key=normalized,
        blocked_by=normalized_blocker,
        previous_blocked_by=row["blocked_by"],
        from_status=from_status,
        to_status=to_status,
    )


def remove_blocked_by(
    db_path: str | Path,
    task_key: str,
    *,
    actor: str,
    reason: str | None = None,
) -> DependencyChange:
    """Remove ``blocked_by``. A dependency-held Ticket becomes ready again."""
    path = require_absolute_path(db_path, "db_path")
    normalized = normalize_task_key(task_key)
    now = utc_now_iso()
    with closing(connect(path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _require_ticket_schema(conn)
        row = _ticket_row(conn, normalized)
        if not row["blocked_by"]:
            raise TicketDependencyError(f"{normalized} has no blocked_by to remove")
        from_status = row["status"]
        if _dependency_owned(conn, row):
            to_status, blocked_reason = "created", None
        else:
            to_status, blocked_reason = from_status, row["blocked_reason"]
        _write(
            conn,
            task_key=normalized,
            blocked_by=None,
            from_status=from_status,
            to_status=to_status,
            blocked_reason=blocked_reason,
            kind=DEPENDENCY_REMOVED,
            message=f"{normalized} is no longer blocked_by {row['blocked_by']}",
            payload={"previous_blocked_by": row["blocked_by"], "actor": actor, "reason": reason},
            now=now,
        )
    return DependencyChange(
        task_key=normalized,
        blocked_by=None,
        previous_blocked_by=row["blocked_by"],
        from_status=from_status,
        to_status=to_status,
    )


def maintain_dependencies(db_path: str | Path, *, actor: str) -> DependencyMaintenanceResult:
    """Release completed dependencies and stop dependents of failed blockers. Idempotent."""
    path = require_absolute_path(db_path, "db_path")
    if not path.is_file():
        return DependencyMaintenanceResult()
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as probe:
        tables = {r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "tasks" not in tables or not tasks_has_ticket_columns(probe):
            return DependencyMaintenanceResult()
        pending = probe.execute(
            "SELECT 1 FROM tasks WHERE prompt IS NOT NULL AND blocked_by IS NOT NULL LIMIT 1"
        ).fetchone()
    if pending is None:
        return DependencyMaintenanceResult()

    released: list[str] = []
    stopped: list[str] = []
    deferred: list[str] = []
    now = utc_now_iso()
    with closing(connect(path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT t.task_key, t.status, t.blocked_by, t.blocked_reason,
                   b.status AS blocker_status
            FROM tasks AS t
            LEFT JOIN tasks AS b ON b.task_key = t.blocked_by
            WHERE t.prompt IS NOT NULL AND t.blocked_by IS NOT NULL
            ORDER BY t.task_key
            """
        ).fetchall()
        for row in rows:
            blocker = row["blocked_by"]
            blocker_status = row["blocker_status"]
            if blocker_status in COMPLETED_BLOCKER_STATUSES:
                if (
                    row["status"] == "blocked"
                    and is_dependency_blocked_reason(row["blocked_reason"])
                    and not _active_attempt_id(conn, row["task_key"])
                ):
                    _write(
                        conn,
                        task_key=row["task_key"],
                        blocked_by=None,
                        from_status="blocked",
                        to_status="created",
                        blocked_reason=None,
                        kind=DEPENDENCY_RELEASED,
                        message=f"Released: blocker {blocker} is {blocker_status}",
                        payload={
                            "released_blocker": blocker,
                            "blocker_status": blocker_status,
                            "actor": actor,
                        },
                        now=now,
                    )
                    released.append(row["task_key"])
                continue
            if blocker_status is None or blocker_status in STOPPED_BLOCKER_STATUSES:
                if row["status"] in RUNNING_STATUSES:
                    deferred.append(row["task_key"])
                    continue
                if row["status"] in _NOT_REDIRECTED_STATUSES:
                    continue
                dependency_owned = row["status"] in READY_STATUSES or (
                    row["status"] == "blocked" and is_dependency_blocked_reason(row["blocked_reason"])
                )
                if row["status"] == "blocked" and not dependency_owned:
                    # Blocked by a failure: it already waits for an operator
                    # retry, and its failure reason must not be erased (D5).
                    continue
                shown = blocker_status or "missing"
                _write(
                    conn,
                    task_key=row["task_key"],
                    blocked_by=blocker,
                    from_status=row["status"],
                    to_status="needs_decision",
                    blocked_reason=None,
                    kind=DEPENDENCY_BLOCKER_STOPPED,
                    message=(
                        f"blocked_by {blocker}: the blocker is {shown}; decide whether to "
                        "remove or replace the dependency, retry the blocker, or cancel"
                    ),
                    payload={
                        "blocker_status": shown,
                        "actor": actor,
                        "previous_status": row["status"],
                        # Only a dependency wait may later return to ready by
                        # removing or replacing the dependency; anything else
                        # goes through the audited retry.
                        "dependency_owned": dependency_owned,
                    },
                    now=now,
                )
                stopped.append(row["task_key"])
    return DependencyMaintenanceResult(
        released=tuple(released),
        needs_decision=tuple(stopped),
        deferred_running=tuple(deferred),
    )


def unreleased_blocker(conn: sqlite3.Connection, blocked_by: str | None) -> bool:
    """True when ``blocked_by`` names a blocker that has not completed."""
    if not blocked_by:
        return False
    row = conn.execute("SELECT status FROM tasks WHERE task_key = ?", (blocked_by,)).fetchone()
    return row is None or row[0] not in COMPLETED_BLOCKER_STATUSES


__all__ = [
    "COMPLETED_BLOCKER_STATUSES",
    "DEPENDENCY_BLOCKER_STOPPED",
    "DEPENDENCY_REASON_PREFIX",
    "DEPENDENCY_RELEASED",
    "DEPENDENCY_REMOVED",
    "DEPENDENCY_SET",
    "DEPENDENCY_SOURCE",
    "DependencyChange",
    "DependencyMaintenanceResult",
    "STOPPED_BLOCKER_STATUSES",
    "TicketDependencyError",
    "dependency_blocked_reason",
    "is_dependency_blocked_reason",
    "maintain_dependencies",
    "remove_blocked_by",
    "set_blocked_by",
    "unreleased_blocker",
    "validate_blocked_by",
]
