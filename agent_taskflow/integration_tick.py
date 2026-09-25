"""One explicit FIFO integration queue drain (§22–23; rulings 59/62/64).

The snapshot bounds the invocation. The controller owns each integration,
its lock, lifecycle writes and dequeue; this consumer owns none of them.

V1-F10 (SPEC §47.2) composes the rest of the integration side around that
drain. With ``consumer_phases=True`` one pass runs, in this fixed order and
once each: the PR-outcome poll, verified-merge cleanup of this repository's
merged-unverified Tickets, the target-freshness poll, then the drain. Outcomes
first, so closed PRs are cancelled and dequeued and merges are recorded
before anything else; cleanup next, so a verified merge completes; freshness
after that, so a target the merge just advanced re-queues stale siblings; and
the drain last, so those re-queued Tickets are re-integrated in the same
pass. Each phase has its own confirmation and is a read-only preview without
it. No phase takes the per-repository integration lock (ruling 62), and none
ever confirms §37.1 cancelled-route cleanup or deletes a remote branch. Every
pass also reports, read-only and never repaired, the lock row, ready Tickets
missing from the queue, and verified merges whose Tickets are not completed.

Before any of that, a confirmed pass (the drain's confirmation) that finds
this repository's Tickets in ``integrating`` has the controller reconcile
them (``reconcile_repository``, OR-11): only a dead holder leaves a Ticket
there (RULINGS 69), and a lone one would otherwise never be reconciled,
because only the controller's flock acquire does that. The tick itself still
takes no lock. With no such Ticket nothing is locked or written; a dry run
only lists them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Sequence

from agent_taskflow import integration_schema as schema
from agent_taskflow.github_pr_adapter import GitHubPrAdapter
from agent_taskflow.governance import (
    assert_not_main_repo_write,
    assert_worktree_inside_repo_worktrees,
)
from agent_taskflow.integration_cleanup import (
    IntegrationCleanupRequest,
    run_integration_cleanup,
)
from agent_taskflow.integration_controller import (
    IntegrationRequest,
    integrate_task,
    reconcile_repository,
)
from agent_taskflow.integration_queue import queue_for_repo
from agent_taskflow.integration_schema import normalize_repo, repo_from_pr_url
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.integration_watcher import (
    WatcherRequest,
    poll_pr_outcomes,
    poll_target_freshness,
)
from agent_taskflow.execution_policy import ExecutionPolicyError, resolve_execution_policy
from agent_taskflow.integration_validators import IntegrationValidatorSpec
from agent_taskflow.models import TASK_STATUSES, require_absolute_path, utc_now_iso
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.ticket_fields_schema import require_ticket_fields
from agent_taskflow.ticket_store import TicketStore
from agent_taskflow.ticket_worktree import (
    WORKTREE_READY,
    TicketWorktree,
    inspect_ticket_worktree,
)


PHASE_PR_OUTCOMES = "pr_outcomes"
PHASE_CLEANUP = "verified_merge_cleanup"
PHASE_FRESHNESS = "target_freshness"
PHASE_DRAIN = "queue_drain"
# The documented, pinned order of one integration-tick pass (V1-F10).
PHASE_ORDER = (PHASE_PR_OUTCOMES, PHASE_CLEANUP, PHASE_FRESHNESS, PHASE_DRAIN)
# A drain entry whose request validators are not its policy's (RULINGS 67).
POLICY_INTEGRATION_VALIDATORS_MISMATCH = "execution_policy_integration_validators_mismatch"


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
    # V1-F10: the consumer phases before the drain. Off by default, so F9's
    # drain-only contract is unchanged. ``dry_run`` and ``confirm_integration``
    # keep governing only the drain; each phase has its own confirmation.
    consumer_phases: bool = False
    confirm_pr_poll: bool = False
    confirm_cleanup: bool = False
    confirm_freshness: bool = False

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
        for name in ("dry_run", "confirm_integration", "consumer_phases",
                     "confirm_pr_poll", "confirm_cleanup", "confirm_freshness"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        if not self.consumer_phases and (
            self.confirm_pr_poll or self.confirm_cleanup or self.confirm_freshness
        ):
            raise ValueError("a consumer phase confirmation requires consumer_phases=True")
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
    # RULINGS 67: integration validators are the Ticket's execution policy's,
    # never a second list kept by a caller.
    try:
        policy = resolve_execution_policy(ticket.repository)
    except ExecutionPolicyError as exc:
        return exc.reason_code
    if policy.integration_validator_specs() != tuple(request.validator_specs):
        return POLICY_INTEGRATION_VALIDATORS_MISMATCH
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

    With ``consumer_phases`` the drain is the last phase of the pass (see
    :data:`PHASE_ORDER`), and its snapshot is taken after the freshness poll.
    A consumer phase that raises ends the pass before the drain.
    """
    if not request.db_path.is_file():
        raise ValueError(f"db_path must name an existing initialized database: {request.db_path}")
    if not request.repo_path.is_dir():
        raise ValueError(f"repo_path must name an existing repository: {request.repo_path}")
    require_ticket_fields(request.db_path)
    tasks = TaskMirrorStore(request.db_path)
    integration = IntegrationStore(store=tasks)
    tickets = TicketStore(request.db_path)
    effective_dry_run = request.dry_run or not request.confirm_integration
    # Read before the reconciliation below, which clears a dead holder's row.
    holder = integration.get_integration_lock(request.repo)
    integrating = _integrating_tickets(request, tasks, integration, tickets)
    crash = _crash_reconciliation(
        request, tasks, integration, integrating, confirmed=not effective_dry_run,
    )
    consumer = (
        _run_consumer_phases(request, tasks, integration, github, holder)
        if request.consumer_phases else None
    )
    entries = [] if consumer and consumer["error"] else queue_for_repo(integration, request.repo)
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
    ok = all(outcome["ok"] for outcome in outcomes) and crash["ok"]
    result = {
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
        "integrating_tickets": integrating,
        "crash_reconciliation": crash,
        "generated_at": utc_now_iso(),
    }
    if consumer is None:
        return result
    return _with_consumer_phases(result, consumer, request, integration, tickets)


