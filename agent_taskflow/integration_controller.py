"""The Step 2 Integration Controller (§23, §24, §26, §27, §29, §31).

One entry point, :func:`integrate_task`, drives a Ticket from
``ready_for_integration`` to ``needs_review`` (or stops it at
``needs_decision``) under the Loose Integration Lock.

The pipeline follows §23.1 exactly, including the ordering that matters most:

    acquire repo lock -> fetch -> update against latest target ->
    resolve conflicts -> validators -> push/update PR ->
    record integrated_base_sha -> release lock -> needs_review

``needs_review`` is set *after* the lock is released, which is what makes
"the integration lock does not wait for human review" (§44) structurally true
rather than merely intended, and what lets several same-repo PRs sit in
``needs_review`` simultaneously (§43.17).

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
from agent_taskflow.integration_conflict_resolver import (
    ConflictResolutionRequest,
    ConflictResolver,
    resolve_conflicts,
)
from agent_taskflow.integration_git import GitCommandLog, IntegrationGitError
from agent_taskflow.integration_queue import (
    IntegrationLock,
    IntegrationLockUnavailable,
    remove_from_queue,
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

    # Ambiguity watchlist (see HANDOFF.md): the spec does not say whether a
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
    hints: tuple[ReviewerHint, ...] = ()

    # Safety facts, always reported so evidence readers never have to infer them.
    force_pushed: bool = False
    merged: bool = False
    cleanup_performed: bool = False
    lock_held_after_return: bool = False

    git_commands: tuple[tuple[str, ...], ...] = ()
    integration_run_id: str | None = None
    integration_json_path: Path | None = None

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
            },
            "hints": [{"code": h.code, "message": h.message} for h in self.hints],
            "git_commands": [list(command) for command in self.git_commands],
            "integration_run_id": self.integration_run_id,
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
        )

    if request.dry_run or not request.confirm_integration:
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

    try:
        lock = IntegrationLock(integration, request.repo, owner=request.owner)
        lock.__enter__()
    except IntegrationLockUnavailable as exc:
        # §22 — the repo is already integrating. Stay queued and try next tick.
        return _simple_result(
            ok=False,
            status="lock_unavailable",
            mode=mode,
            request=request,
            final_task_status=task.status,
            summary=str(exc),
            pr_state=pr_state,
        )

    try:
        return _integrate_under_lock(
            request,
            task=task,
            worktree=worktree,
            mode=mode,
            pr_state=pr_state,
            task_store=task_store,
            integration=integration,
            github=github,
            lock=lock,
        )
    finally:
        lock.__exit__(None, None, None)


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
    lock: IntegrationLock,
) -> IntegrationResult:
    run_id = uuid4().hex[:12]
    log = GitCommandLog()
    worktree_path = worktree.worktree_path
    target_ref = f"{request.remote}/{request.target_branch}"
    previous_base = pr_state["integrated_base_sha"]

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
        payload={"integration_run_id": run_id, "mode": mode, "repo": request.repo},
    )
    integration.update_integration_state(
        request.task_key,
        last_integration_run_id=run_id,
        last_integration_status="running",
        trigger_task_key=request.trigger,
    )

    def stop_for_decision(summary: str, **extra: Any) -> IntegrationResult:
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
            payload={"integration_run_id": run_id, "mode": mode, "reason": summary},
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
            **extra,
        )

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

        still_conflicted = bool(git_ops.conflicted_paths(worktree_path, log=log))
        if not conflict_resolved or still_conflicted:
            # §27.2.1 — no conflict-free tree, so stop. No auto retry.
            abort(worktree_path, log=log)
            return stop_for_decision(
                "Integration conflict could not be resolved into a conflict-free "
                f"tree: {resolution.explanation}",
                conflict_detected=True,
                conflict_resolved=False,
                behind_count=behind,
            )

    # -- validators (§29) — every integration, including re-integration ----
    branch_sha = git_ops.head_sha(worktree_path, log=log)
    diff_context = git_ops.diff_context(worktree_path, target_ref, log=log)
    changed = git_ops.changed_files(worktree_path, target_ref, log=log)

    report = run_integration_validators(
        task_key=request.task_key,
        worktree_path=worktree_path,
        artifact_dir=task.artifact_dir,
        specs=request.validator_specs,
        branch_sha=branch_sha,
        target_sha=target_sha,
        diff_context=diff_context,
        integration_run_id=run_id,
        integration_store=integration,
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
    reintegration_count = pr_state["reintegration_count"] or 0
    projected_count = reintegration_count + (1 if mode == "reintegration" else 0)
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
    should_push = not (mode == "reintegration" and already_up_to_date) or (
        request.push_no_op_reintegration
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
            snapshot = adapter.create_pr(
                base=request.target_branch,
                head=worktree.branch,
                title=title,
                body=body,
                cwd=worktree_path,
                draft=request.draft,
            )
        else:
            snapshot = adapter.update_pr(
                pr_number=int(pr_state["pr_number"]),
                cwd=worktree_path,
                body=body,
            )
    except GitHubPrError as exc:
        return stop_for_decision(f"GitHub PR operation failed: {exc}")

    # -- record integrated_base_sha (§23.1) --------------------------------
    integration.update_pr_state(
        request.task_key,
        pr_number=snapshot.number,
        pr_url=snapshot.url,
        pr_state=snapshot.state or "open",
        pr_head_sha=branch_sha,
        integrated_base_sha=target_sha,
        reintegration_required=False,
    )
    if mode == "reintegration":
        projected_count = integration.increment_reintegration_count(request.task_key)
    integration.update_integration_state(
        request.task_key,
        previous_integrated_base_sha=previous_base,
        new_target_sha=target_sha,
        behind_count=behind,
        last_integration_status="integrated",
    )
    remove_from_queue(integration, request.task_key)

    # -- release the lock, *then* enter review (§23.1, §44) ----------------
    lock.__exit__(None, None, None)

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
        validation_report=report,
        conflict_detected=conflict_detected,
        conflict_resolved=conflict_resolved,
        behind_count=behind,
        already_up_to_date=already_up_to_date,
        hints=hints,
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
    validation_report: IntegrationValidationReport | None = None,
    conflict_detected: bool = False,
    conflict_resolved: bool = False,
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
        hints=tuple(hints),
        git_commands=log.as_tuple(),
        integration_run_id=run_id,
    )

    if task.artifact_dir is None:
        return result

    directory = Path(task.artifact_dir) / "integration"
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
