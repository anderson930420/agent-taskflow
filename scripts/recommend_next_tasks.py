#!/usr/bin/env python3
"""Recommend the next queued tasks from the local task mirror."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.task_recommendation import (  # noqa: E402
    TaskRecommendationError,
    TaskRecommendationRequest,
    recommend_next_tasks,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read queued tasks from the local task mirror and rank them deterministically.",
    )
    parser.add_argument(
        "--db-path",
        help="Path to the Agent Taskflow SQLite state DB. Defaults to ~/.agent-taskflow/state.db.",
    )
    parser.add_argument(
        "--project",
        help="Optional project filter. Only queued tasks in this project are considered.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of ranked tasks to include. Default: 10.",
    )
    parser.add_argument(
        "--include-label",
        action="append",
        default=[],
        help="Require this label before recommending a queued task. May be repeated.",
    )
    parser.add_argument(
        "--exclude-label",
        action="append",
        default=[],
        help="Exclude tasks with this label. May be repeated.",
    )
    parser.add_argument(
        "--max-risk",
        choices=("low", "medium", "high"),
        default="high",
        help="Maximum risk level to include in ranked_tasks. Default: high.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON. JSON output is the only output format and is enabled by default.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output. This is the default for operator readability.",
    )
    return parser


def _emit(payload: dict[str, object], *, compact: bool = False) -> None:
    if compact:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        request = TaskRecommendationRequest(
            db_path=Path(args.db_path).expanduser() if args.db_path else None,
            project=args.project,
            limit=args.limit,
            include_labels=tuple(args.include_label),
            exclude_labels=tuple(args.exclude_label),
            max_risk=args.max_risk,
        )
        payload = recommend_next_tasks(request)
    except (ValueError, TaskRecommendationError) as exc:
        _emit(
            {
                "ok": False,
                "status": "blocked",
                "summary": str(exc),
                "safety": {
                    "read_only": True,
                    "task_status_changed": False,
                    "db_written": False,
                    "artifact_written": False,
                    "workspace_prepared": False,
                    "executor_started": False,
                    "validators_started": False,
                    "branch_pushed": False,
                    "pr_created": False,
                    "merged": False,
                    "approved": False,
                    "cleanup_performed": False,
                },
            }
        )
        return 1

    _emit(payload, compact=args.json and not args.pretty)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
