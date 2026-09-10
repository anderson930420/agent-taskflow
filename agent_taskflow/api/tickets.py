"""Mission Control HTTP entry point for V1 Ticket creation (SPEC §10, §11).

Read-only repository registry + prompt-first Ticket creation and readback.
These routes create local state only: no Git command, no worktree, no branch,
no executor, no GitHub call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from agent_taskflow.ticket_ai_metadata import TicketAIMetadataAdapter
from agent_taskflow.ticket_creation import (
    TicketCreationError,
    TicketCreationRequest,
    create_ticket,
    ticket_to_dict,
)
from agent_taskflow.ticket_models import (
    DEFAULT_TICKET_PRIORITY,
    TicketEventRecord,
    TicketRecord,
)
from agent_taskflow.ticket_repositories import (
    DEFAULT_PROJECTS_CONFIG_PATH,
    TicketRepositoryError,
    list_ticket_repositories,
)
from agent_taskflow.ticket_store import TicketStore


class CreateTicketRequest(BaseModel):
    """SPEC §10: the entire user-supplied Ticket creation surface."""

    repository: str
    prompt: str
    priority: str = DEFAULT_TICKET_PRIORITY
    blocked_by: str | None = None


class TicketResponse(BaseModel):
    """Stable Ticket creation/readback envelope."""

    ok: bool
    ticket_id: str | None = None
    status: str | None = None
    message: str
    item: dict[str, Any] | None = None


def ticket_event_to_dict(event: TicketEventRecord) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "ticket_id": event.ticket_id,
        "event_type": event.event_type,
        "actor": event.actor,
        "message": event.message,
        "payload_json": event.payload_json,
        "created_at": event.created_at,
    }


def build_ticket_router(
    ticket_store: TicketStore,
    *,
    projects_config_path: str | Path = DEFAULT_PROJECTS_CONFIG_PATH,
    ai_adapter: TicketAIMetadataAdapter | None = None,
) -> APIRouter:
    """Return the Ticket router bound to one store and repository registry."""
    router = APIRouter()

    def get_ticket_store() -> TicketStore:
        return ticket_store

    def ticket_or_404(ticket_id: str, store: TicketStore) -> TicketRecord:
        try:
            ticket = store.get_ticket(ticket_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=404,
                detail=f"Ticket not found: {ticket_id}",
            ) from exc
        if ticket is None:
            raise HTTPException(
                status_code=404,
                detail=f"Ticket not found: {ticket_id}",
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
    ) -> dict[str, object]:
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
        except TicketCreationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return {
            "ok": True,
            "ticket_id": result.ticket.ticket_id,
            "status": result.ticket.status,
            "message": "Ticket created",
            "item": ticket_to_dict(result.ticket),
        }

    @router.get("/api/tickets")
    def list_tickets(
        repository: str | None = Query(default=None),
        status: str | None = Query(default=None),
        priority: str | None = Query(default=None),
        store: TicketStore = Depends(get_ticket_store),
    ) -> dict[str, object]:
        try:
            tickets = store.list_tickets(
                repository=repository,
                status=status,
                priority=priority,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        items = [ticket_to_dict(ticket) for ticket in tickets]
        return {"items": items, "count": len(items)}

    @router.get("/api/tickets/{ticket_id}")
    def get_ticket(
        ticket_id: str,
        store: TicketStore = Depends(get_ticket_store),
    ) -> dict[str, object]:
        ticket = ticket_or_404(ticket_id, store)
        events = store.list_ticket_events(ticket.ticket_id)
        return {
            "item": ticket_to_dict(ticket),
            "events": [ticket_event_to_dict(event) for event in events],
        }

    return router


__all__ = [
    "CreateTicketRequest",
    "TicketResponse",
    "build_ticket_router",
    "ticket_event_to_dict",
]
