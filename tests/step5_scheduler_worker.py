"""Test-only scheduler worker for the V1 Step 5 parallel-scheduler tests.

The scheduler tick starts one of these per claimed Ticket (through the
launcher the tests inject). It runs the real, fully layered Dispatcher on the
Ticket, with a fake executor that records where it ran and, depending on
``--mode``:

* ``pass``: waits until ``--expect`` workers have started (a barrier that
  proves they overlap), then completes;
* ``hold``: waits until ``<sync-dir>/release`` exists, then completes;
* ``fail``: returns ``failed``.

Not a test module and not production code.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent_taskflow  # noqa: E402,F401  installs the layered runtime path
from agent_taskflow.dispatcher import Dispatcher  # noqa: E402
from agent_taskflow.executors.base import ExecutorResult  # noqa: E402
from agent_taskflow.validators.base import ValidatorResult  # noqa: E402

WAIT_SECONDS = 90.0


class _Executor:
    name = "fake"

    def __init__(self, sync_dir: Path, mode: str, expect: int) -> None:
        self.sync_dir = sync_dir
        self.mode = mode
        self.expect = expect

    def run(self, context):
        record = {
            "task_key": context.task_key,
            "pid": os.getpid(),
            "attempt_id": context.attempt_id,
            "worktree_path": str(context.worktree_path),
            "started_at": time.time(),
        }
        started = self.sync_dir / f"{context.task_key}.started.json"
        started.write_text(json.dumps(record), encoding="utf-8")
        deadline = time.time() + WAIT_SECONDS
        if self.mode == "pass":
            while len(list(self.sync_dir.glob("*.started.json"))) < self.expect:
                if time.time() > deadline:
                    return ExecutorResult(executor=self.name, status="failed", summary="barrier timeout")
                time.sleep(0.05)
            record["overlapping"] = sorted(
                p.name.removesuffix(".started.json") for p in self.sync_dir.glob("*.started.json")
            )
        elif self.mode == "hold":
            while not (self.sync_dir / "release").exists():
                if time.time() > deadline:
                    return ExecutorResult(executor=self.name, status="failed", summary="hold timeout")
                time.sleep(0.05)
        elif self.mode == "fail":
            record["finished_at"] = time.time()
            (self.sync_dir / f"{context.task_key}.finished.json").write_text(
                json.dumps(record), encoding="utf-8"
            )
            return ExecutorResult(executor=self.name, status="failed", summary="fake failure")
        record["finished_at"] = time.time()
        (self.sync_dir / f"{context.task_key}.finished.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
        return ExecutorResult(executor=self.name, status="completed", summary="fake done")


class _Validator:
    name = "fake-validator"

    def run(self, context):
        return ValidatorResult(validator=self.name, status="passed", summary="fake passed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--task-key", required=True)
    parser.add_argument("--sync-dir", type=Path, required=True)
    parser.add_argument("--mode", default="pass", choices=("pass", "hold", "fail"))
    parser.add_argument("--expect", type=int, default=1)
    args = parser.parse_args()
    dispatcher = Dispatcher(
        db_path=args.db_path,
        executor_registry={"fake": _Executor(args.sync_dir, args.mode, args.expect)},
        validator_registry={"fake-validator": _Validator()},
        validators=("fake-validator",),
        default_executor="fake",
    )
    try:
        result = dispatcher.dispatch_task(args.task_key)
    finally:
        shutdown = getattr(dispatcher.store, "shutdown_runtime_supervisors", None)
        if shutdown is not None:
            shutdown()
    print(json.dumps({"task_key": result.task_key, "status": result.status, "summary": result.summary}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
