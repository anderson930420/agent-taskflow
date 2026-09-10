#!/usr/bin/env python3
"""Install the V1 Step 1 Ticket columns on `tasks` (PR #195 ruling 4a).

Run by hand, by an operator. This migration no longer runs at startup, and the
Mission Control API refuses to start until it has been run.

Additive and idempotent. It adds exactly Step 1's ten nullable `tasks` columns
and its two partial unique indexes — whichever are missing — records
``tasks_ticket_fields`` in ``schema_migrations``, and touches nothing else.

It needs the legacy task-mirror schema and never installs it. If that schema
is missing, the script fails closed (exit 2) before writing anything.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence
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
from agent_taskflow.ticket_fields_schema import (  # noqa: E402
    TICKET_FIELDS_MIGRATION,
    TICKET_FIELDS_MIGRATION_SCRIPT,
    TicketFieldsPreconditionError,
    migrate_ticket_fields,
    missing_ticket_fields,
)

#: Exit status when the legacy-schema precondition is not met.
EXIT_PRECONDITION_FAILED = 2


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = require_absolute_path(args.db_path.expanduser(), "db_path")

    try:
        result = migrate_ticket_fields(db_path)
    except TicketFieldsPreconditionError as exc:
        print(str(exc), file=sys.stderr)
        print(
            json.dumps(
                {
                    "ok": False,
                    "db_path": str(db_path),
                    "migration": TICKET_FIELDS_MIGRATION,
                    "script": TICKET_FIELDS_MIGRATION_SCRIPT,
                    "refused": True,
                    "error": str(exc),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_PRECONDITION_FAILED

    print(
        json.dumps(
            {
                "ok": True,
                "db_path": str(db_path),
                "migration": TICKET_FIELDS_MIGRATION,
                "script": TICKET_FIELDS_MIGRATION_SCRIPT,
                "task_columns_added": list(result.columns_added),
                "indexes_added": list(result.indexes_added),
                "already_installed": not result.changed_schema,
                "migration_newly_recorded": result.migration_newly_recorded,
                "still_missing": list(missing_ticket_fields(db_path)),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
