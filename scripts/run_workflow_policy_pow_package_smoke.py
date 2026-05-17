#!/usr/bin/env python3
"""Smoke test a workflow policy proof-of-work artifact package."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.workflow_schema import load_workflow_policy
from agent_taskflow.workflow_policy_artifacts import (
    WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME,
    WORKFLOW_POLICY_SUMMARY_FILENAME,
    WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE,
    WORKFLOW_POLICY_ARTIFACT_INDEX_TYPE,
    WORKFLOW_POLICY_PACKAGE_TYPE,
    WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION,
    WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS,
)
from scripts.write_workflow_policy_summary_artifact import build_artifact


DEFAULT_POLICY_PATH = Path("examples/workflow-policy.example.json")
ARTIFACT_INDEX_FILENAME = WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME
SUMMARY_ARTIFACT_FILENAME = WORKFLOW_POLICY_SUMMARY_FILENAME
REQUIRED_SUMMARY_FIELDS = WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a standalone workflow policy proof-of-work package smoke.",
    )
    parser.add_argument(
        "--policy",
        default=DEFAULT_POLICY_PATH,
        type=Path,
        help="Path to workflow policy JSON. Defaults to examples/workflow-policy.example.json.",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        help="Directory where the proof-of-work artifact package should be written.",
    )
    parser.add_argument(
        "--keep-artifacts",
        action="store_true",
        help="Preserve generated artifacts for inspection.",
    )
    parser.add_argument(
        "--keep-output",
        action="store_true",
        help="Alias for --keep-artifacts.",
    )
    return parser


def build_artifact_index() -> dict[str, Any]:
    return {
        "artifact_index_version": WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION,
        "package_type": WORKFLOW_POLICY_PACKAGE_TYPE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifacts": [
            {
                "name": "workflow_policy_summary",
                "artifact_type": WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE,
                "path": SUMMARY_ARTIFACT_FILENAME,
                "required": True,
                "description": "Machine-readable workflow policy summary artifact.",
            }
        ],
    }


def verify_summary_artifact(summary: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_SUMMARY_FIELDS:
        if field not in summary:
            errors.append(f"missing required summary field: {field}")

    if summary.get("artifact_type") != WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE:
        errors.append(f"summary artifact_type must be {WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE}")

    if summary.get("validation_status") != "passed":
        errors.append("summary validation_status must be passed")

    return errors


def verify_artifact_index(index: dict[str, Any], artifact_dir: Path) -> list[str]:
    errors: list[str] = []
    artifacts = index.get("artifacts")
    if not isinstance(artifacts, list):
        return ["artifact_index.artifacts must be a list"]

    summary_entries = [
        artifact
        for artifact in artifacts
        if isinstance(artifact, dict) and artifact.get("name") == "workflow_policy_summary"
    ]
    if not summary_entries:
        return ["artifact_index must reference workflow_policy_summary"]

    summary_entry = summary_entries[0]
    if summary_entry.get("artifact_type") != WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE:
        errors.append(f"workflow_policy_summary artifact_type must be {WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE}")
    if summary_entry.get("path") != SUMMARY_ARTIFACT_FILENAME:
        errors.append(f"workflow_policy_summary path must be {SUMMARY_ARTIFACT_FILENAME}")
    if summary_entry.get("required") is not True:
        errors.append("workflow_policy_summary required must be true")

    referenced_path = artifact_dir / str(summary_entry.get("path", ""))
    if not referenced_path.exists():
        errors.append(f"referenced artifact path does not exist: {referenced_path}")

    return errors


def _resolve_artifact_dir(artifact_dir: Path | None) -> tuple[Path, bool]:
    if artifact_dir is not None:
        return Path(artifact_dir), False
    return Path(tempfile.mkdtemp(prefix="agent-taskflow-workflow-policy-pow-")), True


def _print_summary(
    *,
    policy_path: Path,
    artifact_dir: Path,
    index_path: Path,
    summary_path: Path,
    status: str,
    kept: bool,
    errors: Sequence[str] = (),
) -> None:
    print("Workflow policy proof-of-work package smoke")
    print(f"policy path: {policy_path}")
    print(f"artifact dir: {artifact_dir}")
    print(f"artifact index: {index_path}")
    print(f"summary artifact: {summary_path}")
    print(f"status: {status}")
    print(f"artifacts kept: {'yes' if kept else 'no'}")
    print("required artifacts:")
    print("- workflow_policy_summary")
    print("required summary fields:")
    for field in REQUIRED_SUMMARY_FIELDS:
        print(f"- {field}")

    if errors:
        print("errors:")
        for error in errors:
            print(f"- {error}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    policy_path = Path(args.policy)
    artifact_dir, created_temp_dir = _resolve_artifact_dir(args.artifact_dir)
    keep_artifacts = bool(args.keep_artifacts or args.keep_output or args.artifact_dir)
    index_path = artifact_dir / ARTIFACT_INDEX_FILENAME
    summary_path = artifact_dir / SUMMARY_ARTIFACT_FILENAME

    try:
        policy = load_workflow_policy(policy_path)
        result = policy.validate()
        if not result.passed:
            _print_summary(
                policy_path=policy.source_path,
                artifact_dir=artifact_dir,
                index_path=index_path,
                summary_path=summary_path,
                status="failed",
                kept=keep_artifacts,
                errors=result.errors,
            )
            return 1

        artifact_dir.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(build_artifact(policy), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        index_path.write_text(
            json.dumps(build_artifact_index(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        index = json.loads(index_path.read_text(encoding="utf-8"))
        errors = []
        errors.extend(verify_summary_artifact(summary))
        errors.extend(verify_artifact_index(index, artifact_dir))
        if errors:
            _print_summary(
                policy_path=policy.source_path,
                artifact_dir=artifact_dir,
                index_path=index_path,
                summary_path=summary_path,
                status="failed",
                kept=keep_artifacts,
                errors=errors,
            )
            return 1

        _print_summary(
            policy_path=policy.source_path,
            artifact_dir=artifact_dir,
            index_path=index_path,
            summary_path=summary_path,
            status="passed",
            kept=keep_artifacts,
        )
        return 0
    except (FileNotFoundError, ValueError, json.JSONDecodeError, OSError) as exc:
        _print_summary(
            policy_path=policy_path,
            artifact_dir=artifact_dir,
            index_path=index_path,
            summary_path=summary_path,
            status="failed",
            kept=keep_artifacts,
            errors=(str(exc),),
        )
        return 1
    finally:
        if created_temp_dir and not keep_artifacts:
            shutil.rmtree(artifact_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
