"""SPEC §43.12: hand a completed implementation to the per-repo queue.

V1 FOLLOWUPS F4, retargeted by F8. The execution pipeline ends when the
dispatcher records :data:`COMPLETION_STATUS` after the executor and the
Taskflow validators have both passed. This module carries that finished
implementation into Step 2's per-repository Integration Queue, and nothing
more.

What it does:

* calls Step 2's *public* queue API (:func:`enqueue_for_integration`). It never
  touches the queue's internals, the per-repo lock, the controller, the watcher
  or the GitHub adapter;
* writes the §44 audit event for the handoff;
* is idempotent. A re-run, a retry or a second scheduler tick finds the
  existing entry, keeps its original timestamp, and audits the handoff once;
* keys the entry on **the moment the Ticket entered ``ready_for_integration``**
  (SPEC §22.1), read back from that status write's own audit event rather than
  from the clock at enqueue time.

What it deliberately does not do:

* **it changes no lifecycle status.** The Ticket stays exactly where the
  dispatcher left it, at :data:`COMPLETION_STATUS`. Every Step 2 entry point
  still needs its explicit confirmation flag, so a queue entry is a handoff
  record, not permission to integrate.

Only a Ticket that finished implementation is handed off. Every failure status
(``failed``, ``needs_decision``), every refusal (``blocked``, ``paused``) and
every legacy mirror row is skipped without writing anything.

A Ticket whose repository has no ``github_repo`` is also skipped: Step 2's
integration always fetches, pushes and updates a GitHub PR, so such a Ticket
could never leave the queue. That is a known limitation of the registry entry,
not of the handoff.

The handoff is also where the **producer Attempt** is captured (Level 2 M2.2).
The execution caller passes the Attempt it held while producing the work; this
module records it against the one queue entry it created, keyed on that entry's
own ``id``. :func:`resolve_producer_attempt_binding` reads it back for the
integration run that consumes the entry. Four rules keep the binding exact:

* the record names the queue entry (sequence, repo and timestamp), so an entry
  from a later queue generation, or one enqueued by anything other than this
  handoff, resolves to no producer rather than to the previous entry's Attempt;
* only the latest handoff record counts, and a second producer for a still
  queued entry supersedes the binding instead of overwriting it, so a retry
  never inherits the earlier Attempt's identity;
* the recorded Attempt is checked against the Ticket's own task identity, so an
  Attempt belonging to another task is refused;
* a manual, watcher or legacy integration run that no handoff produced resolves
  to ``None`` with the reason, never to a guess from history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.integration_queue import (
    IntegrationQueueEntry,
    enqueue_for_integration,
    queue_for_repo,
)
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.ticket_lifecycle import TICKET_SUCCESS_STATUS
from agent_taskflow.ticket_store import TicketStore


# The persisted status the dispatcher writes when implementation and the
# Taskflow validators are both done. It is the one status that hands off.
#
# V1 FOLLOWUPS F8 (RULINGS 53) moved it from `waiting_approval` to
# `ready_for_integration`: SPEC §22 depicts the queue as holding Tickets at
# that status and §22.1 keys FIFO order on the moment they entered it. The
# legacy GitHub-issue path still ends `waiting_approval` and is skipped here
# both by this status check and by the Ticket check below.
COMPLETION_STATUS = TICKET_SUCCESS_STATUS

HANDOFF_SOURCE = "integration_handoff"
HANDOFF_EVENT = "integration_queued"
SUPERSEDED_EVENT = "integration_producer_superseded"
QUEUE_NAME = "integration_queue"

# `IntegrationHandoffResult.status` values.
ENQUEUED = "enqueued"
ALREADY_QUEUED = "already_queued"
SKIPPED = "skipped"

# `ProducerAttemptBinding.kind` values. A producer handoff is never a live
# runtime claim: the producing Attempt may already have released its claim.
BINDING_PRODUCER_HANDOFF = "producer_handoff"
BINDING_NONE = "none"

# `ProducerAttemptBinding.reason_code` values.
REASON_BOUND = "bound_from_execution_handoff"
REASON_NO_ENTRY = "no_queue_entry_for_repo"
REASON_NO_HANDOFF = "no_recorded_execution_handoff"
REASON_NO_BINDING_IN_EVENT = "queue_event_carries_no_producer_binding"
REASON_ENTRY_MISMATCH = "queue_entry_not_recorded_by_latest_handoff"
REASON_SUPERSEDED = "producer_superseded_by_later_attempt"
REASON_NO_PRODUCER = "handoff_recorded_no_producer_attempt"
REASON_TASK_MISMATCH = "producer_attempt_task_mismatch"

VERIFIED = "verified"
VERIFY_MISMATCH = "mismatch"
VERIFY_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class IntegrationHandoffResult:
    """What the handoff did, for the caller and for the acceptance tests."""

    task_key: str
    status: str
    repo: str | None = None
    enqueued_at: str | None = None
    reason: str | None = None
    producer_attempt_id: str | None = None
    producer_superseded: bool = False


@dataclass(frozen=True)
class ProducerAttemptBinding:
    """The Attempt that produced the work one queue entry carries, or none.

    ``attempt_id`` is ``None`` whenever the producer cannot be established from
    the record; ``reason_code`` and ``reason`` then say exactly why. There is no
    fallback to the task's active Attempt pointer, to the newest Attempt, or to
    another entry's producer.
    """

    task_key: str
    kind: str
    reason_code: str
    reason: str
    attempt_id: str | None = None
    repo: str | None = None
    queue_sequence: int | None = None
    enqueued_at: str | None = None
    handoff_source: str | None = None
    recorded_at: str | None = None
    task_identity_verified: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def bound(self) -> bool:
        return self.attempt_id is not None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_key": self.task_key,
            "attempt_id": self.attempt_id,
            "kind": self.kind,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "repo": self.repo,
            "queue_sequence": self.queue_sequence,
            "enqueued_at": self.enqueued_at,
            "handoff_source": self.handoff_source,
            "recorded_at": self.recorded_at,
            "task_identity_verified": self.task_identity_verified,
        }
        if self.detail:
            payload["detail"] = dict(self.detail)
        return payload


def _entered_status_at(store: TaskMirrorStore, task_key: str, status: str) -> str | None:
    """Return when the task entered ``status``, from its own audit trail.

    SPEC §22.1 keys the per-repo integration queue on the moment the Ticket
    entered ``ready_for_integration``, not on the moment the handoff got round
    to enqueueing it. `update_task_status` writes an append-only
    `status_changed` event stamped with exactly that moment, so this reads it
    back rather than re-reading the clock.

    Returns None when no such event exists, in which case the queue falls back
    to its own timestamp.
    """
    stamp: str | None = None
    for event in store.list_task_events(task_key):
        if event.event_type != "status_changed" or not event.payload_json:
            continue
        try:
            payload = json.loads(event.payload_json)
        except ValueError:  # pragma: no cover - the store writes valid JSON.
            continue
        if payload.get("status") == status and event.created_at:
            stamp = event.created_at
    return stamp


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
    producer_attempt_id: str | None = None,
) -> IntegrationHandoffResult:
    """Put a Ticket that finished implementation in its own repo's queue.

    `task_status` is the status the run ended in. Anything other than
    :data:`COMPLETION_STATUS` is skipped without writing, so a failed, blocked,
    paused or undecided Ticket never reaches the queue.

    `producer_attempt_id` is the Attempt the caller itself held while producing
    this implementation. The caller passes the id it captured; this module never
    looks one up, because by the time integration reads it the claim may be
    released and the task's active pointer may name a different run.
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
        superseded = _record_superseded_producer(
            store,
            key,
            entry=existing,
            source=source,
            producer_attempt_id=producer_attempt_id,
        )
        return IntegrationHandoffResult(
            task_key=key,
            status=ALREADY_QUEUED,
            repo=repo,
            enqueued_at=None if existing is None else existing.enqueued_at,
            reason="already queued",
            producer_attempt_id=producer_attempt_id,
            producer_superseded=superseded,
        )

    entry = enqueue_for_integration(
        integration,
        key,
        repo=repo,
        enqueued_at=_entered_status_at(store, key, task_status),
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
            "queue_sequence": entry.sequence,
            "enqueued_at": entry.enqueued_at,
            "priority": ticket.priority,
            "task_status": task_status,
            # The producing Attempt, exactly as the execution caller captured
            # it. Null means that caller held no Attempt, not that one is
            # waiting to be looked up.
            "producer_attempt_id": producer_attempt_id,
            "producer_attempt_binding": (
                BINDING_PRODUCER_HANDOFF if producer_attempt_id is not None else BINDING_NONE
            ),
        },
    )
    return IntegrationHandoffResult(
        task_key=key,
        status=ENQUEUED,
        repo=repo,
        enqueued_at=entry.enqueued_at,
        producer_attempt_id=producer_attempt_id,
    )


