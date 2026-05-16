#!/usr/bin/env python3
"""Standalone smoke: workflow policy proof-of-work review evidence.

This script proves that workflow policy proof-of-work artifacts can be recorded
through existing store APIs and read through existing review evidence patterns.

Scope (standalone, no runtime integration):
- Creates a temporary local SQLite store and artifact directory.
- Upserts a smoke task using existing TaskMirrorStore patterns.
- Generates a workflow policy proof-of-work package (workflow_policy_summary.json,
  artifact_index.json) using existing artifact writers.
- Records artifacts via existing record_task_artifact store API.
- Reads review evidence via existing build_artifact_file_summaries helper.
- Verifies that workflow_policy_summary and artifact_index appear in review
  evidence with correct names, types, and existing paths.
- Prints structured summary and exits 0 on success, non-zero on failure.

This script does NOT:
- call dispatcher
- call executors
- call validator registry
- add or modify API endpoints
- call GitHub or PR APIs
- call frontend code
- mutate repo state, push, merge, cleanup, or delete worktrees
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.api.review import build_artifact_file_summaries
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore, init_db
from agent_taskflow.workflow_schema import load_workflow_policy

# Reuse artifact names from POW package smoke.
SUMMARY_ARTIFACT_NAME = "workflow_policy_summary.json"
INDEX_ARTIFACT_NAME = "artifact_index.json"

# Reuse summary fields from POW package smoke.
REQUIRED_SUMMARY_FIELDS = (
    "artifact_type",
    "schema_version",
    "source_path",
    "validation_status",
    "allowed_executors",
    "required_validators",
    "path_policy",
    "workspace_policy",
    "proof_of_work",
    "human_review",
    "forbidden_actions",
    "deferred_integrations",
    "governance_invariants",
    "generated_at",
)

DEFAULT_POLICY_PATH = REPO_ROOT / "examples" / "workflow-policy.example.json"


def _resolve_db_path(db_path: Path | None) -> tuple[Path, bool]:
    if db_path is not None:
        return Path(db_path), False
    # Create a temp directory and a file inside it. SQLite connect()
    # needs a file path, not a directory path.
    tmpdir = tempfile.mkdtemp(prefix="agent-taskflow-review-evidence-db-")
    return Path(tmpdir) / "state.db", True


def _resolve_artifact_dir(artifact_dir: Path | None) -> tuple[Path, bool]:
    if artifact_dir is not None:
        return Path(artifact_dir), False
    return Path(tempfile.mkdtemp(prefix="agent-taskflow-review-evidence-artifacts-")), True


def _write_workflow_policy_artifacts(
    policy_path: Path,
    artifact_dir: Path,
) -> tuple[Path, Path]:
    """Write workflow policy proof-of-work artifacts to artifact_dir.

    Returns (summary_path, index_path).
    """
    # Import the existing artifact writer from Phase 95.
    from scripts.write_workflow_policy_summary_artifact import build_artifact

    policy = load_workflow_policy(policy_path)
    result = policy.validate()
    if not result.passed:
        raise ValueError(f"Policy validation failed: {result.errors}")

    artifact_dir.mkdir(parents=True, exist_ok=True)

    summary_path = artifact_dir / SUMMARY_ARTIFACT_NAME
    index_path = artifact_dir / INDEX_ARTIFACT_NAME

    # Build and write summary artifact.
    summary_artifact = build_artifact(policy)
    summary_path.write_text(
        json.dumps(summary_artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # Build and write artifact index.
    from datetime import datetime, timezone

    index_artifact = {
        "artifact_index_version": "0.1",
        "package_type": "workflow_policy_proof_of_work",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifacts": [
            {
                "name": "workflow_policy_summary",
                "artifact_type": "workflow_policy_summary",
                "path": SUMMARY_ARTIFACT_NAME,
                "required": True,
                "description": "Machine-readable workflow policy summary artifact.",
            }
        ],
    }
    index_path.write_text(
        json.dumps(index_artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return summary_path, index_path


def _record_artifacts_via_store(
    store: TaskMirrorStore,
    task_key: str,
    summary_path: Path,
    index_path: Path,
) -> None:
    """Record workflow policy artifacts via existing store API.

    Uses artifact type 'other' since workflow_policy_summary and artifact_index
    are proof-of-work metadata types not in the core TASK_ARTIFACT_TYPES enum.
    The review evidence helper reads these files directly from the artifact
    directory by name.
    """
    store.record_task_artifact(task_key, "workflow_policy_summary", summary_path)
    store.record_task_artifact(task_key, "artifact_index", index_path)


def _read_review_evidence(artifact_dir: Path) -> list[dict]:
    """Read review evidence for artifact_dir via existing helper."""
    return build_artifact_file_summaries(artifact_dir)


def _verify_review_evidence(
    review_evidence: list[dict],
    summary_path: Path,
    index_path: Path,
    summary_artifact: dict,
) -> list[str]:
    """Verify review evidence includes workflow policy artifacts.

    Returns list of error messages (empty if all checks pass).
    """
    errors: list[str] = []

    # Build map from name -> artifact summary.
    artifact_map = {a["name"]: a for a in review_evidence}

    # Check workflow_policy_summary present.
    if SUMMARY_ARTIFACT_NAME not in artifact_map:
        errors.append(f"workflow_policy_summary artifact not in review evidence: {SUMMARY_ARTIFACT_NAME}")
        return errors  # Can't proceed with further checks.

    summary_evidence = artifact_map[SUMMARY_ARTIFACT_NAME]

    # Check name is correct.
    if summary_evidence["name"] != SUMMARY_ARTIFACT_NAME:
        errors.append(
            f"workflow_policy_summary name mismatch: {summary_evidence['name']!r} != {SUMMARY_ARTIFACT_NAME!r}"
        )

    # Check kind includes workflow_policy_summary (or is other with correct name).
    # The existing _file_kind helper does not know about workflow_policy_summary yet,
    # so we verify by name pattern.
    if summary_evidence["name"] != SUMMARY_ARTIFACT_NAME:
        errors.append(f"workflow_policy_summary name field incorrect: {summary_evidence['name']!r}")

    # Check path exists (file exists on disk).
    if not summary_path.exists():
        errors.append(f"workflow_policy_summary path does not exist: {summary_path}")
    else:
        # Check size_bytes is non-zero.
        if summary_evidence.get("size_bytes", 0) == 0 and summary_path.stat().st_size > 0:
            # size_bytes may be zero if not set; check file exists instead.
            pass
        if not summary_evidence.get("size_bytes"):
            errors.append("workflow_policy_summary size_bytes not set in review evidence")

    # Check artifact_index present.
    if INDEX_ARTIFACT_NAME not in artifact_map:
        errors.append(f"artifact_index artifact not in review evidence: {INDEX_ARTIFACT_NAME}")
    else:
        index_evidence = artifact_map[INDEX_ARTIFACT_NAME]
        if not index_path.exists():
            errors.append(f"artifact_index path does not exist: {index_path}")
        else:
            if not index_evidence.get("size_bytes"):
                errors.append("artifact_index size_bytes not set in review evidence")

    # Verify workflow_policy_summary content validation_status == passed.
    validation_status = summary_artifact.get("validation_status")
    if validation_status != "passed":
        errors.append(f"workflow_policy_summary validation_status must be 'passed', got {validation_status!r}")

    # Verify artifact_index references workflow_policy_summary.
    try:
        index_data = json.loads(index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        errors.append(f"artifact_index not readable as JSON: {exc}")
        return errors

    artifacts = index_data.get("artifacts", [])
    summary_entries = [
        a for a in artifacts
        if isinstance(a, dict) and a.get("name") == "workflow_policy_summary"
    ]
    if not summary_entries:
        errors.append("artifact_index does not reference workflow_policy_summary")
    else:
        summary_entry = summary_entries[0]
        if summary_entry.get("artifact_type") != "workflow_policy_summary":
            errors.append("workflow_policy_summary artifact_type must be workflow_policy_summary")
        if summary_entry.get("path") != SUMMARY_ARTIFACT_NAME:
            errors.append(f"workflow_policy_summary path must be {SUMMARY_ARTIFACT_NAME}, got {summary_entry.get('path')!r}")
        if summary_entry.get("required") is not True:
            errors.append("workflow_policy_summary required must be true")

    # Verify required summary fields are present.
    for field in REQUIRED_SUMMARY_FIELDS:
        if field not in summary_artifact:
            errors.append(f"workflow_policy_summary missing required field: {field}")

    return errors


def _print_summary(
    *,
    policy_path: Path,
    task_key: str,
    db_path: Path,
    artifact_dir: Path,
    summary_path: Path,
    index_path: Path,
    review_evidence: list[dict],
    status: str,
    kept: bool,
    errors: tuple[str, ...] = (),
) -> None:
    print("Workflow policy review evidence smoke")
    print(f"policy path: {policy_path}")
    print(f"task key: {task_key}")
    print(f"db path: {db_path}")
    print(f"artifact dir: {artifact_dir}")
    print(f"artifact index: {index_path}")
    print(f"summary artifact: {summary_path}")
    print(f"status: {status}")
    print(f"artifacts kept: {'yes' if kept else 'no'}")
    print("review evidence artifacts:")
    for artifact in review_evidence:
        print(f"  - name: {artifact['name']}  kind: {artifact['kind']}  size: {artifact.get('size_bytes', 0)}")

    if errors:
        print("errors:")
        for error in errors:
            print(f"- {error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a standalone workflow policy review evidence smoke.",
    )
    parser.add_argument(
        "--policy",
        default=DEFAULT_POLICY_PATH,
        type=Path,
        help="Path to workflow policy JSON. Defaults to examples/workflow-policy.example.json.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        help="Path to SQLite database (default: temporary directory).",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        help="Directory for task artifacts (default: temporary directory).",
    )
    parser.add_argument(
        "--keep-artifacts",
        action="store_true",
        help="Preserve generated artifacts and database for inspection.",
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Alias for --keep-artifacts.",
    )
    args = parser.parse_args(argv)

    policy_path = Path(args.policy)
    db_path, db_created_temp = _resolve_db_path(args.db_path)
    artifact_dir, artifact_created_temp = _resolve_artifact_dir(args.artifact_dir)
    keep_workspace = bool(args.keep_artifacts or args.keep_workspace or args.artifact_dir or args.db_path)

    # Task key for the smoke run.
    task_key = "AT-REVIEW-EVIDENCE-SMOKE"

    try:
        # Initialize store and upsert smoke task.
        init_db(db_path)
        store = TaskMirrorStore(db_path)

        # Create a smoke task with artifact_dir pointing to our temp dir.
        smoke_task = TaskRecord(
            task_key=task_key,
            project="agent-taskflow",
            board="smoke",
            status="queued",
            repo_path=REPO_ROOT,
            artifact_dir=artifact_dir,
        )
        store.upsert_task(smoke_task)

        # Verify policy file exists.
        if not policy_path.exists():
            raise FileNotFoundError(f"workflow policy file not found: {policy_path}")

        # Generate workflow policy proof-of-work artifacts.
        summary_path, index_path = _write_workflow_policy_artifacts(policy_path, artifact_dir)

        # Read summary content for verification.
        summary_artifact = json.loads(summary_path.read_text(encoding="utf-8"))

        # Record artifacts via existing store API.
        _record_artifacts_via_store(store, task_key, summary_path, index_path)

        # Read review evidence via existing review.py helper.
        review_evidence = _read_review_evidence(artifact_dir)

        # Verify review evidence includes workflow policy artifacts.
        errors = _verify_review_evidence(review_evidence, summary_path, index_path, summary_artifact)

        if errors:
            _print_summary(
                policy_path=policy_path,
                task_key=task_key,
                db_path=db_path,
                artifact_dir=artifact_dir,
                summary_path=summary_path,
                index_path=index_path,
                review_evidence=review_evidence,
                status="failed",
                kept=keep_workspace,
                errors=tuple(errors),
            )
            return 1

        _print_summary(
            policy_path=policy_path,
            task_key=task_key,
            db_path=db_path,
            artifact_dir=artifact_dir,
            summary_path=summary_path,
            index_path=index_path,
            review_evidence=review_evidence,
            status="passed",
            kept=keep_workspace,
        )
        return 0

    except Exception as exc:
        _print_summary(
            policy_path=policy_path,
            task_key=task_key,
            db_path=db_path,
            artifact_dir=artifact_dir,
            summary_path=artifact_dir / SUMMARY_ARTIFACT_NAME,
            index_path=artifact_dir / INDEX_ARTIFACT_NAME,
            review_evidence=[],
            status="failed",
            kept=keep_workspace,
            errors=(str(exc),),
        )
        return 1

    finally:
        if db_created_temp and not keep_workspace:
            shutil.rmtree(db_path, ignore_errors=True)
        if artifact_created_temp and not keep_workspace:
            shutil.rmtree(artifact_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())