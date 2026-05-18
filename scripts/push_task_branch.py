#!/usr/bin/env python3
"""Preview or push a prepared task branch."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.branch_push import (  # noqa: E402
    BranchPushError,
    BranchPushRequest,
    push_task_branch,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or push the prepared branch for one task.",
    )
    parser.add_argument("--task-key", required=True, help="Task key whose branch to push.")
    parser.add_argument(
        "--db-path",
        help="Absolute path to the Agent Taskflow SQLite state DB.",
    )
    parser.add_argument(
        "--remote",
        default="origin",
        help="Git remote to push to. Default: origin.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Never push; print the command preview. This is also the default without confirmation.",
    )
    parser.add_argument(
        "--confirm-push",
        action="store_true",
        help="Required before the script may push the task branch.",
    )
    parser.add_argument(
        "--set-upstream",
        action="store_true",
        default=True,
        help="Use git push --set-upstream. Enabled by default.",
    )
    return parser


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        request = BranchPushRequest(
            task_key=args.task_key,
            db_path=Path(args.db_path).expanduser() if args.db_path else None,
            remote=args.remote,
            dry_run=args.dry_run or not args.confirm_push,
            confirm_push=args.confirm_push,
            set_upstream=args.set_upstream,
        )
        result = push_task_branch(request)
    except (ValueError, BranchPushError) as exc:
        _emit(
            {
                "ok": False,
                "status": "blocked",
                "task_key": args.task_key,
                "pushed": False,
                "github_mutated": False,
                "force_pushed": False,
                "merged": False,
                "cleanup_performed": False,
                "pr_created": False,
                "summary": str(exc),
            }
        )
        return 1

    _emit(result.to_summary_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
