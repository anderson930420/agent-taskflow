"""Governance checks for Agent Taskflow.

These helpers are intentionally conservative. They should prevent unsafe path
usage without weakening existing script-level safety checks.
"""

from __future__ import annotations

from pathlib import Path


def _resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def assert_not_main_repo_write(worktree_path: str | Path, repo_path: str | Path) -> None:
    """Reject using the main repo itself as a task worktree."""
    wt = _resolve(worktree_path)
    repo = _resolve(repo_path)
    if wt == repo:
        raise ValueError(f"Worktree path must not be the main repo path: {wt}")


def assert_worktree_inside_repo_worktrees(
    worktree_path: str | Path,
    repo_path: str | Path,
) -> None:
    """Require task worktrees to live under <repo>/.worktrees/."""
    wt = _resolve(worktree_path)
    repo = _resolve(repo_path)
    expected_base = repo / ".worktrees"

    try:
        wt.relative_to(expected_base)
    except ValueError as exc:
        raise ValueError(
            f"Worktree path must be inside {expected_base}: {wt}"
        ) from exc


def assert_task_has_artifact_dir(artifact_dir: str | Path) -> None:
    """Require an artifact directory to exist."""
    path = _resolve(artifact_dir)
    if not path.is_dir():
        raise ValueError(f"Artifact directory does not exist: {path}")
