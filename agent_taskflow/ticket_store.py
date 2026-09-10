"""SQLite persistence for V1 Tickets (SPEC §12, §44).

Task ID allocation and the Ticket insert share a single `BEGIN IMMEDIATE`
transaction, so two concurrent creations can never be handed the same Task ID
— and therefore never the same branch or worktree path.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from agent_taskflow._helpers import require_non_empty
from agent_taskflow.models import require_absolute_path, utc_now_iso
from agent_taskflow.store import connect, default_db_path
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.ticket_metadata import (
    FIRST_TICKET_SEQUENCE,
    format_ticket_id,
    parse_ticket_sequence,
)
from agent_taskflow.ticket_models import (
    TicketEventRecord,
    TicketRecord,
    validate_ticket_event_type,
    validate_ticket_priority,
    validate_ticket_status,
)
from agent_taskflow.ticket_schema import migrate_tickets


TICKET_COLUMNS = (
    "ticket_id",
    "repository",
    "ticket_prefix",
    "ticket_sequence",
    "prompt",
    "title",
    "title_source",
    "priority",
    "status",
    "blocked_by",
    "repo_path",
    "github_repo",
    "base_branch",
    "branch",
    "branch_slug_source",
    "worktree_path",
    "artifact_dir",
    "commit_message_suggestion",
    "created_at",
    "updated_at",
)


class TicketStoreError(RuntimeError):
    """Raised when a Ticket cannot be persisted."""


@dataclass(frozen=True)
class TicketAllocation:
    """The Task ID reserved for one in-flight Ticket creation."""

    ticket_id: str
    ticket_prefix: str
    ticket_sequence: int


TicketBuilder = Callable[[TicketAllocation], TicketRecord]


def _row_to_ticket(row: sqlite3.Row) -> TicketRecord:
    return TicketRecord(
        ticket_id=row["ticket_id"],
        repository=row["repository"],
        prompt=row["prompt"],
        title=row["title"],
        priority=row["priority"],
        status=row["status"],
        repo_path=Path(row["repo_path"]),
        base_branch=row["base_branch"],
        branch=row["branch"],
        worktree_path=Path(row["worktree_path"]),
        artifact_dir=Path(row["artifact_dir"]),
        ticket_prefix=row["ticket_prefix"],
        ticket_sequence=int(row["ticket_sequence"]),
        title_source=row["title_source"],
        branch_slug_source=row["branch_slug_source"],
        github_repo=row["github_repo"],
        blocked_by=row["blocked_by"],
        commit_message_suggestion=row["commit_message_suggestion"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_ticket_event(row: sqlite3.Row) -> TicketEventRecord:
    return TicketEventRecord(
        ticket_id=row["ticket_id"],
        event_type=row["event_type"],
        actor=row["actor"],
        message=row["message"],
        payload_json=row["payload_json"],
        created_at=row["created_at"],
        event_id=int(row["event_id"]),
    )


class TicketStore:
    """SQLite access for V1 Ticket records and their audit events."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = (
            default_db_path()
            if db_path is None
            else require_absolute_path(db_path, "db_path")
        )

    def init_db(self) -> None:
        migrate_tickets(self.db_path)

    # ------------------------------------------------------------------
    # Task ID allocation
    # ------------------------------------------------------------------

    def _next_sequence(self, conn: sqlite3.Connection, prefix: str) -> int:
        row = conn.execute(
            """
            SELECT MAX(ticket_sequence) AS highest
            FROM tickets
            WHERE ticket_prefix = ?
            """,
            (prefix,),
        ).fetchone()
        highest = int(row["highest"]) if row is not None and row["highest"] else 0

        # Legacy mirror task keys share the same `<PREFIX>-<n>` shape and the
        # same `.worktrees/<key>` layout, so skip past them too rather than
        # derive a worktree path that an existing task already owns.
        legacy = conn.execute(
            "SELECT task_key FROM tasks WHERE task_key GLOB ?",
            (f"{prefix}-*",),
        ).fetchall()
        for legacy_row in legacy:
            sequence = parse_ticket_sequence(legacy_row["task_key"], prefix)
            if sequence is not None and sequence > highest:
                highest = sequence

        return max(highest + 1, FIRST_TICKET_SEQUENCE)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def create_ticket(
        self,
        *,
        ticket_prefix: str,
        build: TicketBuilder,
        actor: str,
        event_type: str = "ticket_created",
        message: str | None = None,
        payload: dict[str, Any] | None = None,
        blocked_by: str | None = None,
    ) -> TicketRecord:
        """Allocate a Task ID, insert the Ticket, and record its audit event.

        `build` receives the reserved allocation and returns the fully derived
        Ticket. Allocation, insert and audit write share one transaction.
        """
        normalized_event_type = validate_ticket_event_type(event_type)
        normalized_actor = require_non_empty(actor, "actor")
        normalized_blocked_by = (
            normalize_task_key(blocked_by) if blocked_by is not None else None
        )
        created_at = utc_now_iso()

        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")

            if normalized_blocked_by is not None:
                # SPEC §5.2 rule 1: the blocker must exist. Cycle validation is
                # Step 5; a freshly allocated Ticket cannot yet be inside one.
                blocker = conn.execute(
                    "SELECT 1 FROM tickets WHERE ticket_id = ?",
                    (normalized_blocked_by,),
                ).fetchone()
                if blocker is None:
                    raise TicketStoreError(
                        f"blocked_by ticket does not exist: {normalized_blocked_by}"
                    )

            sequence = self._next_sequence(conn, ticket_prefix)
            allocation = TicketAllocation(
                ticket_id=format_ticket_id(ticket_prefix, sequence),
                ticket_prefix=ticket_prefix,
                ticket_sequence=sequence,
            )
            ticket = build(allocation)
            if ticket.ticket_id != allocation.ticket_id:
                raise TicketStoreError(
                    "Ticket builder returned a different ticket_id than allocated"
                )

            record = replace(
                ticket,
                created_at=ticket.created_at or created_at,
                updated_at=ticket.updated_at or created_at,
            )

            try:
                conn.execute(
                    f"""
                    INSERT INTO tickets ({", ".join(TICKET_COLUMNS)})
                    VALUES ({", ".join("?" for _ in TICKET_COLUMNS)})
                    """,
                    (
                        record.ticket_id,
                        record.repository,
                        record.ticket_prefix,
                        record.ticket_sequence,
                        record.prompt,
                        record.title,
                        record.title_source,
                        record.priority,
                        record.status,
                        record.blocked_by,
                        str(record.repo_path),
                        record.github_repo,
                        record.base_branch,
                        record.branch,
                        record.branch_slug_source,
                        str(record.worktree_path),
                        str(record.artifact_dir),
                        record.commit_message_suggestion,
                        record.created_at,
                        record.updated_at,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO ticket_events (
                        ticket_id, event_type, actor, message,
                        payload_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.ticket_id,
                        normalized_event_type,
                        normalized_actor,
                        message,
                        json.dumps(payload or {}, sort_keys=True),
                        record.created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TicketStoreError(
                    f"Could not persist ticket {record.ticket_id}: {exc}"
                ) from exc

            return record

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_ticket(self, ticket_id: str) -> TicketRecord | None:
        normalized = normalize_task_key(ticket_id)
        with closing(connect(self.db_path)) as conn:
            row = conn.execute(
                f"SELECT {', '.join(TICKET_COLUMNS)} FROM tickets WHERE ticket_id = ?",
                (normalized,),
            ).fetchone()
        return None if row is None else _row_to_ticket(row)

    def list_tickets(
        self,
        *,
        repository: str | None = None,
        status: str | None = None,
        priority: str | None = None,
    ) -> list[TicketRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if repository:
            clauses.append("repository = ?")
            params.append(repository)
        if status:
            clauses.append("status = ?")
            params.append(validate_ticket_status(status))
        if priority:
            clauses.append("priority = ?")
            params.append(validate_ticket_priority(priority))

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(connect(self.db_path)) as conn:
            rows = conn.execute(
                f"SELECT {', '.join(TICKET_COLUMNS)} FROM tickets{where}"
                " ORDER BY created_at DESC, ticket_id DESC",
                tuple(params),
            ).fetchall()
        return [_row_to_ticket(row) for row in rows]

    def list_ticket_events(self, ticket_id: str) -> list[TicketEventRecord]:
        normalized = normalize_task_key(ticket_id)
        with closing(connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT event_id, ticket_id, event_type, actor, message,
                       payload_json, created_at
                FROM ticket_events
                WHERE ticket_id = ?
                ORDER BY event_id
                """,
                (normalized,),
            ).fetchall()
        return [_row_to_ticket_event(row) for row in rows]


__all__ = [
    "TICKET_COLUMNS",
    "TicketAllocation",
    "TicketBuilder",
    "TicketStore",
    "TicketStoreError",
]
