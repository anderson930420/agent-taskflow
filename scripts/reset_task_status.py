#!/usr/bin/env python3
"""Safely reset a locally mirrored blocked task back to queued."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.task_status_reset import (  # noqa: E402
    RESET_FROM_STATUS,
    RESET_TO_STATUS,
    TaskStatusResetError,
    TaskStatusResetRequest,
    reset_task_status,
)


def _non_empty_reason(value: str) -> str:
    reason = value.strip()
    if not reason:
        raise argparse.ArgumentTypeError("must not be empty")
    return reason


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Operator-confirmed local mirror reset from blocked to queued. "
            "This command does not approve, merge, clean up, or run validators."
        )
    )
    parser.add_argument("--task-key", required=True)
    parser.add_argument(
        "--db-path",
        type=Path,
        help="SQLite state DB path (default: TaskMirrorStore default)",
    )
    parser.add_argument(
        "--from-status",
        required=True,
        choices=(RESET_FROM_STATUS,),
    )
    parser.add_argument(
        "--to-status",
        default=RESET_TO_STATUS,
        choices=(RESET_TO_STATUS,),
    )
    parser.add_argument("--reason", required=True, type=_non_empty_reason)
    parser.add_argument(
        "--confirm-reset",
        action="store_true",
        help="Confirm the local status mutation",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the planned reset without mutation or audit writes",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        request = TaskStatusResetRequest(
            task_key=args.task_key,
            db_path=args.db_path,
            from_status=args.from_status,
            to_status=args.to_status,
            reason=args.reason,
            confirm_reset=args.confirm_reset,
            dry_run=args.dry_run,
        )
        result = reset_task_status(request)
    except (TaskStatusResetError, ValueError, OSError, sqlite3.DatabaseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
