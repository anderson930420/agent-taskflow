#!/usr/bin/env python3
"""Let a Ticket's Attempts share the Ticket's worktree (V1 Step 5, ruling 26d).

Run by hand, by an operator. Nothing applies this migration at startup; the
scheduler tick and Ticket dispatch refuse to run a Ticket until it has run.

It rebuilds ``attempt_resources`` with exactly two ``UNIQUE`` keywords removed,
on ``branch_name`` and ``worktree_path``. Every row, every other constraint
(``UNIQUE(task_id, attempt_number)`` and the per-Attempt artifact, lock and PID
paths included), its index and both triggers are kept. It records
``v1_step5_ticket_worktree_attempt_resources`` in ``schema_migrations`` and is
idempotent.

It needs the legacy task-mirror schema and never creates it. If that schema is
missing, the script fails closed (exit 2) before writing anything. It installs
the Attempt-resource prerequisites first, as scripts/migrate_attempt_resources.py
does.
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
from agent_taskflow.ticket_worktree_schema import (  # noqa: E402
    TICKET_WORKTREE_MIGRATION_SCRIPT,
    TICKET_WORKTREE_RESOURCES_MIGRATION,
    TicketWorktreeMigrationError,
    TicketWorktreePreconditionError,
    migrate_ticket_worktree_resources,
    ticket_worktree_resources_applied,
)

#: Exit status when a precondition is not met or the stored shape is unexpected.
EXIT_PRECONDITION_FAILED = 2


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = require_absolute_path(args.db_path.expanduser(), "db_path")
    base = {
        "db_path": str(db_path),
        "migration": TICKET_WORKTREE_RESOURCES_MIGRATION,
        "script": TICKET_WORKTREE_MIGRATION_SCRIPT,
    }
    try:
        result = migrate_ticket_worktree_resources(db_path)
    except (TicketWorktreePreconditionError, TicketWorktreeMigrationError) as exc:
        print(str(exc), file=sys.stderr)
        print(
            json.dumps(
                {**base, "ok": False, "refused": True, "error": str(exc)},
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_PRECONDITION_FAILED

    print(
        json.dumps(
            {
                **base,
                "ok": True,
                "rebuilt": result.rebuilt,
                "rows_copied": result.rows_copied,
                "already_installed": not result.changed_schema,
                "migration_newly_recorded": result.migration_newly_recorded,
                "installed": ticket_worktree_resources_applied(db_path),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
