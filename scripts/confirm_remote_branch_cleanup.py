#!/usr/bin/env python3
"""Preview or confirm explicit remote branch cleanup after local cleanup evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.remote_branch_cleanup_confirm import (  # noqa: E402
    RemoteBranchCleanupConfirmError,
    RemoteBranchCleanupConfirmRequest,
    confirm_remote_branch_cleanup,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or confirm explicit remote branch cleanup after merged PR evidence.",
    )
    parser.add_argument("--task-key", required=True, help="Task key whose remote branch to delete.")
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
        help="Optional artifact root used for local remote_branch_cleanup evidence files.",
    )
    parser.add_argument(
        "--remote",
        default="origin",
        help="Git remote to delete from. Default: origin.",
    )
    parser.add_argument(
        "--branch",
        help="Optional branch override. Must match trusted task branch evidence exactly.",
    )
    parser.add_argument(
        "--offline-pr-json",
        help="Optional path to a JSON fixture for offline PR status inspection.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Never perform the actual remote branch delete; only validate and preview the command.",
    )
    parser.add_argument(
        "--confirm-remote-branch-delete",
        action="store_true",
        help="Required before the script may perform the actual git push --delete.",
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
            "workspace_prepared": False,
            "executor_started": False,
            "validators_started": False,
            "local_cleanup_performed": False,
            "worktree_removed": False,
            "local_branch_deleted": False,
            "remote_branch_cleanup_performed": False,
            "remote_branch_deleted": False,
            "github_issue_mutated": False,
            "issue_closed": False,
            "task_archived": False,
            "task_completed": False,
            "merged": False,
            "approved": False,
            "force_delete": False,
            "background_worker_started": False,
            "webhook_started": False,
            "polling_loop_started": False,
        },
    }


def main(argv: list[str] | None = None, *, runner=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        request = RemoteBranchCleanupConfirmRequest(
            task_key=args.task_key,
            repo_path=_resolve_path(args.repo_path) or Path(args.repo_path),
            db_path=_resolve_path(args.db_path),
            artifact_root=_resolve_path(args.artifact_root),
            remote=args.remote,
            branch=args.branch,
            offline_pr_json=_resolve_path(args.offline_pr_json),
            dry_run=args.dry_run,
            confirm_remote_branch_delete=args.confirm_remote_branch_delete,
        )
        result = confirm_remote_branch_cleanup(request, runner=runner)
    except (ValueError, OSError, RemoteBranchCleanupConfirmError) as exc:
        _emit_json(
            _error_payload(args.task_key, str(exc)),
            compact=args.json and not args.pretty,
        )
        return 1

    _emit_json(result.to_dict(), compact=args.json and not args.pretty)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
