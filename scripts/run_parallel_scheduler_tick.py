#!/usr/bin/env python3
"""Run ONE parallel scheduler tick (V1 Step 5, SPEC §20) and exit.

Explicit and idempotent: it runs only when invoked. It is not a daemon, adds no
cron and starts no background thread. A tick reaps stale runtime, releases or
stops dependencies, then starts eligible Tickets, highest priority first, up to
the free ``max_concurrent_tasks`` capacity. Each started Ticket runs in its own
one-shot worker process, in the Ticket's own worktree.

``--db-path`` is required: there is no default database. Startup fails closed
(exit 2) when the Step 1 Ticket columns or the Step 5 Ticket-worktree migration
are missing, naming the script to run. By default the tick waits for its
workers to finish; ``--no-wait`` returns once every started Ticket is claimed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.models import require_absolute_path  # noqa: E402
from agent_taskflow.parallel_scheduler import (  # noqa: E402
    DEFAULT_CLAIM_TIMEOUT_SECONDS,
    run_scheduler_tick,
)
from agent_taskflow.ticket_fields_schema import (  # noqa: E402
    TicketFieldsMigrationRequired,
    require_ticket_fields,
)
from agent_taskflow.ticket_worktree_schema import (  # noqa: E402
    TicketWorktreeMigrationRequired,
    require_ticket_worktree_resources,
)

EXIT_MIGRATION_REQUIRED = 2


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        type=Path,
        required=True,
        help="SQLite state DB to schedule (required; there is no default)",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Return once every started Ticket is claimed instead of waiting for its worker",
    )
    parser.add_argument(
        "--claim-timeout-seconds",
        type=float,
        default=DEFAULT_CLAIM_TIMEOUT_SECONDS,
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = require_absolute_path(args.db_path.expanduser(), "db_path")
    try:
        require_ticket_fields(db_path)
        require_ticket_worktree_resources(db_path)
    except (TicketFieldsMigrationRequired, TicketWorktreeMigrationRequired) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_MIGRATION_REQUIRED
    result = run_scheduler_tick(
        db_path,
        wait=not args.no_wait,
        claim_timeout_seconds=args.claim_timeout_seconds,
    )
    print(
        json.dumps(
            {**result.to_dict(), "daemon": False, "background_thread": False, "cron": False},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
