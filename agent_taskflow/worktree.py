"""Worktree path helpers for Agent Taskflow."""

from __future__ import annotations

from pathlib import Path

from agent_taskflow.tasks import normalize_task_key


def ensure_absolute_path(path: str | Path, *, name: str = "path") -> Path:
    """Return an absolute Path or raise.

    Agent Taskflow config paths should be explicit and absolute to avoid
    accidentally operating on the wrong repository.
    """
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        raise ValueError(f"{name} must be absolute: {path}")
    return resolved


def worktree_path_for(repo_path: str | Path, task_key: str) -> Path:
    """Return the canonical worktree path: <repo>/.worktrees/<TASK_KEY>."""
    repo = ensure_absolute_path(repo_path, name="repo_path")
    key = normalize_task_key(task_key)
    return repo / ".worktrees" / key


def worktree_path_from_base(worktrees_dir: str | Path, task_key: str) -> Path:
    """Return worktree path using configured worktrees_dir."""
    base = ensure_absolute_path(worktrees_dir, name="worktrees_dir")
    key = normalize_task_key(task_key)
    return base / key
