"""Cleanup after a verified merge, or after confirmed cancellation (§37, §37.1).

Cleanup is the only place in Step 2 that removes anything, so it is gated
twice over:

* The **merged route** requires §36 verification — the GitHub PR reports
  merged, ``merge_commit_sha`` is present, and that SHA is contained in the
  latest target history. Only this route may set ``completed``.
* The **cancelled route** (§37.1) requires an explicit, separate confirmation
  flag. Closed-unmerged work is never destroyed automatically, because a PR
  closed without merging does not mean the work is safe to discard (§33.5).
  This route never sets ``completed``.

Remote branch deletion is OFF in V1 (human decision): §37 calls it
"optional", ``git push --delete`` is outside the push allowlist, and remote
task branches are left to GitHub's automatic head-branch deletion or manual
deletion. A request that asks for it is refused before anything is removed.

Cleanup fails closed on its target (V1-F10, orchestrator ruling OR-3), because
the integration tick may now run it unattended:

* it removes only the git-registered, clean worktree at the recorded path on
  the recorded branch, and never deletes a directory git has not registered;
* it deletes the local branch only when its tip is already published in the
  recorded PR head or the remote task branch, never newer local work, and
  only if the tip is still the one that check proved published;
* an unsafe target is refused before anything is recorded, and a cleanup that
  still leaves the worktree or branch behind never completes the Ticket;
* an unconfirmed (preview) request persists nothing at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_taskflow import integration_git as git_ops
from agent_taskflow import integration_schema as schema
from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.governance import assert_worktree_inside_repo_worktrees
from agent_taskflow.integration_git import GitCommandLog
from agent_taskflow.integration_queue import remove_from_queue
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.merge_verification import (
    MergeVerificationRequest,
    MergeVerificationResult,
    verify_merge,
)
from agent_taskflow.models import TaskWorktreeRecord, utc_now_iso
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.ticket_worktree import (
    WORKTREE_ABSENT,
    WORKTREE_READY,
    TicketWorktree,
    inspect_ticket_worktree,
)
from agent_taskflow.worktree import ensure_absolute_path


__all__ = [
    "ARTIFACT_TYPE",
    "IntegrationCleanupError",
    "IntegrationCleanupRequest",
    "IntegrationCleanupResult",
    "run_integration_cleanup",
]


ARTIFACT_TYPE = "integration_cleanup"
SOURCE = "integration_cleanup"


class IntegrationCleanupError(RuntimeError):
    """Raised when cleanup cannot be attempted at all."""


@dataclass(frozen=True)
class IntegrationCleanupRequest:
    """One cleanup attempt for one Ticket."""

    task_key: str
    repo: str
    repo_path: Path
    target_branch: str = "main"
    remote: str = "origin"
    db_path: Path | None = None
    confirm_cleanup: bool = False

    # §37.1 — a second, separate confirmation for closed-unmerged work.
    confirm_cancelled_cleanup: bool = False

    # SPEC §37 "optional remote branch cleanup" is OFF in V1 (human decision).
    # Setting this to True is refused up front: `git push --delete` is outside
    # the push allowlist, and no exception is made for it.
    delete_remote_branch: bool = False
    archive_evidence: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
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


@dataclass(frozen=True)
class IntegrationCleanupResult:
    """The outcome of one cleanup attempt, with every safety fact stated."""

    ok: bool
    status: str
    route: str
    task_key: str
    repo: str
    final_task_status: str
    summary: str

    merge_verified: bool = False
    merge_commit_sha: str | None = None
    target_sha: str | None = None
    verification_reasons: tuple[str, ...] = ()

    worktree_removed: bool = False
    local_branch_deleted: bool = False
    remote_branch_deleted: bool = False
    evidence_archived: bool = False

    merged: bool = False
    force_pushed: bool = False
    confirmation_required: bool = False
    git_commands: tuple[tuple[str, ...], ...] = ()
    cleanup_json_path: Path | None = None

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "kind": ARTIFACT_TYPE,
            "artifact_type": ARTIFACT_TYPE,
            "ok": self.ok,
            "status": self.status,
            "route": self.route,
            "task_key": self.task_key,
            "repo": self.repo,
            "final_task_status": self.final_task_status,
            "merge_verified": self.merge_verified,
            "merge_commit_sha": self.merge_commit_sha,
            "target_sha": self.target_sha,
            "verification_reasons": list(self.verification_reasons),
            "worktree_removed": self.worktree_removed,
            "local_branch_deleted": self.local_branch_deleted,
            "remote_branch_deleted": self.remote_branch_deleted,
            "evidence_archived": self.evidence_archived,
            "git_commands": [list(command) for command in self.git_commands],
            "safety": {
                "merged_by_taskflow": self.merged,
                "force_pushed": self.force_pushed,
                "human_review_required": True,
                "cleanup_requires_verified_merge_or_explicit_confirmation": True,
            },
            "summary": self.summary,
            "generated_at": utc_now_iso(),
        }


def run_integration_cleanup(
    request: IntegrationCleanupRequest,
    *,
    store: TaskMirrorStore | None = None,
    integration_store: IntegrationStore | None = None,
) -> IntegrationCleanupResult:
    """Clean up after a verified merge, or after confirmed cancellation."""
    task_store = store or TaskMirrorStore(request.db_path)
    task_store.init_db()
    integration = integration_store or IntegrationStore(store=task_store)

    task = task_store.get_task(request.task_key)
    if task is None:
        raise IntegrationCleanupError(f"Task not found: {request.task_key}")

    worktree = task_store.get_task_worktree(request.task_key)
    if worktree is None:
        raise IntegrationCleanupError(
            f"TaskWorktreeRecord missing for task: {request.task_key}"
        )

    pr_state = integration.get_pr_state(request.task_key)
    log = GitCommandLog()

    cancelled_route = (
        task.status == schema.CANCELLED
        and not pr_state["pr_merged"]
    )
    route = "cancelled_unmerged" if cancelled_route else "verified_merge"

    def result(
        *,
        ok: bool,
        status: str,
        summary: str,
        verification: MergeVerificationResult | None = None,
        confirmation_required: bool = False,
        **extra: Any,
    ) -> IntegrationCleanupResult:
        return _finish(
            request,
            task=task,
            task_store=task_store,
            ok=ok,
            status=status,
            route=route,
            final_task_status=task_store.get_task(request.task_key).status,
            summary=summary,
            verification=verification,
            confirmation_required=confirmation_required,
            log=log,
            **extra,
        )

    # The transition table decides which statuses a verified merge may complete.
    # After the §32 pickup ruling that includes needs_decision, paused and
    # ready_for_integration, because a human can merge while a Ticket waits in
    # any of them. It never includes an in-flight integration.
    if request.delete_remote_branch:
        # Refused before anything is removed, so a request for remote-branch
        # cleanup can never leave a half-done cleanup behind.
        return result(
            ok=False,
            status="blocked",
            summary=(
                "Cleanup refused: remote-branch cleanup (SPEC §37, optional) is off "
                "in V1 — `git push --delete` is outside the push allowlist. Remote "
                "task branches are left to GitHub's automatic head-branch deletion "
                "or manual deletion. Re-run without delete_remote_branch."
            ),
        )

    if not cancelled_route and not schema.can_transition(task.status, schema.COMPLETED):
        if task.status == schema.INTEGRATING:
            reason = "an integration is in progress; cleanup waits for it to finish (§25.0)"
        else:
            reason = f"a Ticket in {task.status!r} cannot be completed"
        return result(ok=False, status="blocked", summary=f"Cleanup refused: {reason}.")

    if cancelled_route:
        if not request.confirm_cancelled_cleanup:
            return result(
                ok=False,
                status="confirmation_required",
                summary=(
                    "Closed-unmerged work is retained until a human explicitly "
                    "confirms cleanup: pass confirm_cancelled_cleanup=True (§37.1)."
                ),
                confirmation_required=True,
            )
        refusal, verified_tip = _cleanup_target_refusal(request, worktree, pr_state, log=log)
        if refusal:
            return result(
                ok=False, status="cleanup_refused", summary=f"Cleanup refused: {refusal}."
            )
        if not request.confirm_cleanup:
            return result(
                ok=False,
                status="dry_run",
                summary="Dry run only; pass confirm_cleanup=True to remove anything.",
                confirmation_required=True,
            )
        return _perform_cleanup(
            request,
            task=task,
            worktree=worktree,
            route=route,
            task_store=task_store,
            integration=integration,
            log=log,
            verification=None,
            verified_tip=verified_tip,
            complete_task=False,
        )

    # -- verified-merge route ---------------------------------------------
    # A retry after an incomplete cleanup may find the worktree already
    # removed; the fetch and ancestry check then run in the repository itself.
    verification = verify_merge(
        MergeVerificationRequest(
            task_key=request.task_key,
            worktree_path=(
                worktree.worktree_path
                if worktree.worktree_path.is_dir()
                else request.repo_path
            ),
            remote=request.remote,
            target_branch=request.target_branch,
            pr_merged=bool(pr_state["pr_merged"]),
            merge_commit_sha=pr_state["merge_commit_sha"],
        ),
        log=log,
    )
    if not verification.verified:
        return result(
            ok=False,
            status="merge_not_verified",
            summary=(
                "Cleanup refused: the merge is not verified per §36 — "
                + "; ".join(verification.reasons)
            ),
            verification=verification,
        )

    # Checked before merge_verified_at is recorded, so a refused Ticket stays
    # in the merged-unverified pick-up and is reported again on the next tick.
    refusal, verified_tip = _cleanup_target_refusal(request, worktree, pr_state, log=log)
    if refusal:
        return result(
            ok=False,
            status="cleanup_refused",
            summary=f"Cleanup refused: {refusal}.",
            verification=verification,
        )

    if not request.confirm_cleanup:
        return result(
            ok=True,
            status="dry_run",
            summary=(
                "Merge is verified, but cleanup is dry-run by default; pass "
                "confirm_cleanup=True to remove the worktree and branch."
            ),
            verification=verification,
            confirmation_required=True,
        )

    integration.update_integration_state(
        request.task_key, merge_verified_at=verification.verified_at
    )
    task_store.record_task_event(
        request.task_key,
        "merge_verified",
        SOURCE,
        message=f"Merge {verification.merge_commit_sha} verified in target history",
        payload=verification.to_dict(),
    )

    return _perform_cleanup(
        request,
        task=task,
        worktree=worktree,
        route=route,
        task_store=task_store,
        integration=integration,
        log=log,
        verification=verification,
        verified_tip=verified_tip,
        complete_task=True,
    )


def _perform_cleanup(
    request: IntegrationCleanupRequest,
    *,
    task: Any,
    worktree: TaskWorktreeRecord,
    route: str,
    task_store: TaskMirrorStore,
    integration: IntegrationStore,
    log: GitCommandLog,
    verification: MergeVerificationResult | None,
    verified_tip: str | None,
    complete_task: bool,
) -> IntegrationCleanupResult:
    worktree_removed = _remove_worktree(request, worktree, log=log)
    # git refuses to delete a branch a worktree still has checked out.
    branch_retained = (
        _delete_local_branch(request, worktree, verified_tip=verified_tip, log=log)
        if worktree_removed
        else None
    )
    local_branch_deleted = worktree_removed and branch_retained is None
    # SPEC §37 optional remote-branch cleanup is off in V1 (human decision).
    remote_branch_deleted = False

    # "Archive evidence" (§37) means retain and index it, never delete it.
    evidence_archived = bool(request.archive_evidence and task.artifact_dir)

    if worktree_removed and worktree.status != "cleaned":
        task_store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=request.task_key,
                repo_path=worktree.repo_path,
                worktree_path=worktree.worktree_path,
                branch=worktree.branch,
                base_branch=worktree.base_branch,
                base_sha=worktree.base_sha,
                status="cleaned",
                created_at=worktree.created_at,
                cleaned_at=utc_now_iso(),
            )
        )

    if not (worktree_removed and local_branch_deleted):
        # Nothing is forced: the retained worktree or branch waits for a human,
        # and the Ticket is neither completed nor marked cleaned up.
        return _finish(
            request,
            task=task,
            task_store=task_store,
            ok=False,
            status="cleanup_incomplete",
            route=route,
            final_task_status=task_store.get_task(request.task_key).status,
            summary=(
                "Cleanup is incomplete and the Ticket is not completed: "
                f"worktree_removed={worktree_removed}, "
                f"local_branch_deleted={local_branch_deleted}"
                + (f"; {branch_retained}." if branch_retained else ".")
            ),
            verification=verification,
            log=log,
            worktree_removed=worktree_removed,
            local_branch_deleted=local_branch_deleted,
            evidence_archived=evidence_archived,
        )

    integration.update_integration_state(
        request.task_key, cleanup_confirmed_at=utc_now_iso()
    )

    if complete_task:
        task_store.update_task_status(
            request.task_key,
            schema.COMPLETED,
            source=SOURCE,
            message="Merge verified and cleanup completed",
            expected_current_status=task.status,
        )
        # A Ticket completed straight from ready_for_integration must not
        # linger in its repo queue.
        remove_from_queue(integration, request.task_key)
        summary = (
            f"Merge {verification.merge_commit_sha if verification else ''} verified; "
            "cleanup completed and Ticket marked completed"
        )
    else:
        summary = (
            "Confirmed cleanup of closed-unmerged work completed; the Ticket "
            "remains cancelled and is not marked completed (§37.1)."
        )

    task_store.record_task_event(
        request.task_key,
        "integration_cleanup_completed",
        SOURCE,
        message=summary,
        payload={
            "route": route,
            "worktree_removed": worktree_removed,
            "local_branch_deleted": local_branch_deleted,
            "remote_branch_deleted": remote_branch_deleted,
            "evidence_archived": evidence_archived,
            "merge_verified": bool(verification and verification.verified),
        },
    )

    return _finish(
        request,
        task=task,
        task_store=task_store,
        ok=True,
        status="cleaned",
        route=route,
        final_task_status=task_store.get_task(request.task_key).status,
        summary=summary,
        verification=verification,
        log=log,
        worktree_removed=worktree_removed,
        local_branch_deleted=local_branch_deleted,
        remote_branch_deleted=remote_branch_deleted,
        evidence_archived=evidence_archived,
    )


def _remove_worktree(
    request: IntegrationCleanupRequest,
    worktree: TaskWorktreeRecord,
    *,
    log: GitCommandLog,
) -> bool:
    """Remove the task worktree through git only. True once it is gone."""
    path = worktree.worktree_path
    if not path.exists():
        # Already removed, e.g. by an earlier incomplete attempt.
        return True
    try:
        # Refuse to remove anything that is not a task worktree under the repo.
        assert_worktree_inside_repo_worktrees(path, request.repo_path)
    except ValueError:
        return False

    # No --force flag: cleanup is already gated on a verified merge plus an
    # explicit confirmation, and keeping every argv force-free makes "no code
    # path force-pushes" checkable by inspection. A worktree git declines to
    # remove is retained as it is; nothing falls back to deleting the path.
    git_ops.run_git(request.repo_path, ["worktree", "remove", str(path)], log=log)
    return not path.exists()


def _delete_local_branch(
    request: IntegrationCleanupRequest,
    worktree: TaskWorktreeRecord,
    *,
    verified_tip: str | None,
    log: GitCommandLog,
) -> str | None:
    """Delete the local task branch. None once it is gone, else why it is kept."""
    branch = worktree.branch
    if branch in git_ops.PROTECTED_BRANCHES or branch == request.target_branch:
        return f"the branch {branch} is protected and is retained"
    current = git_ops.run_git(
        request.repo_path,
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        log=log,
    )
    if not current.ok:
        # Already deleted, e.g. by an earlier incomplete attempt.
        return None
    tip = current.stdout.strip()
    if tip != verified_tip:
        # Re-read after the worktree is gone: a commit made while it was being
        # removed moved the branch past the tip the target check proved
        # published. The branch is kept; a retry checks its new tip.
        return (
            f"the local branch {branch} moved to {tip} after the target check "
            f"verified {verified_tip or 'no branch'}, and is retained"
        )
    # -D rather than -d: a merge commit / squash / rebase merge means the task
    # branch is often not an ancestor of the target, so -d would refuse even
    # though §36 verification has already proved the work landed. The target
    # check proved this exact tip published.
    deleted = git_ops.run_git(request.repo_path, ["branch", "-D", branch], log=log)
    if deleted.ok:
        return None
    return f"git branch -D {branch} failed: {deleted.combined}"


def _cleanup_target_refusal(
    request: IntegrationCleanupRequest,
    worktree: TaskWorktreeRecord,
    pr_state: dict[str, Any],
    *,
    log: GitCommandLog,
) -> tuple[str | None, str | None]:
    """Return ``(refusal, verified_tip)`` for the recorded cleanup target (OR-3).

    Read-only. The worktree must be absent, or registered with git at exactly
    the recorded path on exactly the recorded branch and clean. The local
    branch must be absent, or have a tip already contained in the recorded PR
    head or the remote task branch, so deleting it can lose no commit.

    ``refusal`` says why the target is unsafe, or is None. ``verified_tip`` is
    the local branch tip proved published (None when there is no branch);
    the branch is deleted later only if its tip is still exactly this one.
    """
    if worktree.repo_path.resolve() != request.repo_path.resolve():
        return (
            f"the recorded worktree belongs to {worktree.repo_path}, "
            f"not {request.repo_path}"
        ), None
    branch = worktree.branch
    normalized = git_ops.normalize_branch_ref(branch)
    if normalized in git_ops.PROTECTED_BRANCHES or normalized == git_ops.normalize_branch_ref(
        request.target_branch
    ):
        return f"the recorded branch {branch!r} is a protected branch", None
    try:
        assert_worktree_inside_repo_worktrees(worktree.worktree_path, request.repo_path)
    except ValueError as exc:
        return str(exc), None

    inspection = inspect_ticket_worktree(
        TicketWorktree(
            task_key=request.task_key,
            repo_path=request.repo_path,
            worktree_path=worktree.worktree_path,
            branch=branch,
            base_branch=worktree.base_branch or request.target_branch,
        )
    )
    if inspection.state not in {WORKTREE_READY, WORKTREE_ABSENT}:
        return f"the worktree is not the registered task worktree ({inspection.detail})", None
    if inspection.dirty:
        return (
            f"the worktree {worktree.worktree_path} has uncommitted or untracked "
            "changes and is retained"
        ), None

    resolved = git_ops.run_git(
        request.repo_path,
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        log=log,
    )
    if not resolved.ok:
        if inspection.state == WORKTREE_READY:
            return f"the registered worktree's branch {branch} cannot be resolved", None
        return None, None
    tip = resolved.stdout.strip()
    published = (pr_state.get("pr_head_sha"), f"refs/remotes/{request.remote}/{branch}")
    if any(
        ref and git_ops.commit_in_history(request.repo_path, tip, ref, log=log)
        for ref in published
    ):
        return None, tip
    return (
        f"the local branch {branch} has commits that are in neither the recorded "
        "PR head nor the remote task branch, and is retained"
    ), None


def _finish(
    request: IntegrationCleanupRequest,
    *,
    task: Any,
    task_store: TaskMirrorStore,
    ok: bool,
    status: str,
    route: str,
    final_task_status: str,
    summary: str,
    verification: MergeVerificationResult | None,
    log: GitCommandLog,
    confirmation_required: bool = False,
    worktree_removed: bool = False,
    local_branch_deleted: bool = False,
    remote_branch_deleted: bool = False,
    evidence_archived: bool = False,
) -> IntegrationCleanupResult:
    result = IntegrationCleanupResult(
        ok=ok,
        status=status,
        route=route,
        task_key=request.task_key,
        repo=request.repo,
        final_task_status=final_task_status,
        summary=summary,
        merge_verified=bool(verification and verification.verified),
        merge_commit_sha=verification.merge_commit_sha if verification else None,
        target_sha=verification.target_sha if verification else None,
        verification_reasons=verification.reasons if verification else (),
        worktree_removed=worktree_removed,
        local_branch_deleted=local_branch_deleted,
        remote_branch_deleted=remote_branch_deleted,
        evidence_archived=evidence_archived,
        confirmation_required=confirmation_required,
        git_commands=log.as_tuple(),
    )

    # A preview (confirm_cleanup=False) persists nothing, not even the record
    # of a refusal: the integration tick runs cleanup in preview unattended.
    if task.artifact_dir is None or status == "dry_run" or not request.confirm_cleanup:
        return result

    directory = Path(task.artifact_dir) / "integration"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"cleanup-{uuid4().hex[:12]}.json"
    atomic_write_json(path, result.to_summary_dict(), sort_keys=True)
    task_store.record_task_artifact(request.task_key, ARTIFACT_TYPE, path)

    return IntegrationCleanupResult(
        **{**result.__dict__, "cleanup_json_path": path}
    )
