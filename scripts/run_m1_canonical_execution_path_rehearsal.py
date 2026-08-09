#!/usr/bin/env python3
"""Write deterministic M1-C canonical execution-path evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.m1_canonical_execution_path_rehearsal import (  # noqa: E402
    M1CanonicalExecutionPathRehearsalRequest,
    run_m1_canonical_execution_path_rehearsal,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Exercise deterministic ExecutionEngine authority, fail-closed, "
            "canonical Attempt, and downstream binding checks."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Absolute path for canonical-execution-path.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = run_m1_canonical_execution_path_rehearsal(
            M1CanonicalExecutionPathRehearsalRequest(
                repo_root=args.repo_root,
                output_path=args.output,
            )
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, "evidence": payload}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
