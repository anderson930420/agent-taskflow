#!/usr/bin/env python3
"""Operator control of Ticket dependencies (V1 Step 5, SPEC §5.4, §18; ruling 28 D6).

Actions, each a preview unless ``--confirm`` is given:

* ``set``     -- set or replace ``blocked_by`` (unknown, self and cyclic
                 dependencies are refused and nothing is written);
* ``remove``  -- remove ``blocked_by``; a Ticket held by it becomes ready;
* ``retry``   -- retry a ``failed`` or ``needs_decision`` Ticket (SPEC §33.3)
                 through the reset path of scripts/reset_task_status.py.

Every change is audited in ``task_events``. This command does not approve,
merge, clean up, execute or validate anything. There is no API route or
Mission Control control for it; that UX is Step 6.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import types
from typing import Any, Sequence

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
from agent_taskflow.task_status_reset import (  # noqa: E402
    TaskStatusResetError,
    TaskStatusResetRequest,
    reset_task_status,
)
from agent_taskflow.tasks import normalize_task_key  # noqa: E402
from agent_taskflow.ticket_dependencies import (  # noqa: E402
    TicketDependencyError,
    remove_blocked_by,
    set_blocked_by,
    validate_blocked_by,
)
from agent_taskflow.ticket_retry import TICKET_RETRY_FROM_STATUSES  # noqa: E402

EXIT_REFUSED = 2


def _non_empty(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise argparse.ArgumentTypeError("must not be empty")
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", type=Path, required=True, help="SQLite state DB (required)")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("set", "remove", "retry"):
        action = sub.add_parser(name)
        action.add_argument("--task-key", required=True, type=_non_empty)
        if name == "set":
            action.add_argument("--blocked-by", required=True, type=_non_empty)
        action.add_argument("--actor", default="ticket_dependency_cli", type=_non_empty)
        action.add_argument(
            "--reason",
            type=_non_empty,
            required=(name == "retry"),
            help="Recorded in the audit event (required for retry)",
        )
        action.add_argument("--confirm", action="store_true", help="Write the change")
    return parser


def _ticket_state(db_path: Path, task_key: str) -> dict[str, Any]:
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        row = conn.execute(
            "SELECT status, blocked_by FROM tasks WHERE task_key = ?", (task_key,)
        ).fetchone()
    if row is None:
        raise TicketDependencyError(f"Ticket does not exist: {task_key}")
    return {"status": row[0], "blocked_by": row[1]}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = require_absolute_path(args.db_path.expanduser(), "db_path")
    task_key = normalize_task_key(args.task_key)
    try:
        state = _ticket_state(db_path, task_key)
        if args.action == "set":
            if args.confirm:
                change = set_blocked_by(db_path, task_key, args.blocked_by, actor=args.actor, reason=args.reason)
                payload = {**change.to_dict(), "mutated": True}
            else:
                validate_blocked_by(db_path, task_key, args.blocked_by)
                payload = {
                    "task_key": task_key,
                    "blocked_by": normalize_task_key(args.blocked_by),
                    "previous_blocked_by": state["blocked_by"],
                    "from_status": state["status"],
                    "mutated": False,
                }
        elif args.action == "remove":
            if args.confirm:
                change = remove_blocked_by(db_path, task_key, actor=args.actor, reason=args.reason)
                payload = {**change.to_dict(), "mutated": True}
            else:
                if not state["blocked_by"]:
                    raise TicketDependencyError(f"{task_key} has no blocked_by to remove")
                payload = {
                    "task_key": task_key,
                    "previous_blocked_by": state["blocked_by"],
                    "from_status": state["status"],
                    "mutated": False,
                }
        else:
            if state["status"] not in TICKET_RETRY_FROM_STATUSES:
                raise TicketDependencyError(
                    f"{task_key} is {state['status']!r}; only a Ticket in "
                    f"{', '.join(TICKET_RETRY_FROM_STATUSES)} is retried"
                )
            result = reset_task_status(
                TaskStatusResetRequest(
                    task_key=task_key,
                    db_path=db_path,
                    from_status=state["status"],
                    reason=args.reason,
                    actor=args.actor,
                    confirm_reset=args.confirm,
                    dry_run=not args.confirm,
                )
            )
            payload = result.to_dict()
    except (TicketDependencyError, TaskStatusResetError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    print(json.dumps({"action": args.action, **payload}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
