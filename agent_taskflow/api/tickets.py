"""Mission Control HTTP entry point for V1 Ticket creation (SPEC §10, §11).

Read-only repository registry + prompt-first Ticket creation and readback.
A Ticket is a row in the canonical `tasks` table, so it is also visible
through the existing `/api/tasks` routes. These routes create local state
only: no Git command, no worktree, no branch, no executor, no GitHub call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agent_taskflow.models import TaskEventRecord
from agent_taskflow.status_vocab import persisted_statuses_for_display
from agent_taskflow.ticket_ai_metadata import TicketAIMetadataAdapter
from agent_taskflow.ticket_creation import (
    TicketBranchCheckUnavailableError,
    TicketBranchCollisionError,
    TicketCreationError,
    TicketCreationRequest,
    create_ticket,
    ticket_to_dict,
)
from agent_taskflow.ticket_models import DEFAULT_TICKET_PRIORITY, TicketRecord
from agent_taskflow.ticket_repositories import (
    TicketRepositoryError,
    list_ticket_repositories,
)
from agent_taskflow.ticket_store import TicketStore
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.ticket_retry import retry_ticket, TicketRetryError, TICKET_RETRY_FROM_STATUSES
from agent_taskflow.task_status_reset import TaskStatusResetRequest
from agent_taskflow.ticket_feedback import validate_note
from agent_taskflow.ticket_rework import rework_ticket
from agent_taskflow.github_pr_adapter import GitHubPrAdapter, GitHubPrError
from agent_taskflow.status_vocab import to_display_status


class CreateTicketRequest(BaseModel):
    """SPEC §10: the entire user-supplied Ticket creation surface."""

    repository: str
    prompt: str
    priority: str = DEFAULT_TICKET_PRIORITY
    blocked_by: str | None = None


class TicketActionRequest(BaseModel):
    note: str | None = None


class TicketResponse(BaseModel):
    """Stable Ticket creation/readback envelope."""

    ok: bool
    task_key: str | None = None
    status: str | None = None
    display_status: str | None = None
    message: str
    item: dict[str, Any] | None = None


def ticket_event_to_dict(event: TaskEventRecord) -> dict[str, Any]:
    return {
        "task_key": event.task_key,
        "event_type": event.event_type,
        "source": event.source,
        "message": event.message,
        "payload_json": event.payload_json,
        "created_at": event.created_at,
    }


def build_ticket_router(
    ticket_store: TicketStore,
    *,
    projects_config_path: str | Path | None = None,
    ai_adapter: TicketAIMetadataAdapter | None = None,
    github_adapter_factory=GitHubPrAdapter,
) -> APIRouter:
    """Return the Ticket router bound to one store and repository registry."""
    router = APIRouter()

    def get_ticket_store() -> TicketStore:
        return ticket_store

    def ticket_or_404(task_key: str, store: TicketStore) -> TicketRecord:
        try:
            ticket = store.get_ticket(task_key)
        except ValueError as exc:
            raise HTTPException(
                status_code=404,
                detail=f"Ticket not found: {task_key}",
            ) from exc
        if ticket is None:
            raise HTTPException(
                status_code=404,
                detail=f"Ticket not found: {task_key}",
            )
        return ticket

    @router.get("/api/repositories")
    def list_repositories() -> dict[str, object]:
        """SPEC §11: read-only registry feed for the repository dropdown."""
        try:
            repositories = list_ticket_repositories(projects_config_path)
        except TicketRepositoryError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        items = [repository.to_dict() for repository in repositories]
        return {"items": items, "count": len(items)}

    @router.post("/api/tickets", response_model=TicketResponse)
    def create_ticket_route(
        request: CreateTicketRequest,
        store: TicketStore = Depends(get_ticket_store),
    ) -> dict[str, object] | JSONResponse:
        try:
            creation_request = TicketCreationRequest(
                repository=request.repository,
                prompt=request.prompt,
                priority=request.priority,
                blocked_by=request.blocked_by,
            )
            result = create_ticket(
                creation_request,
                store=store,
                projects_config_path=projects_config_path,
                ai_adapter=ai_adapter,
            )
        except TicketBranchCollisionError as exc:
            # PR #195 ruling 4b: refuse, name the existing branch, never suffix.
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "detail": str(exc),
                    "branch": exc.branch,
                    "conflict_source": exc.source,
                    "existing": exc.existing,
                },
            )
        except TicketBranchCheckUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except TicketCreationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        item = ticket_to_dict(result.ticket)
        return {
            "ok": True,
            "task_key": result.ticket.task_key,
            "status": item["status"],
            "display_status": item["display_status"],
            "message": "Ticket created",
            "item": item,
        }

    @router.get("/api/tickets")
    def list_tickets(
        repository: str | None = Query(default=None),
        status: str | None = Query(
            default=None,
            description="SPEC §12 display status; matches every persisted alias.",
        ),
        priority: str | None = Query(default=None),
        store: TicketStore = Depends(get_ticket_store),
    ) -> dict[str, object]:
        try:
            statuses = (
                persisted_statuses_for_display(status) if status else None
            )
            tickets = store.list_tickets(
                repository=repository,
                statuses=statuses,
                priority=priority,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        items = [ticket_to_dict(ticket) for ticket in tickets]
        return {"items": items, "count": len(items)}

    @router.get("/api/tickets/{task_key}")
    def get_ticket(
        task_key: str,
        store: TicketStore = Depends(get_ticket_store),
    ) -> dict[str, object]:
        ticket = ticket_or_404(task_key, store)
        events = store.list_ticket_events(ticket.task_key)
        return {
            "item": ticket_to_dict(ticket),
            "events": [ticket_event_to_dict(event) for event in events],
        }

    @router.post("/api/tickets/{task_key}/retry")
    def retry_route(task_key: str, request: TicketActionRequest, store: TicketStore = Depends(get_ticket_store)) -> dict[str, object]:
        ticket = ticket_or_404(task_key, store)
        if request.note is not None:
            try:
                validate_note(request.note)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        if ticket.status not in TICKET_RETRY_FROM_STATUSES:
            raise HTTPException(status_code=409, detail=f"Ticket status is {to_display_status(ticket.status)}")
        try:
            retry_ticket(TaskStatusResetRequest(task_key=ticket.task_key, from_status=ticket.status,
                         reason="Owner requested retry", confirm_reset=True, actor="mission_control"),
                         store=TaskMirrorStore(store.db_path), note=request.note)
        except TicketRetryError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "task_key": ticket.task_key, "status": "created", "display_status": "ready"}

    @router.post("/api/tickets/{task_key}/rework")
    def rework_route(task_key: str, request: TicketActionRequest, store: TicketStore = Depends(get_ticket_store)) -> dict[str, object]:
        ticket = ticket_or_404(task_key, store)
        if request.note is not None:
            try:
                validate_note(request.note)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            new_key = rework_ticket(ticket.task_key, store=store, note=request.note,
                                    projects_config_path=projects_config_path, ai_adapter=ai_adapter,
                                    github_adapter_factory=github_adapter_factory)
        except GitHubPrError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        current = store.get_ticket(ticket.task_key)
        return {"ok": True, "task_key": ticket.task_key, "status": current.status,
                "display_status": to_display_status(current.status), "new_task_key": new_key}

    return router


__all__ = [
    "CreateTicketRequest",
    "TicketResponse",
    "build_ticket_router",
    "ticket_event_to_dict",
]
