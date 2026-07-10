#!/usr/bin/env python3
"""Apply and verify the additive Level 2 Task/Attempt/lifecycle migration."""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path

from agent_taskflow.attempt_store import (
    TASK_ATTEMPT_LIFECYCLE_MIGRATION,
    migrate_task_attempt_lifecycle,
)
from agent_taskflow.models import require_absolute_path
from agent_taskflow.store import connect, default_db_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Apply the additive Task/Attempt/lifecycle schema migration. "
            "Existing tasks are marked legacy and no historical attempts are invented."
        )
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=default_db_path(),
        help="Absolute SQLite database path (default: ~/.agent-taskflow/state.db)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    db_path = require_absolute_path(args.db_path, "db_path")
    migrate_task_attempt_lifecycle(db_path)

    with closing(connect(db_path)) as conn, conn:
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        legacy_task_count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE is_legacy = 1"
        ).fetchone()[0]
        attempt_count = conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        lifecycle_event_count = conn.execute(
            "SELECT COUNT(*) FROM lifecycle_events"
        ).fetchone()[0]
        migration_recorded = (
            conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name = ?",
                (TASK_ATTEMPT_LIFECYCLE_MIGRATION,),
            ).fetchone()
            is not None
        )

    print(
        json.dumps(
            {
                "db_path": str(db_path),
                "migration": TASK_ATTEMPT_LIFECYCLE_MIGRATION,
                "migration_recorded": migration_recorded,
                "task_count": task_count,
                "legacy_task_count": legacy_task_count,
                "attempt_count": attempt_count,
                "lifecycle_event_count": lifecycle_event_count,
                "historical_attempts_synthesized": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
