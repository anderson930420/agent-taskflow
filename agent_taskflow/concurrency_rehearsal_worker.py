"""Separate-process worker for the V1 Step 4 concurrency rehearsal.

Run as ``python -m agent_taskflow.concurrency_rehearsal_worker --request JSON``.
Importing the package installs the real runtime path, so a worker claims and
writes exactly as a runtime process does. It prints one JSON line with its
outcome, pid and contention counters.

With ``--sync-dir`` the worker prepares, reports ready, and waits for the
shared go signal before acting, so many workers contend at the same moment.

Operations:

``claim``            explicit ``RuntimeAdmissionStore.claim()``
``dispatcher-claim`` the dispatcher's ``preparing`` transition
``write-mix``        claim, heartbeat, transition, ObservedStep, evidence, release
``hold-lock``        hold the write lock from before the go signal, then commit
``record-steps``     a list of ``record_step`` writes for one Attempt
``crash-holder``     claim through the dispatcher path, report, then run until killed

Only disposable rehearsal databases are ever passed in.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

import agent_taskflow.canonical_runtime_path as canonical_path
from agent_taskflow.concurrency_rehearsal import (
    dispatcher_preparing_claim,
    dispatcher_runtime_store,
    explicit_claim,
)
from agent_taskflow.runtime_progress import RUNTIME_STEPS, ObservedStepRegressionError
from agent_taskflow.runtime_progress_store import RuntimeProgressStore
from agent_taskflow.sqlite_contention import contention_snapshot
from agent_taskflow.store import TaskMirrorStore, connect

GO_TIMEOUT_SECONDS = 60.0


def _wait_for_go(sync_dir: Path | None, index: int) -> None:
    if sync_dir is None:
        return
    (sync_dir / f"ready-{index}").write_text(str(os.getpid()), encoding="utf-8")
    deadline = time.monotonic() + GO_TIMEOUT_SECONDS
    while not (sync_dir / "go").exists():
        if time.monotonic() > deadline:
            raise TimeoutError("go signal never arrived")
        time.sleep(0.001)


def _write_mix(request: dict[str, Any]) -> dict[str, Any]:
    db = Path(request["db_path"])
    key = request["task_key"]
    owner = request["owner_id"]
    admission = canonical_path.CanonicalRuntimeAdmissionStore(db)
    claim = admission.claim(key, owner_id=owner, ttl_seconds=600)
    token = claim.lease_token
    for _ in range(int(request["heartbeats"])):
        admission.heartbeat(claim.attempt_id, owner_id=owner, lease_token=token)
    admission.transition(
        claim.attempt_id,
        owner_id=owner,
        lease_token=token,
        attempt_status="implementing",
        reason_code="runtime_implementing",
    )
    progress = RuntimeProgressStore(db)
    for step in RUNTIME_STEPS:
        progress.record_step(attempt_id=claim.attempt_id, step=step, status="running")
        progress.set_current_activity(
            attempt_id=claim.attempt_id, phase=step, activity=f"{step} running"
        )
        progress.record_step(attempt_id=claim.attempt_id, step=step, status="passed")
    tasks = TaskMirrorStore(db)
    artifact_dir = Path(request["artifact_root"]) / key
    for index in range(int(request["evidence_rows"])):
        tasks.record_validation_result(
            key,
            f"rehearsal-validator-{index}",
            status="passed",
            summary="Step 4 rehearsal evidence row",
        )
        tasks.record_task_artifact(
            key, "other", artifact_dir / f"step4-rehearsal-evidence-{index}.json"
        )
    admission.transition(
        claim.attempt_id,
        owner_id=owner,
        lease_token=token,
        attempt_status="validating",
        reason_code="runtime_validating",
    )
    admission.release(
        claim.attempt_id,
        owner_id=owner,
        lease_token=token,
        attempt_status="waiting_approval",
        task_status="waiting_approval",
        reason_code="runtime_waiting_approval",
        execution_result="completed",
        validation_result="passed",
    )
    return {"outcome": "done", "task_key": key, "attempt_id": claim.attempt_id}


def _record_steps(request: dict[str, Any]) -> dict[str, Any]:
    progress = RuntimeProgressStore(Path(request["db_path"]))
    applied = refused = 0
    for step, status in request["writes"]:
        try:
            progress.record_step(
                attempt_id=request["attempt_id"], step=step, status=status
            )
            applied += 1
        except ObservedStepRegressionError:
            refused += 1
    return {"outcome": "done", "applied": applied, "refused": refused}


def _crash_holder(request: dict[str, Any]) -> None:
    db = Path(request["db_path"])
    key = request["task_key"]
    store = dispatcher_runtime_store(db, int(request["lease_ttl_seconds"]))
    store.update_task_status(
        key, "preparing", source=request["owner_id"], message="Step 4 crash holder"
    )
    claim = store.runtime_claim(key)
    progress = RuntimeProgressStore(db)
    progress.record_step(attempt_id=claim.attempt_id, step="Prepare", status="running")
    progress.record_step(attempt_id=claim.attempt_id, step="Prepare", status="passed")
    store.update_task_status(
        key, "implementing", source=request["owner_id"], message="Step 4 crash holder"
    )
    progress.record_step(attempt_id=claim.attempt_id, step="Implementer", status="running")
    resource = store.attempt_resource(key)
    with closing(connect(db)) as conn:
        task_status = conn.execute(
            "SELECT status FROM tasks WHERE task_key = ?", (key,)
        ).fetchone()[0]
    print(
        json.dumps(
            {
                "outcome": "holding",
                "op": "crash-holder",
                "pid": os.getpid(),
                "task_key": key,
                "task_status": task_status,
                "attempt_id": claim.attempt_id,
                "lease_id": claim.lease_id,
                "owner_id": claim.owner_id,
                # Disposable rehearsal database only: the parent replays this
                # token to prove a killed owner is refused after recovery.
                "lease_token": claim.lease_token,
                "acquired_at": claim.acquired_at,
                "pid_path": str(resource.pid_path) if resource is not None else None,
            }
        ),
        flush=True,
    )
    while True:  # Mid-run until SIGKILL; the heartbeat thread keeps the lease.
        time.sleep(3600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--sync-dir", type=Path)
    parser.add_argument("--index", type=int, default=0)
    args = parser.parse_args(argv)
    request = json.loads(args.request)
    op = request["op"]

    if op == "crash-holder":
        _crash_holder(request)
        return 0

    if op == "hold-lock":
        with closing(connect(Path(request["db_path"]))) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            _wait_for_go(args.sync_dir, args.index)
            time.sleep(float(request["hold_seconds"]))
        result: dict[str, Any] = {"outcome": "done", "held_seconds": request["hold_seconds"]}
    else:
        _wait_for_go(args.sync_dir, args.index)
        if op == "claim":
            result = explicit_claim(
                request["db_path"], request["task_key"], owner_id=request["owner_id"]
            )
        elif op == "dispatcher-claim":
            result = dispatcher_preparing_claim(
                request["db_path"], request["task_key"], source=request["owner_id"]
            )
        elif op == "write-mix":
            result = _write_mix(request)
        elif op == "record-steps":
            result = _record_steps(request)
        else:
            raise ValueError(f"unknown rehearsal worker op: {op!r}")

    result.update(op=op, pid=os.getpid(), contention=contention_snapshot())
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
