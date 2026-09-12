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
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
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
            complete_task=False,
        )

    # -- verified-merge route ---------------------------------------------
    verification = verify_merge(
        MergeVerificationRequest(
            task_key=request.task_key,
            worktree_path=worktree.worktree_path,
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
    complete_task: bool,
) -> IntegrationCleanupResult:
    worktree_removed = _remove_worktree(request, worktree, log=log)
    local_branch_deleted = _delete_local_branch(request, worktree, log=log)
    # SPEC §37 optional remote-branch cleanup is off in V1 (human decision).
    remote_branch_deleted = False

    # "Archive evidence" (§37) means retain and index it, never delete it.
    evidence_archived = bool(request.archive_evidence and task.artifact_dir)

    if worktree_removed:
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
    path = worktree.worktree_path
    if not path.exists():
        return False
    try:
        # Refuse to remove anything that is not a task worktree under the repo.
        assert_worktree_inside_repo_worktrees(path, request.repo_path)
    except ValueError:
        return False

    # No --force flag: cleanup is already gated on a verified merge plus an
    # explicit confirmation, and keeping every argv force-free makes "no code
    # path force-pushes" checkable by inspection.
    result = git_ops.run_git(
        request.repo_path, ["worktree", "remove", str(path)], log=log
    )
    if not result.ok and path.exists():
        shutil.rmtree(path, ignore_errors=True)
        git_ops.run_git(request.repo_path, ["worktree", "prune"], log=log)
    return not path.exists()


def _delete_local_branch(
    request: IntegrationCleanupRequest,
    worktree: TaskWorktreeRecord,
    *,
    log: GitCommandLog,
) -> bool:
    branch = worktree.branch
    if branch in git_ops.PROTECTED_BRANCHES or branch == request.target_branch:
        return False
    # -D rather than -d: a merge commit / squash / rebase merge means the task
    # branch is often not an ancestor of the target, so -d would refuse even
    # though §36 verification has already proved the work landed.
    return git_ops.run_git(request.repo_path, ["branch", "-D", branch], log=log).ok


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

    if task.artifact_dir is None or status == "dry_run":
        return result

    directory = Path(task.artifact_dir) / "integration"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"cleanup-{uuid4().hex[:12]}.json"
    atomic_write_json(path, result.to_summary_dict(), sort_keys=True)
    task_store.record_task_artifact(request.task_key, ARTIFACT_TYPE, path)

    return IntegrationCleanupResult(
        **{**result.__dict__, "cleanup_json_path": path}
    )
