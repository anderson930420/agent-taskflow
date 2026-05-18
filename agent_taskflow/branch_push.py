"""Explicit task branch push foundation.

This module exposes a narrow, operator-triggered path for publishing an
existing committed task branch from its prepared worktree. It never creates
commits, merges, creates PRs, approves PRs, cleans up, deletes branches or
worktrees, or runs automatically from other workflow phases.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shlex
import subprocess
from typing import Any, Callable, Protocol

from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.worktree import ensure_absolute_path


EVENT_TYPE = "branch_pushed"
ARTIFACT_TYPE = "branch_push"
SOURCE = "branch_push"
PROTECTED_BRANCHES = {"main", "master", "trunk"}


class BranchPushError(RuntimeError):
    """Raised when a task branch cannot be safely pushed."""


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., CompletedProcessLike]


@dataclass(frozen=True)
class BranchPushRequest:
    """Request for previewing or pushing a prepared task branch."""

    task_key: str
    db_path: Path | None = None
    remote: str = "origin"
    dry_run: bool = True
    confirm_push: bool = False
    set_upstream: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        object.__setattr__(self, "remote", _validate_remote(self.remote))
        if self.db_path is not None:
            object.__setattr__(
                self,
                "db_path",
                ensure_absolute_path(self.db_path, name="db_path"),
            )


@dataclass(frozen=True)
class BranchPushCommandPreview:
    """Inert command preview for operator inspection."""

    argv: tuple[str, ...]

    @property
    def text(self) -> str:
        return " ".join(shlex.quote(part) for part in self.argv)


@dataclass(frozen=True)
class BranchPushResult:
    """Result of a branch push dry-run or confirmed push."""

    ok: bool
    status: str
    task_key: str
    remote: str
    branch: str
    worktree_path: Path
    base_branch: str | None
    base_sha: str | None
    ahead_count: int | None
    command_preview: str
    pushed: bool
    github_mutated: bool
    force_pushed: bool
    merged: bool
    cleanup_performed: bool
    pr_created: bool
    event_recorded: bool
    artifact_recorded: bool
    dry_run: bool
    confirmation_required: bool
    summary: str
    branch_push_json_path: Path | None = None

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "task_key": self.task_key,
            "remote": self.remote,
            "branch": self.branch,
            "worktree_path": str(self.worktree_path),
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "ahead_count": self.ahead_count,
            "command_preview": self.command_preview,
            "pushed": self.pushed,
            "github_mutated": self.github_mutated,
            "force_pushed": self.force_pushed,
            "merged": self.merged,
            "cleanup_performed": self.cleanup_performed,
            "pr_created": self.pr_created,
            "event_recorded": self.event_recorded,
            "artifact_recorded": self.artifact_recorded,
            "evidence_recorded": self.event_recorded and self.artifact_recorded,
            "dry_run": self.dry_run,
            "confirmation_required": self.confirmation_required,
            "branch_push_json_path": str(self.branch_push_json_path)
            if self.branch_push_json_path
            else None,
            "summary": self.summary,
        }


def push_task_branch(
    request: BranchPushRequest,
    *,
    store: TaskMirrorStore | None = None,
    runner: Runner | None = None,
) -> BranchPushResult:
    """Preview or push the prepared task branch recorded in the local store."""

    current_store = store or TaskMirrorStore(request.db_path)
    current_store.init_db()
    run = runner or subprocess.run

    context = _load_context(current_store, request, run)
    push_command = _build_git_push_command(
        request.remote,
        context["branch"],
        set_upstream=request.set_upstream,
    )
    _ensure_no_unsafe_push_flags(push_command)
    preview = BranchPushCommandPreview(tuple(push_command)).text
    should_push = request.confirm_push and not request.dry_run

    if not should_push:
        return BranchPushResult(
            ok=True,
            status="dry_run",
            task_key=request.task_key,
            remote=request.remote,
            branch=context["branch"],
            worktree_path=context["worktree_path"],
            base_branch=context["base_branch"],
            base_sha=context["base_sha"],
            ahead_count=context["ahead_count"],
            command_preview=preview,
            pushed=False,
            github_mutated=False,
            force_pushed=False,
            merged=False,
            cleanup_performed=False,
            pr_created=False,
            event_recorded=False,
            artifact_recorded=False,
            dry_run=True,
            confirmation_required=not request.confirm_push,
            summary="Dry run only; task branch was not pushed",
            branch_push_json_path=context["branch_push_json_path"],
        )

    completed = run(
        push_command,
        cwd=context["worktree_path"],
        shell=False,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise BranchPushError(
            f"git push failed with {completed.returncode}: {completed.stderr.strip()}"
        )

    artifact_path = context["branch_push_json_path"]
    evidence = _branch_push_evidence(
        task_key=request.task_key,
        remote=request.remote,
        branch=context["branch"],
        worktree_path=context["worktree_path"],
        base_branch=context["base_branch"],
        base_sha=context["base_sha"],
        ahead_count=context["ahead_count"],
        command_preview=preview,
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    current_store.record_task_artifact(request.task_key, ARTIFACT_TYPE, artifact_path)
    current_store.record_task_event(
        request.task_key,
        EVENT_TYPE,
        SOURCE,
        message="Task branch pushed",
        payload=evidence,
    )

    return BranchPushResult(
        ok=True,
        status="pushed",
        task_key=request.task_key,
        remote=request.remote,
        branch=context["branch"],
        worktree_path=context["worktree_path"],
        base_branch=context["base_branch"],
        base_sha=context["base_sha"],
        ahead_count=context["ahead_count"],
        command_preview=preview,
        pushed=True,
        github_mutated=True,
        force_pushed=False,
        merged=False,
        cleanup_performed=False,
        pr_created=False,
        event_recorded=True,
        artifact_recorded=True,
        dry_run=False,
        confirmation_required=False,
        summary="Task branch pushed",
        branch_push_json_path=artifact_path,
    )


def _load_context(
    store: TaskMirrorStore,
    request: BranchPushRequest,
    runner: Runner,
) -> dict[str, Any]:
    task = store.get_task(request.task_key)
    if task is None:
        raise BranchPushError(f"Task not found: {request.task_key}")

    worktree = store.get_task_worktree(task.task_key)
    if worktree is None:
        raise BranchPushError(f"TaskWorktreeRecord missing for task: {task.task_key}")
    if not worktree.worktree_path.is_dir():
        raise BranchPushError(f"Worktree path is missing: {worktree.worktree_path}")

    branch = _validate_branch_name(worktree.branch, base_branch=worktree.base_branch)
    current_branch = _git(
        worktree.worktree_path,
        ["rev-parse", "--abbrev-ref", "HEAD"],
        runner,
    ).strip()
    if current_branch != branch:
        raise BranchPushError(
            f"current branch {current_branch!r} does not match task branch {branch!r}"
        )

    status_short = _git(worktree.worktree_path, ["status", "--short"], runner)
    if status_short.strip():
        raise BranchPushError(
            "worktree has uncommitted changes; commit or handle changes before pushing"
        )

    ahead_count: int | None = None
    if worktree.base_sha:
        raw_count = _git(
            worktree.worktree_path,
            ["rev-list", "--count", f"{worktree.base_sha}..HEAD"],
            runner,
        ).strip()
        try:
            ahead_count = int(raw_count)
        except ValueError as exc:
            raise BranchPushError(f"git rev-list returned invalid count: {raw_count}") from exc
        if ahead_count <= 0:
            raise BranchPushError("task branch has no commits beyond base_sha")

    if task.artifact_dir is None:
        raise BranchPushError("Task artifact_dir is required for branch push evidence")

    return {
        "task": task,
        "worktree": worktree,
        "branch": branch,
        "worktree_path": worktree.worktree_path,
        "base_branch": worktree.base_branch,
        "base_sha": worktree.base_sha,
        "ahead_count": ahead_count,
        "branch_push_json_path": task.artifact_dir / "branch_push.json",
    }


def _git(worktree_path: Path, args: list[str], runner: Runner) -> str:
    allowed_prefixes = {
        ("rev-parse", "--abbrev-ref", "HEAD"),
        ("status", "--short"),
    }
    if tuple(args) not in allowed_prefixes and not (
        len(args) == 3 and args[:2] == ["rev-list", "--count"] and args[2].endswith("..HEAD")
    ):
        raise BranchPushError(f"Git command is not allowed: git {' '.join(args)}")

    completed = runner(
        ["git", *args],
        cwd=worktree_path,
        shell=False,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise BranchPushError(
            f"git {' '.join(args)} failed with "
            f"{completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.stdout


def _validate_remote(remote: str) -> str:
    normalized = remote.strip()
    if not normalized:
        raise ValueError("remote must not be empty")
    if normalized.startswith("-") or any(ch.isspace() for ch in normalized):
        raise ValueError("remote must be a simple git remote name")
    return normalized


def _validate_branch_name(branch: str, *, base_branch: str | None) -> str:
    normalized = branch.strip()
    if not normalized:
        raise BranchPushError("TaskWorktreeRecord branch is required")
    if normalized.startswith("-") or any(ch.isspace() for ch in normalized):
        raise BranchPushError("task branch must be a simple branch name")
    protected = set(PROTECTED_BRANCHES)
    if base_branch:
        protected.add(base_branch.strip())
    if normalized in protected:
        raise BranchPushError(f"Refusing to push protected branch: {normalized}")
    return normalized


def _build_git_push_command(
    remote: str,
    branch: str,
    *,
    set_upstream: bool = True,
) -> list[str]:
    command = ["git", "push"]
    if set_upstream:
        command.append("--set-upstream")
    command.extend([remote, branch])
    _ensure_no_unsafe_push_flags(command)
    return command


def _ensure_no_unsafe_push_flags(command: list[str]) -> None:
    forbidden = {"--force", "-f", "--force-with-lease"}
    if any(part in forbidden for part in command):
        raise BranchPushError("force push is not allowed")
    if len(command) >= 4 and command[0:2] == ["git", "push"] and command[-1] in PROTECTED_BRANCHES:
        raise BranchPushError(f"Refusing to push protected branch: {command[-1]}")


def _branch_push_evidence(
    *,
    task_key: str,
    remote: str,
    branch: str,
    worktree_path: Path,
    base_branch: str | None,
    base_sha: str | None,
    ahead_count: int | None,
    command_preview: str,
) -> dict[str, Any]:
    return {
        "kind": EVENT_TYPE,
        "artifact_type": ARTIFACT_TYPE,
        "task_key": task_key,
        "remote": remote,
        "branch": branch,
        "worktree_path": str(worktree_path),
        "base_branch": base_branch,
        "base_sha": base_sha,
        "ahead_count": ahead_count,
        "command_preview": command_preview,
        "safety": {
            "pushed": True,
            "force_pushed": False,
            "merged": False,
            "cleanup_performed": False,
            "pr_created": False,
            "human_review_required": True,
        },
        "generated_at": utc_now_iso(),
    }
