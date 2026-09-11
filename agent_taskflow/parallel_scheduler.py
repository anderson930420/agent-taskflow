"""The parallel scheduler tick (SPEC §20, §21; V1 Step 5).

One call of :func:`run_scheduler_tick` is one idempotent pass of::

    reap stale runtime                 (Step 4's reaper)
    release / stop dependencies        (SPEC §5.3, §5.4)
    while capacity_available:
        pick the next eligible Ticket  (ready_queue: priority, FIFO, key)
        prepare its worktree           (SPEC §9: one worktree per Ticket)
        start an executor              (a worker process that claims atomically)

There is no daemon, no cron and no background thread. Each started Ticket runs
in its own one-shot worker process: the worker's Dispatcher claims the Ticket
through the Step 4 claim transaction, whose capacity check is the authority on
``max_concurrent_tasks``, so several ticks, or a tick racing a manual start,
can never exceed it. The tick waits for each worker's claim before it picks the
next Ticket, so it starts at most the free capacity and never starts a Ticket
twice. ``wait=True`` (the CLI default) also waits for the workers to finish.

Integration handoff is not wired here (FOLLOWUPS F4, ruling 21).
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from typing import Any, Callable
from uuid import uuid4

from agent_taskflow.models import require_absolute_path
from agent_taskflow.ready_queue import eligible_tickets
from agent_taskflow.runtime_capacity import (
    count_active_executor_leases_in_connection,
    read_runtime_capacity,
)
from agent_taskflow.runtime_reaper import reap_stale_runtime
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.ticket_dependencies import (
    DependencyMaintenanceResult,
    maintain_dependencies,
)
from agent_taskflow.ticket_lifecycle import FAILURE_WORKTREE, ticket_failure_status
from agent_taskflow.ticket_worktree import ensure_ticket_worktree

SCHEDULER_SOURCE = "parallel_scheduler"
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
WORKER_MODULE = "agent_taskflow.scheduler_worker"
DEFAULT_CLAIM_TIMEOUT_SECONDS = 60.0
_POLL_SECONDS = 0.05

Launcher = Callable[[Path, str], subprocess.Popen]


@dataclass(frozen=True)
class StartedTicket:
    task_key: str
    attempt_id: str
    pid: int

    def to_dict(self) -> dict[str, Any]:
        return {"task_key": self.task_key, "attempt_id": self.attempt_id, "pid": self.pid}


@dataclass
class SchedulerTickResult:
    db_path: str
    reap: dict[str, Any]
    dependencies: dict[str, Any]
    max_concurrent_tasks: int
    active_leases_at_start: int
    candidates: tuple[str, ...]
    started: tuple[StartedTicket, ...]
    preparation_failed: tuple[tuple[str, str], ...]
    not_started: tuple[tuple[str, str], ...]
    worker_results: tuple[dict[str, Any], ...] = ()
    workers: list[subprocess.Popen] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_path": self.db_path,
            "reap": self.reap,
            "dependencies": self.dependencies,
            "max_concurrent_tasks": self.max_concurrent_tasks,
            "active_leases_at_start": self.active_leases_at_start,
            "candidates": list(self.candidates),
            "started": [s.to_dict() for s in self.started],
            "preparation_failed": [
                {"task_key": key, "reason": reason} for key, reason in self.preparation_failed
            ],
            "not_started": [{"task_key": key, "reason": reason} for key, reason in self.not_started],
            "worker_results": list(self.worker_results),
        }


def _worker_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(PACKAGE_ROOT) if not existing else f"{PACKAGE_ROOT}{os.pathsep}{existing}"
    return env


def default_launcher(db_path: Path, task_key: str) -> subprocess.Popen:
    """Start one production worker; its output goes to the Ticket's artifact dir."""
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT artifact_dir FROM tasks WHERE task_key = ?", (task_key,)).fetchone()
    log_dir = Path(row[0]) if row is not None and row[0] else db_path.parent
    log_dir.mkdir(parents=True, exist_ok=True)
    # One log per launch, so a result line always belongs to this worker.
    log_path = log_dir / f"scheduler-worker-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid4().hex[:8]}.log"
    log = log_path.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", WORKER_MODULE, "--db-path", str(db_path), "--task-key", task_key],
            cwd=PACKAGE_ROOT,
            env=_worker_env(),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
    finally:
        log.close()
    process.log_path = log_path  # type: ignore[attr-defined]
    return process


def _active_leases(db_path: Path) -> int:
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        return count_active_executor_leases_in_connection(conn)


def _attempt_snapshot(db_path: Path, task_key: str) -> dict[str, str]:
    """attempt_id -> status for every Attempt of the Ticket."""
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "attempts" not in tables:
            return {}
        rows = conn.execute(
            """
            SELECT attempts.attempt_id, attempts.status FROM attempts
            JOIN tasks ON tasks.task_id = attempts.task_id
            WHERE tasks.task_key = ?
            """,
            (task_key,),
        ).fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


