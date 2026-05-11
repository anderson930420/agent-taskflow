#!/usr/bin/env python3
"""Run the Agent Taskflow dispatcher for one task."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.dispatcher import DEFAULT_VALIDATORS, dispatch_task


def _parse_validators(raw: str) -> tuple[str, ...]:
    validators = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not validators:
        raise argparse.ArgumentTypeError("validators must not be empty")
    return validators


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dispatch one Agent Taskflow task through implementation and validation.",
    )
    parser.add_argument(
        "--task-key",
        required=True,
        help="Task key to dispatch, for example AT-0009.",
    )
    parser.add_argument(
        "--db-path",
        help="Absolute path to the Agent Taskflow SQLite state DB.",
    )
    parser.add_argument(
        "--executor",
        dest="executor_name",
        help="Executor name override, for example manual, noop, or opencode.",
    )
    parser.add_argument(
        "--model",
        help="Model override passed to the executor context.",
    )
    parser.add_argument(
        "--validators",
        type=_parse_validators,
        default=DEFAULT_VALIDATORS,
        help="Comma-separated validator names. Default: pytest,openspec.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate task state and governance without running executor or validators.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    result = dispatch_task(
        args.task_key,
        db_path=Path(args.db_path) if args.db_path else None,
        executor_name=args.executor_name,
        model=args.model,
        validators=args.validators,
        dry_run=args.dry_run,
    )

    payload = {
        "task_key": result.task_key,
        "status": result.status,
        "summary": result.summary,
        "executor_status": result.executor_status,
        "validator_statuses": result.validator_statuses,
        "blocked_reason": result.blocked_reason,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))

    return 0 if result.status in {"waiting_approval", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
