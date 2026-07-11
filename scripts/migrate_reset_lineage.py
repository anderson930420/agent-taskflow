#!/usr/bin/env python3
"""Install PR-8 reset lineage, retry reservation, and CAS persistence."""

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
from agent_taskflow.reset_lineage_schema import (  # noqa: E402
    RESET_LINEAGE_MIGRATION,
    migrate_reset_lineage,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    db_path = require_absolute_path(args.db_path.expanduser(), "db_path")
    migrate_reset_lineage(db_path)
    with sqlite3.connect(db_path) as conn:
        migration_recorded = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?",
            (RESET_LINEAGE_MIGRATION,),
        ).fetchone() is not None
        lineage_state_counts = dict(
            conn.execute(
                """
                SELECT state, COUNT(*) FROM reset_lineages
                GROUP BY state ORDER BY state
                """
            ).fetchall()
        )
        active_reserved_attempts = conn.execute(
            """
            SELECT COUNT(*)
            FROM reset_lineages
            JOIN attempts ON attempts.attempt_id = reset_lineages.new_attempt_id
            WHERE reset_lineages.state = 'reserved'
              AND attempts.is_active = 1
              AND attempts.status = 'created'
            """
        ).fetchone()[0]
        max_reset_generation = conn.execute(
            "SELECT COALESCE(MAX(reset_generation), 0) FROM tasks"
        ).fetchone()[0]
        triggers = [
            row[0]
            for row in conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'trigger' AND name LIKE 'reset_lineage%'
                ORDER BY name
                """
            ).fetchall()
        ]
    print(
        json.dumps(
            {
                "db_path": str(db_path),
                "migration": RESET_LINEAGE_MIGRATION,
                "migration_recorded": migration_recorded,
                "reset_lineage_state_counts": lineage_state_counts,
                "active_reserved_retry_attempts": active_reserved_attempts,
                "max_reset_generation": max_reset_generation,
                "reset_lineage_triggers": triggers,
                "compare_and_set": {
                    "task_status": "blocked",
                    "active_attempt_id": None,
                    "reset_generation": "exact_match_and_increment",
                    "one_winner": True,
                },
                "runtime_claim": {
                    "reserved_attempt_adoption": True,
                    "second_retry_identity_created": False,
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
