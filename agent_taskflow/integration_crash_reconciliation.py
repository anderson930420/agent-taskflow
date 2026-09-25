"""Crash reconciliation for one repository, run on every integration-lock acquire.

RULINGS 69 (owner decision D3). Only ``integrate_task`` moves a Ticket into
``integrating``, and only while it holds the repository's flock. So once a new
writer holds that flock, every Ticket of the repository still in
``integrating`` was left there by a holder that died. Each one is classified
from what the dead run recorded, deterministically, and handled:

====  =========================================  ==============================
case  what the record shows                      action
====  =========================================  ==============================
A     died before publishing (``running``),      abort an in-progress rebase or
      still queued                               merge; -> ready_for_integration,
                                                 queue row and FIFO position kept
B     ``publishing`` with no PR recorded: the    -> needs_decision
      push may have happened, no PR is recorded
C     ``creating_pr`` with no PR recorded: a PR  -> needs_decision
      may exist on GitHub that the DB lacks
D     a PR is recorded but this run's base is    -> ready_for_integration, queue
      not (``publishing``/``creating_pr``),      row kept; the rerun updates the
      still queued                               same PR (see below)
E     ``integrated`` and dequeued, not yet in    -> needs_review
      review
F     anything else, including a mismatch of     -> needs_decision
      the Ticket's repository attribution
====  =========================================  ==============================

B, C and F are human decisions: there is deliberately no automated "find the
existing GitHub PR" recovery (owner, D3). Each gets an explicit reason code, a
task event and an evidence file.

D's ``reintegration_count`` side effect: the completion record (base, count,
``integrated``, dequeue) is one transaction, so a dead run never incremented
it; and a run whose Ticket has a PR but no recorded ``integrated_base_sha`` is
finishing its initial integration and does not increment it either (see
``integration_controller``). Reconciliation itself never touches the count.

Reconciliation is idempotent: a second pass finds no Ticket in
``integrating`` and does nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from agent_taskflow import integration_git as git_ops
from agent_taskflow import integration_schema as schema
from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.ticket_fields_schema import missing_ticket_fields
from agent_taskflow.ticket_store import TicketStore


__all__ = [
    "ARTIFACT_TYPE",
    "CASE_ACTIONS",
    "REASON_AMBIGUOUS",
    "REASON_PR_WITHOUT_RECORD",
    "REASON_PUSHED_WITHOUT_PR",
    "RUN_CREATING_PR",
    "RUN_INTEGRATED",
    "RUN_PUBLISHING",
    "RUN_STARTED",
    "classify_crash",
    "reconcile_integrating_tickets",
]


SOURCE = "integration_crash_reconciliation"
ARTIFACT_TYPE = "integration_crash_reconciliation"

# ``last_integration_status`` checkpoints an integration run writes, in order.
# RUN_STARTED is written before the Ticket enters ``integrating``, so a Ticket
# in ``integrating`` never carries a previous run's checkpoint.
RUN_STARTED = "running"
RUN_PUBLISHING = "publishing"
RUN_CREATING_PR = "creating_pr"
RUN_INTEGRATED = "integrated"
RECONCILED = "crash_reconciled"

REASON_PUSHED_WITHOUT_PR = "integration_crash_pushed_without_pr_record"
REASON_PR_WITHOUT_RECORD = "integration_crash_pr_without_db_record"
REASON_AMBIGUOUS = "integration_crash_ambiguous_state"

CASE_ACTIONS = {
    "A": schema.READY_FOR_INTEGRATION,
    "B": schema.NEEDS_DECISION,
    "C": schema.NEEDS_DECISION,
    "D": schema.READY_FOR_INTEGRATION,
    "E": schema.NEEDS_REVIEW,
    "F": schema.NEEDS_DECISION,
}


def classify_crash(
    marker: str | None,
    *,
    pr_number: int | None,
    integrated_base_sha: str | None,
    queued: bool,
    attribution_conflict: str | None = None,
) -> tuple[str, str, str]:
    """Return ``(case, reason_code, detail)`` for one Ticket left in ``integrating``."""
    if attribution_conflict:
        return "F", REASON_AMBIGUOUS, attribution_conflict
    has_pr = pr_number is not None
    if marker == RUN_STARTED:
        if queued:
            return "A", "integration_crash_before_publish", "died before publishing"
        return "F", REASON_AMBIGUOUS, "died before publishing, but the queue row is gone"
    if marker in (RUN_PUBLISHING, RUN_CREATING_PR):
        if not has_pr:
            if marker == RUN_PUBLISHING:
                return (
                    "B", REASON_PUSHED_WITHOUT_PR,
                    "died while publishing: the task branch may be pushed, no PR is recorded",
                )
            return (
                "C", REASON_PR_WITHOUT_RECORD,
                "died creating the PR: a PR may exist on GitHub that is not recorded",
            )
        if queued:
            return (
                "D", "integration_crash_pr_recorded_base_not",
                f"PR #{pr_number} is recorded; this run's base is not",
            )
        return "F", REASON_AMBIGUOUS, f"{marker} with PR #{pr_number}, but the queue row is gone"
    if marker == RUN_INTEGRATED:
        if not queued and has_pr and integrated_base_sha:
            return (
                "E", "integration_crash_integrated_before_review",
                "integrated and dequeued, but not yet in review",
            )
        return "F", REASON_AMBIGUOUS, "recorded integrated, but the record is incomplete"
    return "F", REASON_AMBIGUOUS, f"unrecognised integration checkpoint {marker!r}"


def _latest_started_repo(task_store: TaskMirrorStore, task_key: str) -> str | None:
    for event in reversed(task_store.list_task_events(task_key)):
        if event.event_type != "integration_started" or not event.payload_json:
            continue
        try:
            payload = json.loads(event.payload_json)
        except ValueError:
            return None
        repo = payload.get("repo") if isinstance(payload, dict) else None
        return repo if isinstance(repo, str) else None
    return None


def _normalized(repo: str | None) -> str | None:
    if not repo:
        return None
    try:
        return schema.normalize_repo(repo)
    except ValueError:
        return repo.strip()


def _attribution(
    task_store: TaskMirrorStore,
    integration: IntegrationStore,
    tickets: TicketStore | None,
    task: Any,
    *,
    queue_repo: str | None,
) -> dict[str, Any]:
    """Every record naming the Ticket's repository, and its clone's git-common-dir."""
    ticket = tickets.get_ticket(task.task_key) if tickets is not None else None
    pr_state = integration.get_pr_state(task.task_key)
    names = {
        "ticket_github_repo": _normalized(ticket.github_repo if ticket else None),
        "queue_repo": _normalized(queue_repo),
        "pr_url_repo": schema.repo_from_pr_url(pr_state["pr_url"]),
        "integration_started_repo": _normalized(
            _latest_started_repo(task_store, task.task_key)
        ),
    }
    worktree = task_store.get_task_worktree(task.task_key)
    repo_path = worktree.repo_path if worktree is not None else task.repo_path
    try:
        clone = str(git_ops.git_common_dir(repo_path)) if Path(repo_path).is_dir() else None
    except (git_ops.IntegrationGitError, OSError):
        clone = None
    return {"names": names, "git_common_dir": clone}


def reconcile_integrating_tickets(
    *,
    task_store: TaskMirrorStore,
    integration: IntegrationStore,
    repo_key: str,
    git_common_dir: str,
    reconciler_run_id: str,
    lock_evidence: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Reconcile every Ticket of ``repo_key`` left in ``integrating``.

    The caller must hold the repository's integration flock. Returns one
    report per Ticket handled, in task-key order.
    """
    candidates = sorted(
        task_store.list_tasks(status=schema.INTEGRATING), key=lambda task: task.task_key
    )
    if not candidates:
        return []
    tickets = None if missing_ticket_fields(task_store.db_path) else TicketStore(
        task_store.db_path
    )
    queue_repos = integration.list_queue_repos()

    reports = []
    for task in candidates:
        attribution = _attribution(
            task_store, integration, tickets, task, queue_repo=queue_repos.get(task.task_key)
        )
        names = {value for value in attribution["names"].values() if value}
        same_clone = attribution["git_common_dir"] == git_common_dir
        if not same_clone and repo_key not in names:
            continue  # another repository's Ticket; its own lock reconciles it
        conflict = None
        if names - {repo_key}:
            conflict = f"records name other repositories: {sorted(names - {repo_key})}"
        elif attribution["git_common_dir"] is not None and not same_clone:
            conflict = (
                f"its clone {attribution['git_common_dir']} is not the lock's "
                f"clone {git_common_dir}"
            )
        reports.append(
            _reconcile_one(
                task_store,
                integration,
                task,
                repo_key=repo_key,
                queued=task.task_key in queue_repos,
                attribution=attribution,
                attribution_conflict=conflict,
                reconciler_run_id=reconciler_run_id,
                lock_evidence=lock_evidence,
            )
        )
    return reports


