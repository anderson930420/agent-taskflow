#!/usr/bin/env python3
"""Install PR-9 validator managed process-group coverage."""

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

from agent_taskflow.models import require_absolute_path  # noqa: E402
from agent_taskflow.validator_process_schema import (  # noqa: E402
    VALIDATOR_PROCESS_MIGRATION,
    migrate_validator_process_lifecycle,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    db_path = require_absolute_path(args.db_path.expanduser(), "db_path")
    migrate_validator_process_lifecycle(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        migration_recorded = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?",
            (VALIDATOR_PROCESS_MIGRATION,),
        ).fetchone() is not None
        columns = [
            row["name"] for row in conn.execute("PRAGMA table_info(executor_processes)")
        ]
        role_state_counts = {
            f"{row['process_role']}:{row['state']}": row["count"]
            for row in conn.execute(
                """
                SELECT process_role, state, COUNT(*) AS count
                FROM executor_processes
                GROUP BY process_role, state
                ORDER BY process_role, state
                """
            )
        }
        active_validator_processes = conn.execute(
            """
            SELECT COUNT(*) FROM executor_processes
            WHERE process_role = 'validator'
              AND state IN ('allocated', 'running', 'term_sent', 'kill_sent')
            """
        ).fetchone()[0]
    print(
        json.dumps(
            {
                "db_path": str(db_path),
                "migration": VALIDATOR_PROCESS_MIGRATION,
                "migration_recorded": migration_recorded,
                "process_role_column_installed": "process_role" in columns,
                "active_validator_processes": active_validator_processes,
                "runtime_process_role_state_counts": role_state_counts,
                "managed_validator_commands": [
                    "pytest",
                    "openspec",
                    "lint",
                    "typecheck",
                    "changed-files:git-status",
                ],
                "launch_isolation": {
                    "shell": False,
                    "start_new_session": True,
                    "close_fds": True,
                    "exact_attempt_paths": True,
                },
                "termination": {
                    "signals": ["SIGTERM", "SIGKILL"],
                    "verified_exit_required": True,
                    "shared_registry": "executor_processes",
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
