#!/usr/bin/env python3
"""Create or preview a GitHub draft PR from local handoff evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.draft_pr import (  # noqa: E402
    DraftPrCreationRequest,
    DraftPrError,
    create_draft_pr,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or create a draft PR from existing PR handoff evidence.",
    )
    parser.add_argument("--task-key", required=True, help="Task key to create a draft PR for.")
    parser.add_argument(
        "--db-path",
        help="Absolute path to the Agent Taskflow SQLite state DB.",
    )
    parser.add_argument(
        "--repo",
        help="GitHub repo, for example anderson930420/agent-taskflow. "
        "Defaults to pr_handoff.json repo when present.",
    )
    parser.add_argument(
        "--handoff-json",
        help="Absolute path to pr_handoff.json. Defaults to recorded pr_handoff artifact.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Never call gh; print the command preview. This is also the default without confirmation.",
    )
    parser.add_argument(
        "--confirm-create-pr",
        action="store_true",
        help="Required before the script may call gh pr create --draft.",
    )
    parser.add_argument("--base-branch", help="Override handoff proposed PR base branch.")
    parser.add_argument("--head-branch", help="Override handoff proposed PR head branch.")
    parser.add_argument("--title", help="Override handoff proposed PR title.")
    parser.add_argument("--body", help="Override handoff proposed PR body.")
    parser.add_argument(
        "--draft-only",
        action="store_true",
        default=True,
        help="Only draft PR creation is supported in this phase.",
    )
    return parser


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        request = DraftPrCreationRequest(
            task_key=args.task_key,
            db_path=Path(args.db_path).expanduser() if args.db_path else None,
            repo=args.repo,
            handoff_json=Path(args.handoff_json).expanduser()
            if args.handoff_json
            else None,
            dry_run=args.dry_run or not args.confirm_create_pr,
            confirm_create_pr=args.confirm_create_pr,
            base_branch=args.base_branch,
            head_branch=args.head_branch,
            title=args.title,
            body=args.body,
            draft_only=args.draft_only,
        )
        result = create_draft_pr(request)
    except (ValueError, DraftPrError) as exc:
        _emit(
            {
                "ok": False,
                "status": "blocked",
                "task_key": args.task_key,
                "github_mutated": False,
                "pr_created": False,
                "pushed": False,
                "merged": False,
                "cleanup_performed": False,
                "summary": str(exc),
            }
        )
        return 1

    _emit(result.to_summary_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
