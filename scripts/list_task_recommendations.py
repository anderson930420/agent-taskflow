#!/usr/bin/env python3
"""List read-only task workflow recommendations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.task_recommendations import (  # noqa: E402
    TaskRecommendationsError,
    TaskRecommendationsRequest,
    list_task_recommendations,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read task evidence and recommend the next safe human-driven phase. "
            "This command is read-only and never executes workflow actions."
        ),
    )
    parser.add_argument(
        "--db-path",
        required=True,
        help="Path to the Agent Taskflow SQLite state DB.",
    )
    parser.add_argument(
        "--status",
        help="Optional task status filter.",
    )
    parser.add_argument(
        "--project",
        help="Optional project filter.",
    )
    parser.add_argument(
        "--task-key",
        help="Optional task key filter.",
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    output.add_argument(
        "--pretty",
        action="store_true",
        help="Emit a readable operator list. This is the default.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        request = TaskRecommendationsRequest(
            db_path=Path(args.db_path).expanduser(),
            status=args.status,
            project=args.project,
            task_key=args.task_key,
        )
        payload = list_task_recommendations(request)
    except (ValueError, TaskRecommendationsError) as exc:
        error_payload = {
            "ok": False,
            "status": "error",
            "error": str(exc),
            "safety_flags": {
                "read_only": True,
                "will_execute": False,
                "will_push": False,
                "will_create_pr": False,
                "will_merge": False,
                "will_cleanup": False,
                "will_approve": False,
                "will_reject": False,
                "will_delete_branch": False,
                "will_delete_worktree": False,
                "will_mutate_db": False,
                "will_mutate_github": False,
            },
        }
        if args.json:
            print(json.dumps(error_payload, indent=2, sort_keys=True))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_format_pretty(payload))
    return 0


def _format_pretty(payload: dict[str, object]) -> str:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return "Task Recommendations\nNo matching tasks."

    lines = ["Task Recommendations"]
    for item in items:
        if not isinstance(item, dict):
            continue
        lines.extend(
            [
                "",
                f"{item.get('task_key')} [{item.get('status')}]",
                f"Project: {item.get('project')}",
                f"Title: {item.get('title') or '(none)'}",
                f"Phase: {item.get('current_phase_label')}",
                f"Recommendation: {item.get('recommended_next_action')}",
                f"Command kind: {item.get('recommended_command_kind')}",
                f"Reason: {item.get('reason')}",
            ]
        )
        blocked_reason = item.get("blocked_reason")
        if blocked_reason:
            lines.append(f"Blocked reason: {blocked_reason}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
