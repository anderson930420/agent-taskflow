#!/usr/bin/env python3
"""Validate a machine-readable workflow policy without runtime integration."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.workflow_schema import load_workflow_policy


DEFAULT_POLICY_PATH = Path("examples/workflow-policy.example.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a draft machine-readable workflow policy.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=DEFAULT_POLICY_PATH,
        type=Path,
        help="Path to workflow policy JSON. Defaults to examples/workflow-policy.example.json.",
    )
    return parser


def print_summary(
    *,
    source_path: Path,
    status: str,
    errors: Sequence[str] = (),
    warnings: Sequence[str] = (),
) -> None:
    print("Workflow policy validation")
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
        policy = load_workflow_policy(source_path)
    except (FileNotFoundError, ValueError) as exc:
        print_summary(
            source_path=source_path,
            status="failed",
            errors=(str(exc),),
        )
        return 1

    result = policy.validate()
    status = "passed" if result.passed else "failed"
    print_summary(
        source_path=policy.source_path,
        status=status,
        errors=result.errors,
        warnings=result.warnings,
    )
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

