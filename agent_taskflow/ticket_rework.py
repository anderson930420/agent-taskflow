"""Replace a stopped Ticket with a fresh Ticket for the same prompt."""
from __future__ import annotations

import fcntl
import json
from pathlib import Path
from typing import Callable

from agent_taskflow.github_pr_adapter import GitHubPrAdapter
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.status_vocab import to_display_status, to_persisted_status
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.ticket_creation import TicketCreationRequest, create_ticket
from agent_taskflow.ticket_feedback import validate_note
from agent_taskflow.ticket_store import TicketStore


def rework_ticket(task_key: str, *, store: TicketStore, note: str | None = None,
                  projects_config_path: str | Path | None = None, ai_adapter=None,
                  github_adapter_factory: Callable = GitHubPrAdapter) -> str:
    if note is not None:
        validate_note(note)
    task_store = TaskMirrorStore(store.db_path)
    # Serialize competing rework requests, including the external close and creation.
    lock_path = Path(str(store.db_path) + ".rework.lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ticket = store.get_ticket(task_key)
        if ticket is None:
            raise KeyError(f"Ticket not found: {task_key}")
        for event in task_store.list_task_events(ticket.task_key):
            if event.event_type == "rework_created":
                return json.loads(event.payload_json)["new_task_key"]
        if to_display_status(ticket.status) not in {"needs_review", "needs_decision", "failed"}:
            raise ValueError(f"Ticket {task_key} status is {to_display_status(ticket.status)}; rework is not allowed")
        integration = IntegrationStore(store.db_path)
        pr = integration.get_pr_state(ticket.task_key)
        def close_pr(conn, record):
            current = conn.execute("SELECT status FROM tasks WHERE task_key = ?", (ticket.task_key,)).fetchone()
            if current is None or current[0] != ticket.status:
                raise ValueError(f"Ticket {ticket.task_key} status changed during rework")
            if pr.get("pr_number") and pr.get("pr_state") == "open" and not pr.get("pr_merged"):
                github_adapter_factory(ticket.github_repo).close_pr(
                    pr_number=pr["pr_number"], cwd=ticket.repo_path,
                    comment=f"Superseded by {record.task_key} (rework).")

        terminal_attempt = None
        def link_and_cancel(conn, record):
            nonlocal terminal_attempt
            task_store.record_task_event(ticket.task_key, "rework_created", "mission_control", payload={"new_task_key": record.task_key}, _conn=conn)
            task_store.record_task_event(record.task_key, "reworked_from", "mission_control", payload={"old_task_key": ticket.task_key}, _conn=conn)
            terminal_attempt = task_store.update_task_status(ticket.task_key, to_persisted_status("cancelled"), source="mission_control",
                message=f"Superseded by {record.task_key} (rework)", expected_current_status=ticket.status, _conn=conn)

        prompt = ticket.prompt + ("\n\n## Rework note\n" + note if note is not None else "")
        created = create_ticket(TicketCreationRequest(ticket.repository, prompt, ticket.priority),
                                store=store, projects_config_path=projects_config_path,
                                ai_adapter=ai_adapter, before_insert=close_pr,
                                after_insert=link_and_cancel).ticket
        if terminal_attempt is not None:
            from agent_taskflow.store import _publish_status_trigger_outcome_ledger
            _publish_status_trigger_outcome_ledger(store.db_path, ticket.task_key, terminal_attempt)
        return created.task_key
