"""Per-repo integration queue and Loose Integration Lock (§22, §22.1, §23.1).

Two rules shape this module:

* **FIFO by entry timestamp.** Priority orders *execution*, never integration
  (§22.1), so ``priority`` is recorded for observability and deliberately not
  used as a sort key.
* **Loose lock.** The lock covers only the window that actually mutates the
  integration branch state. It is released once ``integrated_base_sha`` is
  recorded and never spans human review (§23.1), which is what lets several
  same-repo PRs sit in ``needs_review`` at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType

from agent_taskflow.integration_store import IntegrationStore


__all__ = [
    "IntegrationLock",
    "IntegrationLockUnavailable",
    "IntegrationQueueEntry",
    "enqueue_for_integration",
    "next_for_repo",
    "queue_for_repo",
    "remove_from_queue",
]


class IntegrationLockUnavailable(RuntimeError):
    """Raised when another runtime already holds a repo's integration lock."""


@dataclass(frozen=True)
class IntegrationQueueEntry:
    """One ticket waiting for its repo's integration slot."""

    task_key: str
    repo: str
    enqueued_at: str
    sequence: int
    source: str
    priority: str | None = None


def enqueue_for_integration(
    store: IntegrationStore,
    task_key: str,
    *,
    repo: str,
    enqueued_at: str | None = None,
    source: str = "integration_queue",
    priority: str | None = None,
) -> IntegrationQueueEntry:
    """Place a ticket in its repo queue.

    Idempotent: re-enqueueing an already-queued ticket keeps the original
    timestamp, so a repeated watcher tick cannot push a ticket to the back of
    its own queue.
    """
    store.enqueue(
        task_key,
        repo=repo,
        enqueued_at=enqueued_at,
        source=source,
        priority=priority,
    )
    for entry in queue_for_repo(store, repo):
        if entry.task_key == task_key:
            return entry
    raise RuntimeError(f"Failed to enqueue {task_key} for {repo}")


def queue_for_repo(store: IntegrationStore, repo: str) -> list[IntegrationQueueEntry]:
    """Return the repo's queue in FIFO order."""
    return [
        IntegrationQueueEntry(
            task_key=row["task_key"],
            repo=row["repo"],
            enqueued_at=row["enqueued_at"],
            sequence=row["id"],
            source=row["source"],
            priority=row["priority"],
        )
        for row in store.list_queue(repo)
    ]


def next_for_repo(store: IntegrationStore, repo: str) -> IntegrationQueueEntry | None:
    """Return the head of the repo's queue, or None when it is empty."""
    entries = queue_for_repo(store, repo)
    return entries[0] if entries else None


def remove_from_queue(store: IntegrationStore, task_key: str) -> None:
    """Drop a ticket from whichever repo queue holds it."""
    store.dequeue(task_key)


class IntegrationLock:
    """Context manager for the per-repo integration lock.

    Raises :class:`IntegrationLockUnavailable` on entry when another owner
    holds the lock, and always releases on exit, including when the body
    raises — a crashed integration must never wedge a repo.
    """

    def __init__(self, store: IntegrationStore, repo: str, *, owner: str) -> None:
        self._store = store
        self.repo = repo
        self.owner = owner
        self._held = False

    def __enter__(self) -> "IntegrationLock":
        if not self._store.acquire_integration_lock(self.repo, owner=self.owner):
            holder = self._store.get_integration_lock(self.repo) or {}
            raise IntegrationLockUnavailable(
                f"Integration lock for {self.repo} is held by "
                f"{holder.get('owner', 'another runtime')}"
            )
        self._held = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._held:
            self._store.release_integration_lock(self.repo, owner=self.owner)
            self._held = False

    @property
    def held(self) -> bool:
        return self._held
