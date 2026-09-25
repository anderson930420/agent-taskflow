"""The Step 2 Integration Controller (§23, §24, §26, §27, §29, §31).

One entry point, :func:`integrate_task`, drives a Ticket from
``ready_for_integration`` to ``needs_review`` (or stops it at
``needs_decision``) under the Loose Integration Lock.

The pipeline follows §23.1 exactly, including the ordering that matters most:

    acquire repo lock -> fetch -> update against latest target ->
    resolve conflicts -> validators -> push/update PR ->
    record integrated_base_sha -> needs_review -> release lock

The lock is the repository's OS flock (:mod:`integration_repo_lock`, RULINGS
69), taken and released by each call; the ``integration_locks`` row is only a
journal of it. Every integration write, the ``needs_review`` hand-off
included, happens while it is held: a successor that took the lock in a gap
before ``needs_review`` would reconcile the same Ticket (crash case E) and
race this run's write. The lock is still released when this call returns, so
it never waits for human review (§23.1, §44) and several same-repo PRs can
sit in ``needs_review`` at once (§43.17).

Each acquire first reconciles the repository's Tickets that a dead holder
left in ``integrating`` (:mod:`integration_crash_reconciliation`). The run
records checkpoints in ``last_integration_status`` for that: ``running``
before the Ticket enters ``integrating``, ``publishing`` before the push,
``creating_pr`` before a PR is created, and ``integrated`` together with the
base, the count and the dequeue in one transaction.

Two integration modes:

* **Initial** (§24) — the branch has never been published, so a rebase onto the
  latest target is safe.
* **Re-integration** (§26) — the branch is already a public review branch, so
  the latest target is *merged into* it and pushed normally. No rebase, no
  force push, same PR number, review history preserved.

Everything here is dry-run by default and requires an explicit confirmation
flag before it touches git or GitHub.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from agent_taskflow import integration_git as git_ops
from agent_taskflow import integration_schema as schema
from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.github_pr_adapter import GitHubPrAdapter, GitHubPrError
from agent_taskflow.integration_crash_reconciliation import (
    RUN_CREATING_PR,
    RUN_INTEGRATED,
    RUN_PUBLISHING,
    RUN_STARTED,
    reconcile_integrating_tickets,
)
from agent_taskflow.integration_conflict_resolver import (
    ConflictResolutionRequest,
    ConflictResolver,
    resolve_conflicts,
)
from agent_taskflow.integration_evidence_root import (
    IntegrationEvidenceRoot,
    resolve_integration_evidence_root,
)
from agent_taskflow.integration_git import GitCommandLog, IntegrationGitError
from agent_taskflow.integration_handoff import (
    ProducerAttemptBinding,
    resolve_producer_attempt_binding,
)
from agent_taskflow.integration_repo_lock import (
    ExternalHoldProbe,
    IntegrationRepoLock,
    LockAcquisition,
)
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.integration_validators import (
    IntegrationValidationReport,
    IntegrationValidatorSpec,
    run_integration_validators,
)
from agent_taskflow.models import utc_now_iso
from agent_taskflow.reviewer_hints import (
    ReviewerHint,
    build_reviewer_hints,
    render_hints_markdown,
    render_reintegration_hint_block,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.worktree import ensure_absolute_path


__all__ = [
    "ARTIFACT_TYPE",
    "IntegrationControllerError",
    "IntegrationRequest",
    "IntegrationResult",
    "integrate_task",
]


ARTIFACT_TYPE = "integration_result"
SOURCE = "integration_controller"


class IntegrationControllerError(RuntimeError):
    """Raised when an integration cannot be attempted at all."""


@dataclass(frozen=True)
class IntegrationRequest:
    """One integration attempt for one Ticket."""

    task_key: str
    repo: str
    db_path: Path | None = None
    target_branch: str = "main"
    remote: str = "origin"
    validator_specs: Sequence[IntegrationValidatorSpec] = ()
    owner: str = "integration_controller"
    dry_run: bool = True
    confirm_integration: bool = False
    draft: bool = True
    conflict_resolver: ConflictResolver | None = None
    title: str | None = None
    body: str | None = None
    trigger: str | None = None
    # The integration flock's directory; None resolves the explicit setting or
    # its default (integration_repo_lock.resolve_lock_dir).
    lock_dir: Path | None = None
    # Proves a recorded external hold gone before takeover (OR-8.1); None
    # fails closed on any recorded hold.
    external_hold_probe: ExternalHoldProbe | None = None

    # Ambiguity watchlist (see docs/v1/handoff-step2.md): the spec does not say whether a
    # re-integration that is already up to date should still push. The default
    # is not to push a branch with nothing new on it; validators re-run either
    # way, because §44 requires that unconditionally.
    push_no_op_reintegration: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        repo = self.repo.strip()
        if not repo:
            raise ValueError("repo must not be empty")
        object.__setattr__(self, "repo", repo)
        if self.db_path is not None:
            object.__setattr__(
                self, "db_path", ensure_absolute_path(self.db_path, name="db_path")
            )
        object.__setattr__(self, "validator_specs", tuple(self.validator_specs))
        if self.lock_dir is not None:
            object.__setattr__(
                self, "lock_dir", ensure_absolute_path(self.lock_dir, name="lock_dir")
            )


@dataclass(frozen=True)
class IntegrationResult:
    """The outcome of one integration attempt, with its full audit trail."""

    ok: bool
    status: str
    mode: str
    task_key: str
    repo: str
    final_task_status: str
    summary: str
    dry_run: bool
    confirmation_required: bool

    pr_number: int | None = None
    pr_url: str | None = None
    integrated_base_sha: str | None = None
    previous_integrated_base_sha: str | None = None
    reintegration_count: int = 0
    behind_count: int = 0
    already_up_to_date: bool = False

    validators_passed: bool = False
    validation_report: IntegrationValidationReport | None = None
    conflict_detected: bool = False
    conflict_resolved: bool = False
    # Names of the post-resolution checks that failed (review blocker B2).
    resolution_checks_failed: tuple[str, ...] = ()
    hints: tuple[ReviewerHint, ...] = ()

    # Safety facts, always reported so evidence readers never have to infer them.
    force_pushed: bool = False
    merged: bool = False
    cleanup_performed: bool = False
    lock_held_after_return: bool = False

    git_commands: tuple[tuple[str, ...], ...] = ()
    integration_run_id: str | None = None
    integration_json_path: Path | None = None
    # The Attempt that produced the tree this run integrates, or the recorded
    # reason there is none. Never a guess from the task's Attempt history.
    producer_attempt_binding: dict[str, Any] | None = None
    # Where this run's evidence was written: the producer Attempt's root, or
    # the task level with the reason (L2-M2 Exit Gate row 3).
    evidence_root: dict[str, Any] | None = None
    # The integration flock this run took, and what crash reconciliation did
    # on acquiring it (RULINGS 69). None when no lock was attempted.
    integration_lock: dict[str, Any] | None = None

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "kind": ARTIFACT_TYPE,
            "artifact_type": ARTIFACT_TYPE,
            "ok": self.ok,
            "status": self.status,
            "mode": self.mode,
            "task_key": self.task_key,
            "repo": self.repo,
            "final_task_status": self.final_task_status,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "integrated_base_sha": self.integrated_base_sha,
            "previous_integrated_base_sha": self.previous_integrated_base_sha,
            "reintegration_count": self.reintegration_count,
            "behind_count": self.behind_count,
            "already_up_to_date": self.already_up_to_date,
            "validators_passed": self.validators_passed,
            "validators": (
                self.validation_report.to_dict() if self.validation_report else None
            ),
            "conflict": {
                "detected": self.conflict_detected,
                "resolved": self.conflict_resolved,
                "failed_checks": list(self.resolution_checks_failed),
            },
            "hints": [{"code": h.code, "message": h.message} for h in self.hints],
            "git_commands": [list(command) for command in self.git_commands],
            "integration_run_id": self.integration_run_id,
            "producer_attempt_binding": self.producer_attempt_binding,
            "evidence_root": self.evidence_root,
            "integration_lock": self.integration_lock,
            "dry_run": self.dry_run,
            "confirmation_required": self.confirmation_required,
            "safety": {
                "force_pushed": self.force_pushed,
                "merged": self.merged,
                "cleanup_performed": self.cleanup_performed,
                "lock_held_after_return": self.lock_held_after_return,
                "human_review_required": True,
            },
            "summary": self.summary,
            "generated_at": utc_now_iso(),
        }


def integrate_task(
    request: IntegrationRequest,
    *,
    store: TaskMirrorStore | None = None,
    integration_store: IntegrationStore | None = None,
    github: GitHubPrAdapter | None = None,
) -> IntegrationResult:
    """Run one integration attempt for one Ticket."""
    task_store = store or TaskMirrorStore(request.db_path)
    task_store.init_db()
    integration = integration_store or IntegrationStore(store=task_store)

    task = task_store.get_task(request.task_key)
    if task is None:
        raise IntegrationControllerError(f"Task not found: {request.task_key}")

    worktree = task_store.get_task_worktree(request.task_key)
    if worktree is None:
        raise IntegrationControllerError(
            f"TaskWorktreeRecord missing for task: {request.task_key}"
        )
    if not worktree.worktree_path.is_dir():
        raise IntegrationControllerError(
            f"Worktree path is missing: {worktree.worktree_path}"
        )

    pr_state = integration.get_pr_state(request.task_key)
    mode = "initial" if pr_state["pr_number"] is None else "reintegration"

    pr_repo = schema.repo_from_pr_url(pr_state["pr_url"])
    if pr_repo is not None and pr_repo != schema.normalize_repo(request.repo):
        # §32.0 — a PR belongs to exactly one repository. Updating it from a
        # request for another repository would write to the wrong PR.
        return _simple_result(
            ok=False,
            status="blocked",
            mode=mode,
            request=request,
            final_task_status=task.status,
            summary=(
                f"PR {pr_state['pr_url']} belongs to {pr_repo}, not "
                f"{request.repo}; refusing to integrate it from another repository."
            ),
            pr_state=pr_state,
        )

    guard = _entry_guard(request, task=task, mode=mode, pr_state=pr_state)
    confirmed = not request.dry_run and request.confirm_integration
    # A Ticket in `integrating` may have been left there by a dead holder;
    # only the lock holder can tell, after reconciling it (RULINGS 69).
    if guard is not None and not (confirmed and task.status == schema.INTEGRATING):
        return guard

    if not confirmed:
        return _simple_result(
            ok=True,
            status="dry_run",
            mode=mode,
            request=request,
            final_task_status=task.status,
            summary=(
                "Dry run only; no git, GitHub, or lifecycle mutation was performed. "
                "Pass confirm_integration=True with dry_run=False to integrate."
            ),
            pr_state=pr_state,
            confirmation_required=not request.confirm_integration,
        )

    run_id = uuid4().hex[:12]
    # Refusals (a corrupt record, another host or clone) raise and change
    # nothing: they need a human, not a retry.
    lock = IntegrationRepoLock(
        request.repo,
        git_common_dir=git_ops.git_common_dir(worktree.worktree_path),
        run_id=run_id,
        task_key=request.task_key,
        db_path=task_store.db_path,
        owner=request.owner,
        lock_dir=request.lock_dir,
        external_hold_probe=request.external_hold_probe,
    )
    acquisition = lock.acquire()
    if not acquisition.acquired:
        # §22 — the repo is already integrating. Stay queued and try next tick.
        return _simple_result(
            ok=False,
            status="lock_unavailable",
            mode=mode,
            request=request,
            final_task_status=task.status,
            summary=acquisition.detail,
            pr_state=pr_state,
            integration_lock=_lock_evidence(lock, acquisition),
        )

    try:
        with lock.inherited_by_children():
            lock_evidence = _lock_evidence(lock, acquisition)
            # A journal row found now was written by a holder that died.
            leftover = integration.get_integration_lock(request.repo)
            lock_evidence["leftover_journal_row"] = leftover
            lock_evidence["crash_reconciliation"] = reconcile_integrating_tickets(
                task_store=task_store,
                integration=integration,
                repo_key=lock.key,
                git_common_dir=lock.git_common_dir,
                reconciler_run_id=run_id,
                lock_evidence={
                    key: value
                    for key, value in lock_evidence.items()
                    if key != "crash_reconciliation"
                },
            )
            if leftover is not None:
                integration.clear_integration_lock(request.repo)
            integration.acquire_integration_lock(lock.key, owner=request.owner)

            task = task_store.get_task(request.task_key) or task
            pr_state = integration.get_pr_state(request.task_key)
            mode = "initial" if pr_state["pr_number"] is None else "reintegration"
            guard = _entry_guard(
                request, task=task, mode=mode, pr_state=pr_state,
                integration_lock=lock_evidence,
            )
            if guard is not None:
                return guard
            return _integrate_under_lock(
                request,
                task=task,
                worktree=worktree,
                mode=mode,
                pr_state=pr_state,
                task_store=task_store,
                integration=integration,
                github=github,
                run_id=run_id,
                lock_evidence=lock_evidence,
            )
    finally:
        try:
            integration.release_integration_lock(lock.key, owner=request.owner)
        finally:
            lock.release()


def _lock_evidence(lock: IntegrationRepoLock, acquisition: LockAcquisition) -> dict[str, Any]:
    return {
        "key": lock.key,
        "lock_path": str(lock.lock_path),
        "record_path": str(lock.record_path),
        "git_common_dir": lock.git_common_dir,
        "acquisition": acquisition.to_dict(),
    }


def _entry_guard(
    request: IntegrationRequest,
    *,
    task: Any,
    mode: str,
    pr_state: dict[str, Any],
    integration_lock: dict[str, Any] | None = None,
) -> IntegrationResult | None:
    """Refuse a Ticket that is not ready, or whose PR already merged."""
    if task.status != schema.READY_FOR_INTEGRATION:
        return _simple_result(
            ok=False,
            status="blocked",
            mode=mode,
            request=request,
            final_task_status=task.status,
            summary=(
                f"Task {task.task_key} must be {schema.READY_FOR_INTEGRATION!r} to "
                f"integrate, got {task.status!r}"
            ),
            pr_state=pr_state,
            integration_lock=integration_lock,
        )

    if pr_state["pr_merged"]:
        # The §32 watcher records a human GitHub merge on any Ticket with an
        # open PR, including one queued here for re-integration. Integrating a
        # PR that has already merged would only push commits to a dead branch.
        return _simple_result(
            ok=False,
            status="blocked",
            mode=mode,
            request=request,
            final_task_status=task.status,
            summary=(
                f"PR #{pr_state['pr_number']} is already merged on GitHub; there "
                "is nothing left to integrate. Run merge verification and cleanup."
            ),
            pr_state=pr_state,
            integration_lock=integration_lock,
        )
    return None


def _integrate_under_lock(
    request: IntegrationRequest,
    *,
    task: Any,
    worktree: Any,
    mode: str,
    pr_state: dict[str, Any],
    task_store: TaskMirrorStore,
    integration: IntegrationStore,
    github: GitHubPrAdapter | None,
    run_id: str,
    lock_evidence: dict[str, Any],
) -> IntegrationResult:
    log = GitCommandLog()
    worktree_path = worktree.worktree_path
    target_ref = f"{request.remote}/{request.target_branch}"
    previous_base = pr_state["integrated_base_sha"]

    # Resolved while the entry is still queued — this run removes it once it
    # publishes — and bound to this run only, so a later tick re-resolves for
    # its own entry (M2.2).
    producer_binding = resolve_producer_attempt_binding(
        task_store, integration, request.task_key, repo=request.repo
    )
    # Evidence follows the producer: its Attempt root when one is bound and
    # usable, the task level otherwise, with the reason recorded either way.
    evidence_root = resolve_integration_evidence_root(
        task_store.db_path,
        task_key=request.task_key,
        task_artifact_dir=task.artifact_dir,
        producer_binding=producer_binding,
    )

    # Checkpoint first, so a Ticket in `integrating` never carries a previous
    # run's checkpoint (crash case A, RULINGS 69).
    integration.update_integration_state(
        request.task_key,
        last_integration_run_id=run_id,
        last_integration_status=RUN_STARTED,
        trigger_task_key=request.trigger,
    )
    task_store.update_task_status(
        request.task_key,
        schema.INTEGRATING,
        source=SOURCE,
        message=f"Integration run {run_id} started ({mode})",
        expected_current_status=schema.READY_FOR_INTEGRATION,
    )
    task_store.record_task_event(
        request.task_key,
        "integration_started",
        SOURCE,
        message=f"Integration run {run_id} ({mode})",
        payload={
            "integration_run_id": run_id,
            "mode": mode,
            "repo": request.repo,
            "producer_attempt_binding": producer_binding.to_dict(),
            "evidence_root": evidence_root.to_dict(),
            "integration_lock": {
                key: lock_evidence[key] for key in ("key", "lock_path", "git_common_dir")
            },
        },
    )

    def stop_for_decision(
        summary: str, *, audit: dict[str, Any] | None = None, **extra: Any
    ) -> IntegrationResult:
        task_store.update_task_status(
            request.task_key,
            schema.NEEDS_DECISION,
            source=SOURCE,
            message=summary,
            expected_current_status=schema.INTEGRATING,
        )
        task_store.record_task_event(
            request.task_key,
            "integration_blocked",
            SOURCE,
            message=summary,
            payload={
                "integration_run_id": run_id,
                "mode": mode,
                "reason": summary,
                "failed_checks": list(extra.get("resolution_checks_failed", ())),
                **(audit or {}),
            },
        )
        integration.update_integration_state(
            request.task_key, last_integration_status="needs_decision"
        )
        return _finish(
            request,
            task_store=task_store,
            integration=integration,
            ok=False,
            status="needs_decision",
            mode=mode,
            final_task_status=schema.NEEDS_DECISION,
            summary=summary,
            run_id=run_id,
            log=log,
            previous_base=previous_base,
            pr_state=integration.get_pr_state(request.task_key),
            task=task,
            producer_binding=producer_binding,
            evidence_root=evidence_root,
            integration_lock=lock_evidence,
            **extra,
        )

    # Review Ruling 18 — refuse an unpushable task branch as soon as
    # integration reads it, before any git command runs. A branch recorded as
    # `refs/heads/main` or `heads/main` names main itself; letting it through
    # would push to the target branch.
    try:
        git_ops.assert_task_branch_pushable(
            worktree.branch, base_branch=request.target_branch
        )
    except IntegrationGitError as exc:
        return stop_for_decision(
            f"Refusing to integrate before running any git command: {exc}"
        )

    def refuse_unvalidated_branch(
        exc: IntegrationGitError, *, stage: str, **extra: Any
    ) -> IntegrationResult:
        # Ruling 30b — refused before the push: nothing is pushed and
        # integrated_base_sha is not recorded.
        return stop_for_decision(
            f"Refusing to publish {worktree.branch}: it is not the tree "
            f"integration validated ({exc})",
            audit={
                "check": "head_on_task_branch",
                "stage": stage,
                "task_branch": worktree.branch,
                "mismatch": str(exc),
            },
            **extra,
        )

    def git_phase() -> IntegrationResult:
        """Everything from the fetch to the review hand-off (§23.1)."""
        # -- fetch and resolve the latest target (§44) -------------------------
        try:
            git_ops.fetch(worktree_path, remote=request.remote, log=log)
            target_sha = git_ops.resolve_target_sha(
                worktree_path, request.remote, request.target_branch, log=log
            )
        except IntegrationGitError as exc:
            return stop_for_decision(f"Could not resolve the latest target: {exc}")

        behind = git_ops.behind_count(worktree_path, "HEAD", target_ref, log=log)

        # -- update against the latest target ---------------------------------
        conflict_detected = False
        conflict_resolved = False
        already_up_to_date = False

        if mode == "initial":
            outcome = git_ops.rebase_onto_target(worktree_path, target_ref, log=log)
            conflicted_paths = outcome.conflicted_paths
            succeeded = outcome.ok
            abort = git_ops.abort_rebase
        else:
            outcome = git_ops.merge_target_into_branch(worktree_path, target_ref, log=log)
            conflicted_paths = outcome.conflicted_paths
            succeeded = outcome.ok
            already_up_to_date = outcome.already_up_to_date
            abort = git_ops.abort_merge

        if not succeeded:
            if not conflicted_paths:
                abort(worktree_path, log=log)
                return stop_for_decision(
                    f"Integration update against {target_ref} failed: {outcome.output}"
                )

            conflict_detected = True
            hunks = tuple(git_ops.conflict_hunks(worktree_path, log=log))
            head_before_resolution = git_ops.head_sha(worktree_path, log=log)
            resolution = resolve_conflicts(
                ConflictResolutionRequest(
                    task_key=request.task_key,
                    prompt=task.title or request.task_key,
                    worktree_path=worktree_path,
                    conflict_hunks=hunks,
                    task_diff=git_ops.diff_context(worktree_path, target_ref, log=log),
                    target_ref=target_ref,
                    files=conflicted_paths,
                ),
                resolver=request.conflict_resolver,
                integration_store=integration,
                integration_run_id=run_id,
            )
            conflict_resolved = resolution.resolved

            if not conflict_resolved:
                # §27.2.1 — the resolver could not produce a tree. No auto retry.
                if git_ops.in_progress_operation(worktree_path):
                    abort(worktree_path, log=log)
                return stop_for_decision(
                    "Integration conflict could not be resolved into a conflict-free "
                    f"tree: {resolution.explanation}",
                    conflict_detected=True,
                    conflict_resolved=False,
                    behind_count=behind,
                )

            # Review blocker B2 — a resolver's claim is never trusted on its own.
            # Before validators run, the control plane verifies the tree: nothing
            # in progress, a clean worktree, no conflict markers, a new HEAD, and
            # the latest target in HEAD's history. Any failure stops here: nothing
            # is pushed and integrated_base_sha is not recorded.
            verification = git_ops.verify_conflict_resolution(
                worktree_path,
                head_before=head_before_resolution,
                target_sha=target_sha,
                conflicted_files=conflicted_paths,
                log=log,
            )
            integration.record_conflict_verification(
                request.task_key,
                integration_run_id=run_id,
                checks=verification.to_list(),
            )
            if not verification.passed:
                if git_ops.in_progress_operation(worktree_path):
                    abort(worktree_path, log=log)
                return stop_for_decision(
                    "AI conflict resolution failed deterministic verification "
                    f"({', '.join(verification.failed)}): {resolution.explanation}",
                    conflict_detected=True,
                    conflict_resolved=False,
                    resolution_checks_failed=verification.failed,
                    behind_count=behind,
                )

        # -- the branch the push will publish is the tree we validate ---------
        # Review round 4, Ruling 30: the push names the branch, so HEAD must be
        # on the Ticket's branch and the branch must be HEAD. A detached HEAD
        # would otherwise validate a rebased commit and publish the old branch.
        try:
            branch_sha = git_ops.assert_head_on_task_branch(
                worktree_path, worktree.branch, log=log
            )
        except IntegrationGitError as exc:
            return refuse_unvalidated_branch(exc, stage="before_validators")

        # -- validators (§29) — every integration, including re-integration ----
        diff_context = git_ops.diff_context(worktree_path, target_ref, log=log)
        changed = git_ops.changed_files(worktree_path, target_ref, log=log)

        report = run_integration_validators(
            task_key=request.task_key,
            worktree_path=worktree_path,
            artifact_dir=evidence_root.path,
            specs=request.validator_specs,
            branch_sha=branch_sha,
            target_sha=target_sha,
            diff_context=diff_context,
            integration_run_id=run_id,
            integration_store=integration,
            producer_binding=producer_binding,
        )
        if not report.passed:
            # §29.1 — red validators stop for decision. No auto retry, no triage.
            return stop_for_decision(
                report.summary,
                validation_report=report,
                conflict_detected=conflict_detected,
                conflict_resolved=conflict_resolved,
                behind_count=behind,
            )

        # -- hints, PR body ----------------------------------------------------
        # A Ticket with a PR but no recorded base is finishing its initial
        # integration after a crash (case D), so this run is not a re-integration.
        increments_count = mode == "reintegration" and previous_base is not None
        reintegration_count = pr_state["reintegration_count"] or 0
        projected_count = reintegration_count + (1 if increments_count else 0)
        hints = tuple(
            build_reviewer_hints(
                ai_resolved_conflict=conflict_resolved,
                reintegration_count=projected_count,
                behind_count=behind,
                changed_files=changed,
            )
        )
        body = _pr_body(
            request=request,
            task=task,
            mode=mode,
            hints=hints,
            previous_base=previous_base,
            current_base=target_sha,
        )
        title = request.title or (task.title or request.task_key)

        # -- push and publish --------------------------------------------------
        # Ruling 30 again, now against the validated commit: a validator must
        # not have moved the branch or HEAD. Checked whether or not this run
        # pushes, because integrated_base_sha is recorded either way.
        try:
            git_ops.assert_head_on_task_branch(
                worktree_path, worktree.branch, expected_sha=branch_sha, log=log
            )
        except IntegrationGitError as exc:
            return refuse_unvalidated_branch(
                exc,
                stage="before_push",
                validation_report=report,
                conflict_detected=conflict_detected,
                conflict_resolved=conflict_resolved,
                behind_count=behind,
            )

        should_push = not (mode == "reintegration" and already_up_to_date) or (
            request.push_no_op_reintegration
        )
        if not should_push:
            # A re-integration whose push a crash cut short is up to date with
            # the target locally but not on the remote (crash case D, RULINGS
            # 69). Its validated commit must still be published.
            should_push = (
                git_ops.remote_branch_sha(
                    worktree_path, request.remote, worktree.branch, log=log
                )
                != branch_sha
            )
        integration.update_integration_state(
            request.task_key, last_integration_status=RUN_PUBLISHING
        )
        if should_push:
            try:
                git_ops.push_branch(
                    worktree_path,
                    remote=request.remote,
                    branch=worktree.branch,
                    base_branch=request.target_branch,
                    log=log,
                )
            except IntegrationGitError as exc:
                return stop_for_decision(f"Could not publish the task branch: {exc}")

        adapter = github or GitHubPrAdapter(request.repo)
        try:
            if mode == "initial":
                integration.update_integration_state(
                    request.task_key, last_integration_status=RUN_CREATING_PR
                )
                snapshot = adapter.create_pr(
                    base=request.target_branch,
                    head=worktree.branch,
                    title=title,
                    body=body,
                    cwd=worktree_path,
                    draft=request.draft,
                )
                # Review round 4, Ruling 29d: the PR now exists on GitHub.
                # Record its identity before anything else can fail, so no real
                # PR is ever orphaned and a retry updates it instead of opening
                # a second one. integrated_base_sha is recorded only below.
                integration.update_pr_state(
                    request.task_key,
                    pr_number=snapshot.number,
                    pr_url=snapshot.url,
                    pr_state="open",
                    pr_head_sha=branch_sha,
                )
            else:
                snapshot = adapter.update_pr(
                    pr_number=int(pr_state["pr_number"]),
                    cwd=worktree_path,
                    body=body,
                )
        except GitHubPrError as exc:
            return stop_for_decision(f"GitHub PR operation failed: {exc}")

        # -- record integrated_base_sha (§23.1) and dequeue, atomically --------
        recorded = integration.record_integration_completed(
            request.task_key,
            pr_fields=dict(
                pr_number=snapshot.number,
                pr_url=snapshot.url,
                pr_state=snapshot.state or "open",
                pr_head_sha=branch_sha,
                integrated_base_sha=target_sha,
                reintegration_required=False,
            ),
            increment_reintegration=increments_count,
            state_fields=dict(
                previous_integrated_base_sha=previous_base,
                new_target_sha=target_sha,
                behind_count=behind,
                last_integration_status=RUN_INTEGRATED,
            ),
        )
        projected_count = int(recorded["reintegration_count"] or 0)

        # -- enter review, still under the lock; it is released on return ------
        task_store.update_task_status(
            request.task_key,
            schema.NEEDS_REVIEW,
            source=SOURCE,
            message=f"Integration run {run_id} complete; awaiting human review",
            expected_current_status=schema.INTEGRATING,
        )
        task_store.record_task_event(
            request.task_key,
            "integration_completed",
            SOURCE,
            message=f"Integration run {run_id} complete ({mode})",
            payload={
                "integration_run_id": run_id,
                "mode": mode,
                "pr_number": snapshot.number,
                "integrated_base_sha": target_sha,
                "reintegration_count": projected_count,
                "producer_attempt_binding": producer_binding.to_dict(),
            },
        )

        return _finish(
            request,
            task_store=task_store,
            integration=integration,
            ok=True,
            status="integrated",
            mode=mode,
            final_task_status=schema.NEEDS_REVIEW,
            summary=(
                f"Integrated against {target_ref}@{target_sha[:12]}; "
                f"PR #{snapshot.number} is awaiting human review"
            ),
            run_id=run_id,
            log=log,
            previous_base=previous_base,
            pr_state=integration.get_pr_state(request.task_key),
            task=task,
            producer_binding=producer_binding,
            evidence_root=evidence_root,
            integration_lock=lock_evidence,
            validation_report=report,
            conflict_detected=conflict_detected,
            conflict_resolved=conflict_resolved,
            behind_count=behind,
            already_up_to_date=already_up_to_date,
            hints=hints,
        )

    # Review Rulings 19 and 31 — no failure of any kind may leave the Ticket in
    # `integrating`. Every call in the phase above either handles its own
    # failure or lands here — a git error, an undecodable byte, a missing `gh`
    # or `git` binary, anything — and ends in needs_decision via §27.2.1 with
    # an audited integration_blocked event naming the original exception.
    # Remapping infrastructure failures to §29.2 `failed` is Step 5's job; see
    # docs/v1/handoff-step2.md.
    try:
        return git_phase()
    except Exception as exc:  # noqa: BLE001 - the integration boundary
        current = task_store.get_task(request.task_key)
        if current is None or current.status != schema.INTEGRATING:
            # The Ticket already left `integrating` (the failure came after the
            # hand-off to review, or from inside a stop). It is not stuck, and
            # rewriting its status here would be a guess; surface the error.
            raise
        try:
            operation = git_ops.in_progress_operation(worktree_path)
            if operation is not None:
                abort_operation = (
                    git_ops.abort_merge if operation == "merge" else git_ops.abort_rebase
                )
                abort_operation(worktree_path, log=log)
        except Exception:  # noqa: BLE001 - the stop below is recorded either way
            pass
        if isinstance(exc, IntegrationGitError):
            summary = f"A git operation failed during integration: {exc}"
        else:
            summary = f"Integration failed with {type(exc).__name__}: {exc}"
        return stop_for_decision(
            summary,
            audit={
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
            },
        )


def _pr_body(
    *,
    request: IntegrationRequest,
    task: Any,
    mode: str,
    hints: Sequence[ReviewerHint],
    previous_base: str | None,
    current_base: str,
) -> str:
    if request.body is not None:
        base_body = request.body
    else:
        base_body = (
            f"Ticket: {task.task_key}\n"
            f"{task.title or ''}\n\n"
            f"Integrated against `{request.remote}/{request.target_branch}` "
            f"@ `{current_base}`."
        )

    sections = [base_body]
    if mode == "reintegration":
        sections.append(
            render_reintegration_hint_block(
                previous_base_sha=previous_base,
                current_base_sha=current_base,
                trigger=request.trigger,
            )
        )
    rendered_hints = render_hints_markdown(hints)
    if rendered_hints:
        sections.append(f"Reviewer hints:\n{rendered_hints}")
    return "\n\n".join(section for section in sections if section)


def _simple_result(
    *,
    ok: bool,
    status: str,
    mode: str,
    request: IntegrationRequest,
    final_task_status: str,
    summary: str,
    pr_state: dict[str, Any],
    confirmation_required: bool = False,
    integration_lock: dict[str, Any] | None = None,
) -> IntegrationResult:
    return IntegrationResult(
        ok=ok,
        status=status,
        mode=mode,
        task_key=request.task_key,
        repo=request.repo,
        final_task_status=final_task_status,
        summary=summary,
        dry_run=request.dry_run or not request.confirm_integration,
        confirmation_required=confirmation_required,
        pr_number=pr_state["pr_number"],
        pr_url=pr_state["pr_url"],
        integrated_base_sha=pr_state["integrated_base_sha"],
        reintegration_count=pr_state["reintegration_count"] or 0,
        integration_lock=integration_lock,
    )


def _finish(
    request: IntegrationRequest,
    *,
    task_store: TaskMirrorStore,
    integration: IntegrationStore,
    ok: bool,
    status: str,
    mode: str,
    final_task_status: str,
    summary: str,
    run_id: str,
    log: GitCommandLog,
    previous_base: str | None,
    pr_state: dict[str, Any],
    task: Any,
    producer_binding: ProducerAttemptBinding | None = None,
    evidence_root: IntegrationEvidenceRoot | None = None,
    integration_lock: dict[str, Any] | None = None,
    validation_report: IntegrationValidationReport | None = None,
    conflict_detected: bool = False,
    conflict_resolved: bool = False,
    resolution_checks_failed: Sequence[str] = (),
    behind_count: int = 0,
    already_up_to_date: bool = False,
    hints: Sequence[ReviewerHint] = (),
) -> IntegrationResult:
    result = IntegrationResult(
        ok=ok,
        status=status,
        mode=mode,
        task_key=request.task_key,
        repo=request.repo,
        final_task_status=final_task_status,
        summary=summary,
        dry_run=False,
        confirmation_required=False,
        pr_number=pr_state["pr_number"],
        pr_url=pr_state["pr_url"],
        integrated_base_sha=pr_state["integrated_base_sha"],
        previous_integrated_base_sha=previous_base,
        reintegration_count=pr_state["reintegration_count"] or 0,
        behind_count=behind_count,
        already_up_to_date=already_up_to_date,
        validators_passed=bool(validation_report and validation_report.passed),
        validation_report=validation_report,
        conflict_detected=conflict_detected,
        conflict_resolved=conflict_resolved,
        resolution_checks_failed=tuple(resolution_checks_failed),
        hints=tuple(hints),
        git_commands=log.as_tuple(),
        integration_run_id=run_id,
        producer_attempt_binding=(
            None if producer_binding is None else producer_binding.to_dict()
        ),
        evidence_root=None if evidence_root is None else evidence_root.to_dict(),
        integration_lock=integration_lock,
    )

    directory_root = (
        evidence_root.path if evidence_root is not None else task.artifact_dir
    )
    if directory_root is None:
        return result

    directory = Path(directory_root) / "integration"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"integration-{run_id}.json"
    atomic_write_json(path, result.to_summary_dict(), sort_keys=True)
    task_store.record_task_artifact(request.task_key, ARTIFACT_TYPE, path)

    return IntegrationResult(
        **{
            **result.__dict__,
            "integration_json_path": path,
        }
    )
