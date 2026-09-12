"""SPEC §43.12: hand a completed implementation to the per-repo queue.

V1 FOLLOWUPS F4. The execution pipeline ends when the dispatcher records
``waiting_approval`` after the executor and the Taskflow validators have both
passed. Until now nothing carried that finished implementation into Step 2's
per-repository Integration Queue, so the queue had no producer outside Step 2's
own re-integration edge (`docs/v1/handoff-step2-e2e.md`). This module is that
producer, and nothing more.

What it does:

* calls Step 2's *public* queue API (:func:`enqueue_for_integration`). It never
  touches the queue's internals, the per-repo lock, the controller, the watcher
  or the GitHub adapter;
* writes the §44 audit event for the handoff;
* is idempotent. A re-run, a retry or a second scheduler tick finds the
  existing entry, keeps its original timestamp, and audits the handoff once.

What it deliberately does not do:

* **it changes no lifecycle status.** The Ticket stays exactly where the
  dispatcher left it. WORKFLOW.md's human review gate at ``waiting_approval``
  and the integration controller's own ``ready_for_integration``
  compare-and-set both still stand between a queue entry and an integration
  run, and every Step 2 entry point still needs its explicit confirmation flag.
  A queue entry is a handoff record, not permission to integrate.

Only a Ticket that finished implementation is handed off. Every failure status
(``failed``, ``needs_decision``), every refusal (``blocked``, ``paused``) and
every legacy mirror row is skipped without writing anything.

A Ticket whose repository has no ``github_repo`` is also skipped: Step 2's
integration always fetches, pushes and updates a GitHub PR, so such a Ticket
could never leave the queue. That is a known limitation of the registry entry,
not of the handoff.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_taskflow.integration_queue import (
    IntegrationQueueEntry,
    enqueue_for_integration,
    queue_for_repo,
)
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.ticket_store import TicketStore


# The persisted status the dispatcher writes when implementation and the
# Taskflow validators are both done. It is the one status that hands off.
COMPLETION_STATUS = "waiting_approval"

HANDOFF_SOURCE = "integration_handoff"
HANDOFF_EVENT = "integration_queued"
QUEUE_NAME = "integration_queue"

# `IntegrationHandoffResult.status` values.
ENQUEUED = "enqueued"
ALREADY_QUEUED = "already_queued"
SKIPPED = "skipped"


@dataclass(frozen=True)
class IntegrationHandoffResult:
    """What the handoff did, for the caller and for the acceptance tests."""

    task_key: str
    status: str
    repo: str | None = None
    enqueued_at: str | None = None
    reason: str | None = None


def _entry_for(
    integration: IntegrationStore, task_key: str, repo: str
) -> IntegrationQueueEntry | None:
    for entry in queue_for_repo(integration, repo):
        if entry.task_key == task_key:
            return entry
    return None


def handoff_completed_implementation(
    store: TaskMirrorStore,
    task_key: str,
    *,
    task_status: str,
    source: str = HANDOFF_SOURCE,
    integration_store: IntegrationStore | None = None,
    ticket_store: TicketStore | None = None,
) -> IntegrationHandoffResult:
    """Put a Ticket that finished implementation in its own repo's queue.

    `task_status` is the status the run ended in. Anything other than
    :data:`COMPLETION_STATUS` is skipped without writing, so a failed, blocked,
    paused or undecided Ticket never reaches the queue.
    """
    key = normalize_task_key(task_key)

    if task_status != COMPLETION_STATUS:
        return IntegrationHandoffResult(
            task_key=key,
            status=SKIPPED,
            reason=f"status is {task_status!r}, not {COMPLETION_STATUS!r}",
        )

    tickets = ticket_store if ticket_store is not None else TicketStore(store.db_path)
    ticket = tickets.get_ticket(key)
    if ticket is None:
        return IntegrationHandoffResult(
            task_key=key, status=SKIPPED, reason="not a Ticket"
        )

    repo = (ticket.github_repo or "").strip()
    if not repo:
        return IntegrationHandoffResult(
            task_key=key,
            status=SKIPPED,
            reason=f"Ticket {key} has no github_repo; it cannot be integrated",
        )

    integration = (
        integration_store if integration_store is not None else IntegrationStore(store=store)
    )

    # Idempotence. The queue's own INSERT OR IGNORE already keeps the original
    # timestamp; this guard also keeps the audit event single and reports the
    # existing entry back to the caller.
    if integration.is_queued(key):
        existing = _entry_for(integration, key, repo)
        return IntegrationHandoffResult(
            task_key=key,
            status=ALREADY_QUEUED,
            repo=repo,
            enqueued_at=None if existing is None else existing.enqueued_at,
            reason="already queued",
        )

    entry = enqueue_for_integration(
        integration,
        key,
        repo=repo,
        source=source,
        priority=ticket.priority,
    )
    store.record_task_event(
        key,
        HANDOFF_EVENT,
        source,
        message=f"Ticket {key} handed off to the {repo} {QUEUE_NAME}",
        payload={
            "task_key": key,
            "repo": repo,
            "queue": QUEUE_NAME,
            "enqueued_at": entry.enqueued_at,
            "priority": ticket.priority,
            "task_status": task_status,
        },
    )
    return IntegrationHandoffResult(
        task_key=key,
        status=ENQUEUED,
        repo=repo,
        enqueued_at=entry.enqueued_at,
    )


__all__ = [
    "ALREADY_QUEUED",
    "COMPLETION_STATUS",
    "ENQUEUED",
    "HANDOFF_EVENT",
    "HANDOFF_SOURCE",
    "IntegrationHandoffResult",
    "QUEUE_NAME",
    "SKIPPED",
    "handoff_completed_implementation",
]
