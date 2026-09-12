"""Bounded AI conflict resolver interface and evidence (§27, §27.2.1).

The resolver is deliberately behind an explicit interface with its own
evidence record. Its authority is narrow: it may edit the conflicted files in
the task worktree and explain what it did. It cannot approve, merge, push,
change lifecycle state, or decide that a conflict "does not matter".

When no resolver is configured, or the configured one cannot produce a
conflict-free tree, §27.2.1 applies: the integration stops at
``needs_decision`` with the conflict hunks and the explanation persisted. There
is no auto retry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.tasks import normalize_task_key


__all__ = [
    "ConflictResolutionOutcome",
    "ConflictResolutionRequest",
    "ConflictResolver",
    "NullConflictResolver",
    "resolve_conflicts",
]


@dataclass(frozen=True)
class ConflictResolutionRequest:
    """The bounded input set a resolver is allowed to see (§27.2)."""

    task_key: str
    prompt: str
    worktree_path: Path
    conflict_hunks: tuple[Mapping[str, Any], ...]
    task_diff: str
    target_ref: str
    files: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        object.__setattr__(self, "worktree_path", Path(self.worktree_path))
        object.__setattr__(self, "conflict_hunks", tuple(self.conflict_hunks))
        object.__setattr__(self, "files", tuple(self.files))


@dataclass(frozen=True)
class ConflictResolutionOutcome:
    """What a resolver did.

    There is no approval, merge or lifecycle field here by design: a resolver
    reports a tree state and an explanation, nothing more.
    """

    resolver: str
    resolved: bool
    explanation: str


@runtime_checkable
class ConflictResolver(Protocol):
    """A bounded AI (or deterministic) integration conflict resolver."""

    name: str

    def resolve(self, request: ConflictResolutionRequest) -> ConflictResolutionOutcome:
        """Attempt to produce a conflict-free tree in the task worktree."""


class NullConflictResolver:
    """The default: resolves nothing, so conflicts go to a human (§27.2.1)."""

    name = "null"

    def resolve(self, request: ConflictResolutionRequest) -> ConflictResolutionOutcome:
        return ConflictResolutionOutcome(
            resolver=self.name,
            resolved=False,
            explanation=(
                "No bounded AI conflict resolver is configured; the integration "
                "conflict was left for human decision."
            ),
        )


def resolve_conflicts(
    request: ConflictResolutionRequest,
    *,
    resolver: ConflictResolver | None,
    integration_store: IntegrationStore,
    integration_run_id: str,
) -> ConflictResolutionOutcome:
    """Run the resolver (if any) and persist the §27.2.1 evidence either way."""
    active = resolver or NullConflictResolver()
    try:
        outcome = active.resolve(request)
    except Exception as exc:  # noqa: BLE001 - a resolver failure is a decision, not a crash
        outcome = ConflictResolutionOutcome(
            resolver=getattr(active, "name", "unknown"),
            resolved=False,
            explanation=f"Conflict resolver raised {type(exc).__name__}: {exc}",
        )

    integration_store.record_conflict_evidence(
        request.task_key,
        integration_run_id=integration_run_id,
        resolver=outcome.resolver,
        resolved=outcome.resolved,
        conflict_hunks=request.conflict_hunks,
        explanation=outcome.explanation,
    )
    return outcome