def _handoff_records(store: TaskMirrorStore, task_key: str) -> list[dict[str, Any]]:
    """Return this Ticket's handoff and supersession payloads, in order."""
    records: list[dict[str, Any]] = []
    for event in store.list_task_events(task_key):
        if event.event_type not in {HANDOFF_EVENT, SUPERSEDED_EVENT}:
            continue
        try:
            payload = json.loads(event.payload_json or "{}")
        except ValueError:  # pragma: no cover - the store writes valid JSON.
            continue
        if not isinstance(payload, dict):  # pragma: no cover - defensive.
            continue
        records.append(
            {
                "event_type": event.event_type,
                "source": event.source,
                "created_at": event.created_at,
                "payload": payload,
            }
        )
    return records


def _record_superseded_producer(
    store: TaskMirrorStore,
    task_key: str,
    *,
    entry: IntegrationQueueEntry | None,
    source: str,
    producer_attempt_id: str | None,
) -> bool:
    """Audit a second finisher for an entry that is still queued.

    The queue entry keeps its original position and timestamp (§22.1), so the
    entry a later run finds is the one the earlier Attempt created. Rather than
    let that earlier Attempt stand as the producer of work it did not do, this
    records the conflict; the binding then resolves to no producer.

    A later finisher that holds **no** Attempt supersedes just the same. It
    cannot name itself, but the tree the entry now carries may be its work, so
    leaving the earlier Attempt bound would attribute changed work to a run that
    did not do it (review finding M22-R1-N3). Nothing is inferred about who the
    later finisher was, and the queue is not touched.
    """
    if entry is None:
        return False
    recorded: str | None = None
    already_superseded = False
    for record in _handoff_records(store, task_key):
        payload = record["payload"]
        if payload.get("queue_sequence") != entry.sequence:
            continue
        if record["event_type"] == HANDOFF_EVENT:
            recorded = payload.get("producer_attempt_id")
            already_superseded = False
        elif payload.get("observed_producer_attempt_id") == producer_attempt_id:
            # Same later finisher, including an unbound one (null == null), so
            # a repeated tick audits once.
            already_superseded = True
    if recorded is None or recorded == producer_attempt_id or already_superseded:
        return False
    observed = (
        f"Attempt {producer_attempt_id}" if producer_attempt_id is not None
        else "a later run that holds no Attempt"
    )
    store.record_task_event(
        task_key,
        SUPERSEDED_EVENT,
        source,
        message=(
            f"Queue entry {entry.sequence} for {task_key} was produced by Attempt "
            f"{recorded}; {observed} finished the same Ticket while it was still "
            "queued"
        ),
        payload={
            "task_key": task_key,
            "repo": entry.repo,
            "queue": QUEUE_NAME,
            "queue_sequence": entry.sequence,
            "enqueued_at": entry.enqueued_at,
            "recorded_producer_attempt_id": recorded,
            "observed_producer_attempt_id": producer_attempt_id,
            "observed_producer_binding": (
                BINDING_PRODUCER_HANDOFF if producer_attempt_id is not None
                else BINDING_NONE
            ),
        },
    )
    return True


