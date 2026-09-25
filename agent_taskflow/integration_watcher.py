"""Target-freshness and PR outcome polling (§25, §25.0, §32, §33, §35).

Two tick functions, both idempotent (§32) and both dry-run by default:

* :func:`poll_target_freshness` — fetches the latest target, computes
  ``behind_count`` for the Tickets its §32.0 pick-up selects, and returns
  a stale ``needs_review`` one to ``ready_for_integration`` with the two base SHAs recorded (§25.1).
* :func:`poll_pr_outcomes` — reads every *open* PR, whatever its Ticket's
  status, and applies the §33/§35 outcomes:
  ``CHANGES_REQUESTED`` to ``needs_decision`` with the review persisted as
  retry context, closed-unmerged to ``cancelled`` *without touching the
  workspace*, and merged recorded as merge identity only.

Deliberate non-behaviours:

* A Ticket that is mid-integration is not examined. §25.0 says a target that
  advances during an integration gets no special handling: the run finishes and
  the next tick re-queues it.
* GitHub CI status is recorded but never consulted. §30 — CI is not a Taskflow
  lifecycle authority, so a red rollup leaves a Ticket in ``needs_review``.
* Detecting a merge does not complete the Ticket or clean anything up. Cleanup
  is gated on §36 verification and lives in ``integration_cleanup``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
from typing import Any, Sequence

from agent_taskflow import integration_git as git_ops
from agent_taskflow import integration_schema as schema
from agent_taskflow.github_pr_adapter import GitHubPrAdapter, PrSnapshot
from agent_taskflow.integration_git import GitCommandLog, IntegrationGitError
from agent_taskflow.integration_queue import (
    enqueue_for_integration,
    remove_from_queue,
)
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.worktree import ensure_absolute_path


__all__ = [
    "PrOutcome",
    "TargetFreshnessOutcome",
    "WatcherRequest",
    "poll_pr_outcomes",
    "poll_target_freshness",
]


SOURCE = "integration_watcher"

# RULINGS 70 (amending ruling 29e): this many consecutive failed polls of one
# Ticket's PR escalate it to needs_decision; fewer leave its status alone.
POLL_FAILURE_ESCALATION_THRESHOLD = 6


@dataclass(frozen=True)
class WatcherRequest:
    """One watcher tick for one repository."""

    repo: str
    repo_path: Path
    target_branch: str = "main"
    remote: str = "origin"
    db_path: Path | None = None
    task_keys: Sequence[str] | None = None
    confirm_poll: bool = False

    def __post_init__(self) -> None:
        repo = self.repo.strip()
        if not repo:
            raise ValueError("repo must not be empty")
        object.__setattr__(self, "repo", repo)
        object.__setattr__(
            self, "repo_path", ensure_absolute_path(self.repo_path, name="repo_path")
        )
        if self.db_path is not None:
            object.__setattr__(
                self, "db_path", ensure_absolute_path(self.db_path, name="db_path")
            )
        if self.task_keys is not None:
            object.__setattr__(self, "task_keys", tuple(self.task_keys))


@dataclass(frozen=True)
class TargetFreshnessOutcome:
    """One Ticket's freshness against the latest target."""

    task_key: str
    behind_count: int | None
    stale: bool
    previous_integrated_base_sha: str | None
    new_target_sha: str | None
    requeued: bool = False
    task_status: str | None = None
    # True for an in-flight integration: picked up, but no git work (§25.0).
    deferred: bool = False


@dataclass(frozen=True)
class PrOutcome:
    """One Ticket's PR state after a poll."""

    task_key: str
    pr_number: int | None
    pr_state: str | None
    merged: bool
    review_decision: str | None
    ci_status: str | None
    merge_commit_sha: str | None = None
    proposed_transition: str | None = None
    applied_transition: str | None = None
    cleanup_performed: bool = False
    task_status: str | None = None
    # True for an in-flight integration: picked up, but not polled (§25.0).
    deferred: bool = False
    # Set when the poll itself failed: "<ExceptionType>: <message>".
    poll_error: str | None = None
    # The consecutive failed polls this one makes (a preview counts it too).
    consecutive_poll_failures: int | None = None


