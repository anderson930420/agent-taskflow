#!/usr/bin/env python3
"""Run one-shot GitHub Issue discovery to one-task watcher automation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.github_issue_one_task_automation import (  # noqa: E402
    GITHUB_ISSUE_ONE_TASK_AUTOMATION_SCHEMA_VERSION,
    GITHUB_ISSUE_ONE_TASK_AUTOMATION_SOURCE,
    GitHubIssueOneTaskAutomationError,
    GitHubIssueOneTaskAutomationRequest,
    run_github_issue_one_task_automation,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a one-shot GitHub Issue discovery, ingest one selected issue, "
            "then invoke the confirmed one-task watcher and stop."
        )
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--local-repo-path", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--issue-limit", type=int, default=100)
    parser.add_argument("--include-label", action="append", default=[])
    parser.add_argument("--exclude-label", action="append", default=[])
    parser.add_argument("--select-first-issue", action="store_true")
    parser.add_argument("--confirm-select-first-issue", action="store_true")
    parser.add_argument("--confirm-ingest-issue", action="store_true")
    parser.add_argument("--confirm-run-watcher-one-task", action="store_true")
    parser.add_argument("--confirm-run-one-shot-pipeline", action="store_true")
    parser.add_argument("--confirm-prepare-pr", action="store_true")
    parser.add_argument("--confirm-github-mutations", action="store_true")
    parser.add_argument("--confirm-branch-push", action="store_true")
    parser.add_argument("--confirm-draft-pr", action="store_true")
    parser.add_argument("--operator", default=None)
    parser.add_argument("--operator-note", default=None)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--base-branch", default=None)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dry_run = not _confirmed_mode_requested(args)

    try:
        request = GitHubIssueOneTaskAutomationRequest(
            repo=args.repo,
            db_path=Path(args.db_path).expanduser(),
            local_repo_path=Path(args.local_repo_path).expanduser(),
            artifact_root=Path(args.artifact_root).expanduser(),
            dry_run=dry_run,
            issue_limit=int(args.issue_limit),
            include_labels=tuple(args.include_label),
            exclude_labels=tuple(args.exclude_label),
            select_first_issue=bool(args.select_first_issue),
            confirm_select_first_issue=bool(args.confirm_select_first_issue),
            confirm_ingest_issue=bool(args.confirm_ingest_issue),
            confirm_run_watcher_one_task=bool(args.confirm_run_watcher_one_task),
            confirm_run_one_shot_pipeline=bool(args.confirm_run_one_shot_pipeline),
            confirm_prepare_pr=bool(args.confirm_prepare_pr),
            confirm_github_mutations=bool(args.confirm_github_mutations),
            confirm_branch_push=bool(args.confirm_branch_push),
            confirm_draft_pr=bool(args.confirm_draft_pr),
            operator=args.operator,
            operator_note=args.operator_note,
            remote=args.remote,
            base_branch=args.base_branch,
            draft=True,
        )
        payload = run_github_issue_one_task_automation(request)
    except (ValueError, GitHubIssueOneTaskAutomationError) as exc:
        payload = _error_payload(str(exc), dry_run=dry_run, repo=str(args.repo))
        _emit(payload, compact=bool(args.json))
        return 1

    _emit(payload, compact=bool(args.json))
    return 0 if payload.get("ok") else 1


def _confirmed_mode_requested(args: argparse.Namespace) -> bool:
    return any(
        bool(getattr(args, field_name))
        for field_name in (
            "confirm_ingest_issue",
            "confirm_run_watcher_one_task",
            "confirm_run_one_shot_pipeline",
            "confirm_prepare_pr",
            "confirm_github_mutations",
            "confirm_branch_push",
            "confirm_draft_pr",
        )
    )


def _error_payload(message: str, *, dry_run: bool, repo: str) -> dict[str, object]:
    return {
        "ok": False,
        "schema_version": GITHUB_ISSUE_ONE_TASK_AUTOMATION_SCHEMA_VERSION,
        "source": GITHUB_ISSUE_ONE_TASK_AUTOMATION_SOURCE,
        "status": "error",
        "mode": "dry_run" if dry_run else "confirmed",
        "repo": repo,
        "selected_issue": None,
        "ingestion": None,
        "watcher": None,
        "selected_task_key": None,
        "reasons": [message],
        "safety": {
            "dry_run": dry_run,
            "one_issue_only": True,
            "one_task_only": True,
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
