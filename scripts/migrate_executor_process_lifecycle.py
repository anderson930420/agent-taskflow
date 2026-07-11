#!/usr/bin/env python3
"""Install PR-7 managed executor process-group persistence."""

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

from agent_taskflow.executor_process_schema import (  # noqa: E402
    EXECUTOR_PROCESS_MIGRATION,
    migrate_executor_process_lifecycle,
)
from agent_taskflow.models import require_absolute_path  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    db_path = require_absolute_path(args.db_path.expanduser(), "db_path")
    migrate_executor_process_lifecycle(db_path)
    with sqlite3.connect(db_path) as conn:
        migration_recorded = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?",
            (EXECUTOR_PROCESS_MIGRATION,),
        ).fetchone() is not None
        state_counts = dict(
            conn.execute(
                """
                SELECT state, COUNT(*) FROM executor_processes
                GROUP BY state ORDER BY state
                """
            ).fetchall()
        )
        active_processes = conn.execute(
            """
            SELECT COUNT(*) FROM executor_processes
            WHERE state IN ('allocated', 'running', 'term_sent', 'kill_sent')
            """
        ).fetchone()[0]
        triggers = [
            row[0]
            for row in conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'trigger' AND name LIKE 'executor_process%'
                ORDER BY name
                """
            ).fetchall()
        ]
    print(
        json.dumps(
            {
                "db_path": str(db_path),
                "migration": EXECUTOR_PROCESS_MIGRATION,
                "migration_recorded": migration_recorded,
                "active_executor_processes": active_processes,
                "executor_process_state_counts": state_counts,
                "executor_process_triggers": triggers,
                "launch_isolation": {
                    "shell": False,
                    "start_new_session": True,
                    "close_fds": True,
                    "exact_attempt_paths": True,
                    "network_isolation": False,
                },
                "termination": {
                    "signals": ["SIGTERM", "SIGKILL"],
                    "identity_verification": "linux_proc_pid_pgid_session_start_ticks",
                    "verified_exit_required": True,
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