def poll_target_freshness(
    request: WatcherRequest,
    *,
    store: TaskMirrorStore | None = None,
    integration_store: IntegrationStore | None = None,
) -> list[TargetFreshnessOutcome]:
    """Detect stale PR branches in this tick's repository (§25.1).

    Pick-up is the §32.0 condition — repo == tick repo AND pr_number IS NOT
    NULL AND pr_state = 'open' — and is not scoped by status. What happens to
    a picked-up Ticket is gated:

    * only a ``needs_review`` Ticket is re-queued, because that is the §25.1
      transition; a paused Ticket does not enter integration (§13) and a
      needs_decision Ticket is waiting for a human;
    * an ``integrating`` Ticket is deferred with no git work at all (§25.0),
      so this tick never fetches underneath a live integration.

    Re-queued Tickets go only into this tick's own repository queue.
    """
    task_store = store or TaskMirrorStore(request.db_path)
    task_store.init_db()
    integration = integration_store or IntegrationStore(store=task_store)

    log = GitCommandLog()
    target_ref = f"{request.remote}/{request.target_branch}"
    outcomes: list[TargetFreshnessOutcome] = []

    for task, pr_state in _open_pr_candidates(
        task_store, integration, request.repo, request.task_keys
    ):
        if task.status == schema.INTEGRATING:
            outcomes.append(
                TargetFreshnessOutcome(
                    task_key=task.task_key,
                    behind_count=None,
                    stale=False,
                    previous_integrated_base_sha=pr_state["integrated_base_sha"],
                    new_target_sha=None,
                    task_status=task.status,
                    deferred=True,
                )
            )
            continue

        worktree = task_store.get_task_worktree(task.task_key)
        if worktree is None or not worktree.worktree_path.is_dir():
            continue

        try:
            git_ops.fetch(worktree.worktree_path, remote=request.remote, log=log)
            target_sha = git_ops.resolve_target_sha(
                worktree.worktree_path, request.remote, request.target_branch, log=log
            )
            behind = git_ops.behind_count(
                worktree.worktree_path, "HEAD", target_ref, log=log
            )
        except IntegrationGitError:
            # A repo that cannot be read is not evidence of staleness.
            continue

        previous_base = pr_state["integrated_base_sha"]
        outcome = TargetFreshnessOutcome(
            task_key=task.task_key,
            behind_count=behind,
            stale=behind > 0,
            previous_integrated_base_sha=previous_base,
            new_target_sha=target_sha,
            task_status=task.status,
        )
        if not outcome.stale or task.status != schema.NEEDS_REVIEW or not request.confirm_poll:
            outcomes.append(outcome)
            continue

        integration.update_pr_state(task.task_key, reintegration_required=True)
        integration.update_integration_state(
            task.task_key,
            previous_integrated_base_sha=previous_base,
            new_target_sha=target_sha,
            behind_count=behind,
        )
        task_store.update_task_status(
            task.task_key,
            schema.READY_FOR_INTEGRATION,
            source=SOURCE,
            message=(
                f"Target advanced; branch is {behind} commit(s) behind {target_ref}"
            ),
            expected_current_status=schema.NEEDS_REVIEW,
        )
        task_store.record_task_event(
            task.task_key,
            "reintegration_required",
            SOURCE,
            message=f"Re-integration required; behind_count={behind}",
            payload={
                "behind_count": behind,
                "previous_integrated_base_sha": previous_base,
                "new_target_sha": target_sha,
                "repo": request.repo,
            },
        )
        enqueue_for_integration(
            integration, task.task_key, repo=request.repo, source=SOURCE
        )
        task_store.record_task_event(
            task.task_key,
            "integration_queued",
            SOURCE,
            message="Queued for re-integration",
            payload={"repo": request.repo},
        )
        outcomes.append(replace(outcome, requeued=True))

    return outcomes


