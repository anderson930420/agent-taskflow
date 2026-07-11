#!/usr/bin/env python3
"""Inspect or change persisted runtime pause/kill controls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
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

from agent_taskflow.lifecycle_control import RuntimeControlStore  # noqa: E402
from agent_taskflow.models import require_absolute_path  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "pause", "kill", "clear"))
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument(
        "--scope-kind",
        choices=("global", "task", "attempt"),
        default="global",
    )
    parser.add_argument("--scope-id")
    parser.add_argument("--actor", default="operator")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    db_path = require_absolute_path(args.db_path.expanduser(), "db_path")
    store = RuntimeControlStore(db_path)
    if args.action == "pause":
        record = store.pause(
            scope_kind=args.scope_kind,
            scope_id=args.scope_id,
            actor=args.actor,
        )
    elif args.action == "kill":
        record = store.request_kill(
            scope_kind=args.scope_kind,
            scope_id=args.scope_id,
            actor=args.actor,
        )
    elif args.action == "clear":
        record = store.clear(
            scope_kind=args.scope_kind,
            scope_id=args.scope_id,
            actor=args.actor,
        )
    else:
        record = store.get_control(
            scope_kind=args.scope_kind,
            scope_id=args.scope_id,
        )

    effective = store.effective_control(
        task_key=args.scope_id if args.scope_kind == "task" else None,
        attempt_id=args.scope_id if args.scope_kind == "attempt" else None,
    )
    print(
        json.dumps(
            {
                "db_path": str(db_path),
                "action": args.action,
                "scope_kind": args.scope_kind,
                "scope_id": "*" if args.scope_kind == "global" else args.scope_id,
                "control": (
                    {
                        "mode": record.mode,
                        "reason_code": record.reason_code,
                        "requested_by": record.requested_by,
                        "requested_at": record.requested_at,
                        "generation": record.generation,
                    }
                    if record is not None
                    else None
                ),
                "effective_mode": effective.mode,
                "pause_semantics": "deny_new_admission_only",
                "kill_semantics": "cooperative_runtime_boundaries",
                "os_signals_sent": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
