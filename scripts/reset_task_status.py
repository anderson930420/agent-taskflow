#!/usr/bin/env python3
"""Atomically reserve one retry Attempt for a blocked task."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import types
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "agent_taskflow"


def _bootstrap_source_package_without_runtime_imports() -> None:
    """Expose package submodules without executing runtime-heavy ``__init__``.

    The reset command is a SQLite/operator utility and must remain runnable from
    a source checkout that has no application dependencies installed. Importing
    ``agent_taskflow`` normally executes package runtime wiring, which imports
    the API schemas and therefore Pydantic. A synthetic package module preserves
    normal submodule resolution while deliberately skipping that bootstrap.
    """

    if "agent_taskflow" in sys.modules:
        return
    package = types.ModuleType("agent_taskflow")
    package.__file__ = str(PACKAGE_ROOT / "__init__.py")
    package.__package__ = "agent_taskflow"
    package.__path__ = [str(PACKAGE_ROOT)]
    sys.modules["agent_taskflow"] = package


_bootstrap_source_package_without_runtime_imports()

from agent_taskflow.task_status_reset import (  # noqa: E402
    RESET_FROM_STATUS,
    RESET_TO_STATUS,
    TaskStatusResetError,
    TaskStatusResetRequest,
    reset_task_status,
)


def _non_empty(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise argparse.ArgumentTypeError("must not be empty")
    return normalized


def _non_negative(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Operator-confirmed blocked-to-queued reset that atomically binds "
            "the closed Attempt to one newly reserved retry Attempt. This "
            "command does not approve, merge, clean up, execute, or validate."
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
    parser.add_argument("--reason", required=True, type=_non_empty)
    parser.add_argument(
        "--actor",
        default="reset_task_status_cli",
        type=_non_empty,
        help="Stable operator or automation identity recorded in reset audit",
    )
    parser.add_argument(
        "--request-id",
        type=_non_empty,
        help="Optional idempotency key; reusing it replays the same reset result",
    )
    parser.add_argument(
        "--expected-reset-generation",
        type=_non_negative,
        help="Optional compare-and-set generation read before issuing reset",
    )
    parser.add_argument(
        "--expected-old-attempt-id",
        type=_non_empty,
        help="Optional compare-and-set check for the latest closed Attempt",
    )
    parser.add_argument(
        "--confirm-reset",
        action="store_true",
        help="Confirm the reset transaction and new retry Attempt reservation",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the planned lineage without mutation or audit writes",
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
            actor=args.actor,
            request_id=args.request_id,
            expected_reset_generation=args.expected_reset_generation,
            expected_old_attempt_id=args.expected_old_attempt_id,
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
