#!/usr/bin/env python3
"""Audited operator recovery for codex_advisory_evidence blocking.

Re-checks the existing Attempt artifact directory against the same v0.2.5
required Codex advisory evidence gate the runner uses and, when every
precondition holds, performs the ``blocked -> waiting_approval`` transition the
runner would have performed. This command never approves, merges, pushes,
cleans up, deletes branches or worktrees, reserves a new Attempt, or invokes a
subprocess. Human final approval is still required after ``waiting_approval``.
"""

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

    This recovery command is a SQLite/artifact operator utility and must remain
    runnable from a source checkout that has no application dependencies
    installed, matching ``scripts/reset_task_status.py``.
    """

    if "agent_taskflow" in sys.modules:
        return
    package = types.ModuleType("agent_taskflow")
    package.__file__ = str(PACKAGE_ROOT / "__init__.py")
    package.__package__ = "agent_taskflow"
    package.__path__ = [str(PACKAGE_ROOT)]
    sys.modules["agent_taskflow"] = package


_bootstrap_source_package_without_runtime_imports()

from agent_taskflow.advisory_evidence_retry import (  # noqa: E402
    AdvisoryEvidenceRetryError,
    AdvisoryEvidenceRetryRequest,
    run_advisory_evidence_retry,
)


def _non_empty(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise argparse.ArgumentTypeError("must not be empty")
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Operator recovery for tasks blocked at the codex_advisory_evidence "
            "phase. Re-checks the EXISTING Attempt artifact dir against the same "
            "advisory evidence gate and, with --confirm-transition, performs the "
            "audited blocked-to-waiting_approval transition. This command does "
            "not approve, merge, push, clean up, reserve a new Attempt, or run "
            "any subprocess."
        )
    )
    parser.add_argument("--task-key", required=True, type=_non_empty)
    parser.add_argument(
        "--db-path",
        type=Path,
        help="SQLite state DB path (default: TaskMirrorStore default)",
    )
    parser.add_argument(
        "--artifact-dir",
        required=True,
        type=Path,
        help="Existing Attempt artifact dir holding the evidence to re-check",
    )
    parser.add_argument(
        "--operator",
        required=True,
        type=_non_empty,
        help="Stable operator identity recorded in the retry audit event",
    )
    parser.add_argument(
        "--confirm-transition",
        action="store_true",
        help=(
            "Confirm the blocked-to-waiting_approval transition; without it the "
            "command only prints a read-only precondition report"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        request = AdvisoryEvidenceRetryRequest(
            task_key=args.task_key,
            db_path=args.db_path,
            artifact_dir=args.artifact_dir,
            operator=args.operator,
            confirm_transition=args.confirm_transition,
        )
        result = run_advisory_evidence_retry(request)
    except (
        AdvisoryEvidenceRetryError,
        ValueError,
        OSError,
        sqlite3.DatabaseError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    if args.confirm_transition and not result.mutated:
        for error in result.blocking_errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
