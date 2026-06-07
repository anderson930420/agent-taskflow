#!/usr/bin/env python3
"""Summarize the local worktree / tmp worktree / dirty backup inventory (P2-a).

This command runs a strictly read-only inventory of the local Git worktrees
attached to an Agent Taskflow repository. For each worktree it reports the path,
existence, branch/HEAD/detached status, whether the record is missing/prunable,
whether the path is inside /tmp, whether it matches the known cron runtime or the
known dirty/manual checkout, local changes via a read-only ``git status
--short``, common local-only markers, and a per-worktree recommendation.

It is inventory only. It performs no ``git clean``, no ``git reset``, no ``git
worktree remove``, no ``git worktree prune``, no ``rm``, no DB write, no crontab
write, no GitHub call, and starts no executor or validator. Explicit, confirmed
cleanup is a later phase (P2-b).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.local_workspace_inventory import (  # noqa: E402
    DEFAULT_MANUAL_REVIEW_WORKTREE,
    DEFAULT_PATH_PREFIXES,
    DEFAULT_REPO_ROOT,
    DEFAULT_RUNTIME_WORKTREE,
    DEFAULT_STATUS_LIMIT,
    LOCAL_WORKSPACE_INVENTORY_SCHEMA_VERSION,
    LOCAL_WORKSPACE_INVENTORY_SOURCE,
    LocalWorkspaceInventoryRequest,
    inventory_safety_flags,
    render_local_workspace_inventory_summary,
    summarize_local_workspace_inventory,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only inventory of local Git worktrees (P2-a). Summarizes "
            "runtime, manual/dirty, tmp, and prunable worktrees with cleanup "
            "recommendations. It performs no cleanup of any kind."
        )
    )
    parser.add_argument(
        "--repo-root",
        default=DEFAULT_REPO_ROOT,
        help=(
            "Repository root used to run 'git worktree list --porcelain'. "
            f"Default: {DEFAULT_REPO_ROOT}."
        ),
    )
    parser.add_argument(
        "--runtime-worktree",
        action="append",
        default=None,
        help=(
            "Path of a worktree that must be preserved (the cron runtime). "
            "Repeatable. "
            f"Default includes {DEFAULT_RUNTIME_WORKTREE}."
        ),
    )
    parser.add_argument(
        "--manual-review-worktree",
        action="append",
        default=None,
        help=(
            "Path of a dirty/manual checkout that must be reviewed by a human "
            "before cleanup. Repeatable. "
            f"Default includes {DEFAULT_MANUAL_REVIEW_WORKTREE}."
        ),
    )
    parser.add_argument(
        "--path-prefix",
        action="append",
        default=None,
        help=(
            "Restrict the inventory to worktrees under these path prefixes. "
            "Repeatable. "
            f"Default: {', '.join(DEFAULT_PATH_PREFIXES)}."
        ),
    )
    parser.add_argument(
        "--status-limit",
        type=int,
        default=DEFAULT_STATUS_LIMIT,
        help=(
            "Maximum number of changed paths to list per worktree. "
            f"Default: {DEFAULT_STATUS_LIMIT}."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output. Without this flag the output is human-readable.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    runtime_worktrees = tuple(
        Path(p) for p in (args.runtime_worktree or [DEFAULT_RUNTIME_WORKTREE])
    )
    manual_review_worktrees = tuple(
        Path(p) for p in (args.manual_review_worktree or [DEFAULT_MANUAL_REVIEW_WORKTREE])
    )
    path_prefixes = tuple(
        Path(p) for p in (args.path_prefix or list(DEFAULT_PATH_PREFIXES))
    )

    try:
        request = LocalWorkspaceInventoryRequest(
            repo_root=Path(args.repo_root).expanduser(),
            runtime_worktrees=runtime_worktrees,
            manual_review_worktrees=manual_review_worktrees,
            path_prefixes=path_prefixes,
            status_limit=int(args.status_limit),
        )
        summary = summarize_local_workspace_inventory(request)
    except (ValueError, OSError) as exc:
        payload = _error_payload(str(exc), repo_root=args.repo_root)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"Local workspace inventory error: {exc}")
        return 1

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(render_local_workspace_inventory_summary(summary), end="")

    return 0 if summary.get("ok") else 1


def _error_payload(message: str, *, repo_root: str) -> dict[str, object]:
    return {
        "ok": False,
        "schema_version": LOCAL_WORKSPACE_INVENTORY_SCHEMA_VERSION,
        "source": LOCAL_WORKSPACE_INVENTORY_SOURCE,
        "repo_root": repo_root,
        "runtime_worktrees": [],
        "manual_review_worktrees": [],
        "path_prefixes": [],
        "worktrees": [],
        "summary": None,
        "warnings": [message],
        "safety": inventory_safety_flags(),
    }


if __name__ == "__main__":
    raise SystemExit(main())
