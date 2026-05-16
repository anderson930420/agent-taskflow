#!/usr/bin/env python3
"""Smoke test workflow policy summary artifact generation and readback."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.workflow_schema import load_workflow_policy
from agent_taskflow.workflow_policy_artifacts import (
    WORKFLOW_POLICY_SUMMARY_FILENAME,
    WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE,
    WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS,
)
from scripts.write_workflow_policy_summary_artifact import build_artifact


DEFAULT_POLICY_PATH = Path("examples/workflow-policy.example.json")
ARTIFACT_FILENAME = WORKFLOW_POLICY_SUMMARY_FILENAME
REQUIRED_ARTIFACT_FIELDS = WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a standalone workflow policy summary artifact smoke.",
    )
    parser.add_argument(
        "--policy",
        default=DEFAULT_POLICY_PATH,
        type=Path,
        help="Path to workflow policy JSON. Defaults to examples/workflow-policy.example.json.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory where workflow_policy_summary.json should be written.",
    )
    parser.add_argument(
        "--keep-output",
        action="store_true",
        help="Preserve the generated output directory for inspection.",
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Alias for --keep-output.",
    )
    return parser


def verify_artifact(artifact: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_ARTIFACT_FIELDS:
        if field not in artifact:
            errors.append(f"missing required artifact field: {field}")

    if artifact.get("artifact_type") != WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE:
        errors.append(f"artifact_type must be {WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE}")

    if artifact.get("validation_status") != "passed":
        errors.append("validation_status must be passed")

    return errors


def _print_summary(
    *,
    policy_path: Path,
    output_dir: Path,
    artifact_path: Path,
    status: str,
    errors: Sequence[str] = (),
    kept: bool = False,
) -> None:
    print("Workflow policy artifact smoke")
    print(f"policy path: {policy_path}")
    print(f"output dir: {output_dir}")
    print(f"artifact path: {artifact_path}")
    print(f"status: {status}")
    print(f"output kept: {'yes' if kept else 'no'}")
    for field in WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS:
        print(f"- {field}")
    # Also print optional_validators for visibility (validation_errors and
    # validation_warnings are required fields per the contract).
    print(f"- optional_validators  [optional]")

    if errors:
        print("errors:")
        for error in errors:
            print(f"- {error}")


def _resolve_output_dir(output_dir: Path | None) -> tuple[Path, bool]:
    if output_dir is not None:
        return Path(output_dir), False
    return Path(tempfile.mkdtemp(prefix="agent-taskflow-workflow-policy-artifact-")), True


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    policy_path = Path(args.policy)
    output_dir, created_temp_dir = _resolve_output_dir(args.output_dir)
    keep_output = bool(args.keep_output or args.keep_workspace or args.output_dir)
    artifact_path = output_dir / ARTIFACT_FILENAME

    try:
        policy = load_workflow_policy(policy_path)
        result = policy.validate()
        if not result.passed:
            _print_summary(
                policy_path=policy.source_path,
                output_dir=output_dir,
                artifact_path=artifact_path,
                status="failed",
                errors=result.errors,
                kept=keep_output,
            )
            return 1

        artifact = build_artifact(policy)
        output_dir.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(artifact, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        readback = json.loads(artifact_path.read_text(encoding="utf-8"))
        errors = verify_artifact(readback)
        if errors:
            _print_summary(
                policy_path=policy.source_path,
                output_dir=output_dir,
                artifact_path=artifact_path,
                status="failed",
                errors=errors,
                kept=keep_output,
            )
            return 1

        _print_summary(
            policy_path=policy.source_path,
            output_dir=output_dir,
            artifact_path=artifact_path,
            status="passed",
            kept=keep_output,
        )
        return 0
    except (FileNotFoundError, ValueError, json.JSONDecodeError, OSError) as exc:
        _print_summary(
            policy_path=policy_path,
            output_dir=output_dir,
            artifact_path=artifact_path,
            status="failed",
            errors=(str(exc),),
            kept=keep_output,
        )
        return 1
    finally:
        if created_temp_dir and not keep_output:
            shutil.rmtree(output_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
