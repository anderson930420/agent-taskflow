"""Ticket persistence over the canonical `tasks` table.

There is no separate Ticket table: `tasks` is the only canonical Ticket
entity. A Ticket is a `tasks` row whose Step 1 columns (added by the
`tasks_ticket_fields` migration in :mod:`agent_taskflow.store`) are populated,
and its creation audit event goes to the existing `task_events` log.

Task key allocation, the `tasks` insert and the audit write share one
`BEGIN IMMEDIATE` transaction, so two concurrent creations can never be handed
the same key — and therefore never the same branch or worktree path.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from agent_taskflow._helpers import require_non_empty
from agent_taskflow.models import (
    TaskEventRecord,
    require_absolute_path,
    utc_now_iso,
    validate_task_event_type,
    validate_task_status,
)
from agent_taskflow.store import (
    TaskMirrorStore,
    connect,
    default_db_path,
    init_db as init_task_db,
)
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.ticket_metadata import (
    FIRST_TASK_KEY_SEQUENCE,
    TASK_KEY_PREFIX,
    format_task_key,
    parse_task_key_sequence,
)
from agent_taskflow.ticket_models import TicketRecord, validate_ticket_priority


TICKET_CREATED_EVENT_TYPE = "created"

# `tasks` columns a TicketRecord reads. `project` holds the repository slug.
TICKET_SELECT_COLUMNS = (
    "task_key",
    "project",
    "prompt",
    "title",
    "priority",
    "status",
    "repo_path",
    "base_branch",
    "branch",
    "worktree_path",
    "artifact_dir",
    "ai_title_status",
    "branch_slug_source",
    "github_repo",
    "blocked_by",
    "commit_message_suggestion",
    "created_at",
    "updated_at",
)

# A `tasks` row is a prompt-first Ticket when it carries a prompt. Legacy
# mirror rows leave it NULL and stay readable through the task mirror API.
_IS_TICKET = "prompt IS NOT NULL"


class TicketStoreError(RuntimeError):
    """Raised when a Ticket cannot be persisted."""


TicketBuilder = Callable[[str], TicketRecord]


def _row_to_ticket(row: sqlite3.Row) -> TicketRecord:
    return TicketRecord(
        task_key=row["task_key"],
        repository=row["project"],
        prompt=row["prompt"],
        title=row["title"],
        priority=row["priority"],
        status=row["status"],
        repo_path=Path(row["repo_path"]),
        base_branch=row["base_branch"],
        branch=row["branch"],
        worktree_path=Path(row["worktree_path"]),
        artifact_dir=Path(row["artifact_dir"]),
        ai_title_status=row["ai_title_status"],
        branch_slug_source=row["branch_slug_source"],
        github_repo=row["github_repo"],
        blocked_by=row["blocked_by"],
        commit_message_suggestion=row["commit_message_suggestion"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class TicketStore:
    """SQLite access for prompt-first Tickets stored in `tasks`."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = (
            default_db_path()
            if db_path is None
            else require_absolute_path(db_path, "db_path")
        )

    def init_db(self) -> None:
        init_task_db(self.db_path)

    # ------------------------------------------------------------------
    # Task key allocation
    # ------------------------------------------------------------------

    @staticmethod
    def _next_task_key_sequence(conn: sqlite3.Connection) -> int:
        """One global counter over every `AT-<digits>` key in `tasks`.

        Legacy mirror keys of the same shape (`AT-0009`) are counted too, so a
        new Ticket can never reuse a key — or the `.worktrees/<key>` path — an
        existing task already owns. Other shapes (`AT-GH-188`) are ignored.
        """
        rows = conn.execute(
            "SELECT task_key FROM tasks WHERE task_key GLOB ?",
            (f"{TASK_KEY_PREFIX}-*",),
        ).fetchall()
        highest = 0
        for row in rows:
            sequence = parse_task_key_sequence(row["task_key"])
            if sequence is not None and sequence > highest:
                highest = sequence
        return max(highest + 1, FIRST_TASK_KEY_SEQUENCE)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def create_ticket(
        self,
        *,
        build: TicketBuilder,
        actor: str,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
        blocked_by: str | None = None,
    ) -> TicketRecord:
        """Allocate a task key, insert the `tasks` row, record the audit event.

        `build` receives the reserved task key and returns the fully derived
        Ticket. Allocation, insert and audit write share one transaction.
        """
        event_type = validate_task_event_type(TICKET_CREATED_EVENT_TYPE)
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
                    "SELECT 1 FROM tasks WHERE task_key = ?",
                    (normalized_blocked_by,),
                ).fetchone()
                if blocker is None:
                    raise TicketStoreError(
                        f"blocked_by task does not exist: {normalized_blocked_by}"
                    )

            task_key = format_task_key(self._next_task_key_sequence(conn))
            ticket = build(task_key)
            if ticket.task_key != task_key:
                raise TicketStoreError(
                    "Ticket builder returned a different task_key than allocated"
                )

            record = replace(
                ticket,
                created_at=ticket.created_at or created_at,
                updated_at=ticket.updated_at or created_at,
            )

            try:
                conn.execute(
                    """
                    INSERT INTO tasks (
                        task_key, project, board, title, status,
                        repo_path, artifact_dir,
                        created_at, updated_at, last_synced_at,
                        prompt, priority, ai_title_status, branch_slug_source,
                        blocked_by, github_repo, base_branch, branch,
                        worktree_path, commit_message_suggestion
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.task_key,
                        record.repository,
                        record.repository,
                        record.title,
                        record.status,
                        str(record.repo_path),
                        str(record.artifact_dir),
                        record.created_at,
                        record.updated_at,
                        record.created_at,
                        record.prompt,
                        record.priority,
                        record.ai_title_status,
                        record.branch_slug_source,
                        record.blocked_by,
                        record.github_repo,
                        record.base_branch,
                        record.branch,
                        str(record.worktree_path),
                        record.commit_message_suggestion,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO task_events (
                        task_key, event_type, source, message,
                        payload_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.task_key,
                        event_type,
                        normalized_actor,
                        message,
                        json.dumps(payload or {}, sort_keys=True),
                        record.created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TicketStoreError(
                    f"Could not persist ticket {record.task_key}: {exc}"
                ) from exc

            return record

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_ticket(self, task_key: str) -> TicketRecord | None:
        """Return the Ticket view of one `tasks` row, or None.

        None also covers legacy mirror rows, which have no Ticket columns.
        """
        normalized = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn:
            row = conn.execute(
                f"SELECT {', '.join(TICKET_SELECT_COLUMNS)} FROM tasks"
                f" WHERE task_key = ? AND {_IS_TICKET}",
                (normalized,),
            ).fetchone()
        return None if row is None else _row_to_ticket(row)

    def list_tickets(
        self,
        *,
        repository: str | None = None,
        statuses: Iterable[str] | None = None,
        priority: str | None = None,
    ) -> list[TicketRecord]:
        """List Tickets. `statuses` are persisted `TASK_STATUSES` values."""
        clauses: list[str] = [_IS_TICKET]
        params: list[Any] = []
        if repository:
            clauses.append("project = ?")
            params.append(repository)
        if statuses is not None:
            normalized = sorted({validate_task_status(value) for value in statuses})
            if not normalized:
                return []
            clauses.append(f"status IN ({', '.join('?' for _ in normalized)})")
            params.extend(normalized)
        if priority:
            clauses.append("priority = ?")
            params.append(validate_ticket_priority(priority))

        with closing(connect(self.db_path)) as conn:
            rows = conn.execute(
                f"SELECT {', '.join(TICKET_SELECT_COLUMNS)} FROM tasks"
                f" WHERE {' AND '.join(clauses)}"
                " ORDER BY created_at DESC, task_key DESC",
                tuple(params),
            ).fetchall()
        return [_row_to_ticket(row) for row in rows]

    def list_ticket_events(self, task_key: str) -> list[TaskEventRecord]:
        """Return the Ticket's `task_events` audit trail, oldest first."""
        return TaskMirrorStore(self.db_path).list_task_events(task_key)


__all__ = [
    "TICKET_CREATED_EVENT_TYPE",
    "TICKET_SELECT_COLUMNS",
    "TicketBuilder",
    "TicketStore",
    "TicketStoreError",
]
