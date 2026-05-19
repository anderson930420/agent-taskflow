"""CLI wrapper for explicit task closeout confirmation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_taskflow.task_closeout_confirm import (
    TaskCloseoutConfirmError,
    TaskCloseoutConfirmRequest,
    confirm_task_closeout,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Confirm explicit task closeout after merged PR and cleanup evidence."
    )
    parser.add_argument("--task-key", required=True, help="Task key, for example AT-GH-20.")
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
        "--db-path",
        help="Absolute path to the Agent Taskflow SQLite state DB.",
    )
    parser.add_argument(
        "--artifact-root",
        help="Optional artifact root used for task closeout evidence files.",
    )
    parser.add_argument(
        "--offline-pr-json",
        help="Optional path to a JSON fixture for offline PR status inspection.",
    )
    parser.add_argument(
        "--target-status",
        default="completed",
        help="Terminal task status to write on success. Default: completed.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Never perform the status update or write closeout evidence; only validate and preview.",
    )
    parser.add_argument(
        "--confirm-task-closeout",
        action="store_true",
        help="Required before the script may perform the actual task closeout status update.",
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
        "error": message,
        "safety": {
            "human_confirmation_required": True,
            "human_confirmation_confirmed": False,
            "task_status_changed": False,
            "db_written": False,
            "task_closeout_performed": False,
            "github_mutated": False,
            "issue_closed": False,
            "local_branch_deleted": False,
            "remote_branch_deleted": False,
            "worktree_removed": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "task_completed": False,
            "task_archived": False,
            "background_worker_started": False,
            "webhook_started": False,
            "polling_loop_started": False,
        },
    }


def main(argv: list[str] | None = None, *, runner=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        request = TaskCloseoutConfirmRequest(
            task_key=args.task_key,
            repo=args.repo,
            repo_path=_resolve_path(args.repo_path) or Path(args.repo_path),
            db_path=_resolve_path(args.db_path),
            artifact_root=_resolve_path(args.artifact_root),
            offline_pr_json=_resolve_path(args.offline_pr_json),
            target_status=args.target_status,
            dry_run=args.dry_run,
            confirm_task_closeout=args.confirm_task_closeout,
        )
        result = confirm_task_closeout(request, runner=runner)
    except (ValueError, OSError, TaskCloseoutConfirmError) as exc:
        _emit_json(
            _error_payload(args.task_key, str(exc)),
            compact=args.json and not args.pretty,
        )
        return 1

    _emit_json(result.to_dict(), compact=args.json and not args.pretty)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