def _claimed_attempt(before: dict[str, str], after: dict[str, str]) -> str | None:
    """The Attempt this worker's claim created (or adopted), if any."""
    for attempt_id, status in after.items():
        if attempt_id not in before:
            return attempt_id
        if before[attempt_id] == "created" and status != "created":
            return attempt_id
    return None


def _worker_output(process: subprocess.Popen, timeout: float | None = None) -> dict[str, Any]:
    stdout, stderr = process.communicate(timeout=timeout)
    result: dict[str, Any] = {"pid": process.pid, "returncode": process.returncode}
    log_path = getattr(process, "log_path", None)
    if stdout is None and log_path is not None:
        # The default launcher writes to the Ticket's worker log; its last
        # JSON line is this worker's result.
        result["log_path"] = str(log_path)
        try:
            stdout = Path(log_path).read_text(encoding="utf-8")[-20000:]
        except OSError:
            stdout = None
    for line in reversed((stdout or "").splitlines()):
        try:
            result.update(json.loads(line))
            break
        except ValueError:
            continue
    if stderr:
        result["stderr_tail"] = stderr[-2000:]
    return result


def _await_claim(
    db_path: Path,
    task_key: str,
    process: subprocess.Popen,
    before: dict[str, str],
    timeout: float,
) -> tuple[str | None, str]:
    """Wait until the worker has claimed (attempt id) or given up (reason)."""
    deadline = time.monotonic() + timeout
    while True:
        claimed = _claimed_attempt(before, _attempt_snapshot(db_path, task_key))
        if claimed is not None:
            return claimed, "claimed"
        if process.poll() is not None:
            claimed = _claimed_attempt(before, _attempt_snapshot(db_path, task_key))
            if claimed is not None:
                return claimed, "claimed"
            reason = f"worker exited with {process.returncode} before claiming"
            if process.stdout is not None or getattr(process, "log_path", None) is not None:
                output = _worker_output(process)
                reason = str(output.get("summary") or output.get("stderr_tail") or reason)
            return None, reason
        if time.monotonic() > deadline:
            process.kill()
            process.wait()
            return None, f"no claim observed within {timeout:g}s; worker stopped"
        time.sleep(_POLL_SECONDS)


def run_scheduler_tick(
    db_path: str | Path,
    *,
    launcher: Launcher | None = None,
    wait: bool = True,
    claim_timeout_seconds: float = DEFAULT_CLAIM_TIMEOUT_SECONDS,
    actor: str = SCHEDULER_SOURCE,
) -> SchedulerTickResult:
    """Run one scheduler tick against ``db_path``. Idempotent; see the module doc."""
    path = require_absolute_path(db_path, "db_path")
    launch = launcher or default_launcher

    reap = reap_stale_runtime(path)
    dependencies = maintain_dependencies(path, actor=actor)
    capacity = read_runtime_capacity(path).max_concurrent_tasks
    active_at_start = _active_leases(path)
    candidates = eligible_tickets(path)

    started: list[StartedTicket] = []
    preparation_failed: list[tuple[str, str]] = []
    not_started: list[tuple[str, str]] = []
    workers: list[subprocess.Popen] = []
    store = TaskMirrorStore(path)

    for candidate in candidates:
        if _active_leases(path) >= capacity:
            break
        worktree = ensure_ticket_worktree(path, candidate.task_key, source=actor)
        if not worktree.ok:
            reason = worktree.reason or "Ticket worktree preparation failed"
            try:
                store.update_task_status(
                    candidate.task_key,
                    ticket_failure_status(FAILURE_WORKTREE),
                    source=actor,
                    message=reason,
                    expected_current_status=candidate.status,
                )
            except (KeyError, ValueError) as exc:
                # Someone else moved the Ticket first; leave it to them.
                not_started.append((candidate.task_key, f"{reason}; not marked failed: {exc}"))
                continue
            preparation_failed.append((candidate.task_key, reason))
            continue
        before = _attempt_snapshot(path, candidate.task_key)
        process = launch(path, candidate.task_key)
        attempt_id, reason = _await_claim(
            path, candidate.task_key, process, before, claim_timeout_seconds
        )
        if attempt_id is None:
            not_started.append((candidate.task_key, reason))
            if "runtime_capacity_exceeded" in reason:
                break
            continue
        started.append(StartedTicket(candidate.task_key, attempt_id, process.pid))
        workers.append(process)

    worker_results: list[dict[str, Any]] = []
    if wait:
        for process in workers:
            worker_results.append(_worker_output(process))

    return SchedulerTickResult(
        db_path=str(path),
        reap=reap.to_dict(),
        dependencies=dependencies.to_dict(),
        max_concurrent_tasks=capacity,
        active_leases_at_start=active_at_start,
        candidates=tuple(c.task_key for c in candidates),
        started=tuple(started),
        preparation_failed=tuple(preparation_failed),
        not_started=tuple(not_started),
        worker_results=tuple(worker_results),
        workers=workers,
    )


__all__ = [
    "DEFAULT_CLAIM_TIMEOUT_SECONDS",
    "DependencyMaintenanceResult",
    "SCHEDULER_SOURCE",
    "SchedulerTickResult",
    "StartedTicket",
    "default_launcher",
    "run_scheduler_tick",
]
