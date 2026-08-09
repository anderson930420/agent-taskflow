#!/usr/bin/env python3
"""Install M1-D project admission and task-class governance control scopes."""

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
from agent_taskflow.project_class_control_schema import (  # noqa: E402
    PROJECT_CLASS_CONTROLS_MIGRATION,
    PROJECT_CLASS_CONTROL_SCOPES,
    migrate_project_class_controls,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    db_path = require_absolute_path(args.db_path.expanduser(), "db_path")
    migrate_project_class_controls(db_path)
    with sqlite3.connect(db_path) as conn:
        migration_recorded = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?",
            (PROJECT_CLASS_CONTROLS_MIGRATION,),
        ).fetchone() is not None
        schema_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'runtime_controls'"
        ).fetchone()[0]
        control_count = conn.execute(
            "SELECT COUNT(*) FROM runtime_controls"
        ).fetchone()[0]
        event_count = conn.execute(
            "SELECT COUNT(*) FROM runtime_control_events"
        ).fetchone()[0]
    print(
        json.dumps(
            {
                "db_path": str(db_path),
                "migration": PROJECT_CLASS_CONTROLS_MIGRATION,
                "migration_recorded": migration_recorded,
                "supported_scopes": list(PROJECT_CLASS_CONTROL_SCOPES),
                "scope_schema_verified": all(
                    f"'{scope}'" in schema_sql.lower()
                    for scope in PROJECT_CLASS_CONTROL_SCOPES
                ),
                "runtime_control_count": control_count,
                "runtime_control_event_count": event_count,
                "project_pause_semantics": "deny_new_admission_only",
                "task_class_semantics": "governance_eligibility_only",
                "actual_auto_merge_enabled": False,
                "os_signals_sent": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
