#!/usr/bin/env python3
"""Summarize a machine-readable workflow policy without runtime integration."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.workflow_schema import load_workflow_policy


DEFAULT_POLICY_PATH = Path("examples/workflow-policy.example.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Summarize a draft machine-readable workflow policy.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=DEFAULT_POLICY_PATH,
        type=Path,
        help="Path to workflow policy JSON. Defaults to examples/workflow-policy.example.json.",
    )
    return parser


def _format_value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "(none)"
    if isinstance(value, dict):
        return ", ".join(f"{key}={value[key]}" for key in sorted(value)) or "(none)"
    if value is None:
        return "(none)"
    return str(value)


def _print_items(label: str, values: Any) -> None:
    print(f"{label}: {_format_value(values)}")


def print_error_summary(*, source_path: Path, errors: Sequence[str]) -> None:
    print("Workflow policy summary")
    print(f"source path: {source_path}")
    print("validation status: failed")
    print("errors:")
    for error in errors:
        print(f"- {error}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    source_path = Path(args.path)

    try:
        policy = load_workflow_policy(source_path)
    except (FileNotFoundError, ValueError) as exc:
        print_error_summary(source_path=source_path, errors=(str(exc),))
        return 1

    result = policy.validate()
    status = "passed" if result.passed else "failed"

    print("Workflow policy summary")
    print(f"source path: {policy.source_path}")
    print(f"schema_version: {_format_value(policy.schema_version)}")
    print(f"validation status: {status}")
    _print_items("allowed_executors", policy.allowed_executors)
    _print_items("required_validators", policy.required_validators)
    _print_items("optional_validators", policy.optional_validators)
    _print_items("path_policy", policy.path_policy)
    _print_items("workspace_policy", policy.workspace_policy)
    _print_items(
        "proof_of_work required_artifacts",
        policy.proof_of_work.get("required_artifacts") if isinstance(policy.proof_of_work, dict) else None,
    )

    if isinstance(policy.human_review, dict):
        print(f"human_review required: {_format_value(policy.human_review.get('required'))}")
        _print_items("human_review allowed_decisions", policy.human_review.get("allowed_decisions"))
    else:
        _print_items("human_review", policy.human_review)

    _print_items("forbidden_actions", policy.forbidden_actions)
    _print_items("deferred_integrations", policy.deferred_integrations)

    orchestration_boundary = (
        policy.orchestration_boundary if isinstance(policy.orchestration_boundary, dict) else {}
    )
    print("governance invariant summary:")
    print(
        "- AI workers may not schedule tasks: "
        f"{orchestration_boundary.get('ai_workers_may_schedule_tasks') is False}"
    )
    print(
        "- AI workers may not approve: "
        f"{orchestration_boundary.get('ai_workers_may_approve') is False}"
    )
    print(
        "- AI workers may not merge: "
        f"{orchestration_boundary.get('ai_workers_may_merge') is False}"
    )
    print(
        "- AI workers may not push: "
        f"{orchestration_boundary.get('ai_workers_may_push') is False}"
    )
    print(
        "- AI workers may not cleanup: "
        f"{orchestration_boundary.get('ai_workers_may_cleanup') is False}"
    )

    if result.errors:
        print("errors:")
        for error in result.errors:
            print(f"- {error}")

    if result.warnings:
        print("warnings:")
        for warning in result.warnings:
            print(f"- {warning}")

    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
