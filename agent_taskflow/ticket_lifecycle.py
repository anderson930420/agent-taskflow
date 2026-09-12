"""Ticket identity and the §29 failure vocabulary (V1 Step 5, ruling 27).

A Ticket is a `tasks` row created through Step 1's prompt-first path: it has
Step 1's columns and a non-NULL `prompt` (the same test
:mod:`agent_taskflow.ticket_store` uses). A database where Step 1's migration
never ran has no Ticket at all.

Ruling 27 scopes the failure remap to Tickets. Legacy tasks, including the
GitHub-issue path, keep writing ``blocked`` (FOLLOWUPS F5). For a Ticket:

* SPEC §29.1: a Taskflow validator that returns ``failed`` stops at
  ``needs_decision``;
* SPEC §29.2: everything else is an infrastructure or runtime failure and ends
  ``failed`` — governance refusal, worktree preparation failure, an executor
  that fails, returns ``blocked`` (including a cooperative operator kill),
  raises or is unavailable, a validator that returns ``blocked``, raises or is
  unavailable, and an expired runtime lease.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from agent_taskflow.models import require_absolute_path
from agent_taskflow.tasks import normalize_task_key

TICKET_FAILED_STATUS = "failed"
TICKET_NEEDS_DECISION_STATUS = "needs_decision"

# The success terminal, split the same way the failure vocabulary is.
#
# V1 FOLLOWUPS F8 (RULINGS 53): a Ticket whose implementation and Taskflow
# validators both pass goes straight to `ready_for_integration`, which is where
# SPEC §22 depicts the per-repo Integration Queue holding it and what §22.1
# keys FIFO order on. The human gate stays where SPEC §31 and §44 put it —
# review of the GitHub PR before merge. §12's status model has no approval
# state between `validating` and `ready_for_integration`, and §18's manual
# controls list no "approve".
#
# The legacy GitHub-issue path keeps `waiting_approval` and its approve/reject
# routes; that split is FOLLOWUPS F5.
TICKET_SUCCESS_STATUS = "ready_for_integration"
LEGACY_SUCCESS_STATUS = "waiting_approval"

# Where a Ticket failure came from. Only VALIDATOR_RED stops for a decision.
FAILURE_GOVERNANCE = "governance_refusal"
FAILURE_WORKTREE = "worktree_preparation"
FAILURE_EXECUTOR = "executor"
FAILURE_VALIDATOR_RED = "validator_red"
FAILURE_VALIDATOR_ERROR = "validator_error"
FAILURE_LEASE_EXPIRED = "lease_expired"

FAILURE_KINDS = frozenset(
    {
        FAILURE_GOVERNANCE,
        FAILURE_WORKTREE,
        FAILURE_EXECUTOR,
        FAILURE_VALIDATOR_RED,
        FAILURE_VALIDATOR_ERROR,
        FAILURE_LEASE_EXPIRED,
    }
)

# A Ticket in one of these was stopped by the remap. Dispatching it again
# writes nothing (SPEC §44); the retry path is scripts/reset_task_status.py.
TICKET_STOPPED_STATUSES = frozenset({TICKET_FAILED_STATUS, TICKET_NEEDS_DECISION_STATUS})


def ticket_success_status(*, ticket: bool) -> str:
    """Return the persisted status a successful run ends in.

    A Ticket ends `ready_for_integration` (SPEC §22, §43.12); a legacy mirror
    row keeps `waiting_approval`.
    """
    return TICKET_SUCCESS_STATUS if ticket else LEGACY_SUCCESS_STATUS


def ticket_failure_status(kind: str) -> str:
    """Return the persisted status a Ticket failure of ``kind`` ends in."""
    if kind not in FAILURE_KINDS:
        raise ValueError(f"Unknown Ticket failure kind: {kind!r}")
    if kind == FAILURE_VALIDATOR_RED:
        return TICKET_NEEDS_DECISION_STATUS
    return TICKET_FAILED_STATUS


def tasks_has_ticket_columns(conn: sqlite3.Connection) -> bool:
    """Return True when `tasks` carries Step 1's `prompt` column."""
    return any(row[1] == "prompt" for row in conn.execute("PRAGMA table_info(tasks)"))


def is_ticket_in_connection(conn: sqlite3.Connection, task_key: str) -> bool:
    """Return True when ``task_key`` is a Ticket row. Never raises on legacy schemas."""
    if not tasks_has_ticket_columns(conn):
        return False
    row = conn.execute(
        "SELECT prompt IS NOT NULL FROM tasks WHERE task_key = ?",
        (normalize_task_key(task_key),),
    ).fetchone()
    return bool(row is not None and row[0])


def is_ticket(db_path: str | Path, task_key: str) -> bool:
    """Return True when ``task_key`` is a Ticket row. Read-only."""
    path = require_absolute_path(db_path, "db_path")
    if not path.exists():
        return False
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "tasks" not in tables:
            return False
        return is_ticket_in_connection(conn, task_key)


__all__ = [
    "FAILURE_EXECUTOR",
    "FAILURE_GOVERNANCE",
    "FAILURE_KINDS",
    "FAILURE_LEASE_EXPIRED",
    "FAILURE_VALIDATOR_ERROR",
    "FAILURE_VALIDATOR_RED",
    "FAILURE_WORKTREE",
    "LEGACY_SUCCESS_STATUS",
    "TICKET_FAILED_STATUS",
    "TICKET_NEEDS_DECISION_STATUS",
    "TICKET_STOPPED_STATUSES",
    "TICKET_SUCCESS_STATUS",
    "is_ticket",
    "is_ticket_in_connection",
    "tasks_has_ticket_columns",
    "ticket_failure_status",
    "ticket_success_status",
]
