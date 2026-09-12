"""Merge detection identity and verification (§35, §36, §36.1).

The verification identity is the **GitHub PR merge result**, never the original
task branch HEAD. Merge commits, squash merges and rebase merges all produce
SHAs with different semantics, and for squash and rebase the original task
commit does not exist in the target branch at all (§36.1). Verifying against
the task SHA would therefore fail for two of the three supported merge methods
and — worse — could pass by coincidence for the wrong reason.

All three conditions of §36 must hold before a merge is verified:

    PR merged == true
    AND merge_commit_sha != null
    AND merge_commit_sha exists in latest target branch history
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_taskflow import integration_git as git_ops
from agent_taskflow.integration_git import GitCommandLog, IntegrationGitError
from agent_taskflow.models import utc_now_iso
from agent_taskflow.tasks import normalize_task_key


__all__ = [
    "MergeVerificationRequest",
    "MergeVerificationResult",
    "verify_merge",
]


@dataclass(frozen=True)
class MergeVerificationRequest:
    """Inputs for one §36 merge verification."""

    task_key: str
    worktree_path: Path
    remote: str = "origin"
    target_branch: str = "main"
    pr_merged: bool = False
    merge_commit_sha: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        object.__setattr__(self, "worktree_path", Path(self.worktree_path))


@dataclass(frozen=True)
class MergeVerificationResult:
    """The verdict, with every reason it failed spelled out."""

    task_key: str
    verified: bool
    pr_merged: bool
    merge_commit_sha: str | None
    target_sha: str | None
    contained_in_target: bool
    reasons: tuple[str, ...] = ()
    verified_at: str | None = None
    git_commands: tuple[tuple[str, ...], ...] = ()

    # Recorded explicitly so the evidence shows *which* identity was used.
    original_task_sha_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "merge_verification",
            "task_key": self.task_key,
            "verified": self.verified,
            "pr_merged": self.pr_merged,
            "merge_commit_sha": self.merge_commit_sha,
            "target_sha": self.target_sha,
            "contained_in_target": self.contained_in_target,
            "original_task_sha_used": self.original_task_sha_used,
            "reasons": list(self.reasons),
            "verified_at": self.verified_at,
        }


def verify_merge(
    request: MergeVerificationRequest,
    *,
    log: GitCommandLog | None = None,
) -> MergeVerificationResult:
    """Verify a merge per §36, fetching the latest target first."""
    command_log = log or GitCommandLog()
    reasons: list[str] = []

    if not request.pr_merged:
        reasons.append("pr_merged is not true")
    merge_sha = (request.merge_commit_sha or "").strip()
    if not merge_sha:
        reasons.append("merge_commit_sha is null")

    if reasons:
        return MergeVerificationResult(
            task_key=request.task_key,
            verified=False,
            pr_merged=request.pr_merged,
            merge_commit_sha=merge_sha or None,
            target_sha=None,
            contained_in_target=False,
            reasons=tuple(reasons),
            git_commands=command_log.as_tuple(),
        )

    target_ref = f"{request.remote}/{request.target_branch}"
    try:
        git_ops.fetch(request.worktree_path, remote=request.remote, log=command_log)
        target_sha = git_ops.resolve_target_sha(
            request.worktree_path, request.remote, request.target_branch, log=command_log
        )
    except IntegrationGitError as exc:
        return MergeVerificationResult(
            task_key=request.task_key,
            verified=False,
            pr_merged=request.pr_merged,
            merge_commit_sha=merge_sha,
            target_sha=None,
            contained_in_target=False,
            reasons=(f"could not resolve the latest target: {exc}",),
            git_commands=command_log.as_tuple(),
        )

    contained = git_ops.commit_in_history(
        request.worktree_path, merge_sha, target_ref, log=command_log
    )
    if not contained:
        reasons.append(
            f"merge_commit_sha {merge_sha} is not contained in {target_ref} history"
        )

    verified = contained
    return MergeVerificationResult(
        task_key=request.task_key,
        verified=verified,
        pr_merged=request.pr_merged,
        merge_commit_sha=merge_sha,
        target_sha=target_sha,
        contained_in_target=contained,
        reasons=tuple(reasons),
        verified_at=utc_now_iso() if verified else None,
        git_commands=command_log.as_tuple(),
    )