def _reconcile_one(
    task_store: TaskMirrorStore,
    integration: IntegrationStore,
    task: Any,
    *,
    repo_key: str,
    queued: bool,
    attribution: dict[str, Any],
    attribution_conflict: str | None,
    reconciler_run_id: str,
    lock_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    state = integration.get_integration_state(task.task_key)
    pr_state = integration.get_pr_state(task.task_key)
    marker = state["last_integration_status"]
    case, reason_code, detail = classify_crash(
        marker,
        pr_number=pr_state["pr_number"],
        integrated_base_sha=pr_state["integrated_base_sha"],
        queued=queued,
        attribution_conflict=attribution_conflict,
    )

    aborted = None
    if case == "A":
        worktree = task_store.get_task_worktree(task.task_key)
        if worktree is not None and worktree.worktree_path.is_dir():
            try:
                aborted = git_ops.in_progress_operation(worktree.worktree_path)
                if aborted == "merge":
                    git_ops.abort_merge(worktree.worktree_path)
                elif aborted == "rebase":
                    git_ops.abort_rebase(worktree.worktree_path)
                still = git_ops.in_progress_operation(worktree.worktree_path)
            except (git_ops.IntegrationGitError, OSError) as exc:
                still = f"error: {exc}"
            if still is not None:
                case, reason_code = "F", REASON_AMBIGUOUS
                detail = f"died before publishing, but the in-progress {aborted} could not be aborted ({still})"

    target = CASE_ACTIONS[case]
    report = {
        "kind": ARTIFACT_TYPE,
        "task_key": task.task_key,
        "repo": repo_key,
        "case": case,
        "reason_code": reason_code,
        "detail": detail,
        "action": f"{schema.INTEGRATING} -> {target}",
        "dead_run_id": state["last_integration_run_id"],
        "checkpoint": marker,
        "pr_number": pr_state["pr_number"],
        "pr_url": pr_state["pr_url"],
        "integrated_base_sha": pr_state["integrated_base_sha"],
        "reintegration_count": pr_state["reintegration_count"] or 0,
        "queued": queued,
        "aborted_operation": aborted,
        "attribution": attribution,
        "reconciler_run_id": reconciler_run_id,
        "lock": dict(lock_evidence),
        "automated_pr_discovery": False,
        "generated_at": utc_now_iso(),
    }
    message = f"Crash reconciliation {case} ({reason_code}): {detail}"
    schema.validate_transition(schema.INTEGRATING, target)
    task_store.update_task_status(
        task.task_key,
        target,
        source=SOURCE,
        message=message,
        expected_current_status=schema.INTEGRATING,
    )
    task_store.record_task_event(
        task.task_key, "integration_crash_reconciled", SOURCE, message=message, payload=report
    )
    if target == schema.NEEDS_DECISION:
        task_store.record_task_event(
            task.task_key,
            "integration_blocked",
            SOURCE,
            message=message,
            payload={
                "integration_run_id": state["last_integration_run_id"],
                "reason": message,
                "reason_code": reason_code,
                "crash_case": case,
            },
        )
        integration.update_integration_state(
            task.task_key, last_integration_status="needs_decision"
        )
    elif target == schema.READY_FOR_INTEGRATION:
        integration.update_integration_state(task.task_key, last_integration_status=RECONCILED)

    if task.artifact_dir is not None:
        directory = Path(task.artifact_dir) / "integration"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"crash-reconciliation-{reconciler_run_id}-{uuid4().hex[:6]}.json"
        atomic_write_json(path, report, sort_keys=True)
        task_store.record_task_artifact(task.task_key, ARTIFACT_TYPE, path)
        report["evidence_path"] = str(path)
    return report