def _open_pr_candidates(
    task_store: TaskMirrorStore,
    integration: IntegrationStore,
    repo: str,
    task_keys: Sequence[str] | None,
) -> list[tuple[Any, dict[str, Any]]]:
    """Select the §32.0 pick-up set for one repository's tick.

        repo == tick repo AND pr_number IS NOT NULL AND pr_state = 'open'

    Human rulings: the condition is not scoped by status — a human can merge
    a PR on GitHub while its Ticket sits in needs_decision or paused, and a
    watcher scoped to needs_review would never see it — and every tick is
    scoped to its own repository, because the same PR number can be open in
    two repositories at once. The repository filter runs in SQL
    (:meth:`IntegrationStore.list_open_pr_states`), so another repository's
    Tickets are never loaded, polled, transitioned or queued.
    """
    wanted = set(task_keys) if task_keys is not None else None
    selected: list[tuple[Any, dict[str, Any]]] = []
    for state in integration.list_open_pr_states(repo):
        if wanted is not None and state["task_key"] not in wanted:
            continue
        task = task_store.get_task(state["task_key"])
        if task is not None:
            selected.append((task, state))
    return selected


def poll_pr_outcomes(
    request: WatcherRequest,
    *,
    store: TaskMirrorStore | None = None,
    integration_store: IntegrationStore | None = None,
    github: GitHubPrAdapter | None = None,
) -> list[PrOutcome]:
    """Poll every open PR and apply the §33/§35 outcomes. Idempotent.

    Pickup is ``pr_number IS NOT NULL AND pr_state = 'open'``; see
    :func:`_open_pr_candidates` for why it is not scoped by status.
    """
    task_store = store or TaskMirrorStore(request.db_path)
    task_store.init_db()
    integration = integration_store or IntegrationStore(store=task_store)
    adapter = github or GitHubPrAdapter(request.repo)

    outcomes: list[PrOutcome] = []

    for task, pr_state in _open_pr_candidates(
        task_store, integration, request.repo, request.task_keys
    ):
        pr_number = int(pr_state["pr_number"])

        if task.status == schema.INTEGRATING:
            # §25.0 — an in-flight integration is never interrupted. Nothing is
            # polled or recorded, so pr_state stays 'open' and the next tick
            # picks the Ticket up again once the integration has finished.
            outcomes.append(
                PrOutcome(
                    task_key=task.task_key,
                    pr_number=pr_number,
                    pr_state=pr_state["pr_state"],
                    merged=bool(pr_state["pr_merged"]),
                    review_decision=pr_state["review_decision"],
                    ci_status=pr_state["ci_status"],
                    task_status=task.status,
                    deferred=True,
                )
            )
            continue

        worktree = task_store.get_task_worktree(task.task_key)
        cwd = (
            worktree.worktree_path
            if worktree is not None and worktree.worktree_path.is_dir()
            else request.repo_path
        )
        try:
            snapshot = adapter.poll_pr(pr_number=pr_number, cwd=cwd)
        except Exception as exc:  # noqa: BLE001 - every failure is audited
            # Review round 4, Ruling 29e: a failed poll is never skipped
            # silently. A skipped poll is a watcher that has gone blind.
            # RULINGS 70 amends what happens next: see _poll_failed.
            outcomes.append(
                _poll_failed(
                    task_store,
                    integration,
                    task,
                    pr_state,
                    exc,
                    confirm=request.confirm_poll,
                )
            )
            continue

        transition = _proposed_transition(snapshot, task.status)
        if not request.confirm_poll:
            outcomes.append(_outcome(task.task_key, snapshot, transition, None, task.status))
            continue

        changed = _record_pr_state(integration, task.task_key, snapshot, pr_state)
        _record_poll_recovery(task_store, integration, task, snapshot.number)
        # RULINGS 70 (F10-FU1): the domain event is written only when a §32.1
        # field changed. pr_last_polled_at, written on every successful poll,
        # is the heartbeat, so an unchanged PR adds no event per tick.
        if changed:
            task_store.record_task_event(
                task.task_key,
                "pr_state_polled",
                SOURCE,
                message=f"PR #{snapshot.number} polled; changed: {', '.join(sorted(changed))}",
                payload={
                    "pr_number": snapshot.number,
                    "pr_state": snapshot.state,
                    "merged": snapshot.merged,
                    "review_decision": snapshot.review_decision,
                    "task_status": task.status,
                    # Recorded for observability only; §30 forbids it from
                    # influencing any lifecycle transition.
                    "ci_status": snapshot.ci_status,
                    "ci_is_not_a_lifecycle_authority": True,
                    "changed_fields": changed,
                },
            )

        # GitHub does not always return headRefOid; the §32.1 field we already
        # recorded at integration time is then the authoritative reviewed head.
        effective_head = snapshot.head_sha or pr_state["pr_head_sha"]
        applied = _apply_transition(
            task_store,
            integration,
            task.task_key,
            snapshot,
            transition,
            task_status=task.status,
            effective_head_sha=effective_head,
        )
        outcomes.append(_outcome(task.task_key, snapshot, transition, applied, task.status))

    return outcomes


