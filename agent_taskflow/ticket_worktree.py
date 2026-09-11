"""One git worktree per Ticket (SPEC §9, §43.4; V1 Step 5, ruling 26).

Step 1 derives a Ticket's branch and worktree path as strings only. This module
turns them into the real thing, before any claim: a git worktree at
``tasks.worktree_path`` on ``tasks.branch``, created from ``tasks.base_branch``,
plus the Ticket's single ``task_worktrees`` row.

* Idempotent. A worktree that is already registered in the Ticket's repository
  on the Ticket's branch is left exactly as it is, clean or dirty. Nothing is
  cleaned, reset or discarded: a retry continues where the last Attempt stopped
  (SPEC §33.3, ruling 26e).
* Fail closed (ruling 26f). A path that exists but is not a worktree of this
  repository on the Ticket's branch, or a Ticket branch that exists without its
  worktree, is never recreated or deleted. The result says why; the caller ends
  the Ticket ``failed``.
* Audited. Creating the worktree, recording its row and refusing it each write a
  ``task_events`` entry.

It never runs for legacy tasks; they keep the Attempt-scoped fresh-worktree
contract (docs/attempt-scoped-runtime-resources.md).
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import subprocess
from typing import Any

from agent_taskflow.governance import (
    assert_not_main_repo_write,
    assert_worktree_inside_repo_worktrees,
)
from agent_taskflow.models import TaskWorktreeRecord, require_absolute_path
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.ticket_lifecycle import tasks_has_ticket_columns
from agent_taskflow.ticket_worktree_schema import require_ticket_worktree_resources

TICKET_WORKTREE_SOURCE = "ticket_worktree"

# task_events payload kinds.
TICKET_WORKTREE_CREATED = "ticket_worktree_created"
TICKET_WORKTREE_RECORDED = "ticket_worktree_recorded"
TICKET_WORKTREE_REUSED = "ticket_worktree_reused"
TICKET_WORKTREE_REFUSED = "ticket_worktree_refused"

# inspect_ticket_worktree states.
WORKTREE_ABSENT = "absent"
WORKTREE_READY = "ready"
WORKTREE_MISMATCH = "mismatch"

# ensure_ticket_worktree actions.
ACTION_CREATED = "created"
ACTION_EXISTING = "existing"
ACTION_REFUSED = "refused"


@dataclass(frozen=True)
class TicketWorktree:
    """The Step 1 derived identity of a Ticket's one worktree."""

    task_key: str
    repo_path: Path
    worktree_path: Path
    branch: str
    base_branch: str


@dataclass(frozen=True)
class TicketWorktreeInspection:
    state: str
    detail: str | None = None
    dirty: bool = False
    head_sha: str | None = None


