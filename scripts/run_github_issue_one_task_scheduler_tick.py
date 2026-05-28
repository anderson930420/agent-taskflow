#!/usr/bin/env python3
"""Run one scheduled, locked GitHub Issue one-task tick."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.github_issue_one_task_scheduler_tick import (  # noqa: E402
    GITHUB_ISSUE_ONE_TASK_SCHEDULER_TICK_SCHEMA_VERSION,
    GITHUB_ISSUE_ONE_TASK_SCHEDULER_TICK_SOURCE,
    GitHubIssueOneTaskSchedulerTickError,
    GitHubIssueOneTaskSchedulerTickRequest,
    default_lock_path,
    run_github_issue_one_task_scheduler_tick,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one scheduled GitHub Issue one-task tick under a non-overlap "
            "lock. Dry-run is the default. --confirmed applies the controlled "
            "lower-level confirmation preset, processes at most one issue/task, "
            "and stops."
        )
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--local-repo-path", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--issue-limit", type=int, default=100)
    parser.add_argument("--include-label", action="append", default=[])
    parser.add_argument("--exclude-label", action="append", default=[])
    parser.add_argument("--lock-path", default=None)
    parser.add_argument("--operator", default=None)
    parser.add_argument("--operator-note", default=None)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--base-branch", default=None)
    parser.add_argument("--confirmed", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    confirmed = bool(args.confirmed)
    dry_run = not confirmed

    try:
        request = GitHubIssueOneTaskSchedulerTickRequest(
            repo=args.repo,
            db_path=Path(args.db_path).expanduser(),
            local_repo_path=Path(args.local_repo_path).expanduser(),
            artifact_root=Path(args.artifact_root).expanduser(),
            dry_run=dry_run,
            confirmed=confirmed,
            issue_limit=int(args.issue_limit),
            include_labels=tuple(args.include_label),
            exclude_labels=tuple(args.exclude_label),
            lock_path=(
                Path(args.lock_path).expanduser()
                if args.lock_path is not None
                else None
            ),
            fail_if_locked=True,
            operator=args.operator,
            operator_note=args.operator_note,
            remote=args.remote,
            base_branch=args.base_branch,
            draft=True,
        )
        payload = run_github_issue_one_task_scheduler_tick(request)
    except (ValueError, GitHubIssueOneTaskSchedulerTickError) as exc:
        payload = _error_payload(
            str(exc),
            dry_run=dry_run,
            confirmed=confirmed,
            repo=str(args.repo),
            lock_path=(
                Path(args.lock_path).expanduser()
                if args.lock_path is not None
                else default_lock_path()
            ),
        )
        _emit(payload, compact=bool(args.json))
        return 1

    _emit(payload, compact=bool(args.json))
    return 0 if payload.get("ok") else 1


def _error_payload(
    message: str,
    *,
    dry_run: bool,
    confirmed: bool,
    repo: str,
    lock_path: Path,
) -> dict[str, object]:
    return {
        "ok": False,
        "schema_version": GITHUB_ISSUE_ONE_TASK_SCHEDULER_TICK_SCHEMA_VERSION,
        "source": GITHUB_ISSUE_ONE_TASK_SCHEDULER_TICK_SOURCE,
        "status": "error",
        "mode": "confirmed" if confirmed else "dry_run",
        "repo": repo,
        "lock": {
            "path": str(lock_path),
            "acquired": False,
            "contended": False,
            "released": False,
            "fail_if_locked": True,
        },
        "automation": None,
        "selected_task_key": None,
        "reasons": [message],
        "safety": {
            "scheduled_tick": True,
            "one_tick_only": True,
            "one_issue_only": True,
            "one_task_only": True,
            "lock_acquired": False,
            "lock_contended": False,
            "dry_run": dry_run,
            "confirmed": confirmed,
            "automation_called": False,
            "discovery_called": False,
            "issue_ingested": False,
            "watcher_called": False,
            "approved_task_runner_called": False,
            "github_mutated": False,
            "branch_pushed": False,
            "draft_pr_created": False,
            "approved": False,
            "merged": False,
            "cleanup_performed": False,
            "branch_deleted": False,
            "worktree_deleted": False,
            "scheduler_loop_started": False,
            "background_worker_started": False,
            "multi_task_batch_started": False,
            "human_review_required": True,
        },
    }


def _emit(payload: dict[str, object], *, compact: bool) -> None:
    if compact:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
