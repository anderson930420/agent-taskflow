#!/usr/bin/env python3
"""Install canonical explicit-token runtime admission on a state database."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sqlite3
import sys
import types

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "agent_taskflow"


def _bootstrap_source_package_without_runtime_imports() -> None:
    """Expose package submodules without executing agent_taskflow.__init__.

    Database migrations use only the SQLite/model layers. Importing the package
    initializer would eagerly load ApprovedTaskRunner and FastAPI/Pydantic, which
    makes a source-checkout migration depend on application runtime extras.
    """
    if "agent_taskflow" in sys.modules:
        return
    package = types.ModuleType("agent_taskflow")
    package.__file__ = str(PACKAGE_ROOT / "__init__.py")
    package.__package__ = "agent_taskflow"
    package.__path__ = [str(PACKAGE_ROOT)]
    sys.modules["agent_taskflow"] = package


_bootstrap_source_package_without_runtime_imports()

from agent_taskflow.canonical_runtime_schema import (  # noqa: E402
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


def _normalized_sql(value: str | None) -> str:
    return " ".join((value or "").lower().split())


def _implicit_pickup_disabled(trigger_sql: dict[str, str | None]) -> bool:
    """Verify that canonical claim enforcement exists and PR-3 pickup is inert."""
    canonical_guard = trigger_sql.get("runtime_preparing_requires_canonical_claim")
    compatibility_trigger = trigger_sql.get("runtime_pickup_claim_after_preparing")
    if canonical_guard is None or compatibility_trigger is None:
        return False
    normalized = _normalized_sql(compatibility_trigger)
    return re.search(r"\bwhen\s+0\b", normalized) is not None


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
        trigger_rows = conn.execute(
            """
            SELECT name, sql
            FROM sqlite_master
            WHERE type = 'trigger' AND name LIKE 'runtime_%'
            ORDER BY name
            """
        ).fetchall()
        trigger_sql = {name: sql for name, sql in trigger_rows}
        trigger_names = list(trigger_sql)

    payload = {
        "db_path": str(db_path),
        "migration": CANONICAL_RUNTIME_ADMISSION_MIGRATION,
        "migration_recorded": migration_recorded,
        "active_leases_by_auth_mode": dict(active_rows),
        "implicit_pickup_disabled": _implicit_pickup_disabled(trigger_sql),
        "executor_start_requires_claim_metadata": (
            "runtime_executor_start_requires_canonical_claim" in trigger_sql
        ),
        "token_terminal_requires_owned_release": (
            "runtime_token_terminal_requires_owned_release" in trigger_sql
        ),
        "runtime_triggers": trigger_names,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