def _integrating_tickets(
    request: IntegrationTickRequest, tasks: TaskMirrorStore, integration: IntegrationStore,
    tickets: TicketStore,
) -> list[dict[str, Any]]:
    """This repository's Tickets in ``integrating``, read-only.

    A Ticket belongs to the repository when its ``github_repo``, its queue
    row or its PR URL names it; the reconciler filters again under the lock.
    """
    wanted = normalize_repo(request.repo)
    queue_repos = integration.list_queue_repos()
    found = []
    for task in tasks.list_tasks(status=schema.INTEGRATING):
        ticket = tickets.get_ticket(task.task_key)
        pr = integration.get_pr_state(task.task_key)
        names = set()
        for name in (ticket.github_repo if ticket else None, queue_repos.get(task.task_key)):
            try:
                names.add(normalize_repo(name) if name else None)
            except ValueError:
                continue
        names.add(repo_from_pr_url(pr["pr_url"]))
        if wanted in names:
            state = integration.get_integration_state(task.task_key)
            found.append({"task_key": task.task_key, "status": task.status,
                          "checkpoint": state["last_integration_status"],
                          "queued": task.task_key in queue_repos,
                          "pr_number": pr["pr_number"], "pr_url": pr["pr_url"]})
    return sorted(found, key=lambda item: item["task_key"])


def _crash_reconciliation(
    request: IntegrationTickRequest, tasks: TaskMirrorStore, integration: IntegrationStore,
    integrating: list[dict[str, Any]], *, confirmed: bool,
) -> dict[str, Any]:
    """Have the controller reconcile this repository's ``integrating`` Tickets."""
    report: dict[str, Any] = {"ran": False, "confirmed": confirmed, "ok": True, "reports": []}
    if not integrating:
        return {**report, "status": "not_needed"}
    if not confirmed:
        return {**report, "status": "dry_run"}
    try:
        result = reconcile_repository(
            request.repo, repo_path=request.repo_path,
            task_key=integrating[0]["task_key"], db_path=request.db_path,
            owner=request.owner, store=tasks, integration_store=integration,
        )
    except Exception as exc:
        # Like a drain error: observable, not retried in this tick.
        return {**report, "ran": True, "ok": False, "status": "error",
                "reason": f"{type(exc).__name__}: {exc}"}
    return {**report, "ran": True, **result}


# -- V1-F10 consumer phases ------------------------------------------------


