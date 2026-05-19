#!/usr/bin/env python3
"""Preview or confirm an explicit task branch push."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.branch_push_confirm import (  # noqa: E402
    BranchPushConfirmError,
    BranchPushConfirmRequest,
    confirm_branch_push,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or confirm an explicit task branch push.",
    )
    parser.add_argument("--task-key", required=True, help="Task key whose branch to push.")
    parser.add_argument(
        "--repo-path",
        required=True,
        help="Absolute path to the repository root for the task worktree.",
    )
    parser.add_argument(
        "--db-path",
        help="Absolute path to the Agent Taskflow SQLite state DB.",
    )
    parser.add_argument(
        "--artifact-root",
        help="Optional artifact root used for local branch_push evidence files.",
    )
    parser.add_argument(
        "--remote",
        default="origin",
        help="Git remote to push to. Default: origin.",
    )
    parser.add_argument(
        "--branch",
        help="Optional branch override. Must match the task worktree branch.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Never perform the actual push; only validate and preview the command.",
    )
    parser.add_argument(
        "--confirm-branch-push",
        action="store_true",
        help="Required before the script may perform the actual git push.",
    )
    parser.add_argument(
        "--allow-non-waiting",
        action="store_true",
        help="Allow inspection/dry-run on tasks that are not in waiting_approval.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output. This is the default output format.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    return parser


def _resolve_path(value: str | None) -> Path | None:
    if value is None:
        return None
    return Path(value).expanduser().resolve()


def _emit_json(payload: dict[str, object], *, compact: bool) -> None:
    if compact:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: list[str] | None = None, *, runner=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        request = BranchPushConfirmRequest(
            task_key=args.task_key,
            repo_path=_resolve_path(args.repo_path) or Path(args.repo_path),
            db_path=_resolve_path(args.db_path),
            artifact_root=_resolve_path(args.artifact_root),
            remote=args.remote,
            branch=args.branch,
            dry_run=args.dry_run,
            confirm_branch_push=args.confirm_branch_push,
            allow_non_waiting=args.allow_non_waiting,
        )
        result = confirm_branch_push(request, runner=runner)
    except (ValueError, BranchPushConfirmError) as exc:
        _emit_json(
            {
                "ok": False,
                "status": "blocked",
                "task_key": args.task_key,
                "branch_pushed": False,
                "pr_created": False,
                "merged": False,
                "approved": False,
                "cleanup_performed": False,
                "summary": str(exc),
                "safety": {
                    "human_confirmation_required": True,
                    "human_confirmation_confirmed": False,
                    "task_status_changed": False,
                    "workspace_prepared": False,
                    "executor_started": False,
                    "validators_started": False,
                    "branch_pushed": False,
                    "pr_created": False,
                    "merged": False,
                    "approved": False,
                    "cleanup_performed": False,
                    "branch_deleted": False,
                    "worktree_deleted": False,
                    "force_push": False,
                    "background_worker_started": False,
                },
            },
            compact=args.json and not args.pretty,
        )
        return 1

    _emit_json(result.to_dict(), compact=args.json and not args.pretty)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
