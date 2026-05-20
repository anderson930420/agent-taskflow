#!/usr/bin/env python3
"""Explicit queued-task handoff runner CLI.

Verifies the Task Execution Package for a queued task and, under
--confirm-handoff, hands the task to approved_task_runner.

Dry-run by default. This is not a scheduler, not a background loop, and
does not auto-pick queued tasks. One explicit operator command per
explicit task key.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.dispatcher import DEFAULT_VALIDATORS  # noqa: E402
from agent_taskflow.queued_task_handoff import (  # noqa: E402
    QueuedTaskHandoffError,
    QueuedTaskHandoffRequest,
    run_queued_task_handoff,
)


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
        description=(
            "Verify a queued task's Task Execution Package and, under "
            "--confirm-handoff, hand the task to approved_task_runner."
        ),
    )
    parser.add_argument("--task-key", required=True, help="Task key to hand off.")
    parser.add_argument(
        "--executor",
        required=True,
        help="Executor name to run, for example shell, pi, or opencode.",
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
        help="Validator name to run. May be repeated.",
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
        help="Run real executor preflight before execution. Default.",
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
        help=(
            "Verify the execution package without calling approved_task_runner. "
            "Default behavior when --confirm-handoff is absent."
        ),
    )
    parser.add_argument(
        "--confirm-handoff",
        action="store_true",
        help=(
            "Required for non-dry-run handoff. Calls approved_task_runner "
            "with confirm_approved_task=True after package verification."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit compact JSON output.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output (default when --json is omitted).",
    )
    return parser


def _emit(payload: dict[str, object], *, compact: bool) -> None:
    if compact:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


def _safety_for_cli_error() -> dict[str, object]:
    return {
        "read_only": True,
        "db_written": False,
        "artifact_written": False,
        "package_verified": False,
        "handoff_confirmed": False,
        "approved_task_runner_started": False,
        "workspace_prepared": False,
        "executor_started": False,
        "validators_started": False,
        "branch_pushed": False,
        "pr_created": False,
        "merged": False,
        "approved": False,
        "cleanup_performed": False,
        "background_worker_started": False,
    }


def _error_payload(
    task_key: str,
    executor: str,
    *,
    phase: str,
    message: str,
) -> dict[str, object]:
    return {
        "ok": False,
        "status": "blocked",
        "phase": phase,
        "task_key": task_key,
        "executor": executor,
        "dry_run": True,
        "package": {
            "verified": False,
            "package_path": None,
            "implementation_prompt_path": None,
            "schema_version": None,
            "task_key": None,
            "status_before": None,
        },
        "handoff": {
            "confirmed": False,
            "approved_task_runner_invoked": False,
            "executor": executor,
            "base_branch": None,
            "validators": None,
            "command": None,
            "preflight": None,
        },
        "runner_result": None,
        "safety": _safety_for_cli_error(),
        "error": message,
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    compact = args.json and not args.pretty

    if args.dry_run and args.confirm_handoff:
        _emit(
            _error_payload(
                args.task_key,
                args.executor,
                phase="cli",
                message="--dry-run and --confirm-handoff are mutually exclusive",
            ),
            compact=compact,
        )
        return 2

    confirm_handoff = bool(args.confirm_handoff)
    dry_run = not confirm_handoff

    try:
        request = QueuedTaskHandoffRequest(
            task_key=args.task_key,
            executor=args.executor,
            repo_path=_resolve_path(args.repo_path),
            db_path=_resolve_path(args.db_path),
            artifact_root=_resolve_path(args.artifact_root),
            worktree_root=_resolve_path(args.worktree_root),
            base_branch=args.base_branch,
            validators=_parse_validators(args.validators),
            command=tuple(args.command) if args.command is not None else None,
            preflight=args.preflight,
            dry_run=dry_run,
            confirm_handoff=confirm_handoff,
        )
        result = run_queued_task_handoff(request)
    except (ValueError, OSError, QueuedTaskHandoffError) as exc:
        _emit(
            _error_payload(
                args.task_key,
                args.executor,
                phase="cli",
                message=str(exc),
            ),
            compact=compact,
        )
        return 1

    _emit(result.to_dict(), compact=compact)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
