#!/usr/bin/env python3
"""Create a local PR handoff package for a task at waiting_approval."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.pr_handoff import (  # noqa: E402
    PrHandoffError,
    PrHandoffRequest,
    create_pr_handoff,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create local PR handoff evidence without mutating GitHub.",
    )
    parser.add_argument("--task-key", required=True, help="Task key to hand off.")
    parser.add_argument(
        "--db-path",
        help="Absolute path to the Agent Taskflow SQLite state DB.",
    )
    parser.add_argument(
        "--output-dir",
        help="Optional absolute output root. Package is written under <output-dir>/<task-key>.",
    )
    parser.add_argument(
        "--repo",
        help="Optional GitHub repo label, for example anderson930420/agent-taskflow.",
    )
    parser.add_argument(
        "--base-branch",
        help="Optional PR base branch. Defaults to TaskWorktreeRecord.base_branch or main.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the package in memory and print the summary without writing files/events.",
    )
    return parser


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        request = PrHandoffRequest(
            task_key=args.task_key,
            db_path=Path(args.db_path).expanduser() if args.db_path else None,
            output_dir=Path(args.output_dir).expanduser() if args.output_dir else None,
            repo=args.repo,
            base_branch=args.base_branch,
            dry_run=args.dry_run,
        )
        result = create_pr_handoff(request)
    except (ValueError, PrHandoffError) as exc:
        _emit(
            {
                "ok": False,
                "status": "blocked",
                "task_key": args.task_key,
                "summary": str(exc),
            }
        )
        return 1

    _emit(result.to_summary_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