def _verify_task_identity(
    store: TaskMirrorStore, task_key: str, attempt_id: str
) -> tuple[str, dict[str, Any]]:
    """Check that ``attempt_id`` is an Attempt of ``task_key``.

    An Attempt row always carries the task identity it was created under, so a
    Ticket that has no identity at all cannot own one: that is a mismatch, not
    an unverifiable case. Only a missing Attempt row — a database without the
    Attempt tables, or a producer older than them — leaves the check
    unavailable, and the recorded handoff still stands on its own.
    """
    try:
        attempts = AttemptStore(store.db_path)
        identity = attempts.get_task_identity(task_key)
        attempt = attempts.get_attempt(attempt_id)
    except Exception as exc:  # noqa: BLE001 - a store without the Attempt tables.
        return VERIFY_UNAVAILABLE, {"verification_error": f"{type(exc).__name__}: {exc}"}
    if attempt is None:
        return VERIFY_UNAVAILABLE, {"verification_detail": "attempt row not found"}
    if identity is None:
        return VERIFY_MISMATCH, {
            "attempt_task_id": attempt.task_id,
            "ticket_task_id": None,
            "verification_detail": "task has no Attempt identity of its own",
        }
    if attempt.task_id != identity.task_id:
        return VERIFY_MISMATCH, {
            "attempt_task_id": attempt.task_id,
            "ticket_task_id": identity.task_id,
        }
    return VERIFIED, {"task_id": identity.task_id}


