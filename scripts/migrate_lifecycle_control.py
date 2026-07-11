#!/usr/bin/env python3
"""Install PR-6 lifecycle transition and runtime control persistence."""

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

from agent_taskflow.lifecycle_control import RuntimeControlStore  # noqa: E402
from agent_taskflow.lifecycle_control_schema import (  # noqa: E402
    LIFECYCLE_CONTROL_MIGRATION,
    migrate_lifecycle_control,
)
from agent_taskflow.models import require_absolute_path  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    db_path = require_absolute_path(args.db_path.expanduser(), "db_path")
    migrate_lifecycle_control(db_path)
    controls = RuntimeControlStore(db_path)
    effective = controls.effective_control()
    with sqlite3.connect(db_path) as conn:
        migration_recorded = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?",
            (LIFECYCLE_CONTROL_MIGRATION,),
        ).fetchone() is not None
        transition_count = conn.execute(
            "SELECT COUNT(*) FROM lifecycle_allowed_transitions"
        ).fetchone()[0]
        trigger_installed = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'trigger' AND name = 'lifecycle_attempt_transition_guard'
            """
        ).fetchone() is not None
        control_counts = dict(
            conn.execute(
                """
                SELECT mode, COUNT(*) FROM runtime_controls
                GROUP BY mode ORDER BY mode
                """
            ).fetchall()
        )
    print(
        json.dumps(
            {
                "db_path": str(db_path),
                "migration": LIFECYCLE_CONTROL_MIGRATION,
                "migration_recorded": migration_recorded,
                "attempt_transition_count": transition_count,
                "transition_guard_installed": trigger_installed,
                "runtime_control_mode_counts": control_counts,
                "effective_global_mode": effective.mode,
                "pause_semantics": "deny_new_admission_only",
                "kill_semantics": "cooperative_runtime_boundaries",
                "os_signals_sent": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
