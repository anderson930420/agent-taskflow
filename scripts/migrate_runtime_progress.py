#!/usr/bin/env python3
"""Install the V1 Step 3 attempt-scoped runtime progress tables (SPEC 14).

Additive and idempotent. It creates ``attempt_progress`` and
``attempt_observed_steps`` only. It does not create, rename, backfill, or write
any Ticket lifecycle column, and in particular none of the SPEC 32.1 PR fields
-- the Step 2 watcher is their sole writer.
"""

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
from agent_taskflow.realtime_projection import PR_FIELD_NAMES  # noqa: E402
from agent_taskflow.runtime_progress_schema import (  # noqa: E402
    RUNTIME_PROGRESS_MIGRATION,
    migrate_runtime_progress,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    return parser.parse_args()


def _task_columns(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}


def main() -> int:
    args = _parse_args()
    db_path = require_absolute_path(args.db_path.expanduser(), "db_path")

    # Proof, not assertion: capture the Ticket columns on both sides of the
    # migration so the operator can see it added none of them.
    columns_before = _task_columns(db_path)
    migrate_runtime_progress(db_path)

    with sqlite3.connect(db_path) as conn:
        migration_recorded = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?",
            (RUNTIME_PROGRESS_MIGRATION,),
        ).fetchone() is not None
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        progress_rows = conn.execute(
            "SELECT COUNT(*) FROM attempt_progress"
        ).fetchone()[0]
        step_rows = conn.execute(
            "SELECT COUNT(*) FROM attempt_observed_steps"
        ).fetchone()[0]

    print(
        json.dumps(
            {
                "db_path": str(db_path),
                "migration": RUNTIME_PROGRESS_MIGRATION,
                "migration_recorded": migration_recorded,
                "attempt_progress_installed": "attempt_progress" in tables,
                "attempt_observed_steps_installed": (
                    "attempt_observed_steps" in tables
                ),
                "attempt_progress_rows": progress_rows,
                "attempt_observed_step_rows": step_rows,
                "task_columns_added_by_this_migration": sorted(
                    task_columns - columns_before
                ),
                "pr_columns_present": sorted(
                    set(PR_FIELD_NAMES) & task_columns
                ),
                "pr_columns_created_by_this_migration": sorted(
                    set(PR_FIELD_NAMES) & (task_columns - columns_before)
                ),
                "pr_fields_written": False,
                "github_contacted": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
