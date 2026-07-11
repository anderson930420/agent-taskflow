#!/usr/bin/env python3
"""Install and optionally reap Attempt-scoped runtime resources."""

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

from agent_taskflow.attempt_resources import AttemptResourceManager  # noqa: E402
from agent_taskflow.attempt_resources_schema import (  # noqa: E402
    ATTEMPT_RESOURCES_MIGRATION,
    migrate_attempt_resources,
)
from agent_taskflow.models import require_absolute_path  # noqa: E402
from agent_taskflow.runtime_admission import RuntimeAdmissionStore  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument(
        "--reap",
        action="store_true",
        help="Expire stale runtime leases, then reap stale lock/PID markers.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    db_path = require_absolute_path(args.db_path.expanduser(), "db_path")
    migrate_attempt_resources(db_path)
    expired_attempt_ids: list[str] = []
    resource_reap = {
        "reaped_attempt_ids": [],
        "blocked_live_pid_attempt_ids": [],
    }
    if args.reap:
        expired_attempt_ids = RuntimeAdmissionStore(db_path).expire_stale_leases()
        resource_reap = AttemptResourceManager(db_path).reap_stale_resources()

    with sqlite3.connect(db_path) as conn:
        migration_recorded = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?",
            (ATTEMPT_RESOURCES_MIGRATION,),
        ).fetchone() is not None
        status_rows = conn.execute(
            """
            SELECT status, COUNT(*)
            FROM attempt_resources
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
        active_attempt_resources = conn.execute(
            """
            SELECT COUNT(*)
            FROM attempt_resources
            WHERE status IN ('allocated', 'active', 'reap_blocked_live_pid')
            """
        ).fetchone()[0]

    print(
        json.dumps(
            {
                "db_path": str(db_path),
                "migration": ATTEMPT_RESOURCES_MIGRATION,
                "migration_recorded": migration_recorded,
                "attempt_resource_status_counts": dict(status_rows),
                "active_attempt_resources": active_attempt_resources,
                "expired_attempt_ids_reaped": expired_attempt_ids,
                **resource_reap,
                "historical_worktrees_deleted": False,
                "historical_artifacts_deleted": False,
                "historical_branches_deleted": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