def _poll_failure_fingerprint(exc: BaseException) -> str:
    """sha256 of the exception type and message (RULINGS 70, OR-9)."""
    return hashlib.sha256(f"{type(exc).__name__}\n{exc}".encode("utf-8")).hexdigest()


def _poll_failed(
    task_store: TaskMirrorStore,
    integration: IntegrationStore,
    task: Any,
    pr_state: dict[str, Any],
    exc: BaseException,
    *,
    confirm: bool,
) -> PrOutcome:
    """Audit a failed PR poll; escalate only a persistent failure (§27.2.1).

    RULINGS 70 amends ruling 29e. A failed poll is counted in the Ticket's
    ``pr_poll_consecutive_failures`` and never changes its status by itself.
    ``pr_poll_failed`` is written only when the failure fingerprint (exception
    type and message) differs from the stored one, so a persistent failure
    adds one event, not one per tick.

    Once the count is at or past :data:`POLL_FAILURE_ESCALATION_THRESHOLD`,
    a failed poll of a Ticket the transition table lets integration move to
    ``needs_decision`` escalates it there, with one ``pr_poll_escalated``
    event. The escalation itself leaves the escalatable state, so this fires
    at most once per return to one: at the threshold, and again only if a
    human puts the Ticket back (for example to ``needs_review``) while the
    failures continue (review N1). A Ticket that cannot move there
    (``ready_for_integration``, ``paused``, or already ``needs_decision``)
    keeps its status; it gets the event, with ``transition`` null, only on
    the poll that reaches the threshold, and escalates on its first failed
    poll after it becomes escalatable. A successful poll resets the count
    (:func:`_record_poll_recovery`). Nothing in §32.1 is recorded from a poll
    that failed.
    """
    pr_number = int(pr_state["pr_number"])
    error = f"{type(exc).__name__}: {exc}"
    state = integration.get_integration_state(task.task_key)
    previous_fingerprint = state["pr_poll_failure_fingerprint"]
    failures = (state["pr_poll_consecutive_failures"] or 0) + 1
    fingerprint = _poll_failure_fingerprint(exc)
    over_threshold = failures >= POLL_FAILURE_ESCALATION_THRESHOLD
    target = (
        schema.NEEDS_DECISION
        if over_threshold and schema.can_transition(task.status, schema.NEEDS_DECISION)
        else None
    )
    escalate = target is not None or failures == POLL_FAILURE_ESCALATION_THRESHOLD
    applied: str | None = None
    if confirm:
        message = f"Polling PR #{pr_number} failed: {error}"
        payload = {
            "pr_number": pr_number,
            "task_status": task.status,
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "fingerprint": fingerprint,
            "consecutive_failures": failures,
            "escalation_threshold": POLL_FAILURE_ESCALATION_THRESHOLD,
        }
        if fingerprint != previous_fingerprint:
            task_store.record_task_event(
                task.task_key,
                "pr_poll_failed",
                SOURCE,
                message=message,
                payload={**payload, "previous_fingerprint": previous_fingerprint},
            )
        if escalate:
            task_store.record_task_event(
                task.task_key,
                "pr_poll_escalated",
                SOURCE,
                message=(
                    f"Polling PR #{pr_number} failed {failures} consecutive times: {error}"
                ),
                payload={
                    **payload,
                    "transition": target,
                    "reason": (
                        "threshold_reached"
                        if failures == POLL_FAILURE_ESCALATION_THRESHOLD
                        else "escalatable_again_while_failing"
                    ),
                },
            )
            if target is not None:
                task_store.update_task_status(
                    task.task_key,
                    target,
                    source=SOURCE,
                    message=message,
                    expected_current_status=task.status,
                )
                applied = target
        # Written last: a crash before this point repeats the poll's events on
        # the next tick instead of losing the escalation.
        integration.update_integration_state(
            task.task_key,
            pr_poll_consecutive_failures=failures,
            pr_poll_failure_fingerprint=fingerprint,
        )
    return PrOutcome(
        task_key=task.task_key,
        pr_number=pr_number,
        pr_state=pr_state["pr_state"],
        merged=bool(pr_state["pr_merged"]),
        review_decision=pr_state["review_decision"],
        ci_status=pr_state["ci_status"],
        proposed_transition=target,
        applied_transition=applied,
        task_status=task.status,
        poll_error=error,
        consecutive_poll_failures=failures,
    )


