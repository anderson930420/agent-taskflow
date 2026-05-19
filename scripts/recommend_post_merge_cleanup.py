#!/usr/bin/env python3
"""Recommend post-merge cleanup actions for a task."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.post_merge_cleanup_recommendation import (  # noqa: E402
    PostMergeCleanupRecommendationError,
    PostMergeCleanupRecommendationRequest,
    recommend_post_merge_cleanup,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recommend post-merge cleanup actions without performing any cleanup.",
    )
    parser.add_argument("--task-key", required=True, help="Task key to inspect.")
    parser.add_argument(
        "--db-path",
        help="Absolute path to the Agent Taskflow SQLite state DB.",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="GitHub repo, for example anderson930420/agent-taskflow.",
    )
    parser.add_argument(
        "--repo-path",
        required=True,
        help="Absolute path to the repository root for the task worktree.",
    )
    parser.add_argument(
        "--artifact-root",
        help="Optional artifact root used for local recommendation inspection.",
    )
    parser.add_argument(
        "--pr-number",
        type=int,
        help="Optional PR number override that must match draft PR evidence.",
    )
    parser.add_argument(
        "--pr-url",
        help="Optional PR URL override that must match draft PR evidence.",
    )
    parser.add_argument(
        "--remote",
        default="origin",
        help="Git remote to inspect. Default: origin.",
    )
    parser.add_argument(
        "--offline-pr-json",
        help="Optional path to a JSON fixture for offline PR status inspection.",
    )
    parser.add_argument(
        "--allow-non-waiting",
        action="store_true",
        help="Allow inspection of tasks that are not in waiting_approval.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output. JSON is always the output format.",
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


def _error_payload(task_key: str, message: str) -> dict[str, object]:
    return {
        "ok": False,
        "status": "blocked",
        "task_key": task_key,
        "summary": message,
        "safety": {
            "recommendation_only": True,
            "read_only": True,
            "task_status_changed": False,
            "db_written": False,
            "artifact_written": False,
            "cleanup_performed": False,
            "local_branch_deleted": False,
            "remote_branch_deleted": False,
            "worktree_removed": False,
            "issue_closed": False,
            "merged": False,
            "approved": False,
            "github_mutated": False,
            "background_worker_started": False,
        },
    }


def main(argv: list[str] | None = None, *, runner=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        request = PostMergeCleanupRecommendationRequest(
            task_key=args.task_key,
            repo=args.repo,
            repo_path=_resolve_path(args.repo_path) or Path(args.repo_path),
            db_path=_resolve_path(args.db_path),
            artifact_root=_resolve_path(args.artifact_root),
            remote=args.remote,
            pr_number=args.pr_number,
            pr_url=args.pr_url,
            offline_pr_json=_resolve_path(args.offline_pr_json),
            allow_non_waiting=args.allow_non_waiting,
        )
        result = recommend_post_merge_cleanup(request, runner=runner)
    except (ValueError, OSError, PostMergeCleanupRecommendationError) as exc:
        _emit_json(_error_payload(args.task_key, str(exc)), compact=args.json and not args.pretty)
        return 1

    _emit_json(result.to_dict(), compact=args.json and not args.pretty)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
