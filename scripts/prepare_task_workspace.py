#!/usr/bin/env python3
"""Prepare a task workspace through the deterministic workspace manager."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.api.schemas import workspace_preparation_result_to_dict
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.worktree import ensure_absolute_path
from agent_taskflow.workspace_manager import (
    WorkspacePreparationRequest,
    prepare_task_workspace,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare an isolated git worktree for an existing task.",
    )
    parser.add_argument("--task-key", required=True, help="Task key to prepare.")
    parser.add_argument(
        "--db-path",
        help="Absolute path to the Agent Taskflow SQLite state DB.",
    )
    parser.add_argument(
        "--base-branch",
        default="main",
        help="Base branch or ref to prepare from. Default: main.",
    )
    parser.add_argument("--branch", help="Optional task branch name.")
    parser.add_argument(
        "--worktree-root",
        help="Optional absolute worktree root. Default: <repo>/.worktrees.",
    )
    return parser


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        task_key = normalize_task_key(args.task_key)
        db_path = ensure_absolute_path(args.db_path, name="db_path") if args.db_path else None
        worktree_root = (
            ensure_absolute_path(args.worktree_root, name="worktree_root")
            if args.worktree_root
            else None
        )
    except ValueError as exc:
        _emit({"ok": False, "status": "blocked", "summary": str(exc)})
        return 2

    store = TaskMirrorStore(db_path)
    store.init_db()
    task = store.get_task(task_key)
    if task is None:
        _emit(
            {
                "ok": False,
                "task_key": task_key,
                "status": "blocked",
                "summary": f"Task not found: {task_key}",
            }
        )
        return 1

    try:
        request = WorkspacePreparationRequest(
            task_key=task.task_key,
            repo_path=task.repo_path,
            base_branch=args.base_branch,
            branch=args.branch,
            worktree_root=worktree_root,
        )
    except ValueError as exc:
        _emit(
            {
                "ok": False,
                "task_key": task.task_key,
                "status": "blocked",
                "summary": str(exc),
            }
        )
        return 2

    result = prepare_task_workspace(request, store=store)
    payload = workspace_preparation_result_to_dict(result)
    payload["ok"] = result.ok
    _emit(payload)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
