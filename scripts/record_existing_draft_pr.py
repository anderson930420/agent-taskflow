#!/usr/bin/env python3
"""Record draft_pr evidence for a pre-existing GitHub PR.

Use this script after a human created the PR manually (for example, with
``gh pr create``) so the Agent Taskflow store has the ``draft_pr``
artifact and ``draft_pr_created`` event that the post-merge cleanup
chain requires. The script is read-only against GitHub; it only writes
local evidence under explicit confirmation.

It never creates, edits, merges, closes, approves, or cleans up a PR.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.draft_pr_record import (  # noqa: E402
    DraftPrConfirmError,
    DraftPrRecordRequest,
    record_existing_draft_pr,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record draft_pr evidence for a pre-existing GitHub PR.",
    )
    parser.add_argument("--task-key", required=True, help="Task key.")
    parser.add_argument("--db-path", required=True, help="Absolute path to the SQLite state DB.")
    parser.add_argument(
        "--repo",
        required=True,
        help="GitHub repo, for example anderson930420/agent-taskflow.",
    )
    parser.add_argument(
        "--target-repo",
        help=(
            "Explicit GitHub target repo. Must equal --repo. Provide together "
            "with --allow-source-repo-mismatch when the handoff source repo "
            "differs from the actual GitHub repo (dogfood scenario)."
        ),
    )
    parser.add_argument(
        "--allow-source-repo-mismatch",
        action="store_true",
        help=(
            "Allow the recorded PR target repo to differ from the handoff "
            "source repo. Must be paired with --target-repo."
        ),
    )
    parser.add_argument(
        "--pr-number",
        type=int,
        required=True,
        help="The existing PR number to record evidence for (positive integer).",
    )
    parser.add_argument(
        "--repo-path",
        required=True,
        help="Absolute path to the repository root for the task worktree.",
    )
    parser.add_argument(
        "--artifact-root",
        help="Optional artifact root used for the recorded draft_pr.json artifact.",
    )
    parser.add_argument(
        "--allow-non-waiting",
        action="store_true",
        help="Allow inspection of tasks that are not in waiting_approval.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Never write evidence; only validate and preview.",
    )
    parser.add_argument(
        "--confirm-record-existing-pr",
        action="store_true",
        help="Required before the script writes the draft_pr artifact and event.",
    )
    parser.add_argument("--json", action="store_true", help="Emit compact JSON output.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
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
            "branch_pushed": False,
            "pr_created": False,
            "draft_pr": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "branch_deleted": False,
            "worktree_deleted": False,
            "background_worker_started": False,
            "github_mutated": False,
            "read_only_github": True,
        },
    }


def main(argv: list[str] | None = None, *, runner=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        request = DraftPrRecordRequest(
            task_key=args.task_key,
            repo=args.repo,
            target_repo=args.target_repo,
            allow_source_repo_mismatch=args.allow_source_repo_mismatch,
            pr_number=int(args.pr_number),
            repo_path=_resolve_path(args.repo_path) or Path(args.repo_path),
            db_path=_resolve_path(args.db_path),
            artifact_root=_resolve_path(args.artifact_root),
            allow_non_waiting=args.allow_non_waiting,
            dry_run=args.dry_run,
            confirm_record_existing_pr=args.confirm_record_existing_pr,
        )
        result = record_existing_draft_pr(request, runner=runner)
    except (ValueError, TypeError, OSError, DraftPrConfirmError) as exc:
        _emit_json(
            _error_payload(args.task_key, str(exc)),
            compact=args.json and not args.pretty,
        )
        return 1

    _emit_json(result.to_dict(), compact=args.json and not args.pretty)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
