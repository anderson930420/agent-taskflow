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

The run holds a non-overlap lock keyed by the database (SPEC §47.3; V1-F10),
for as long as the tick itself runs. A second invocation against the same
database prints one ``skipped_overlap`` JSON result and exits 75 at once,
doing no work. The lock is always ``<db>.execution-tick.lock``; there is no
``--lock-path`` override (RULINGS 70, F10-FU4). A lock that cannot be taken for
any other reason (for example an unwritable directory, or a lock file that is
not a holder record) prints one error JSON result and exits 2. It never
integrates (§47.4).
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
from agent_taskflow.tick_lock import (  # noqa: E402
    EXIT_SKIPPED_OVERLAP,
    TickLock,
    execution_tick_lock_path,
    skipped_overlap_result,
)

EXIT_MIGRATION_REQUIRED = 2
EXIT_ERROR = 2
KIND = "parallel_scheduler_tick"


def _emit(value: dict, *, jsonl: bool) -> None:
    if jsonl:
        print(json.dumps(value, sort_keys=True))
    else:
        print(json.dumps(value, indent=2, sort_keys=True))


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
    parser.add_argument(
        "--jsonl",
        action="store_true",
        help="Print the result as one compact JSON line, for append-only logs",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = require_absolute_path(args.db_path.expanduser(), "db_path")
    lock = None
    # A missing database keeps its existing fail-closed message below, and no
    # lock file is ever left beside a database that does not exist.
    if db_path.is_file():
        try:
            lock = TickLock(
                execution_tick_lock_path(db_path),
                holder={"kind": KIND, "db_path": str(db_path)},
                db_path=db_path,
            )
            acquired = lock.acquire()
        except Exception as exc:
            # An unusable lock path (unwritable, missing directory, refused)
            # is an error, logged as one JSON line like the integration tick's.
            print(json.dumps({"kind": KIND, "ok": False, "status": "error",
                              "reason": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
            return EXIT_ERROR
        if not acquired:
            _emit(skipped_overlap_result(KIND, lock, db_path=str(db_path)), jsonl=args.jsonl)
            return EXIT_SKIPPED_OVERLAP
    # A corrupt holder record that the lock reclaimed is reported on this run's line.
    reclaim = {"lock_reclaimed": lock.reclaimed} if lock is not None and lock.reclaimed else {}
    try:
        try:
            require_ticket_fields(db_path)
            require_ticket_worktree_resources(db_path)
        except (TicketFieldsMigrationRequired, TicketWorktreeMigrationRequired) as exc:
            print(str(exc), file=sys.stderr)
            if reclaim:
                print(json.dumps({"kind": KIND, **reclaim}, sort_keys=True), file=sys.stderr)
            return EXIT_MIGRATION_REQUIRED
        result = run_scheduler_tick(
            db_path,
            wait=not args.no_wait,
            claim_timeout_seconds=args.claim_timeout_seconds,
        )
        _emit(
            {**result.to_dict(), "daemon": False, "background_thread": False, "cron": False,
             **reclaim},
            jsonl=args.jsonl,
        )
        return 0
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
