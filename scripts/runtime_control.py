#!/usr/bin/env python3
"""Inspect or change persisted admission, kill, and governance controls."""

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
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Project pause denies new execution admission and does not stop active "
            "Attempts. Task-class governance disable denies only the class control "
            "term of future automatic-merge/promotion eligibility; it sends no OS "
            "signal and does not enable automatic merge."
        ),
    )
    parser.add_argument(
        "action",
        choices=("status", "pause", "kill", "clear", "disable-governance"),
    )
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument(
        "--scope-kind",
        choices=("global", "project", "task_class", "task", "attempt"),
        default="global",
    )
    parser.add_argument("--scope-id")
    parser.add_argument(
        "--actor",
        help="Required operator identity for every mutation",
    )
    parser.add_argument("--metadata-note", help="Optional human-readable reason detail")
    args = parser.parse_args()
    if args.scope_kind != "global" and not (args.scope_id or "").strip():
        parser.error("--scope-id is required for non-global controls")
    if args.action != "status" and not (args.actor or "").strip():
        parser.error("--actor is required for control mutations")
    if args.action == "disable-governance" and args.scope_kind != "task_class":
        parser.error("disable-governance requires --scope-kind task_class")
    if args.action == "kill" and args.scope_kind in {"project", "task_class"}:
        parser.error(
            "kill applies only to global/task/attempt; use project pause or "
            "task_class disable-governance"
        )
    return args


def main() -> int:
    args = _parse_args()
    db_path = require_absolute_path(args.db_path.expanduser(), "db_path")
    store = RuntimeControlStore(db_path)
    metadata = {"note": args.metadata_note} if args.metadata_note else None
    if args.action == "pause":
        record = store.pause(
            scope_kind=args.scope_kind,
            scope_id=args.scope_id,
            actor=args.actor,
            metadata=metadata,
        )
    elif args.action == "kill":
        record = store.request_kill(
            scope_kind=args.scope_kind,
            scope_id=args.scope_id,
            actor=args.actor,
            metadata=metadata,
        )
    elif args.action == "disable-governance":
        record = store.disable_task_class_governance(
            args.scope_id,
            actor=args.actor,
            metadata=metadata,
        )
    elif args.action == "clear":
        record = store.clear(
            scope_kind=args.scope_kind,
            scope_id=args.scope_id,
            actor=args.actor,
            metadata=metadata,
        )
    else:
        record = store.get_control(
            scope_kind=args.scope_kind,
            scope_id=args.scope_id,
        )

    effective_mode = record.mode if record is not None else "running"
    if args.scope_kind in {"global", "task", "attempt"}:
        effective_mode = store.effective_control(
            task_key=args.scope_id if args.scope_kind == "task" else None,
            attempt_id=args.scope_id if args.scope_kind == "attempt" else None,
        ).mode
    governance_permitted = (
        store.task_class_governance_permitted(args.scope_id)
        if args.scope_kind == "task_class"
        else None
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
                "effective_mode": effective_mode,
                "task_class_governance_permitted": governance_permitted,
                "actual_auto_merge_enabled": False,
                "pause_semantics": "deny_new_admission_only",
                "kill_semantics": "cooperative_runtime_boundaries",
                "project_pause_semantics": "deny_new_admission_only",
                "task_class_semantics": "governance_eligibility_only",
                "os_signals_sent": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