def _record_poll_recovery(
    task_store: TaskMirrorStore,
    integration: IntegrationStore,
    task: Any,
    pr_number: int,
) -> None:
    """Reset the failure count after a confirmed successful poll (RULINGS 70).

    Writes one ``pr_poll_recovered`` event when there was a count to reset.
    It never changes the Ticket's status: an escalated Ticket stays with the
    human who is deciding it.
    """
    state = integration.get_integration_state(task.task_key)
    failures = state["pr_poll_consecutive_failures"]
    if not failures and state["pr_poll_failure_fingerprint"] is None:
        return
    task_store.record_task_event(
        task.task_key,
        "pr_poll_recovered",
        SOURCE,
        message=f"Polling PR #{pr_number} recovered after {failures or 0} failed poll(s)",
        payload={
            "pr_number": pr_number,
            "task_status": task.status,
            "consecutive_failures": failures or 0,
            "previous_fingerprint": state["pr_poll_failure_fingerprint"],
            "escalated": (failures or 0) >= POLL_FAILURE_ESCALATION_THRESHOLD,
        },
    )
    integration.update_integration_state(
        task.task_key,
        pr_poll_consecutive_failures=None,
        pr_poll_failure_fingerprint=None,
    )


def _proposed_transition(snapshot: PrSnapshot, task_status: str) -> str | None:
    """Return the status change this poll calls for, if Step 2 may make it."""
    if snapshot.merged:
        # §35 — merge is *detected* here. Completing the Ticket requires §36
        # verification plus cleanup, which this watcher deliberately does not do.
        return None
    if snapshot.state == "closed":
        target = schema.CANCELLED
    elif snapshot.review_decision == "changes_requested":
        target = schema.NEEDS_DECISION
    else:
        return None
    return target if schema.can_transition(task_status, target) else None