def _run_consumer_phases(
    request: IntegrationTickRequest,
    tasks: TaskMirrorStore,
    integration: IntegrationStore,
    github: GitHubPrAdapter | None,
    holder: dict[str, Any] | None,
) -> dict[str, Any]:
    """Run the three pre-drain phases once each, in :data:`PHASE_ORDER`."""
    watcher = WatcherRequest(
        repo=request.repo, repo_path=request.repo_path,
        target_branch=request.target_branch, remote=request.remote,
        db_path=request.db_path,
    )
    runners = (
        (PHASE_PR_OUTCOMES, request.confirm_pr_poll,
         lambda: _pr_outcomes_phase(request, watcher, tasks, integration, github)),
        (PHASE_CLEANUP, request.confirm_cleanup,
         lambda: _cleanup_phase(request, tasks, integration)),
        (PHASE_FRESHNESS, request.confirm_freshness,
         lambda: _freshness_phase(request, watcher, tasks, integration)),
    )
    phases: dict[str, dict[str, Any]] = {}
    error: dict[str, str] | None = None
    for name, confirmed, run in runners:
        if error:
            phases[name] = {"ran": False, "confirmed": confirmed, "ok": False,
                            "status": "not_run", "reason": f"phase {error['phase']} raised"}
            continue
        try:
            phases[name] = {"ran": True, "confirmed": confirmed, **run()}
        except Exception as exc:
            # Like an escaping drain error: observable, no retry, pass ends.
            error = {"phase": name, "reason": f"{type(exc).__name__}: {exc}"}
            phases[name] = {"ran": True, "confirmed": confirmed, "ok": False,
                            "status": "error", "reason": error["reason"]}
    return {
        "phases": phases,
        "error": error,
        "integration_lock_at_start": (
            {"held": False} if holder is None else {
                "held": True, "repo": holder["repo"], "owner": holder["owner"],
                "acquired_at": holder["acquired_at"],
                "note": "Read-only report, taken at the start of the tick. The row is "
                        "a journal of the flock (RULINGS 69); the next flock holder "
                        "reconciles and clears a row left by a killed integration.",
            }
        ),
    }


def _pr_outcomes_phase(
    request: IntegrationTickRequest, watcher: WatcherRequest, tasks: TaskMirrorStore,
    integration: IntegrationStore, github: GitHubPrAdapter | None,
) -> dict[str, Any]:
    outcomes = poll_pr_outcomes(
        replace(watcher, confirm_poll=request.confirm_pr_poll),
        store=tasks, integration_store=integration, github=github,
    )
    # A failed poll is audited by the watcher when confirmed and always
    # reported here; it is never swallowed.
    return {
        "ok": not any(outcome.poll_error for outcome in outcomes),
        "outcomes": [asdict(outcome) for outcome in outcomes],
    }


def _cleanup_phase(
    request: IntegrationTickRequest, tasks: TaskMirrorStore, integration: IntegrationStore,
) -> dict[str, Any]:
    candidates = [
        state["task_key"]
        for state in integration.list_merged_unverified_pr_states(request.repo)
    ]
    outcomes: list[dict[str, Any]] = []
    for task_key in candidates:
        try:
            result = run_integration_cleanup(
                IntegrationCleanupRequest(
                    task_key=task_key, repo=request.repo, repo_path=request.repo_path,
                    target_branch=request.target_branch, remote=request.remote,
                    db_path=request.db_path, confirm_cleanup=request.confirm_cleanup,
                    # The §37.1 cancelled route is never automated, and remote
                    # branch cleanup is off in V1 (ruling 5).
                    confirm_cancelled_cleanup=False, delete_remote_branch=False,
                ),
                store=tasks, integration_store=integration,
            )
            outcomes.append(result.to_summary_dict())
        except Exception as exc:
            outcomes.append({"kind": "integration_cleanup", "task_key": task_key,
                             "ok": False, "status": "error",
                             "summary": f"{type(exc).__name__}: {exc}"})
    return {
        "ok": all(outcome["ok"] for outcome in outcomes),
        "candidates": candidates,
        "outcomes": outcomes,
    }


def _freshness_phase(
    request: IntegrationTickRequest, watcher: WatcherRequest, tasks: TaskMirrorStore,
    integration: IntegrationStore,
) -> dict[str, Any]:
    candidates = [state["task_key"] for state in integration.list_open_pr_states(request.repo)]
    outcomes = poll_target_freshness(
        replace(watcher, confirm_poll=request.confirm_freshness),
        store=tasks, integration_store=integration,
    )
    examined = {outcome.task_key for outcome in outcomes}
    # The poll skips a Ticket whose worktree is missing or unreadable without
    # an outcome; name it, so a blind watcher is never mistaken for a fresh one.
    not_examined = [key for key in candidates if key not in examined]
    return {
        "ok": not not_examined,
        "outcomes": [asdict(outcome) for outcome in outcomes],
        "requeued": [outcome.task_key for outcome in outcomes if outcome.requeued],
        "would_requeue": [
            outcome.task_key for outcome in outcomes
            if outcome.stale and not outcome.requeued
            and outcome.task_status == schema.NEEDS_REVIEW and not request.confirm_freshness
        ],
        "not_examined": not_examined,
    }


