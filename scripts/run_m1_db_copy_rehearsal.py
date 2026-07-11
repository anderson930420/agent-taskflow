#!/usr/bin/env python3
"""Run the M1-A production DB-copy migration and rollback rehearsal."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import types

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

from agent_taskflow.m1_db_copy_rehearsal import (  # noqa: E402
    EVIDENCE_FILENAME,
    run_m1_db_copy_rehearsal,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--confirm-production-copy-rehearsal",
        action="store_true",
        help=(
            "Confirm that the source DB must be opened read-only and all migration/"
            "restore work must stay inside the fresh output directory."
        ),
    )
    return parser.parse_args()


def _require_absolute(path: Path, name: str) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise ValueError(f"{name} must be an absolute path: {expanded}")
    return expanded.resolve()


def main() -> int:
    args = _parse_args()
    if not args.confirm_production_copy_rehearsal:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "missing --confirm-production-copy-rehearsal",
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
        repo = _require_absolute(args.repo_root, "repo_root")
        evidence = run_m1_db_copy_rehearsal(
            source_db=source,
            output_dir=output,
            actor=args.actor,
            repo_root=repo,
        )
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
                "rehearsal_id": evidence["rehearsal_id"],
                "source_db_path": evidence["source_db_path"],
                "source_connection_mode": evidence["source_connection_mode"],
                "source_quiescent": evidence["source_quiescent"],
                "migration_dry_run": evidence["migration_dry_run"],
                "migration_idempotent": evidence["migration_idempotent"],
                "integrity_check": evidence["integrity_check"],
                "foreign_key_check": evidence["foreign_key_check"],
                "rollback_rehearsal": evidence["rollback_rehearsal"],
                "evidence_path": str(output / EVIDENCE_FILENAME),
                "artifacts": evidence["artifacts"],
                "production_database_modified": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
