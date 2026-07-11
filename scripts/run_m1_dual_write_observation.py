#!/usr/bin/env python3
"""Run the M1-B dual-write observation on a production database copy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import types
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "agent_taskflow"


def _bootstrap_source_package_without_runtime_imports() -> None:
    if "agent_taskflow" in sys.modules:
        return
    package = types.ModuleType("agent_taskflow")
    package.__file__ = str(PACKAGE_ROOT / "__init__.py")
    package.__package__ = "agent_taskflow"
    package.__path__ = [str(PACKAGE_ROOT)]
    sys.modules["agent_taskflow"] = package


_bootstrap_source_package_without_runtime_imports()

from agent_taskflow.atomic_write import atomic_write_json  # noqa: E402
from agent_taskflow.m1_db_copy_rehearsal import (  # noqa: E402
    EVIDENCE_FILENAME as M1A_EVIDENCE_FILENAME,
    M1_DB_COPY_REHEARSAL_SCHEMA_VERSION,
)
from agent_taskflow.m1_dual_write_observation import (  # noqa: E402
    DEFAULT_WORKLOAD_TASKS,
    EVIDENCE_FILENAME,
    run_m1_dual_write_observation,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prior-evidence-dir", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--workload-tasks",
        type=int,
        default=DEFAULT_WORKLOAD_TASKS,
        help="Number of disposable observation-copy tasks (default: 3).",
    )
    parser.add_argument(
        "--confirm-production-copy-observation",
        action="store_true",
        help=(
            "Confirm that production is read-only and all disposable tasks are "
            "created only in the fresh observation copy."
        ),
    )
    return parser.parse_args()


def _require_absolute(path: Path, name: str) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise ValueError(f"{name} must be an absolute path: {expanded}")
    return expanded.resolve()


def _load_valid_m1a_evidence(prior_dir: Path) -> dict[str, Any]:
    source = prior_dir / M1A_EVIDENCE_FILENAME
    if not source.is_file():
        raise FileNotFoundError(f"M1-A evidence does not exist: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"M1-A evidence is invalid JSON: {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("M1-A evidence top-level JSON must be an object")
    errors: list[str] = []
    if payload.get("schema_version") != M1_DB_COPY_REHEARSAL_SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {M1_DB_COPY_REHEARSAL_SCHEMA_VERSION}"
        )
    for field in ("migration_dry_run", "integrity_check", "rollback_rehearsal"):
        if payload.get(field) is not True:
            errors.append(f"{field} must be true")
    if payload.get("production_database_modified") is not False:
        errors.append("production_database_modified must be false")
    if errors:
        raise ValueError("M1-A evidence is not passing: " + "; ".join(errors))
    return payload


def main() -> int:
    args = _parse_args()
    if not args.confirm_production_copy_observation:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "missing --confirm-production-copy-observation",
                    "production_database_modified": False,
                },
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    try:
        source = _require_absolute(args.db_path, "db_path")
        output = _require_absolute(args.output_dir, "output_dir")
        prior = _require_absolute(args.prior_evidence_dir, "prior_evidence_dir")
        repo = _require_absolute(args.repo_root, "repo_root")
        m1a_evidence = _load_valid_m1a_evidence(prior)
        evidence = run_m1_dual_write_observation(
            source_db=source,
            output_dir=output,
            actor=args.actor,
            repo_root=repo,
            workload_tasks=args.workload_tasks,
        )
        carried_path = output / M1A_EVIDENCE_FILENAME
        atomic_write_json(carried_path, m1a_evidence, indent=2, sort_keys=True)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "production_database_modified": False,
                    "output_dir": str(args.output_dir.expanduser()),
                },
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {
                "ok": True,
                "schema_version": evidence["schema_version"],
                "observation_id": evidence["observation_id"],
                "observation_scope": evidence["observation_scope"],
                "observation_window_started_at": evidence[
                    "observation_window_started_at"
                ],
                "observation_window_ended_at": evidence[
                    "observation_window_ended_at"
                ],
                "workload_task_count": evidence["workload_task_count"],
                "records_compared": evidence["records_compared"],
                "mismatch_count": evidence["mismatch_count"],
                "silent_failure_count": evidence["silent_failure_count"],
                "source_connection_mode": evidence["source_connection_mode"],
                "source_quiescent": evidence["source_quiescent"],
                "production_database_modified": False,
                "evidence_path": str(output / EVIDENCE_FILENAME),
                "carried_m1a_evidence_path": str(carried_path),
                "artifacts": evidence["artifacts"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
