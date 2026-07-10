#!/usr/bin/env python3
"""Install the Level 2 runtime admission and lease migration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

from agent_taskflow.models import require_absolute_path
from agent_taskflow.runtime_admission import RuntimeAdmissionStore
from agent_taskflow.runtime_admission_schema import (
    RUNTIME_ADMISSION_MIGRATION,
    migrate_runtime_admission,
)
from agent_taskflow.store import connect


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Install atomic runtime pickup, execution ownership, lease, "
            "heartbeat, and executor-start database guards."
        )
    )
    parser.add_argument("--db-path", required=True, help="Absolute SQLite DB path")
    parser.add_argument(
        "--reap-expired",
        action="store_true",
        help="After migration, close expired leases and block their tasks.",
    )
    return parser


def _summary(db_path: Path, expired_attempt_ids: list[str]) -> dict[str, object]:
    with connect(db_path) as conn:
        migration_recorded = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?",
            (RUNTIME_ADMISSION_MIGRATION,),
        ).fetchone() is not None
        active_leases = conn.execute(
            "SELECT count(*) FROM runtime_leases WHERE is_active = 1"
        ).fetchone()[0]
        trigger_names = sorted(
            row[0]
            for row in conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'trigger' AND name LIKE 'runtime_%'
                """
            ).fetchall()
        )
    return {
        "db_path": str(db_path),
        "migration": RUNTIME_ADMISSION_MIGRATION,
        "migration_recorded": migration_recorded,
        "active_leases": active_leases,
        "expired_attempt_ids_reaped": expired_attempt_ids,
        "runtime_triggers": trigger_names,
        "executor_start_fail_closed": True,
        "historical_attempts_synthesized": False,
    }


def main() -> int:
    args = _parser().parse_args()
    db_path = require_absolute_path(Path(args.db_path).expanduser(), "db_path")
    migrate_runtime_admission(db_path)
    expired_attempt_ids: list[str] = []
    if args.reap_expired:
        expired_attempt_ids = RuntimeAdmissionStore(db_path).expire_stale_leases()
    print(json.dumps(_summary(db_path, expired_attempt_ids), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