def _ready_unqueued(
    request: IntegrationTickRequest, integration: IntegrationStore, tickets: TicketStore,
) -> list[dict[str, Any]]:
    """This repository's ready_for_integration Tickets missing from any queue.

    Detected and reported only (orchestrator ruling OR-3, orphan G3c): the
    dispatcher's handoff audits an enqueue failure rather than failing the
    run, so such a Ticket is never integrated. Re-enqueueing is lifecycle
    behaviour that is not ruled, so the tick never does it.
    """
    wanted = normalize_repo(request.repo)
    found = []
    for ticket in tickets.list_tickets(statuses=(schema.READY_FOR_INTEGRATION,)):
        try:
            same_repo = bool(ticket.github_repo) and normalize_repo(ticket.github_repo) == wanted
        except ValueError:
            same_repo = False
        if same_repo and not integration.is_queued(ticket.task_key):
            found.append({"task_key": ticket.task_key, "github_repo": ticket.github_repo,
                          "status": ticket.status, "updated_at": ticket.updated_at})
    return sorted(found, key=lambda item: item["task_key"])


def _merge_verified_not_completed(
    request: IntegrationTickRequest, integration: IntegrationStore, tickets: TicketStore,
) -> list[dict[str, Any]]:
    """This repository's Tickets whose merge is verified but not completed.

    Detected and reported only, on every tick (review N4). Cleanup records
    ``merge_verified_at`` before it removes anything (Step 2's order; changing
    it is F10-FU3, an owner decision), so when git then declines part of the
    removal (``cleanup_incomplete``) the Ticket leaves every pick-up: its PR is
    closed, its merge is no longer unverified, and it is not queued. A
    human-confirmed ``run_integration_cleanup`` retry finishes it; the tick
    never retries it. Scoped like the cleanup pick-up, by the PR's own URL.
    """
    wanted = normalize_repo(request.repo)
    found = []
    for ticket in tickets.list_tickets(statuses=TASK_STATUSES - {schema.COMPLETED}):
        state = integration.get_integration_state(ticket.task_key)
        if not state["merge_verified_at"]:
            continue
        pr = integration.get_pr_state(ticket.task_key)
        if not pr["pr_merged"] or repo_from_pr_url(pr["pr_url"]) != wanted:
            continue
        found.append({"task_key": ticket.task_key, "status": ticket.status,
                      "pr_url": pr["pr_url"], "merge_commit_sha": pr["merge_commit_sha"],
                      "merge_verified_at": state["merge_verified_at"],
                      "cleanup_confirmed_at": state["cleanup_confirmed_at"],
                      "worktree_path": str(ticket.worktree_path), "branch": ticket.branch})
    return sorted(found, key=lambda item: item["task_key"])


def _with_consumer_phases(
    result: dict[str, Any], consumer: dict[str, Any], request: IntegrationTickRequest,
    integration: IntegrationStore, tickets: TicketStore,
) -> dict[str, Any]:
    """Add the consumer phases to F9's drain result; F9's keys keep their meaning."""
    error = consumer["error"]
    drain_ok = all(outcome["ok"] for outcome in result["outcomes"])
    if error:
        result.update(status="stopped", stopped_reason=f"phase_error:{error['phase']}")
    phases = dict(consumer["phases"])
    phases[PHASE_DRAIN] = {
        "ran": not error, "confirmed": not request.dry_run and request.confirm_integration,
        "ok": drain_ok and not error, "status": result["status"],
        "detail": "F9's drain result is the top-level outcomes, status and stopped_reason",
    }
    unqueued = _ready_unqueued(request, integration, tickets)
    uncompleted = _merge_verified_not_completed(request, integration, tickets)
    ok = (
        all(phase["ok"] for phase in phases.values()) and not unqueued and not uncompleted
        and result["crash_reconciliation"]["ok"]
    )
    result.update(
        ok=ok,
        drain_ok=drain_ok,
        tick_status="error" if error else "ok" if ok else "not_ok",
        phase_order=list(PHASE_ORDER),
        phases=phases,
        confirmations={
            PHASE_PR_OUTCOMES: request.confirm_pr_poll,
            PHASE_CLEANUP: request.confirm_cleanup,
            PHASE_FRESHNESS: request.confirm_freshness,
            PHASE_DRAIN: not request.dry_run and request.confirm_integration,
        },
        integration_lock_at_start=consumer["integration_lock_at_start"],
        ready_for_integration_unqueued=unqueued,
        merge_verified_not_completed=uncompleted,
        generated_at=utc_now_iso(),
    )
    return result
