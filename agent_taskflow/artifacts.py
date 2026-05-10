"""Artifact path helpers for Agent Taskflow."""

from __future__ import annotations

from pathlib import Path

from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.worktree import ensure_absolute_path


def artifact_dir_for(task_key: str, artifacts_root: str | Path) -> Path:
    """Return the canonical artifact directory: <artifacts_root>/<TASK_KEY>."""
    root = ensure_absolute_path(artifacts_root, name="artifacts_root")
    key = normalize_task_key(task_key)
    return root / key
