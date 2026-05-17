#!/usr/bin/env python3
"""Write a workflow policy summary artifact without runtime integration."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.workflow_schema import load_workflow_policy
from agent_taskflow.workflow_policy_artifacts import WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE


DEFAULT_POLICY_PATH = Path("examples/workflow-policy.example.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write a workflow policy summary JSON artifact.",
    )
    parser.add_argument(
        "--policy",
        default=DEFAULT_POLICY_PATH,
        type=Path,
        help="Path to workflow policy JSON. Defaults to examples/workflow-policy.example.json.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Path where the workflow policy summary artifact should be written.",
    )
    return parser


def _governance_invariants(orchestration_boundary: Any) -> dict[str, Any]:
    boundary = orchestration_boundary if isinstance(orchestration_boundary, dict) else {}
    return {
        "ai_workers_may_schedule_tasks": boundary.get("ai_workers_may_schedule_tasks"),
        "ai_workers_may_approve": boundary.get("ai_workers_may_approve"),
        "ai_workers_may_merge": boundary.get("ai_workers_may_merge"),
        "ai_workers_may_push": boundary.get("ai_workers_may_push"),
        "ai_workers_may_cleanup": boundary.get("ai_workers_may_cleanup"),
    }


def build_artifact(policy: Any) -> dict[str, Any]:
    """Build a JSON-serializable summary artifact from a valid policy."""
    result = policy.validate()
    status = "passed" if result.passed else "failed"
    return {
        "artifact_type": WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE,
        "schema_version": policy.schema_version,
        "source_path": str(policy.source_path),
        "validation_status": status,
        "validation_errors": result.errors,
        "validation_warnings": result.warnings,
        "allowed_executors": policy.allowed_executors,
        "required_validators": policy.required_validators,
        "optional_validators": policy.optional_validators,
        "path_policy": policy.path_policy,
        "workspace_policy": policy.workspace_policy,
        "proof_of_work": policy.proof_of_work,
        "human_review": policy.human_review,
        "forbidden_actions": policy.forbidden_actions,
        "deferred_integrations": policy.deferred_integrations,
        "governance_invariants": _governance_invariants(policy.orchestration_boundary),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def print_summary(
    *,
    source_path: Path,
    output_path: Path,
    status: str,
    errors: Sequence[str] = (),
    warnings: Sequence[str] = (),
) -> None:
    print("Workflow policy summary artifact")
    print(f"source path: {source_path}")
    print(f"output path: {output_path}")
    print(f"validation status: {status}")

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
    policy_path = Path(args.policy)
    output_path = Path(args.output)

    try:
        policy = load_workflow_policy(policy_path)
    except (FileNotFoundError, ValueError) as exc:
        print_summary(
            source_path=policy_path,
            output_path=output_path,
            status="failed",
            errors=(str(exc),),
        )
        return 1

    result = policy.validate()
    if not result.passed:
        print_summary(
            source_path=policy.source_path,
            output_path=output_path,
            status="failed",
            errors=result.errors,
            warnings=result.warnings,
        )
        return 1

    artifact = build_artifact(policy)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print_summary(
        source_path=policy.source_path,
        output_path=output_path,
        status="passed",
        warnings=result.warnings,
    )
    print("artifact written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
