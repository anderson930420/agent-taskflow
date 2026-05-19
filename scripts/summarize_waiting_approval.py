#!/usr/bin/env python3
"""Summarize one waiting-approval task for human review."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.waiting_approval_summary import (  # noqa: E402
    WaitingApprovalSummaryRequest,
    summarize_waiting_approval_task,
    summarize_waiting_approval_task_markdown,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read waiting-approval evidence from the local task mirror and summarize it for human review.",
    )
    parser.add_argument(
        "--task-key",
        required=True,
        help="Explicit task key to summarize, for example AT-GH-123.",
    )
    parser.add_argument(
        "--db-path",
        help="Absolute path to the Agent Taskflow SQLite state DB. Defaults to ~/.agent-taskflow/state.db.",
    )
    parser.add_argument(
        "--artifact-root",
        help="Optional artifact root used when the task record does not include an artifact_dir.",
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
        help="Allow summarizing tasks that are not in waiting_approval mode.",
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    output_format = "json" if args.json else args.format
    try:
        request = WaitingApprovalSummaryRequest(
            task_key=args.task_key,
            db_path=_resolve_path(args.db_path),
            artifact_root=_resolve_path(args.artifact_root),
            allow_non_waiting=args.allow_non_waiting,
        )
        result = summarize_waiting_approval_task(request)
    except (ValueError, OSError) as exc:
        payload = {
            "ok": False,
            "status": "error",
            "task_key": args.task_key,
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
                "branch_deleted": False,
                "worktree_deleted": False,
                "background_worker_started": False,
                "webhook_started": False,
                "polling_loop_started": False,
            },
        }
        if output_format == "markdown":
            print(f"# Waiting Approval Review Summary\n\n- Task key: {args.task_key}\n- Error: {str(exc)}\n")
        else:
            _emit_json(payload, compact=args.json and not args.pretty)
        return 1

    if output_format == "markdown":
        _, markdown = summarize_waiting_approval_task_markdown(request)
        print(markdown, end="")
    else:
        _emit_json(result.to_dict(), compact=args.json and not args.pretty)

    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
