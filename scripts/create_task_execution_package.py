#!/usr/bin/env python3
"""Create a deterministic Task Execution Package for a queued task.

Dry-run by default. Requires --confirm-create-package to write artifacts
and record store events. Does not run the executor, prepare a worktree,
run validators, push, create PRs, merge, approve, or clean up.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.task_execution_package import (  # noqa: E402
    TaskExecutionPackageError,
    TaskExecutionPackageRequest,
    create_task_execution_package,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a Task Execution Package (implementation_prompt.md + "
            "task_execution_package.json) for a queued TaskRecord. "
            "Dry-run by default."
        ),
    )
    parser.add_argument("--task-key", required=True, help="Task key to package.")
    parser.add_argument(
        "--db-path",
        help="Absolute path to the Agent Taskflow SQLite state DB.",
    )
    parser.add_argument(
        "--artifact-root",
        help=(
            "Optional artifact root. Used to derive artifact_dir as "
            "<artifact_root>/<task_key> when the TaskRecord has no "
            "artifact_dir."
        ),
    )
    parser.add_argument(
        "--required-validator",
        action="append",
        default=None,
        help=(
            "Override required_validators (repeatable). Defaults to "
            "pytest, policy, changed-files."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the package without writing files or recording events.",
    )
    parser.add_argument(
        "--confirm-create-package",
        action="store_true",
        help=(
            "Write implementation_prompt.md and task_execution_package.json, "
            "record store artifacts, and emit the "
            "task_execution_package_created event."
        ),
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output (default is pretty when --json is unset).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit compact JSON output.",
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


def _safety_for_error() -> dict[str, object]:
    return {
        "read_only": True,
        "db_written": False,
        "artifact_written": False,
        "execution_package_created": False,
        "implementation_prompt_created": False,
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


def _error_payload(task_key: str, message: str) -> dict[str, object]:
    return {
        "ok": False,
        "status": "blocked",
        "mode": "dry_run",
        "task_key": task_key,
        "task_status_before": None,
        "artifact_dir": None,
        "implementation_prompt_path": None,
        "package_path": None,
        "package": None,
        "source_evidence": None,
        "error": message,
        "safety": _safety_for_error(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    compact = args.json and not args.pretty

    if args.dry_run and args.confirm_create_package:
        _emit_json(
            _error_payload(
                args.task_key,
                "--dry-run and --confirm-create-package are mutually exclusive",
            ),
            compact=compact,
        )
        return 2

    confirm = bool(args.confirm_create_package)
    dry_run = not confirm

    required_validators = (
        tuple(args.required_validator)
        if args.required_validator is not None
        else None
    )

    try:
        request = TaskExecutionPackageRequest(
            task_key=args.task_key,
            db_path=_resolve_path(args.db_path),
            artifact_root=_resolve_path(args.artifact_root),
            required_validators=required_validators,
            dry_run=dry_run,
            confirm=confirm,
        )
        result = create_task_execution_package(request)
    except (ValueError, OSError, TaskExecutionPackageError) as exc:
        _emit_json(_error_payload(args.task_key, str(exc)), compact=compact)
        return 1

    _emit_json(result, compact=compact)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
