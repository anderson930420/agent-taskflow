#!/usr/bin/env python3
"""Install canonical explicit-token runtime admission on a state database."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.canonical_runtime_path import (  # noqa: E402
    CANONICAL_RUNTIME_ADMISSION_MIGRATION,
    migrate_canonical_runtime_admission,
)
from agent_taskflow.models import require_absolute_path  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        type=Path,
        required=True,
        help="Absolute path to the Agent Taskflow SQLite state database.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    db_path = require_absolute_path(args.db_path.expanduser(), "db_path")
    migrate_canonical_runtime_admission(db_path)

    with sqlite3.connect(db_path) as conn:
        migration_recorded = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?",
            (CANONICAL_RUNTIME_ADMISSION_MIGRATION,),
        ).fetchone() is not None
        active_rows = conn.execute(
            """
            SELECT auth_mode, COUNT(*)
            FROM runtime_leases
            WHERE is_active = 1
            GROUP BY auth_mode
            ORDER BY auth_mode
            """
        ).fetchall()
        trigger_names = [
            row[0]
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'trigger' AND name LIKE 'runtime_%'
                ORDER BY name
                """
            ).fetchall()
        ]

    payload = {
        "db_path": str(db_path),
        "migration": CANONICAL_RUNTIME_ADMISSION_MIGRATION,
        "migration_recorded": migration_recorded,
        "active_leases_by_auth_mode": dict(active_rows),
        "implicit_pickup_disabled": (
            "runtime_pickup_claim_after_preparing" not in trigger_names
            and "runtime_preparing_requires_canonical_claim" in trigger_names
        ),
        "executor_start_requires_claim_metadata": (
            "runtime_executor_start_requires_canonical_claim" in trigger_names
        ),
        "token_terminal_requires_owned_release": (
            "runtime_token_terminal_requires_owned_release" in trigger_names
        ),
        "runtime_triggers": trigger_names,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