def _record_pr_state(
    integration: IntegrationStore,
    task_key: str,
    snapshot: PrSnapshot,
    previous: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Write the polled §32.1 fields and the heartbeat; return what changed.

    The result maps each written field whose value differs from ``previous``
    to ``{"old": ..., "new": ...}``. ``pr_last_polled_at`` is not a change.
    """
    fields = {
        key: value
        for key, value in snapshot.to_pr_state_fields().items()
        if value is not None or key in {"pr_merged"}
    }
    changed = {
        key: {"old": previous.get(key), "new": value}
        for key, value in fields.items()
        if previous.get(key) != value
    }
    fields["pr_last_polled_at"] = utc_now_iso()
    integration.update_pr_state(task_key, **fields)
    return changed


def _apply_transition(
    task_store: TaskMirrorStore,
    integration: IntegrationStore,
    task_key: str,
    snapshot: PrSnapshot,
    transition: str | None,
    *,
    task_status: str,
    effective_head_sha: str | None = None,
) -> str | None:
    if snapshot.merged:
        state = integration.get_integration_state(task_key)
        if state["merge_verified_at"] is None:
            task_store.record_task_event(
                task_key,
                "merge_detected",
                SOURCE,
                message=f"PR #{snapshot.number} reported merged by GitHub",
                payload={
                    "pr_number": snapshot.number,
                    "merged_at": snapshot.merged_at,
                    "merge_commit_sha": snapshot.merge_commit_sha,
                    "task_status": task_status,
                    "cleanup_performed": False,
                    "requires_merge_verification": True,
                },
            )
            integration.update_integration_state(
                task_key, last_integration_status="merge_detected"
            )
        return None

    if snapshot.state != "closed" and snapshot.review_decision == "changes_requested":
        review = snapshot.latest_review or {}
        # Idempotency: one review evidence row per reviewed head SHA.
        already = any(
            row["reviewed_head_sha"] == effective_head_sha
            and row["review_decision"] == "changes_requested"
            for row in integration.list_review_evidence(task_key)
        )
        if already:
            return None
        # The review is always kept as retry context (§33.2), even when the
        # Ticket is paused or already waiting on a decision.
        integration.record_review_evidence(
            task_key,
            pr_number=snapshot.number,
            pr_url=snapshot.url,
            review_decision=snapshot.review_decision,
            reviewer=review.get("author"),
            reviewed_at=review.get("submitted_at"),
            reviewed_head_sha=effective_head_sha,
            comments=[review] if review else [],
        )
        applied = None
        if transition == schema.NEEDS_DECISION:
            task_store.update_task_status(
                task_key,
                schema.NEEDS_DECISION,
                source=SOURCE,
                message=f"Changes requested on PR #{snapshot.number}",
                expected_current_status=task_status,
            )
            applied = schema.NEEDS_DECISION
        task_store.record_task_event(
            task_key,
            "pr_review_changes_requested",
            SOURCE,
            message=f"Changes requested on PR #{snapshot.number}",
            payload={
                "pr_number": snapshot.number,
                "pr_url": snapshot.url,
                "reviewer": review.get("author"),
                "reviewed_at": review.get("submitted_at"),
                "reviewed_head_sha": effective_head_sha,
                "task_status": task_status,
                "status_changed": applied is not None,
            },
        )
        return applied

    if transition == schema.CANCELLED:
        # §33.5 / §37.1 — cancel the Ticket but keep the workspace, branch,
        # artifacts and review evidence until a human confirms cleanup.
        integration.update_integration_state(task_key, closed_unmerged_at=utc_now_iso())
        task_store.update_task_status(
            task_key,
            schema.CANCELLED,
            source=SOURCE,
            message=f"PR #{snapshot.number} was closed without merging",
            expected_current_status=task_status,
        )
        # A Ticket cancelled while queued for re-integration must leave the queue.
        remove_from_queue(integration, task_key)
        task_store.record_task_event(
            task_key,
            "pr_closed_unmerged",
            SOURCE,
            message=f"PR #{snapshot.number} closed without merging",
            payload={
                "pr_number": snapshot.number,
                "pr_url": snapshot.url,
                "previous_task_status": task_status,
                "worktree_retained": True,
                "branch_retained": True,
                "artifacts_retained": True,
                "cleanup_requires_explicit_confirmation": True,
            },
        )
        return schema.CANCELLED

    return None


def _outcome(
    task_key: str,
    snapshot: PrSnapshot,
    transition: str | None,
    applied: str | None,
    task_status: str | None = None,
) -> PrOutcome:
    return PrOutcome(
        task_key=task_key,
        pr_number=snapshot.number,
        pr_state=snapshot.state,
        merged=snapshot.merged,
        review_decision=snapshot.review_decision,
        ci_status=snapshot.ci_status,
        merge_commit_sha=snapshot.merge_commit_sha,
        proposed_transition=transition,
        applied_transition=applied,
        cleanup_performed=False,
        task_status=task_status,
    )
