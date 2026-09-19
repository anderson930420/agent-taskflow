"""One explicit FIFO integration queue drain (§22–23; rulings 59/62/64).

The snapshot bounds the invocation. The controller owns each integration,
its lock, lifecycle writes and dequeue; this consumer owns none of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from agent_taskflow.github_pr_adapter import GitHubPrAdapter
from agent_taskflow.governance import (
    assert_not_main_repo_write,
    assert_worktree_inside_repo_worktrees,
)
from agent_taskflow.integration_controller import IntegrationRequest, integrate_task
from agent_taskflow.integration_queue import queue_for_repo
from agent_taskflow.integration_schema import normalize_repo
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.integration_validators import IntegrationValidatorSpec
from agent_taskflow.models import require_absolute_path, utc_now_iso
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.ticket_fields_schema import require_ticket_fields
from agent_taskflow.ticket_store import TicketStore
from agent_taskflow.ticket_worktree import (
    WORKTREE_READY,
    TicketWorktree,
    inspect_ticket_worktree,
)


@dataclass(frozen=True)
class IntegrationTickRequest:
    repo: str
    repo_path: Path
    db_path: Path
    validator_specs: Sequence[IntegrationValidatorSpec]
    target_branch: str = "main"
    remote: str = "origin"
    dry_run: bool = True
    confirm_integration: bool = False
    owner: str = "integration_tick"

    def __post_init__(self) -> None:
        # Keep the queue's existing exact repository key; normalize only for
        # identity comparison, never silently select a differently keyed queue.
        normalize_repo(self.repo)
        object.__setattr__(self, "repo", self.repo.strip())
        for name in ("repo_path", "db_path"):
            object.__setattr__(self, name, require_absolute_path(getattr(self, name), name))
        for name in ("target_branch", "remote", "owner"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value.strip().startswith("-"):
                raise ValueError(f"{name} must be a nonempty name, not an option")
            object.__setattr__(self, name, value.strip())
        for name in ("dry_run", "confirm_integration"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        specs = tuple(self.validator_specs)
        if not specs or any(not isinstance(spec, IntegrationValidatorSpec) for spec in specs):
            raise ValueError("explicit nonempty integration validator_specs are required")
        if len({spec.name for spec in specs}) != len(specs):
            raise ValueError("integration validator names must be unique")
        object.__setattr__(self, "validator_specs", specs)


def _binding_error(
    request: IntegrationTickRequest, task_key: str, tasks: TaskMirrorStore,
    tickets: TicketStore,
) -> str | None:
    ticket = tickets.get_ticket(task_key)
    if ticket is None:
        return "ticket_missing"
    if not ticket.github_repo or normalize_repo(ticket.github_repo) != normalize_repo(request.repo):
        return "ticket_repository_mismatch"
    if ticket.repo_path.resolve() != request.repo_path.resolve():
        return "ticket_repo_path_mismatch"
    if ticket.base_branch != request.target_branch:
        return "ticket_target_branch_mismatch"
    worktree = tasks.get_task_worktree(task_key)
    if worktree is None:
        return "task_worktree_missing"
    if (
        worktree.repo_path.resolve() != ticket.repo_path.resolve()
        or worktree.worktree_path.resolve() != ticket.worktree_path.resolve()
        or worktree.branch != ticket.branch
        or worktree.base_branch != ticket.base_branch
        or worktree.status != "active"
    ):
        return "ticket_worktree_binding_mismatch"
    assert_not_main_repo_write(ticket.worktree_path, ticket.repo_path)
    assert_worktree_inside_repo_worktrees(ticket.worktree_path, ticket.repo_path)
    inspection = inspect_ticket_worktree(
        TicketWorktree(
            task_key=task_key, repo_path=ticket.repo_path,
            worktree_path=ticket.worktree_path, branch=ticket.branch,
            base_branch=ticket.base_branch,
        )
    )
    if inspection.state != WORKTREE_READY:
        return f"ticket_worktree_invalid: {inspection.detail or inspection.state}"
    return None


def run_integration_tick(
    request: IntegrationTickRequest,
    *,
    github: GitHubPrAdapter | None = None,
) -> dict[str, Any]:
    """Visit this repo's initial FIFO snapshot once, then return JSON evidence.

    A refusal or validator failure is reported and never retried in this tick;
    later snapshot entries may proceed. Lock contention or an escaping
    exception ends the tick. Entries added/re-enqueued during the pass wait
    for a later invocation. No queue row is deleted by the tick itself.
    """
    if not request.db_path.is_file():
        raise ValueError(f"db_path must name an existing initialized database: {request.db_path}")
    if not request.repo_path.is_dir():
        raise ValueError(f"repo_path must name an existing repository: {request.repo_path}")
    require_ticket_fields(request.db_path)
    tasks = TaskMirrorStore(request.db_path)
    integration = IntegrationStore(store=tasks)
    tickets = TicketStore(request.db_path)
    entries = queue_for_repo(integration, request.repo)
    effective_dry_run = request.dry_run or not request.confirm_integration
    outcomes: list[dict[str, Any]] = []
    stopped_reason = None
    for entry in entries:
        outcome: dict[str, Any] = {
            "task_key": entry.task_key, "sequence": entry.sequence,
            "enqueued_at": entry.enqueued_at, "priority": entry.priority,
            "controller_called": False,
        }
        try:
            # Another invocation may have completed or re-enqueued this item.
            current = {item.task_key: item for item in queue_for_repo(integration, request.repo)}
            if current.get(entry.task_key) != entry:
                outcome.update(ok=True, status="skipped", reason="queue_entry_changed")
            else:
                reason = _binding_error(request, entry.task_key, tasks, tickets)
                if reason:
                    outcome.update(ok=False, status="blocked", reason=reason)
                else:
                    outcome["controller_called"] = True
                    result = integrate_task(
                        IntegrationRequest(
                            task_key=entry.task_key, repo=request.repo,
                            db_path=request.db_path, target_branch=request.target_branch,
                            remote=request.remote, validator_specs=request.validator_specs,
                            owner=request.owner, dry_run=request.dry_run,
                            confirm_integration=request.confirm_integration, draft=True,
                        ),
                        store=tasks, integration_store=integration, github=github,
                    )
                    outcome.update(
                        ok=result.ok, status=result.status, reason=result.summary,
                        integration=result.to_summary_dict(),
                    )
                    if result.status == "lock_unavailable":
                        stopped_reason = "lock_unavailable"
        except Exception as exc:
            # Keep unexpected infrastructure failures observable without
            # inventing lifecycle writes or trying the same item again.
            outcome.update(
                ok=False, status="error", reason=f"{type(exc).__name__}: {exc}",
            )
            stopped_reason = "integration_error"
        outcomes.append(outcome)
        if stopped_reason:
            break
    remaining = queue_for_repo(integration, request.repo)
    ok = all(outcome["ok"] for outcome in outcomes)
    return {
        "kind": "integration_tick", "ok": ok,
        "status": (
            "stopped" if stopped_reason else "dry_run" if effective_dry_run
            else "drained" if not remaining else "partial"
        ),
        "repo": request.repo, "repo_path": str(request.repo_path),
        "db_path": str(request.db_path), "target_branch": request.target_branch,
        "remote": request.remote, "dry_run": effective_dry_run,
        "confirmation_required": not request.confirm_integration,
        "validator_specs": [
            {"name": spec.name, "command": list(spec.command),
             "timeout_seconds": spec.timeout_seconds}
            for spec in request.validator_specs
        ],
        "queued_count": len(entries), "visited_count": len(outcomes),
        "outcomes": outcomes, "stopped_reason": stopped_reason,
        "remaining_task_keys": [entry.task_key for entry in remaining],
        "generated_at": utc_now_iso(),
    }
