#!/usr/bin/env python3
"""Run one explicitly approved queued task through Agent Taskflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.approved_task_runner import (  # noqa: E402
    ApprovedTaskRunRequest,
    ApprovedTaskRunnerError,
    run_approved_task,
)
from agent_taskflow.dispatcher import DEFAULT_VALIDATORS  # noqa: E402


def _parse_validators(values: list[str] | None) -> tuple[str, ...]:
    if not values:
        return DEFAULT_VALIDATORS
    validators = tuple(value.strip() for value in values if value.strip())
    if not validators:
        raise argparse.ArgumentTypeError("validator must not be empty")
    return validators


def _resolve_path(value: str | None) -> Path | None:
    if value is None:
        return None
    return Path(value).expanduser().resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one explicitly approved queued task and stop at waiting_approval.",
    )
    parser.add_argument(
        "--task-key",
        required=True,
        help="Explicit task key to run, for example AT-GH-123.",
    )
    parser.add_argument(
        "--executor",
        required=True,
        help="Explicit executor name to run, for example noop, shell, pi, or opencode.",
    )
    parser.add_argument(
        "--confirm-approved-task",
        action="store_true",
        help="Required for non-dry-run execution.",
    )
    parser.add_argument(
        "--repo-path",
        required=True,
        help="Absolute path to the repository root for the selected task.",
    )
    parser.add_argument(
        "--db-path",
        help="Absolute path to the Agent Taskflow SQLite state DB.",
    )
    parser.add_argument(
        "--artifact-root",
        help="Root directory for task artifacts.",
    )
    parser.add_argument(
        "--worktree-root",
        help="Root directory for isolated task worktrees.",
    )
    parser.add_argument(
        "--base-branch",
        default="main",
        help="Base branch to use when preparing the worktree. Default: main.",
    )
    parser.add_argument(
        "--validator",
        action="append",
        dest="validators",
        help="Validator name to run. May be repeated. Default validators are used when omitted.",
    )
    parser.add_argument(
        "--command",
        nargs="+",
        help="Shell command to run when --executor shell is selected.",
    )
    preflight_group = parser.add_mutually_exclusive_group()
    preflight_group.add_argument(
        "--preflight",
        dest="preflight",
        action="store_true",
        help="Run real executor preflight before execution. This is the default.",
    )
    preflight_group.add_argument(
        "--skip-preflight",
        dest="preflight",
        action="store_false",
        help="Skip real executor preflight.",
    )
    parser.set_defaults(preflight=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the selection without preparing the workspace or dispatching the executor.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON. JSON is always the output format.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output. This is the default when --json is omitted.",
    )
    return parser


def _emit(payload: dict[str, object], *, compact: bool) -> None:
    if compact:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        request = ApprovedTaskRunRequest(
            task_key=args.task_key,
            executor=args.executor,
            repo_path=_resolve_path(args.repo_path),
            db_path=_resolve_path(args.db_path),
            artifact_root=_resolve_path(args.artifact_root),
            worktree_root=_resolve_path(args.worktree_root),
            base_branch=args.base_branch,
            validators=_parse_validators(args.validators),
            confirm_approved_task=args.confirm_approved_task,
            dry_run=args.dry_run,
            preflight=args.preflight,
            command=tuple(args.command) if args.command is not None else None,
        )
        result = run_approved_task(request)
    except (ValueError, ApprovedTaskRunnerError) as exc:
        payload = {
            "ok": False,
            "status": "blocked",
            "phase": "cli",
            "summary": str(exc),
            "safety": {
                "read_only": True,
                "human_approval_required": True,
                "human_approval_confirmed": False,
                "auto_selected_task": False,
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
                "background_worker_started": False,
            },
        }
        _emit(payload, compact=args.json and not args.pretty)
        return 1

    _emit(result.to_dict(), compact=args.json and not args.pretty)
    return 0 if result.status in {"waiting_approval", "preview"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