@dataclass(frozen=True)
class TicketWorktreeResult:
    """What :func:`ensure_ticket_worktree` found or did."""

    task_key: str
    worktree_path: Path
    branch: str
    base_branch: str
    action: str
    reason: str | None = None
    dirty: bool = False
    base_sha: str | None = None
    head_sha: str | None = None

    @property
    def ok(self) -> bool:
        return self.action != ACTION_REFUSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key,
            "worktree_path": str(self.worktree_path),
            "branch": self.branch,
            "base_branch": self.base_branch,
            "action": self.action,
            "ok": self.ok,
            "reason": self.reason,
            "dirty": self.dirty,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
        }


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        shell=False,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _git_message(result: subprocess.CompletedProcess[bytes]) -> str:
    return (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()


def ticket_worktree_in_connection(
    conn: sqlite3.Connection,
    task_key: str,
) -> TicketWorktree | None:
    """Return the Ticket's derived worktree identity, or None for a legacy row."""
    if not tasks_has_ticket_columns(conn):
        return None
    row = conn.execute(
        """
        SELECT task_key, repo_path, worktree_path, branch, base_branch
        FROM tasks
        WHERE task_key = ? AND prompt IS NOT NULL
          AND worktree_path IS NOT NULL AND branch IS NOT NULL
        """,
        (normalize_task_key(task_key),),
    ).fetchone()
    if row is None:
        return None
    return TicketWorktree(
        task_key=str(row[0]),
        repo_path=require_absolute_path(row[1], "repo_path"),
        worktree_path=require_absolute_path(row[2], "worktree_path"),
        branch=str(row[3]),
        base_branch=str(row[4] or "main"),
    )


def ticket_worktree_for(db_path: str | Path, task_key: str) -> TicketWorktree | None:
    """Read-only lookup of a Ticket's derived worktree identity."""
    path = require_absolute_path(db_path, "db_path")
    if not path.exists():
        return None
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "tasks" not in tables:
            return None
        return ticket_worktree_in_connection(conn, task_key)


def _registered_worktrees(repo_path: Path) -> list[dict[str, str]] | None:
    result = _git(["worktree", "list", "--porcelain"], repo_path)
    if result.returncode != 0:
        return None
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        entries.append(current)
    return entries


def inspect_ticket_worktree(ticket: TicketWorktree) -> TicketWorktreeInspection:
    """Classify the Ticket's worktree path without changing anything."""
    repo_check = _git(["rev-parse", "--show-toplevel"], ticket.repo_path)
    if repo_check.returncode != 0:
        return TicketWorktreeInspection(
            WORKTREE_MISMATCH,
            f"repo_path is not a git repository: {_git_message(repo_check)}",
        )
    if Path(repo_check.stdout.decode().strip()).resolve() != ticket.repo_path.resolve():
        return TicketWorktreeInspection(
            WORKTREE_MISMATCH, "repo_path must be the git repository root"
        )
    entries = _registered_worktrees(ticket.repo_path)
    if entries is None:
        return TicketWorktreeInspection(WORKTREE_MISMATCH, "could not list git worktrees")
    target = ticket.worktree_path.resolve()
    registered = next(
        (
            entry
            for entry in entries
            if entry.get("worktree") and Path(entry["worktree"]).resolve() == target
        ),
        None,
    )
    if not ticket.worktree_path.exists():
        if registered is not None:
            return TicketWorktreeInspection(
                WORKTREE_MISMATCH,
                "the worktree is registered with git but its directory is missing",
            )
        return TicketWorktreeInspection(WORKTREE_ABSENT)
    if registered is None:
        return TicketWorktreeInspection(
            WORKTREE_MISMATCH,
            f"{ticket.worktree_path} exists but is not a git worktree of {ticket.repo_path}",
        )
    if registered.get("branch") != f"refs/heads/{ticket.branch}":
        found = registered.get("branch") or "a detached HEAD"
        return TicketWorktreeInspection(
            WORKTREE_MISMATCH,
            f"{ticket.worktree_path} is on {found}, not refs/heads/{ticket.branch}",
        )
    status = _git(["status", "--porcelain=v1", "-z"], ticket.worktree_path)
    if status.returncode != 0:
        return TicketWorktreeInspection(
            WORKTREE_MISMATCH, f"git status failed: {_git_message(status)}"
        )
    head = _git(["rev-parse", "HEAD"], ticket.worktree_path)
    return TicketWorktreeInspection(
        WORKTREE_READY,
        dirty=bool(status.stdout),
        head_sha=head.stdout.decode().strip() if head.returncode == 0 else None,
    )


def _branch_exists(ticket: TicketWorktree) -> bool:
    result = _git(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{ticket.branch}"],
        ticket.repo_path,
    )
    return result.returncode == 0


def _record_event(
    store: TaskMirrorStore,
    ticket: TicketWorktree,
    *,
    event_type: str,
    kind: str,
    message: str,
    source: str,
    extra: dict[str, Any] | None = None,
) -> None:
    store.record_task_event(
        ticket.task_key,
        event_type,
        source,
        message=message,
        payload={
            "kind": kind,
            "worktree_path": str(ticket.worktree_path),
            "branch": ticket.branch,
            "base_branch": ticket.base_branch,
            "repo_path": str(ticket.repo_path),
            **(extra or {}),
        },
    )


def _refuse(
    store: TaskMirrorStore,
    ticket: TicketWorktree,
    reason: str,
    *,
    source: str,
) -> TicketWorktreeResult:
    _record_event(
        store,
        ticket,
        event_type="note",
        kind=TICKET_WORKTREE_REFUSED,
        message=reason,
        source=source,
        extra={"deleted": False, "recreated": False},
    )
    return TicketWorktreeResult(
        task_key=ticket.task_key,
        worktree_path=ticket.worktree_path,
        branch=ticket.branch,
        base_branch=ticket.base_branch,
        action=ACTION_REFUSED,
        reason=reason,
    )


def _record_row_if_needed(
    store: TaskMirrorStore,
    ticket: TicketWorktree,
    *,
    base_sha: str | None,
    source: str,
) -> None:
    existing = store.get_task_worktree(ticket.task_key)
    if (
        existing is not None
        and Path(existing.worktree_path) == ticket.worktree_path
        and existing.branch == ticket.branch
        and Path(existing.repo_path) == ticket.repo_path
    ):
        return
    store.upsert_task_worktree(
        TaskWorktreeRecord(
            task_key=ticket.task_key,
            repo_path=ticket.repo_path,
            worktree_path=ticket.worktree_path,
            branch=ticket.branch,
            base_branch=ticket.base_branch,
            base_sha=base_sha,
            status="active",
        )
    )
    _record_event(
        store,
        ticket,
        event_type="worktree_recorded",
        kind=TICKET_WORKTREE_RECORDED,
        message=f"Recorded the Ticket's worktree {ticket.worktree_path}",
        source=source,
        extra={"base_sha": base_sha},
    )


def ensure_ticket_worktree(
    db_path: str | Path,
    task_key: str,
    *,
    source: str = TICKET_WORKTREE_SOURCE,
) -> TicketWorktreeResult:
    """Create or confirm the Ticket's one worktree and its row. Idempotent.

    Raises :class:`~agent_taskflow.ticket_worktree_schema.TicketWorktreeMigrationRequired`
    before touching anything when the Step 5 migration is missing, and
    ``ValueError`` for a row that is not a Ticket. Every other problem is a
    refused result the caller turns into ``failed``.
    """
    path = require_absolute_path(db_path, "db_path")
    require_ticket_worktree_resources(path)
    ticket = ticket_worktree_for(path, task_key)
    if ticket is None:
        raise ValueError(f"{normalize_task_key(task_key)} is not a Ticket with a derived worktree")
    store = TaskMirrorStore(path)

    try:
        assert_not_main_repo_write(ticket.worktree_path, ticket.repo_path)
        assert_worktree_inside_repo_worktrees(ticket.worktree_path, ticket.repo_path)
    except (OSError, ValueError) as exc:
        return _refuse(store, ticket, f"Ticket worktree path refused: {exc}", source=source)

    inspection = inspect_ticket_worktree(ticket)
    if inspection.state == WORKTREE_MISMATCH:
        return _refuse(
            store,
            ticket,
            f"Ticket worktree cannot be used: {inspection.detail}",
            source=source,
        )
    if inspection.state == WORKTREE_READY:
        existing = store.get_task_worktree(ticket.task_key)
        _record_row_if_needed(
            store,
            ticket,
            base_sha=existing.base_sha if existing is not None else inspection.head_sha,
            source=source,
        )
        return TicketWorktreeResult(
            task_key=ticket.task_key,
            worktree_path=ticket.worktree_path,
            branch=ticket.branch,
            base_branch=ticket.base_branch,
            action=ACTION_EXISTING,
            dirty=inspection.dirty,
            base_sha=existing.base_sha if existing is not None else None,
            head_sha=inspection.head_sha,
        )

    if _branch_exists(ticket):
        return _refuse(
            store,
            ticket,
            f"Ticket branch {ticket.branch} already exists without its worktree "
            f"{ticket.worktree_path}; refusing to reattach or recreate it",
            source=source,
        )
    base = _git(["rev-parse", "--verify", ticket.base_branch], ticket.repo_path)
    if base.returncode != 0:
        return _refuse(
            store,
            ticket,
            f"Ticket base branch {ticket.base_branch} could not be resolved: {_git_message(base)}",
            source=source,
        )
    base_sha = base.stdout.decode().strip()
    ticket.worktree_path.parent.mkdir(parents=True, exist_ok=True)
    created = _git(
        [
            "worktree",
            "add",
            str(ticket.worktree_path),
            "-b",
            ticket.branch,
            ticket.base_branch,
        ],
        ticket.repo_path,
    )
    if created.returncode != 0:
        return _refuse(
            store,
            ticket,
            f"git worktree add failed: {_git_message(created)}",
            source=source,
        )
    store.upsert_task_worktree(
        TaskWorktreeRecord(
            task_key=ticket.task_key,
            repo_path=ticket.repo_path,
            worktree_path=ticket.worktree_path,
            branch=ticket.branch,
            base_branch=ticket.base_branch,
            base_sha=base_sha,
            status="active",
        )
    )
    _record_event(
        store,
        ticket,
        event_type="worktree_recorded",
        kind=TICKET_WORKTREE_CREATED,
        message=f"Created the Ticket's worktree {ticket.worktree_path} on {ticket.branch}",
        source=source,
        extra={"base_sha": base_sha},
    )
    return TicketWorktreeResult(
        task_key=ticket.task_key,
        worktree_path=ticket.worktree_path,
        branch=ticket.branch,
        base_branch=ticket.base_branch,
        action=ACTION_CREATED,
        base_sha=base_sha,
        head_sha=base_sha,
    )


def record_ticket_worktree_reuse(
    store: TaskMirrorStore,
    ticket: TicketWorktree,
    *,
    attempt_id: str,
    attempt_number: int,
    dirty: bool,
    head_sha: str | None,
    source: str = TICKET_WORKTREE_SOURCE,
) -> None:
    """Audit that an Attempt runs in the Ticket's worktree, as it was left."""
    state = "dirty" if dirty else "clean"
    _record_event(
        store,
        ticket,
        event_type="note",
        kind=TICKET_WORKTREE_REUSED,
        message=(
            f"Attempt {attempt_number} runs in the Ticket's worktree as the last Attempt "
            f"left it ({state}); nothing was cleaned, reset or discarded"
        ),
        source=source,
        extra={
            "attempt_id": attempt_id,
            "attempt_number": attempt_number,
            "dirty": dirty,
            "head_sha": head_sha,
            "cleaned": False,
        },
    )


__all__ = [
    "ACTION_CREATED",
    "ACTION_EXISTING",
    "ACTION_REFUSED",
    "TICKET_WORKTREE_CREATED",
    "TICKET_WORKTREE_RECORDED",
    "TICKET_WORKTREE_REFUSED",
    "TICKET_WORKTREE_REUSED",
    "TICKET_WORKTREE_SOURCE",
    "TicketWorktree",
    "TicketWorktreeInspection",
    "TicketWorktreeResult",
    "WORKTREE_ABSENT",
    "WORKTREE_MISMATCH",
    "WORKTREE_READY",
    "ensure_ticket_worktree",
    "inspect_ticket_worktree",
    "record_ticket_worktree_reuse",
    "ticket_worktree_for",
    "ticket_worktree_in_connection",
]
