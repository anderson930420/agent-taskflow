"""Deterministic git worktree preparation for task execution."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent_taskflow.governance import (
    assert_not_main_repo_write,
    assert_worktree_inside_repo_worktrees,
)
from agent_taskflow.models import TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.worktree import ensure_absolute_path, worktree_path_from_base


WORKSPACE_PREPARED = "prepared"
WORKSPACE_REUSED = "reused"
WORKSPACE_BLOCKED = "blocked"


@dataclass(frozen=True)
class WorkspacePreparationRequest:
    """Input for deterministic task workspace preparation."""

    task_key: str
    repo_path: str | Path
    base_branch: str = "main"
    branch: str | None = None
    worktree_root: str | Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", normalize_task_key(self.task_key))
        object.__setattr__(
            self,
            "repo_path",
            ensure_absolute_path(self.repo_path, name="repo_path"),
        )
        base_branch = self.base_branch.strip()
        if not base_branch:
            raise ValueError("base_branch must not be empty")
        object.__setattr__(self, "base_branch", base_branch)
        if self.branch is not None:
            branch = self.branch.strip()
            if not branch:
                raise ValueError("branch must not be empty")
            object.__setattr__(self, "branch", branch)
        if self.worktree_root is not None:
            object.__setattr__(
                self,
                "worktree_root",
                ensure_absolute_path(self.worktree_root, name="worktree_root"),
            )


@dataclass(frozen=True)
class WorkspacePreparationResult:
    """Structured result from workspace preparation."""

    task_key: str
    repo_path: Path
    worktree_path: Path
    branch: str
    base_branch: str
    base_sha: str | None
    status: str
    summary: str

    @property
    def ok(self) -> bool:
        return self.status in {WORKSPACE_PREPARED, WORKSPACE_REUSED}


@dataclass(frozen=True)
class _GitResult:
    args: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")

    @property
    def combined_text(self) -> str:
        return (self.stderr_text or self.stdout_text).strip()


class WorkspaceManager:
    """Prepare isolated local git worktrees without remote or cleanup actions."""

    def prepare(
        self,
        request: WorkspacePreparationRequest,
    ) -> WorkspacePreparationResult:
        repo_path = Path(request.repo_path)
        branch = request.branch or f"task/{request.task_key}"
        worktree_root = Path(request.worktree_root or repo_path / ".worktrees")
        worktree_path = worktree_path_from_base(worktree_root, request.task_key)

        try:
            assert_not_main_repo_write(worktree_path, repo_path)
            assert_worktree_inside_repo_worktrees(worktree_path, repo_path)
        except ValueError as exc:
            return self._blocked(request, worktree_path, branch, None, str(exc))

        repo_check = self._git(["rev-parse", "--show-toplevel"], repo_path)
        if repo_check.returncode != 0:
            return self._blocked(
                request,
                worktree_path,
                branch,
                None,
                f"repo_path is not a git repository: {repo_path}: {repo_check.combined_text}",
            )

        git_root = Path(repo_check.stdout_text.strip()).resolve()
        if git_root != repo_path.resolve():
            return self._blocked(
                request,
                worktree_path,
                branch,
                None,
                f"repo_path must be the git repository root: {repo_path}",
            )

        base = self._git(["rev-parse", request.base_branch], repo_path)
        if base.returncode != 0:
            return self._blocked(
                request,
                worktree_path,
                branch,
                None,
                f"base ref could not be resolved: {request.base_branch}: {base.combined_text}",
            )
        base_sha = base.stdout_text.strip()

        worktrees = self._worktrees(repo_path)
        if worktrees is None:
            return self._blocked(
                request,
                worktree_path,
                branch,
                base_sha,
                "could not list git worktrees",
            )

        target_entry = self._find_worktree(worktrees, worktree_path)
        if worktree_path.exists():
            if target_entry is None:
                return self._blocked(
                    request,
                    worktree_path,
                    branch,
                    base_sha,
                    f"target worktree path already exists but is not registered: {worktree_path}",
                )
            entry_branch = target_entry.get("branch")
            if entry_branch != f"refs/heads/{branch}":
                return self._blocked(
                    request,
                    worktree_path,
                    branch,
                    base_sha,
                    f"registered worktree branch mismatch: expected {branch}, found {entry_branch}",
                )
            dirty = self._git(["status", "--porcelain=v1", "-z"], worktree_path)
            if dirty.returncode != 0:
                return self._blocked(
                    request,
                    worktree_path,
                    branch,
                    base_sha,
                    f"could not inspect existing worktree status: {dirty.combined_text}",
                )
            if dirty.stdout:
                return self._blocked(
                    request,
                    worktree_path,
                    branch,
                    base_sha,
                    f"existing worktree is dirty and cannot be reused: {worktree_path}",
                )
            return WorkspacePreparationResult(
                task_key=request.task_key,
                repo_path=repo_path,
                worktree_path=worktree_path,
                branch=branch,
                base_branch=request.base_branch,
                base_sha=base_sha,
                status=WORKSPACE_REUSED,
                summary=f"Reused clean registered worktree: {worktree_path}",
            )

        if self._branch_exists(repo_path, branch):
            return self._blocked(
                request,
                worktree_path,
                branch,
                base_sha,
                f"branch already exists without matching registered worktree: {branch}",
            )

        worktree_root.mkdir(parents=True, exist_ok=True)
        created = self._git(
            ["worktree", "add", str(worktree_path), "-b", branch, request.base_branch],
            repo_path,
        )
        if created.returncode != 0:
            return self._blocked(
                request,
                worktree_path,
                branch,
                base_sha,
                f"git worktree add failed: {created.combined_text}",
            )

        return WorkspacePreparationResult(
            task_key=request.task_key,
            repo_path=repo_path,
            worktree_path=worktree_path,
            branch=branch,
            base_branch=request.base_branch,
            base_sha=base_sha,
            status=WORKSPACE_PREPARED,
            summary=f"Prepared worktree {worktree_path} on branch {branch} from {request.base_branch}@{base_sha}",
        )

    def _blocked(
        self,
        request: WorkspacePreparationRequest,
        worktree_path: Path,
        branch: str,
        base_sha: str | None,
        summary: str,
    ) -> WorkspacePreparationResult:
        return WorkspacePreparationResult(
            task_key=request.task_key,
            repo_path=Path(request.repo_path),
            worktree_path=worktree_path,
            branch=branch,
            base_branch=request.base_branch,
            base_sha=base_sha,
            status=WORKSPACE_BLOCKED,
            summary=summary,
        )

    @staticmethod
    def _git(args: list[str], cwd: Path) -> _GitResult:
        full_args = ("git", *args)
        try:
            completed = subprocess.run(
                full_args,
                cwd=cwd,
                shell=False,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            return _GitResult(full_args, 127, b"", str(exc).encode("utf-8"))
        return _GitResult(
            full_args,
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )

    def _branch_exists(self, repo_path: Path, branch: str) -> bool:
        result = self._git(["branch", "--list", branch], repo_path)
        return result.returncode == 0 and bool(result.stdout_text.strip())

    def _worktrees(self, repo_path: Path) -> list[dict[str, str]] | None:
        result = self._git(["worktree", "list", "--porcelain"], repo_path)
        if result.returncode != 0:
            return None

        entries: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for raw_line in result.stdout_text.splitlines():
            if not raw_line:
                if current:
                    entries.append(current)
                    current = {}
                continue
            key, _, value = raw_line.partition(" ")
            current[key] = value
        if current:
            entries.append(current)
        return entries

    @staticmethod
    def _find_worktree(
        worktrees: list[dict[str, str]],
        worktree_path: Path,
    ) -> dict[str, str] | None:
        target = worktree_path.resolve()
        for entry in worktrees:
            path = entry.get("worktree")
            if path and Path(path).resolve() == target:
                return entry
        return None


def record_prepared_workspace(
    store: TaskMirrorStore,
    result: WorkspacePreparationResult,
) -> None:
    """Record a prepared/reused workspace in the local task mirror."""

    if not result.ok:
        raise ValueError("Only prepared or reused workspaces can be recorded")
    store.upsert_task_worktree(
        TaskWorktreeRecord(
            task_key=result.task_key,
            repo_path=result.repo_path,
            worktree_path=result.worktree_path,
            branch=result.branch,
            base_branch=result.base_branch,
            base_sha=result.base_sha,
            status="active",
        )
    )


def prepare_task_workspace(
    request: WorkspacePreparationRequest,
    *,
    store: TaskMirrorStore | None = None,
) -> WorkspacePreparationResult:
    """Prepare a task workspace and optionally record it in TaskMirrorStore."""

    result = WorkspaceManager().prepare(request)
    if store is not None and result.ok:
        record_prepared_workspace(store, result)
    return result