def resolve_producer_attempt_binding(
    store: TaskMirrorStore,
    integration: IntegrationStore,
    task_key: str,
    *,
    repo: str,
) -> ProducerAttemptBinding:
    """Return the producer Attempt bound to this Ticket's live queue entry."""
    key = normalize_task_key(task_key)

    def none(reason_code: str, reason: str, **fields: Any) -> ProducerAttemptBinding:
        return ProducerAttemptBinding(
            task_key=key,
            kind=BINDING_NONE,
            reason_code=reason_code,
            reason=reason,
            repo=repo,
            **fields,
        )

    entry = _entry_for(integration, key, repo)
    if entry is None:
        return none(
            REASON_NO_ENTRY,
            (
                f"{key} has no {repo} integration queue entry, so this run was not "
                "handed off by the execution pipeline. Manual, watcher and legacy "
                "runs have no authoritative producer Attempt."
            ),
        )

    handoff: dict[str, Any] | None = None
    superseded: dict[str, Any] | None = None
    for record in _handoff_records(store, key):
        if record["event_type"] == HANDOFF_EVENT:
            handoff = record
            superseded = None
        elif (
            handoff is not None
            and record["payload"].get("queue_sequence")
            == handoff["payload"].get("queue_sequence")
        ):
            superseded = record

    if handoff is None:
        return none(
            REASON_NO_HANDOFF,
            (
                f"{key} is queued for {repo} but no execution handoff is recorded, "
                "so the entry has no authoritative producer Attempt."
            ),
            queue_sequence=entry.sequence,
            enqueued_at=entry.enqueued_at,
        )

    payload = handoff["payload"]
    if "queue_sequence" not in payload:
        # The watcher's re-integration re-queue and a handoff written before
        # producer binding existed both look like this: a queue event that
        # names no entry and carries no Attempt.
        return none(
            REASON_NO_BINDING_IN_EVENT,
            (
                f"The latest {HANDOFF_EVENT} event for {key} (source "
                f"{handoff['source']}) names no queue entry and carries no "
                "producer Attempt, so nothing can be bound to this entry."
            ),
            queue_sequence=entry.sequence,
            enqueued_at=entry.enqueued_at,
            handoff_source=handoff["source"],
            recorded_at=handoff["created_at"],
        )

    mismatched = {
        name: (payload.get(name), observed)
        for name, observed in (
            ("task_key", key),
            ("repo", entry.repo),
            ("queue_sequence", entry.sequence),
            ("enqueued_at", entry.enqueued_at),
        )
        if payload.get(name) != observed
    }
    if mismatched:
        return none(
            REASON_ENTRY_MISMATCH,
            (
                f"The latest recorded handoff for {key} does not name this queue "
                "entry, so its Attempt belongs to another entry or another queue "
                "generation."
            ),
            queue_sequence=entry.sequence,
            enqueued_at=entry.enqueued_at,
            handoff_source=handoff["source"],
            recorded_at=handoff["created_at"],
            detail={"mismatched": {k: list(v) for k, v in mismatched.items()}},
        )

    if superseded is not None:
        observed_id = superseded["payload"].get("observed_producer_attempt_id")
        observed = (
            f"Attempt {observed_id}" if observed_id is not None
            else "a later run that held no Attempt"
        )
        return none(
            REASON_SUPERSEDED,
            (
                f"Queue entry {entry.sequence} was produced by Attempt "
                f"{superseded['payload'].get('recorded_producer_attempt_id')}, but "
                f"{observed} finished the same Ticket while it stayed queued. "
                "Neither can be claimed as the producer of what this run "
                "integrates."
            ),
            queue_sequence=entry.sequence,
            enqueued_at=entry.enqueued_at,
            handoff_source=handoff["source"],
            recorded_at=handoff["created_at"],
            detail={
                "recorded_producer_attempt_id": superseded["payload"].get(
                    "recorded_producer_attempt_id"
                ),
                "observed_producer_attempt_id": observed_id,
                "observed_producer_binding": superseded["payload"].get(
                    "observed_producer_binding", BINDING_PRODUCER_HANDOFF
                ),
                "superseded_at": superseded["created_at"],
            },
        )

    attempt_id = payload.get("producer_attempt_id")
    if attempt_id is None:
        return none(
            REASON_NO_PRODUCER,
            (
                f"The execution handoff for queue entry {entry.sequence} recorded no "
                "producer Attempt; its caller held none."
            ),
            queue_sequence=entry.sequence,
            enqueued_at=entry.enqueued_at,
            handoff_source=handoff["source"],
            recorded_at=handoff["created_at"],
        )

    verification, detail = _verify_task_identity(store, key, str(attempt_id))
    if verification == VERIFY_MISMATCH:
        return none(
            REASON_TASK_MISMATCH,
            (
                f"Recorded producer Attempt {attempt_id} belongs to another task, so "
                f"it cannot be bound to {key}."
            ),
            queue_sequence=entry.sequence,
            enqueued_at=entry.enqueued_at,
            handoff_source=handoff["source"],
            recorded_at=handoff["created_at"],
            task_identity_verified=verification,
            detail={**detail, "rejected_attempt_id": str(attempt_id)},
        )

    return ProducerAttemptBinding(
        task_key=key,
        attempt_id=str(attempt_id),
        kind=BINDING_PRODUCER_HANDOFF,
        reason_code=REASON_BOUND,
        reason=(
            f"Attempt {attempt_id} produced the implementation this entry carries, "
            f"captured at the {handoff['source']} handoff that created queue entry "
            f"{entry.sequence}. This is a completed producer, not a live runtime claim."
        ),
        repo=entry.repo,
        queue_sequence=entry.sequence,
        enqueued_at=entry.enqueued_at,
        handoff_source=handoff["source"],
        recorded_at=handoff["created_at"],
        task_identity_verified=verification,
        detail=detail,
    )


__all__ = [
    "ALREADY_QUEUED",
    "BINDING_NONE",
    "BINDING_PRODUCER_HANDOFF",
    "COMPLETION_STATUS",
    "ENQUEUED",
    "HANDOFF_EVENT",
    "HANDOFF_SOURCE",
    "IntegrationHandoffResult",
    "ProducerAttemptBinding",
    "QUEUE_NAME",
    "REASON_BOUND",
    "REASON_ENTRY_MISMATCH",
    "REASON_NO_BINDING_IN_EVENT",
    "REASON_NO_ENTRY",
    "REASON_NO_HANDOFF",
    "REASON_NO_PRODUCER",
    "REASON_SUPERSEDED",
    "REASON_TASK_MISMATCH",
    "SKIPPED",
    "SUPERSEDED_EVENT",
    "handoff_completed_implementation",
    "resolve_producer_attempt_binding",
]
