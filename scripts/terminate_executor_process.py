#!/usr/bin/env python3
"""Inspect or hard-terminate registered executor or validator process groups."""

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

from agent_taskflow.executor_launch import (  # noqa: E402
    ExecutorProcessRecord,
    ExecutorProcessStore,
    ProcessIdentityError,
    inspect_process_group,
    terminate_registered_process,
)
from agent_taskflow.lifecycle_control import RuntimeControlStore  # noqa: E402
from agent_taskflow.models import require_absolute_path, utc_now_iso  # noqa: E402
from agent_taskflow.store import connect  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "terminate", "reap-stale"))
    parser.add_argument("--db-path", type=Path, required=True)
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--process-id")
    selector.add_argument("--attempt-id")
    parser.add_argument("--actor", default="operator")
    parser.add_argument("--terminate-grace-seconds", type=float, default=2.0)
    parser.add_argument("--kill-wait-seconds", type=float, default=3.0)
    return parser.parse_args()


def _record_payload(record: ExecutorProcessRecord) -> dict[str, object]:
    snapshot = (
        inspect_process_group(record.pgid, record.session_id)
        if record.pgid is not None and record.session_id is not None
        else None
    )
    return {
        "process_id": record.process_id,
        "attempt_id": record.attempt_id,
        "task_key": record.task_key,
        "process_role": record.process_role,
        "process_name": record.executor_name,
        "executor_name": record.executor_name,
        "pid": record.pid,
        "pgid": record.pgid,
        "session_id": record.session_id,
        "state": record.state,
        "exit_code": record.exit_code,
        "termination_reason": record.termination_reason,
        "verified_exit": record.verified_exit,
        "live_member_pids": (
            [member.pid for member in snapshot.live_members]
            if snapshot is not None
            else []
        ),
    }


def _select(
    store: ExecutorProcessStore,
    *,
    process_id: str | None,
    attempt_id: str | None,
) -> list[ExecutorProcessRecord]:
    if process_id is not None:
        record = store.get(process_id)
        if record is None:
            raise KeyError(f"Managed process not found: {process_id}")
        return [record]
    if attempt_id is not None:
        record = store.active_for_attempt(attempt_id)
        if record is None:
            raise KeyError(f"No active managed process for Attempt: {attempt_id}")
        return [record]
    return store.list_active()


def _stale_records(store: ExecutorProcessStore) -> list[ExecutorProcessRecord]:
    store.init_db()
    now = utc_now_iso()
    records: list[ExecutorProcessRecord] = []
    with connect(store.db_path) as conn:
        for record in store.list_active():
            lease = conn.execute(
                """
                SELECT is_active, expires_at FROM runtime_leases
                WHERE attempt_id = ?
                """,
                (record.attempt_id,),
            ).fetchone()
            if lease is None or not lease["is_active"] or lease["expires_at"] <= now:
                records.append(record)
    return records


def main() -> int:
    args = _parse_args()
    db_path = require_absolute_path(args.db_path.expanduser(), "db_path")
    store = ExecutorProcessStore(db_path)
    store.init_db()
    if args.action == "reap-stale":
        records = _stale_records(store)
    else:
        records = _select(
            store,
            process_id=args.process_id,
            attempt_id=args.attempt_id,
        )

    results: list[dict[str, object]] = []
    failed = False
    for record in records:
        before = _record_payload(record)
        if args.action == "status":
            results.append({"before": before, "after": before, "acted": False})
            continue
        RuntimeControlStore(db_path).request_kill(
            scope_kind="attempt",
            scope_id=record.attempt_id,
            actor=args.actor,
            metadata={
                "source": "terminate_executor_process.py",
                "process_id": record.process_id,
                "hard_termination_requested": True,
            },
        )
        try:
            terminated = terminate_registered_process(
                store,
                record,
                actor=args.actor,
                termination_reason="operator_kill_requested",
                terminate_grace_seconds=args.terminate_grace_seconds,
                kill_wait_seconds=args.kill_wait_seconds,
            )
            after = _record_payload(terminated)
            failed = failed or not terminated.verified_exit
            results.append({"before": before, "after": after, "acted": True})
        except ProcessIdentityError as exc:
            failed = True
            results.append(
                {
                    "before": before,
                    "after": before,
                    "acted": False,
                    "error": str(exc),
                }
            )

    print(
        json.dumps(
            {
                "db_path": str(db_path),
                "action": args.action,
                "selected_count": len(records),
                "all_verified_exit": not failed,
                "identity_verification": "linux_proc_pid_pgid_session_start_ticks",
                "signal_escalation": ["SIGTERM", "SIGKILL"],
                "results": results,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
