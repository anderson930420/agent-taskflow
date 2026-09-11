"""One scheduler worker: dispatch one Ticket, then exit (V1 Step 5, SPEC §20).

Started by :func:`agent_taskflow.parallel_scheduler.default_launcher`, one
process per Ticket. It runs the installed, fully layered Dispatcher, whose
``preparing`` transition is the Step 4 atomic claim, so this process becomes
the Attempt's only owner and heartbeats its lease until the run ends. It prints
the DispatcherResult as one JSON line. It is not a daemon: it handles exactly
one Ticket.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

import agent_taskflow  # noqa: F401  installs the layered runtime path
from agent_taskflow.dispatcher import Dispatcher
from agent_taskflow.models import require_absolute_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--task-key", required=True)
    args = parser.parse_args(argv)
    db_path = require_absolute_path(args.db_path.expanduser(), "db_path")
    dispatcher = Dispatcher(db_path=db_path)
    try:
        result = dispatcher.dispatch_task(args.task_key)
    finally:
        shutdown = getattr(dispatcher.store, "shutdown_runtime_supervisors", None)
        if shutdown is not None:
            shutdown()
    print(
        json.dumps(
            {
                "task_key": result.task_key,
                "status": result.status,
                "summary": result.summary,
                "executor_status": result.executor_status,
                "validator_statuses": result.validator_statuses,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
