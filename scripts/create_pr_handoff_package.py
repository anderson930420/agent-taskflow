#!/usr/bin/env python3
"""Create a waiting-approval PR handoff package."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.pr_handoff_package import (  # noqa: E402
    PrHandoffPackageError,
    PrHandoffPackageRequest,
    create_pr_handoff_package,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a local PR handoff package from waiting-approval evidence.",
    )
    parser.add_argument("--task-key", required=True, help="Task key to package.")
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
        help="Optional artifact root used for local package files.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
        help="Output format. Default: json.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output. This is the default output format.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output. This is the default when --json is not used.",
    )
    parser.add_argument(
        "--allow-non-waiting",
        action="store_true",
        help="Allow packaging tasks that are not in waiting_approval mode.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect evidence and git state without writing package artifacts or events.",
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
            "human_review_required": True,
            "read_only": True,
            "read_only_git_remote": True,
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
            "branch_deleted": False,
            "worktree_deleted": False,
            "background_worker_started": False,
            "webhook_started": False,
            "polling_loop_started": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    output_format = "json" if args.json else args.format
    try:
        request = PrHandoffPackageRequest(
            task_key=args.task_key,
            repo_path=_resolve_path(args.repo_path),
            db_path=_resolve_path(args.db_path),
            artifact_root=_resolve_path(args.artifact_root),
            dry_run=args.dry_run,
            allow_non_waiting=args.allow_non_waiting,
        )
        result = create_pr_handoff_package(request)
    except (ValueError, OSError, PrHandoffPackageError) as exc:
        if output_format == "markdown":
            print(
                "# PR Handoff Package\n\n"
                f"- Task key: {args.task_key}\n"
                f"- Error: {exc}\n"
            )
        else:
            _emit_json(_error_payload(args.task_key, str(exc)), compact=args.json and not args.pretty)
        return 1

    if output_format == "markdown":
        print(result.to_markdown(), end="")
    else:
        payload = result.to_dict()
        if not result.ok:
            payload = {
                "ok": False,
                "status": result.status,
                "task_key": result.task_key,
                "summary": result.error or result.summary.get("next_phase"),
                "warnings": result.warnings,
                "safety": result.safety,
            }
        _emit_json(payload, compact=args.json and not args.pretty)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
