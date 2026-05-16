#!/usr/bin/env python3
"""Standalone workflow policy review evidence report command.

Reads or generates workflow policy proof-of-work artifacts and outputs a
read-only JSON report using existing review evidence helpers.

This is a standalone API-free reporting command. It does not:
- call dispatcher, executor, validator registry, API, or Mission Control UI
- create approval/merge/push/cleanup/delete artifacts
- modify artifact file contents
- add endpoints or UI elements
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agent_taskflow.api.review import build_artifact_file_summaries
from agent_taskflow.workflow_policy_artifacts import (
    WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME,
    WORKFLOW_POLICY_SUMMARY_FILENAME,
    WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE,
    WORKFLOW_POLICY_ARTIFACT_INDEX_TYPE,
    WORKFLOW_POLICY_PACKAGE_TYPE,
    WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION,
)

DEFAULT_POLICY_PATH = _REPO_ROOT / "examples" / "workflow-policy.example.json"


# ----------------------------------------------------------------------
# Helpers from existing scripts (reused without modification)
# ----------------------------------------------------------------------


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "run_workflow_policy_pow_package_smoke",
        _REPO_ROOT / "scripts" / "run_workflow_policy_pow_package_smoke.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_write_module():
    spec = importlib.util.spec_from_file_location(
        "write_workflow_policy_summary_artifact",
        _REPO_ROOT / "scripts" / "write_workflow_policy_summary_artifact.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------------
# Report builder
# ----------------------------------------------------------------------


def _build_report(
    *,
    artifact_dir: Path,
    source_policy_path: Path,
    validation_status: str,
    report_output_path: Path | None = None,
) -> dict[str, Any]:
    """Build the report dict from an artifact directory."""
    index_path = artifact_dir / WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME
    summary_path = artifact_dir / WORKFLOW_POLICY_SUMMARY_FILENAME

    # Build artifact file summaries using the read-only helper.
    artifact_summaries = build_artifact_file_summaries(artifact_dir)
    artifact_entries = []
    for entry in artifact_summaries:
        artifact_entries.append({
            "name": entry["name"],
            "kind": entry["kind"],
            "size_bytes": entry["size_bytes"],
            "is_validator_log": entry["is_validator_log"],
            "is_executor_log": entry["is_executor_log"],
            "is_mission_contract": entry["is_mission_contract"],
        })

    # Load workflow_policy_summary content.
    workflow_policy_summary: dict[str, Any] = {}
    if summary_path.exists():
        workflow_policy_summary = json.loads(summary_path.read_text(encoding="utf-8"))

    # Load artifact_index content.
    artifact_index: dict[str, Any] = {}
    if index_path.exists():
        artifact_index = json.loads(index_path.read_text(encoding="utf-8"))

    report: dict[str, Any] = {
        "report_type": "workflow_policy_review_evidence",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifact_dir": str(artifact_dir),
        "source_policy_path": str(source_policy_path),
        "validation_status": validation_status,
        "artifacts": artifact_entries,
        "workflow_policy_summary": workflow_policy_summary,
        "artifact_index": artifact_index,
    }

    if report_output_path is not None:
        report_output_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    return report


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate or read a workflow policy proof-of-work artifact package "
            "and output a read-only JSON review evidence report."
        ),
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=DEFAULT_POLICY_PATH,
        help="Path to workflow policy JSON. Defaults to examples/workflow-policy.example.json.",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        help="Directory where artifacts are written or read.",
    )
    parser.add_argument(
        "--no-generate",
        action="store_true",
        help="Read existing artifacts from --artifact-dir instead of generating.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the JSON report to this path. Defaults to stdout.",
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


def _resolve_artifact_dir(
    requested_dir: Path | None,
    keep_artifacts: bool,
) -> tuple[Path, bool]:
    """Resolve artifact dir, returning (path, created_temp_dir)."""
    if requested_dir is not None:
        return Path(requested_dir), False
    return Path(tempfile.mkdtemp(prefix="agent-taskflow-wp-review-")), True


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    policy_path = Path(args.policy)
    artifact_dir, created_temp_dir = _resolve_artifact_dir(
        args.artifact_dir, bool(args.keep_artifacts or args.keep_output)
    )
    keep_artifacts = bool(args.keep_artifacts or args.keep_output or args.artifact_dir)
    output_path = Path(args.output) if args.output else None

    try:
        if args.no_generate:
            # Mode B: read existing artifacts.
            index_path = artifact_dir / WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME
            summary_path = artifact_dir / WORKFLOW_POLICY_SUMMARY_FILENAME
            if not index_path.exists():
                print(f"artifact index not found: {index_path}", file=sys.stderr)
                return 1
            if not summary_path.exists():
                print(f"workflow policy summary not found: {summary_path}", file=sys.stderr)
                return 1
            # Try to determine validation_status from summary.
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            validation_status = summary.get("validation_status", "unknown")
            source_policy_path = summary.get("source_path", str(policy_path))
        else:
            # Mode A: generate artifacts.
            smoke_module = _load_smoke_module()
            write_module = _load_write_module()

            artifact_dir.mkdir(parents=True, exist_ok=True)

            # Load and validate policy.
            policy = smoke_module.load_workflow_policy(policy_path)
            result = policy.validate()
            if not result.passed:
                for error in result.errors:
                    print(f"error: {error}", file=sys.stderr)
                return 1

            validation_status = "passed"
            source_policy_path = str(policy.source_path)

            # Generate summary artifact.
            summary_path = artifact_dir / WORKFLOW_POLICY_SUMMARY_FILENAME
            summary_artifact = write_module.build_artifact(policy)
            summary_path.write_text(
                json.dumps(summary_artifact, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            # Generate artifact index.
            index_path = artifact_dir / WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME
            index_artifact = smoke_module.build_artifact_index()
            index_path.write_text(
                json.dumps(index_artifact, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

        # Build and output report.
        report = _build_report(
            artifact_dir=artifact_dir,
            source_policy_path=Path(source_policy_path),
            validation_status=validation_status,
            report_output_path=output_path,
        )

        if output_path is None:
            print(json.dumps(report, indent=2, sort_keys=True))

        return 0

    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if created_temp_dir and not keep_artifacts:
            shutil.rmtree(artifact_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())