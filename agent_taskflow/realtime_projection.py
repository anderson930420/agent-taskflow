"""Read-only board / Ticket projection for V1 Step 3 (SPEC §16, §17, §30, §31).

This module renders persisted SQLite state. It is a projection, never a source
of truth (§44): it opens read-only queries, computes no lifecycle decision, and
writes nothing at all — no Ticket status, no Attempt status, no schema.

Two shapes are produced:

``BoardProjection``
    the §16 live board — RUNNING / READY / BLOCKED / PAUSED / READY FOR REVIEW.

``TicketProjection``
    the §17 live Ticket page — repository, priority, status, branch, worktree,
    execution steps, current activity, artifact links, plus the read-only §31
    review surface (PR identity, Taskflow validator evidence, GitHub CI
    summary, reviewer hints, integrated base SHA).

Hard rules honoured here:

* The §32.1 PR fields are read-only and every one of them may be absent or
  null. A missing column, a missing row, or a null value renders as ``—``; it
  never raises and never blocks a render. Step 2 is their sole writer.
* No polling. Persisted state only; GitHub is never contacted.
* No completion claim of any kind (§14.2).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_taskflow.execution_observability import (
    ExecutionObservedStep,
    to_observability_dict,
)
from agent_taskflow.models import utc_now_iso
from agent_taskflow.runtime_progress import (
    RUNTIME_STEPS,
    RUNTIME_STEP_LABELS,
    AttemptProgressSnapshot,
    pending_step,
    step_glyph,
)
from agent_taskflow.runtime_progress_store import RuntimeProgressStore
from agent_taskflow.store import TaskMirrorStore, connect
from agent_taskflow.tasks import normalize_task_key


REALTIME_PROJECTION_SCHEMA_VERSION = "realtime_projection.v1"

#: Rendered in place of any null or missing value.
DASH = "—"

# -- §16 board sections ----------------------------------------------------

BOARD_SECTION_RUNNING = "RUNNING"
BOARD_SECTION_READY = "READY"
BOARD_SECTION_BLOCKED = "BLOCKED"
BOARD_SECTION_PAUSED = "PAUSED"
BOARD_SECTION_READY_FOR_REVIEW = "READY FOR REVIEW"

#: Exactly the five sections §16 shows, in the order it shows them.
BOARD_SECTIONS: tuple[str, ...] = (
    BOARD_SECTION_RUNNING,
    BOARD_SECTION_READY,
    BOARD_SECTION_BLOCKED,
    BOARD_SECTION_PAUSED,
    BOARD_SECTION_READY_FOR_REVIEW,
)

# The repository's own status vocabulary and the §12 V1 vocabulary are both
# mapped, because Step 1 (which introduces the §12 names) has not landed. A
# status that is absent from this table is deliberately left unsectioned rather
# than guessed into one of the five.
STATUS_SECTIONS: dict[str, str] = {
    # §12 V1 vocabulary
    "running": BOARD_SECTION_RUNNING,
    "preparing": BOARD_SECTION_RUNNING,
    "validating": BOARD_SECTION_RUNNING,
    "integrating": BOARD_SECTION_RUNNING,
    "ready_for_integration": BOARD_SECTION_RUNNING,
    "ready": BOARD_SECTION_READY,
    "queued": BOARD_SECTION_READY,
    "blocked": BOARD_SECTION_BLOCKED,
    "paused": BOARD_SECTION_PAUSED,
    "needs_review": BOARD_SECTION_READY_FOR_REVIEW,
    # existing repository vocabulary
    "implementing": BOARD_SECTION_RUNNING,
    "in_progress": BOARD_SECTION_RUNNING,
    "waiting_approval": BOARD_SECTION_READY_FOR_REVIEW,
    "waiting_for_review": BOARD_SECTION_READY_FOR_REVIEW,
    "review": BOARD_SECTION_READY_FOR_REVIEW,
}

BOARD_NOTES: tuple[str, ...] = (
    "Read-only projection of persisted SQLite state. Mission Control renders "
    "lifecycle; it does not own it (SPEC 2.1, SPEC 44).",
    "A Ticket in needs_decision is listed as unsectioned: SPEC 16 shows five "
    "board sections and does not include needs_decision. Flagged, not guessed.",
    "SPEC 32.1 PR fields are read-only here and may be absent or null; the "
    "Step 2 watcher is their sole writer.",
)

# -- §32.1 Ticket PR fields ------------------------------------------------

#: The §32.1 authoritative field list, in spec order. Read-only to Step 3.
PR_FIELD_NAMES: tuple[str, ...] = (
    "pr_number",
    "pr_url",
    "pr_state",
    "pr_merged",
    "pr_head_sha",
    "merge_commit_sha",
    "review_decision",
    "ci_status",
    "integrated_base_sha",
    "reintegration_count",
    "reintegration_required",
    "pr_last_polled_at",
)

_BOOL_PR_FIELDS = frozenset({"pr_merged", "reintegration_required"})
_INT_PR_FIELDS = frozenset({"pr_number", "reintegration_count"})

# Ticket keys look like ``AT-101`` / ``AT-GH-188`` / ``BJ-0001``.
_TICKET_KEY = re.compile(r"\b([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\b")


def _render(value: Any) -> str:
    if value is None:
        return DASH
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip()
    return text or DASH


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _row_value(row: sqlite3.Row, key: str) -> Any:
    return row[key] if key in row.keys() else None


# -- dataclasses -----------------------------------------------------------


@dataclass(frozen=True)
class TicketPrView:
    """Read-only §32.1 PR state. Every field may be absent or null."""

    #: Whether any §32.1 column exists yet — false until Step 2 lands.
    available: bool = False
    pr_number: int | None = None
    pr_url: str | None = None
    pr_state: str | None = None
    pr_merged: bool | None = None
    pr_head_sha: str | None = None
    merge_commit_sha: str | None = None
    review_decision: str | None = None
    ci_status: str | None = None
    integrated_base_sha: str | None = None
    reintegration_count: int | None = None
    reintegration_required: bool | None = None
    pr_last_polled_at: str | None = None

    def display(self) -> dict[str, str]:
        return {name: _render(getattr(self, name)) for name in PR_FIELD_NAMES}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"available": self.available}
        payload.update({name: getattr(self, name) for name in PR_FIELD_NAMES})
        payload["display"] = self.display()
        return payload


#: §17 fields that render ``—`` when absent.
_TICKET_DISPLAY_FIELDS: tuple[str, ...] = (
    "task_key",
    "title",
    "repository",
    "repo_path",
    "priority",
    "status",
    "section",
    "branch",
    "worktree_path",
    "attempt_id",
    "attempt_number",
    "current_phase",
    "current_activity",
    "blocker",
    "blocker_hint",
    "updated_at",
)


@dataclass(frozen=True)
class BoardTicket:
    """One Ticket as the board and the Ticket page render it."""

    task_key: str
    repository: str
    status: str
    section: str | None = None
    title: str | None = None
    repo_path: str | None = None
    priority: str | None = None
    branch: str | None = None
    worktree_path: str | None = None
    running: bool = False
    eligible_for_execution: bool = False
    blocked: bool = False
    paused: bool = False
    awaiting_review: bool = False
    blocker: str | None = None
    blocker_hint: str | None = None
    attempt_id: str | None = None
    attempt_number: int | None = None
    current_phase: str | None = None
    current_activity: str | None = None
    steps: tuple[ExecutionObservedStep, ...] = field(default_factory=tuple)
    pr: TicketPrView = field(default_factory=TicketPrView)
    updated_at: str | None = None

    def display(self) -> dict[str, str]:
        return {
            name: _render(getattr(self, name)) for name in _TICKET_DISPLAY_FIELDS
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key,
            "repository": self.repository,
            "status": self.status,
            "section": self.section,
            "title": self.title,
            "repo_path": self.repo_path,
            "priority": self.priority,
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "running": self.running,
            "eligible_for_execution": self.eligible_for_execution,
            "blocked": self.blocked,
            "paused": self.paused,
            "awaiting_review": self.awaiting_review,
            "blocker": self.blocker,
            "blocker_hint": self.blocker_hint,
            "attempt_id": self.attempt_id,
            "attempt_number": self.attempt_number,
            "current_phase": self.current_phase,
            "current_activity": self.current_activity,
            "steps": [_step_to_dict(step) for step in self.steps],
            "pr": self.pr.to_dict(),
            "updated_at": self.updated_at,
            "display": self.display(),
        }


@dataclass(frozen=True)
class BoardSection:
    key: str
    title: str
    tickets: tuple[BoardTicket, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "count": len(self.tickets),
            "tickets": [ticket.to_dict() for ticket in self.tickets],
        }


@dataclass(frozen=True)
class BoardProjection:
    schema_version: str
    generated_at: str
    sections: tuple[BoardSection, ...] = field(default_factory=tuple)
    unsectioned: tuple[BoardTicket, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = BOARD_NOTES

    def find(self, task_key: str) -> BoardTicket | None:
        normalized = normalize_task_key(task_key)
        for section in self.sections:
            for ticket in section.tickets:
                if ticket.task_key == normalized:
                    return ticket
        for ticket in self.unsectioned:
            if ticket.task_key == normalized:
                return ticket
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "sections": [section.to_dict() for section in self.sections],
            "unsectioned": [ticket.to_dict() for ticket in self.unsectioned],
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class TicketAttemptRef:
    """One selectable Attempt (§14.0: earlier Attempts stay viewable)."""

    attempt_id: str
    attempt_number: int
    is_active: bool
    current_phase: str | None = None
    current_activity: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "attempt_number": self.attempt_number,
            "is_active": self.is_active,
            "current_phase": self.current_phase,
            "current_activity": self.current_activity,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class TicketProjection:
    schema_version: str
    generated_at: str
    ticket: BoardTicket
    attempts: tuple[TicketAttemptRef, ...] = field(default_factory=tuple)
    selected_attempt_id: str | None = None
    artifacts: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    validators: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    reviewer_hints: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = BOARD_NOTES

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "ticket": self.ticket.to_dict(),
            "attempts": [item.to_dict() for item in self.attempts],
            "selected_attempt_id": self.selected_attempt_id,
            "artifacts": [dict(item) for item in self.artifacts],
            "validators": [dict(item) for item in self.validators],
            "reviewer_hints": list(self.reviewer_hints),
            "notes": list(self.notes),
        }


def _step_to_dict(step: ExecutionObservedStep) -> dict[str, Any]:
    return {
        "name": step.name,
        "label": RUNTIME_STEP_LABELS.get(step.name, step.name),
        "status": step.status,
        "glyph": step_glyph(step.status),
        "summary": step.summary,
        "metadata": to_observability_dict(step.metadata),
    }


def projection_to_dict(value: Any) -> Any:
    """Return a JSON-safe dict for a projection value."""

    if value is None:
        return None
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return to_observability_dict(value)


# -- reading ---------------------------------------------------------------


def _pr_view(row: sqlite3.Row, present: set[str]) -> TicketPrView:
    """Build the §32.1 view from whatever columns actually exist."""

    if not present:
        return TicketPrView(available=False)

    values: dict[str, Any] = {"available": True}
    for name in PR_FIELD_NAMES:
        raw = _row_value(row, name) if name in present else None
        if raw is None:
            values[name] = None
        elif name in _BOOL_PR_FIELDS:
            values[name] = bool(raw)
        elif name in _INT_PR_FIELDS:
            try:
                values[name] = int(raw)
            except (TypeError, ValueError):
                values[name] = None
        else:
            text = str(raw).strip()
            values[name] = text or None
    return TicketPrView(**values)


def _blocker(row: sqlite3.Row) -> tuple[str | None, str | None]:
    """Return ``(blocker_key, hint)`` from whatever the row actually holds."""

    blocked_by = _row_value(row, "blocked_by")
    if blocked_by:
        key = str(blocked_by).strip()
        if key:
            return key, f"Waiting for {key}"

    reason = _row_value(row, "blocked_reason")
    if not reason:
        return None, None
    text = str(reason).strip()
    if not text:
        return None, None
    match = _TICKET_KEY.search(text)
    return (match.group(1) if match else None), text


def _reviewer_hints(pr: TicketPrView) -> tuple[str, ...]:
    """Derive §38 attention hints from persisted state only."""

    hints: list[str] = []
    count = pr.reintegration_count or 0
    if count == 1:
        hints.append("Re-integrated after the target branch advanced.")
    elif count > 1:
        hints.append(f"Re-integrated {count} times.")
    if pr.reintegration_required:
        hints.append(
            "Re-integration required: the target branch advanced while this "
            "PR was waiting."
        )
    if pr.review_decision == "changes_requested":
        hints.append("Reviewer requested changes on this PR.")
    if pr.ci_status == "failure":
        hints.append(
            "GitHub CI is red. GitHub CI is not a Taskflow lifecycle "
            "authority (SPEC 30); branch protection, not Taskflow, gates the "
            "merge."
        )
    return tuple(hints)


def _latest_attempt_rows(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    if not _table_exists(conn, "attempts"):
        return {}
    rows = conn.execute(
        """
        SELECT
            tasks.task_key AS task_key,
            attempts.attempt_id AS attempt_id,
            attempts.attempt_number AS attempt_number
        FROM attempts
        JOIN tasks ON tasks.task_id = attempts.task_id
        ORDER BY attempts.attempt_number ASC
        """
    ).fetchall()
    latest: dict[str, sqlite3.Row] = {}
    for row in rows:
        latest[row["task_key"]] = row
    return latest


def _worktree_rows(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT task_key, branch, worktree_path FROM task_worktrees"
    ).fetchall()
    return {row["task_key"]: row for row in rows}


def _build_ticket(
    row: sqlite3.Row,
    *,
    pr_columns: set[str],
    worktree: sqlite3.Row | None,
    snapshot: AttemptProgressSnapshot | None,
) -> BoardTicket:
    status = str(row["status"])
    section = STATUS_SECTIONS.get(status.lower())
    blocker, hint = _blocker(row)
    steps = (
        snapshot.ordered_steps()
        if snapshot is not None
        else tuple(pending_step(name) for name in RUNTIME_STEPS)
    )
    priority = _row_value(row, "priority")

    return BoardTicket(
        task_key=row["task_key"],
        repository=str(row["project"]),
        status=status,
        section=section,
        title=_row_value(row, "title"),
        repo_path=_row_value(row, "repo_path"),
        priority=str(priority).strip() if priority else None,
        branch=worktree["branch"] if worktree is not None else None,
        worktree_path=worktree["worktree_path"] if worktree is not None else None,
        running=section == BOARD_SECTION_RUNNING,
        eligible_for_execution=section == BOARD_SECTION_READY,
        blocked=section == BOARD_SECTION_BLOCKED,
        paused=section == BOARD_SECTION_PAUSED,
        awaiting_review=section == BOARD_SECTION_READY_FOR_REVIEW,
        blocker=blocker,
        blocker_hint=hint,
        attempt_id=snapshot.attempt_id if snapshot is not None else None,
        attempt_number=snapshot.attempt_number if snapshot is not None else None,
        current_phase=snapshot.current_phase if snapshot is not None else None,
        current_activity=(
            snapshot.current_activity if snapshot is not None else None
        ),
        steps=steps,
        pr=_pr_view(row, pr_columns),
        updated_at=_row_value(row, "updated_at"),
    )


def build_board_projection(
    db_path: str | Path | None = None,
    *,
    project: str | None = None,
) -> BoardProjection:
    """Return the §16 live board as a read-only projection."""

    progress_store = RuntimeProgressStore(db_path)
    buckets: dict[str, list[BoardTicket]] = {key: [] for key in BOARD_SECTIONS}
    unsectioned: list[BoardTicket] = []

    with closing(connect(db_path)) as conn:
        pr_columns = _columns(conn, "tasks") & set(PR_FIELD_NAMES)
        worktrees = _worktree_rows(conn)
        latest_attempts = _latest_attempt_rows(conn)

        if project:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE project = ? ORDER BY task_key ASC",
                (project,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY task_key ASC"
            ).fetchall()

    # One batched read for the whole board. The SSE stream rebuilds this
    # projection on every poll, so a per-Ticket lookup here would mean a fresh
    # SQLite connection per Ticket per poll.
    snapshots = progress_store.snapshots_for_attempts(
        [attempt["attempt_id"] for attempt in latest_attempts.values()]
    )

    for row in rows:
        attempt = latest_attempts.get(row["task_key"])
        snapshot = (
            snapshots.get(attempt["attempt_id"]) if attempt is not None else None
        )
        ticket = _build_ticket(
            row,
            pr_columns=pr_columns,
            worktree=worktrees.get(row["task_key"]),
            snapshot=snapshot,
        )
        if ticket.section in buckets:
            buckets[ticket.section].append(ticket)
        else:
            unsectioned.append(ticket)

    return BoardProjection(
        schema_version=REALTIME_PROJECTION_SCHEMA_VERSION,
        generated_at=utc_now_iso(),
        sections=tuple(
            BoardSection(key=key, title=key, tickets=tuple(buckets[key]))
            for key in BOARD_SECTIONS
        ),
        unsectioned=tuple(unsectioned),
    )


def build_ticket_projection(
    db_path: str | Path | None = None,
    task_key: str = "",
    *,
    attempt_id: str | None = None,
) -> TicketProjection | None:
    """Return the §17 live Ticket page, or ``None`` if the Ticket is unknown."""

    try:
        normalized = normalize_task_key(task_key)
    except ValueError:
        return None

    progress_store = RuntimeProgressStore(db_path)

    with closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_key = ?", (normalized,)
        ).fetchone()
        if row is None:
            return None
        pr_columns = _columns(conn, "tasks") & set(PR_FIELD_NAMES)
        worktree = conn.execute(
            "SELECT task_key, branch, worktree_path FROM task_worktrees "
            "WHERE task_key = ?",
            (normalized,),
        ).fetchone()

    history = progress_store.list_attempt_progress(normalized)
    selected: AttemptProgressSnapshot | None = None
    if attempt_id:
        selected = next(
            (item for item in history if item.attempt_id == attempt_id), None
        )
    if selected is None and history:
        selected = history[-1]

    ticket = _build_ticket(
        row,
        pr_columns=pr_columns,
        worktree=worktree,
        snapshot=selected,
    )

    store = TaskMirrorStore(db_path)
    artifacts = tuple(
        {
            "artifact_type": record.artifact_type,
            "path": str(record.path),
            "created_at": record.created_at,
        }
        for record in store.list_task_artifacts(normalized)
    )
    validators = tuple(
        dict(item) for item in store.list_validation_results(normalized)
    )

    return TicketProjection(
        schema_version=REALTIME_PROJECTION_SCHEMA_VERSION,
        generated_at=utc_now_iso(),
        ticket=ticket,
        attempts=tuple(
            TicketAttemptRef(
                attempt_id=item.attempt_id,
                attempt_number=item.attempt_number,
                is_active=item.is_active,
                current_phase=item.current_phase,
                current_activity=item.current_activity,
                updated_at=item.updated_at,
            )
            for item in history
        ),
        selected_attempt_id=selected.attempt_id if selected is not None else None,
        artifacts=artifacts,
        validators=validators,
        reviewer_hints=_reviewer_hints(ticket.pr),
    )


__all__ = [
    "BOARD_NOTES",
    "BOARD_SECTIONS",
    "BOARD_SECTION_BLOCKED",
    "BOARD_SECTION_PAUSED",
    "BOARD_SECTION_READY",
    "BOARD_SECTION_READY_FOR_REVIEW",
    "BOARD_SECTION_RUNNING",
    "DASH",
    "PR_FIELD_NAMES",
    "REALTIME_PROJECTION_SCHEMA_VERSION",
    "STATUS_SECTIONS",
    "BoardProjection",
    "BoardSection",
    "BoardTicket",
    "TicketAttemptRef",
    "TicketPrView",
    "TicketProjection",
    "build_board_projection",
    "build_ticket_projection",
    "projection_to_dict",
]
