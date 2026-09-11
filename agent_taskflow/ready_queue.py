"""The ready queue: which Tickets may start, in what order (SPEC §3, §7, §20).

Eligible (step5.md layer 2):

* a Ticket (Step 1 row), in a claimable status (``created``, or a legacy
  ``queued`` reset) — so never ``blocked``, ``paused``, ``failed`` or
  ``needs_decision``;
* not blocked by an unreleased dependency (its blocker, if any, is in a §12
  ``completed`` status);
* not owned: no active runtime lease;
* not paused or killed by a runtime control (global, project, task);
* with the Step 1 derived worktree path and branch it needs to run.

Order (ruling 28 D3): priority ``critical > high > normal > low``, then
``created_at`` (FIFO), then ``task_key``. There is no preferred-order column
until Step 6's drag ordering. Read-only: it opens the database read-only, and
the claim itself re-checks every runtime control.

Legacy ``queued`` tasks are not scheduled here; the GitHub-issue path keeps its
own runner (ruling 27f).
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Any

from agent_taskflow.models import require_absolute_path
from agent_taskflow.ticket_dependencies import COMPLETED_BLOCKER_STATUSES
from agent_taskflow.ticket_lifecycle import tasks_has_ticket_columns

PRIORITY_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3}
CLAIMABLE_STATUSES = ("created", "queued")


@dataclass(frozen=True)
class ReadyTicket:
    task_key: str
    priority: str
    created_at: str
    status: str
    project: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key,
            "priority": self.priority,
            "created_at": self.created_at,
            "status": self.status,
            "project": self.project,
        }


def _sort_key(ticket: ReadyTicket) -> tuple[int, str, str]:
    return (PRIORITY_RANK.get(ticket.priority, PRIORITY_RANK["normal"]), ticket.created_at, ticket.task_key)


def eligible_tickets(db_path: str | Path) -> list[ReadyTicket]:
    """Return eligible Tickets in scheduling order. Deterministic."""
    path = require_absolute_path(db_path, "db_path")
    if not path.is_file():
        return []
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "tasks" not in tables or not tasks_has_ticket_columns(conn):
            return []
        lease_clause = (
            """
            AND NOT EXISTS (
                SELECT 1 FROM runtime_leases
                WHERE runtime_leases.task_id = t.task_id AND runtime_leases.is_active = 1
            )
            """
            if "runtime_leases" in tables and "task_id" in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
            else ""
        )
        placeholders = ", ".join("?" for _ in CLAIMABLE_STATUSES)
        completed = sorted(COMPLETED_BLOCKER_STATUSES)
        completed_placeholders = ", ".join("?" for _ in completed)
        rows = conn.execute(
            f"""
            SELECT t.task_key, t.priority, t.created_at, t.status, t.project
            FROM tasks AS t
            WHERE t.prompt IS NOT NULL
              AND t.worktree_path IS NOT NULL AND t.branch IS NOT NULL
              AND t.status IN ({placeholders})
              AND (
                  t.blocked_by IS NULL
                  OR EXISTS (
                      SELECT 1 FROM tasks AS b
                      WHERE b.task_key = t.blocked_by AND b.status IN ({completed_placeholders})
                  )
              )
              {lease_clause}
            """,
            (*CLAIMABLE_STATUSES, *completed),
        ).fetchall()
        halted = _halted_scopes(conn) if "runtime_controls" in tables else set()
    candidates = [
        ReadyTicket(
            task_key=str(row["task_key"]),
            priority=str(row["priority"] or "normal"),
            created_at=str(row["created_at"] or ""),
            status=str(row["status"]),
            project=str(row["project"] or ""),
        )
        for row in rows
    ]
    eligible = [
        ticket
        for ticket in candidates
        if ("global", "*") not in halted
        and ("project", ticket.project) not in halted
        and ("task", ticket.task_key) not in halted
    ]
    return sorted(eligible, key=_sort_key)


def _halted_scopes(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """Runtime-control scopes currently paused or kill-requested. Read-only.

    The same global / project / task scopes the claim's admission check
    applies before a Ticket has an Attempt (lifecycle_control). The project is
    matched on ``tasks.project`` directly, because a never-claimed Ticket has no
    task identity yet.
    """
    return {
        (str(row[0]), str(row[1]))
        for row in conn.execute(
            "SELECT scope_kind, scope_id FROM runtime_controls WHERE mode != 'running'"
        )
    }


__all__ = ["CLAIMABLE_STATUSES", "PRIORITY_RANK", "ReadyTicket", "eligible_tickets"]
