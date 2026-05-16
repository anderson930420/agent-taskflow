#!/usr/bin/env python3
"""Validate a WORKFLOW.md contract without runtime integration."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.workflow_contract import load_workflow_contract


DEFAULT_WORKFLOW_PATH = Path("WORKFLOW.md")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate the repo-owned WORKFLOW.md contract skeleton.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=DEFAULT_WORKFLOW_PATH,
        type=Path,
        help="Path to WORKFLOW.md. Defaults to ./WORKFLOW.md.",
    )
    return parser


def print_summary(
    *,
    source_path: Path,
    status: str,
    warnings: Sequence[str] = (),
    errors: Sequence[str] = (),
) -> None:
    print("Workflow contract validation")
    print(f"source path: {source_path}")
    print(f"status: {status}")

    if errors:
        print("errors:")
        for error in errors:
            print(f"- {error}")

    if warnings:
        print("warnings:")
        for warning in warnings:
            print(f"- {warning}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    source_path = Path(args.path)

    try:
        contract = load_workflow_contract(source_path)
    except FileNotFoundError as exc:
        print_summary(
            source_path=source_path,
            status="failed",
            errors=(str(exc),),
        )
        return 1

    result = contract.validate()
    status = "passed" if result.passed else "failed"
    print_summary(
        source_path=result.source_path,
        status=status,
        warnings=result.warnings,
        errors=result.errors,
    )
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

