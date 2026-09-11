"""V1 Step 4 concurrency-readiness rehearsal (SPEC §19.1-§19.3).

The rehearsal proves, against disposable databases only, the three properties
§19 requires before ``max_concurrent_tasks`` may exceed 1:

§19.1 atomic claim
    N threads and N separate processes race to claim one Ticket, through the
    explicit ``RuntimeAdmissionStore.claim()`` API and through the dispatcher's
    ``preparing`` path. Exactly one wins, every loser gets a typed refusal, and
    exactly one Attempt and one lease exist afterwards.

§19.2 concurrent writes
    N runtime processes write state, Attempt, lease, ObservedStep and evidence
    at once, starting behind another process that holds the write lock. No row
    is lost, ``PRAGMA integrity_check`` is ``ok``, the lifecycle event log
    replays without an invalid transition, and the lock waits are counted.

§19.3 crash recovery
    A process holding a lease through the dispatcher path is SIGKILLed mid-run.
    The lease expires on schedule, :func:`reap_stale_runtime` recovers the
    Ticket, ``scripts/reset_task_status.py`` (the existing retry path) brings
    it back, nothing stays running, ownership is never doubled, and the killed
    Attempt stays readable with its events.

:func:`run_concurrency_rehearsal` runs all three in a fresh output directory and
writes ``concurrency-rehearsal.json``, the evidence
:mod:`agent_taskflow.concurrency_gate` checks. Every database it opens lives
inside that directory; the default state database is never used. No real
executor runs and nothing contacts GitHub.

The same fixtures, race harness and checkers back the Step 4 unit tests.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass
import json
import os
from pathlib import Path
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

import agent_taskflow.canonical_runtime_path as canonical_path
import agent_taskflow.dispatcher as dispatcher_module
from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.attempt_models import ActiveAttemptExistsError
from agent_taskflow.concurrency_gate import (
    CONCURRENCY_EVIDENCE_FILENAME,
    CONCURRENCY_REHEARSAL_SCHEMA_VERSION,
    REQUIRED_CONCURRENCY_CHECKS,
    git_head,
)
from agent_taskflow.lifecycle_control_schema import (
    ATTEMPT_TRANSITIONS,
    RECOVERY_ATTEMPT_TRANSITIONS,
)
from agent_taskflow.models import TaskRecord, utc_now_iso
from agent_taskflow.runtime_admission import (
    LeaseOwnershipError,
    RuntimeAdmissionError,
    RuntimeAdmissionStore,
)
from agent_taskflow.runtime_progress import RUNTIME_STEPS
from agent_taskflow.runtime_progress_schema import migrate_runtime_progress
from agent_taskflow.runtime_progress_store import RuntimeProgressStore
from agent_taskflow.runtime_reaper import reap_stale_runtime, running_without_live_lease
from agent_taskflow.store import TaskMirrorStore, connect

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
WORKER_MODULE = "agent_taskflow.concurrency_rehearsal_worker"
REHEARSAL_PROJECT = "step4-concurrency-rehearsal"

# Refusals a losing claimer may legitimately get. Anything else (a SQLite
# error in particular) is a failure, not a refusal.
_EXPLICIT_REFUSAL_TYPES: tuple[type[BaseException], ...] = (
    RuntimeAdmissionError,
    ActiveAttemptExistsError,
)
# The dispatcher's canonical store compares the Ticket status before it
# claims, and raises a plain ValueError when a winner already moved it.
_DISPATCHER_REFUSAL_TYPES = _EXPLICIT_REFUSAL_TYPES + (ValueError,)
EXPLICIT_CLAIM_REFUSALS = (
    "RuntimeAdmissionError",
    "RuntimeCapacityExceededError",
    "ActiveAttemptExistsError",
)
DISPATCHER_PATH_REFUSALS = EXPLICIT_CLAIM_REFUSALS + ("ValueError",)

__all__ = [
    "DISPATCHER_PATH_REFUSALS",
    "EXPLICIT_CLAIM_REFUSALS",
    "RUNTIME_STEPS",
    "RehearsalFixture",
    "add_rehearsal_task",
    "create_rehearsal_fixture",
    "dispatcher_preparing_claim",
    "dispatcher_runtime_store",
    "explicit_claim",
    "integrity_errors",
    "lifecycle_log_errors",
    "ownership_violations",
    "read_worker_line",
    "rehearse_atomic_claim",
    "rehearse_concurrent_writes",
    "rehearse_crash_recovery",
    "run_concurrency_rehearsal",
    "run_thread_race",
    "run_worker_processes",
    "start_worker_process",
]


# -- fixtures ----------------------------------------------------------------


@dataclass(frozen=True)
class RehearsalFixture:
    root: Path
    db_path: Path
    repo_path: Path
    artifact_root: Path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Step 4 Rehearsal",
            "-c",
            "user.email=step4-rehearsal@example.invalid",
            *args,
        ],
        cwd=repo,
        shell=False,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def create_rehearsal_fixture(root: str | Path) -> RehearsalFixture:
    """Create a fresh disposable database, git repository and artifact root.

    The database gets the full runtime schema the installed runtime path uses,
    plus Step 3's progress tables. The capacity control is not installed.
    """
    base = Path(root).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=False)
    fixture = RehearsalFixture(
        root=base,
        db_path=base / "state.db",
        repo_path=base / "repo",
        artifact_root=base / "artifacts",
    )
    fixture.repo_path.mkdir()
    fixture.artifact_root.mkdir()
    _git(fixture.repo_path, "init", "-q", "-b", "main")
    (fixture.repo_path / "README.md").write_text("Step 4 rehearsal\n", encoding="utf-8")
    _git(fixture.repo_path, "add", "README.md")
    _git(fixture.repo_path, "commit", "-q", "-m", "rehearsal fixture")
    TaskMirrorStore(fixture.db_path).init_db()
    canonical_path.canonical_runtime_task_store(fixture.db_path).init_db()
    migrate_runtime_progress(fixture.db_path)
    return fixture


def add_rehearsal_task(
    fixture: RehearsalFixture,
    task_key: str,
    *,
    status: str = "queued",
) -> None:
    TaskMirrorStore(fixture.db_path).upsert_task(
        TaskRecord(
            task_key=task_key,
            project=REHEARSAL_PROJECT,
            board=REHEARSAL_PROJECT,
            title=f"Step 4 rehearsal {task_key}",
            status=status,
            repo_path=fixture.repo_path,
            artifact_dir=fixture.artifact_root / task_key,
            executor="manual",
        )
    )


# -- claim attempts ----------------------------------------------------------


def _refusal(exc: BaseException, allowed: tuple[type[BaseException], ...]) -> dict[str, Any]:
    return {
        "outcome": "refused" if isinstance(exc, allowed) else "error",
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


def explicit_claim(
    db_path: str | Path,
    task_key: str,
    *,
    owner_id: str,
    ttl_seconds: int = 600,
    runtime_admission: bool = False,
) -> dict[str, Any]:
    """Claim through ``RuntimeAdmissionStore.claim()``; never raises.

    ``runtime_admission=True`` uses the admission store the installed runtime
    path uses, which also adopts a reset-reserved retry Attempt.
    """
    store = (
        canonical_path.CanonicalRuntimeAdmissionStore(Path(db_path))
        if runtime_admission
        else RuntimeAdmissionStore(Path(db_path))
    )
    try:
        claim = store.claim(task_key, owner_id=owner_id, ttl_seconds=ttl_seconds)
    except Exception as exc:  # noqa: BLE001 - classified, not swallowed.
        return {**_refusal(exc, _EXPLICIT_REFUSAL_TYPES), "owner_id": owner_id}
    return {
        "outcome": "claimed",
        "task_key": claim.task_key,
        "attempt_id": claim.attempt_id,
        "lease_id": claim.lease_id,
        "owner_id": claim.owner_id,
    }


def dispatcher_runtime_store(db_path: Path, lease_ttl_seconds: int | None) -> Any:
    store = dispatcher_module.Dispatcher(db_path=db_path).store
    if lease_ttl_seconds is not None:
        store = type(store)(db_path, lease_ttl_seconds=lease_ttl_seconds)
    return store


def dispatcher_preparing_claim(
    db_path: str | Path,
    task_key: str,
    *,
    source: str,
) -> dict[str, Any]:
    """Claim the way ``Dispatcher.dispatch_task`` does; never raises.

    It calls ``update_task_status(task, "preparing")`` on the store the
    dispatcher is constructed with, which is the installed runtime store.
    The heartbeat thread is stopped afterwards; the lease stays active.
    """
    store = dispatcher_runtime_store(Path(db_path), None)
    try:
        store.update_task_status(
            task_key,
            "preparing",
            source=source,
            message="Step 4 rehearsal dispatcher claim",
        )
        claim = store.runtime_claim(task_key)
    except Exception as exc:  # noqa: BLE001 - classified, not swallowed.
        return {**_refusal(exc, _DISPATCHER_REFUSAL_TYPES), "owner_id": source}
    finally:
        store.shutdown_runtime_supervisors()
    return {
        "outcome": "claimed",
        "task_key": task_key,
        "attempt_id": claim.attempt_id,
        "lease_id": claim.lease_id,
        "owner_id": claim.owner_id,
    }


def run_thread_race(callables: Sequence[Callable[[], dict[str, Any]]]) -> list[dict[str, Any]]:
    """Release every callable at once from its own thread; keep call order."""
    barrier = threading.Barrier(len(callables))
    results: list[dict[str, Any] | None] = [None] * len(callables)

    def run(index: int, fn: Callable[[], dict[str, Any]]) -> None:
        barrier.wait()
        try:
            results[index] = fn()
        except Exception as exc:  # noqa: BLE001 - reported as an error outcome.
            results[index] = {
                "outcome": "error",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

    threads = [
        threading.Thread(target=run, args=(index, fn), name=f"step4-race-{index}")
        for index, fn in enumerate(callables)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return [item or {"outcome": "error", "error": "no result"} for item in results]


# -- separate processes ------------------------------------------------------


def _worker_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(PACKAGE_ROOT) if not existing else f"{PACKAGE_ROOT}{os.pathsep}{existing}"
    )
    return env


def start_worker_process(
    request: Mapping[str, Any],
    *,
    sync_dir: Path | None = None,
    index: int = 0,
) -> subprocess.Popen[str]:
    """Start one worker process for ``request`` (see the worker module)."""
    command = [sys.executable, "-m", WORKER_MODULE, "--request", json.dumps(dict(request))]
    if sync_dir is not None:
        command += ["--sync-dir", str(sync_dir), "--index", str(index)]
    return subprocess.Popen(
        command,
        cwd=PACKAGE_ROOT,
        env=_worker_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def read_worker_line(process: subprocess.Popen[str], *, timeout: float) -> dict[str, Any]:
    """Read one JSON line from a running worker, or fail with its stderr."""
    assert process.stdout is not None
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    line = process.stdout.readline() if ready else ""
    if not line:
        process.kill()
        _, stderr = process.communicate(timeout=30)
        raise RuntimeError(f"worker produced no output: {stderr[-4000:]}")
    return json.loads(line)


def run_worker_processes(
    requests: Sequence[Mapping[str, Any]],
    *,
    timeout: float = 120.0,
) -> list[dict[str, Any]]:
    """Run one process per request and release them all at the same moment.

    Each worker imports and prepares, reports ready, then waits for a shared
    go signal, so the requests contend for real. Results keep request order.
    """
    sync_dir = Path(tempfile.mkdtemp(prefix="step4-sync-"))
    processes: list[subprocess.Popen[str]] = []
    try:
        processes = [
            start_worker_process(request, sync_dir=sync_dir, index=index)
            for index, request in enumerate(requests)
        ]
        deadline = time.monotonic() + timeout
        pending = set(range(len(processes)))
        while pending:
            for index in list(pending):
                if (sync_dir / f"ready-{index}").exists():
                    pending.discard(index)
                elif processes[index].poll() is not None:
                    _, stderr = processes[index].communicate()
                    raise RuntimeError(
                        f"worker {index} exited before it was ready: {stderr[-4000:]}"
                    )
            if pending and time.monotonic() > deadline:
                raise RuntimeError("workers did not become ready in time")
            if pending:
                time.sleep(0.01)
        (sync_dir / "go").write_text("go", encoding="utf-8")
        results: list[dict[str, Any]] = []
        for index, process in enumerate(processes):
            stdout, stderr = process.communicate(
                timeout=max(1.0, deadline - time.monotonic())
            )
            lines = [line for line in stdout.splitlines() if line.strip()]
            if process.returncode != 0 or not lines:
                raise RuntimeError(
                    f"worker {index} failed with {process.returncode}: {stderr[-4000:]}"
                )
            results.append(json.loads(lines[-1]))
        return results
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
        shutil.rmtree(sync_dir, ignore_errors=True)


# -- checkers ----------------------------------------------------------------


def _rows(db_path: Path, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    with closing(connect(db_path)) as conn:
        return conn.execute(sql, tuple(params)).fetchall()


def integrity_errors(db_path: str | Path) -> list[str]:
    """Return ``PRAGMA integrity_check`` findings; ``[]`` means ``ok``."""
    rows = [str(row[0]) for row in _rows(Path(db_path), "PRAGMA integrity_check")]
    return [] if rows == ["ok"] else rows


def foreign_key_violations(db_path: str | Path) -> list[list[Any]]:
    return [list(row) for row in _rows(Path(db_path), "PRAGMA foreign_key_check")]


_ALLOWED_EDGES = set(ATTEMPT_TRANSITIONS)
_RECOVERY_EDGES = {(source, target): reason for source, target, reason in RECOVERY_ATTEMPT_TRANSITIONS}
_ATTEMPT_CREATION_STATUSES = frozenset({"created", "preparing"})


def lifecycle_log_errors(db_path: str | Path) -> list[str]:
    """Replay every Attempt's ``lifecycle_events`` against the Attempt graph.

    The first event creates the Attempt (it ends in ``created`` or
    ``preparing``). Every later event must continue the chain, and either keep
    the status (a heartbeat) or follow an edge of the forward-only graph in
    ``lifecycle_control_schema``. The chain must end at the Attempt's
    persisted status.
    """
    path = Path(db_path)
    errors: list[str] = []
    attempts = _rows(path, "SELECT attempt_id, status FROM attempts ORDER BY attempt_id")
    for attempt in attempts:
        events = _rows(
            path,
            """
            SELECT event_id, from_status, to_status, reason_code
            FROM lifecycle_events WHERE attempt_id = ? ORDER BY event_id
            """,
            (attempt["attempt_id"],),
        )
        attempt_id = attempt["attempt_id"]
        if not events:
            errors.append(f"{attempt_id}: no lifecycle events")
            continue
        if events[0]["to_status"] not in _ATTEMPT_CREATION_STATUSES:
            errors.append(
                f"{attempt_id}: first event ends in {events[0]['to_status']!r}, "
                "not an Attempt creation status"
            )
        previous = events[0]["to_status"]
        for event in events[1:]:
            source, target = event["from_status"], event["to_status"]
            if source != previous:
                errors.append(
                    f"{attempt_id}: discontinuous chain at event {event['event_id']}: "
                    f"{previous!r} then {source!r}"
                )
            legal = (
                source == target
                or (source, target) in _ALLOWED_EDGES
                or _RECOVERY_EDGES.get((source, target)) == event["reason_code"]
            )
            if not legal:
                errors.append(
                    f"{attempt_id}: illegal transition {source} -> {target} "
                    f"({event['reason_code']}) at event {event['event_id']}"
                )
            previous = target
        if previous != attempt["status"]:
            errors.append(
                f"{attempt_id}: replay ends at {previous!r}, row is {attempt['status']!r}"
            )
    return errors


def ownership_violations(db_path: str | Path) -> list[str]:
    """Return every breach of "one active owner per Attempt" (§44)."""
    path = Path(db_path)
    violations: list[str] = []
    for row in _rows(
        path,
        """
        SELECT task_id, COUNT(*) AS leases FROM runtime_leases
        WHERE is_active = 1 GROUP BY task_id HAVING COUNT(*) > 1
        """,
    ):
        violations.append(f"task {row['task_id']} has {row['leases']} active leases")
    for row in _rows(
        path,
        """
        SELECT task_id, COUNT(*) AS attempts FROM attempts
        WHERE is_active = 1 GROUP BY task_id HAVING COUNT(*) > 1
        """,
    ):
        violations.append(f"task {row['task_id']} has {row['attempts']} active Attempts")
    for row in _rows(
        path,
        """
        SELECT runtime_leases.lease_id, runtime_leases.attempt_id,
               attempts.is_active AS attempt_active,
               tasks.active_attempt_id
        FROM runtime_leases
        JOIN attempts ON attempts.attempt_id = runtime_leases.attempt_id
        JOIN tasks ON tasks.task_id = runtime_leases.task_id
        WHERE runtime_leases.is_active = 1
        """,
    ):
        if not row["attempt_active"]:
            violations.append(
                f"lease {row['lease_id']} is active for closed Attempt {row['attempt_id']}"
            )
        if row["active_attempt_id"] != row["attempt_id"]:
            violations.append(
                f"lease {row['lease_id']} owns Attempt {row['attempt_id']} but the "
                f"Ticket points at {row['active_attempt_id']}"
            )
    return violations


# -- §19.1 atomic claim ------------------------------------------------------


def _single_owner_state(db_path: Path, task_key: str, winner: dict[str, Any] | None) -> dict[str, Any]:
    task = _rows(
        db_path,
        "SELECT task_id, status, active_attempt_id FROM tasks WHERE task_key = ?",
        (task_key,),
    )[0]
    attempts = _rows(
        db_path, "SELECT attempt_id FROM attempts WHERE task_id = ?", (task["task_id"],)
    )
    leases = _rows(
        db_path,
        "SELECT attempt_id, is_active FROM runtime_leases WHERE task_id = ?",
        (task["task_id"],),
    )
    claims = _rows(
        db_path,
        """
        SELECT COUNT(*) FROM lifecycle_events
        WHERE task_id = ? AND to_status = 'preparing' AND from_status <> 'preparing'
        """,
        (task["task_id"],),
    )[0][0]
    ok = bool(
        winner is not None
        and len(attempts) == 1
        and len(leases) == 1
        and leases[0]["is_active"] == 1
        and task["status"] == "preparing"
        and task["active_attempt_id"] == attempts[0]["attempt_id"]
        and leases[0]["attempt_id"] == winner.get("attempt_id")
        and claims == 1
    )
    return {"ok": ok, "attempts": len(attempts), "leases": len(leases), "claim_events": claims}


def _race_summary(
    db_path: Path,
    task_key: str,
    outcomes: list[dict[str, Any]],
    allowed: tuple[str, ...],
) -> dict[str, Any]:
    winners = [item for item in outcomes if item.get("outcome") == "claimed"]
    losers = [item for item in outcomes if item.get("outcome") != "claimed"]
    state = _single_owner_state(db_path, task_key, winners[0] if len(winners) == 1 else None)
    return {
        "claimers": len(outcomes),
        "winners": len(winners),
        "losers_typed": all(
            item.get("outcome") == "refused" and item.get("error_type") in allowed
            for item in losers
        ),
        "refusal_types": dict(Counter(str(item.get("error_type")) for item in losers)),
        "distinct_pids": len({item["pid"] for item in outcomes if "pid" in item}),
        "single_winner": len(winners) == 1 and state["ok"],
        **state,
    }


def rehearse_atomic_claim(
    workdir: str | Path,
    *,
    threads: int = 8,
    processes: int = 4,
) -> dict[str, Any]:
    fixture = create_rehearsal_fixture(Path(workdir))
    db = fixture.db_path
    races: dict[str, dict[str, Any]] = {}

    add_rehearsal_task(fixture, "AT-S4-CLAIM-THREADS")
    races["threads_explicit"] = _race_summary(
        db,
        "AT-S4-CLAIM-THREADS",
        run_thread_race(
            [
                (lambda index=index: explicit_claim(
                    db, "AT-S4-CLAIM-THREADS", owner_id=f"thread-{index}"
                ))
                for index in range(threads)
            ]
        ),
        EXPLICIT_CLAIM_REFUSALS,
    )

    add_rehearsal_task(fixture, "AT-S4-CLAIM-PROCS")
    races["processes_explicit"] = _race_summary(
        db,
        "AT-S4-CLAIM-PROCS",
        run_worker_processes(
            [
                {
                    "op": "claim",
                    "db_path": str(db),
                    "task_key": "AT-S4-CLAIM-PROCS",
                    "owner_id": f"process-{index}",
                }
                for index in range(processes)
            ]
        ),
        EXPLICIT_CLAIM_REFUSALS,
    )

    add_rehearsal_task(fixture, "AT-S4-DISPATCH-THREADS")
    races["threads_dispatcher"] = _race_summary(
        db,
        "AT-S4-DISPATCH-THREADS",
        run_thread_race(
            [
                (lambda index=index: dispatcher_preparing_claim(
                    db, "AT-S4-DISPATCH-THREADS", source=f"dispatcher-thread-{index}"
                ))
                for index in range(threads)
            ]
        ),
        DISPATCHER_PATH_REFUSALS,
    )

    add_rehearsal_task(fixture, "AT-S4-DISPATCH-PROCS")
    races["processes_dispatcher"] = _race_summary(
        db,
        "AT-S4-DISPATCH-PROCS",
        run_worker_processes(
            [
                {
                    "op": "dispatcher-claim",
                    "db_path": str(db),
                    "task_key": "AT-S4-DISPATCH-PROCS",
                    "owner_id": f"dispatcher-process-{index}",
                }
                for index in range(processes)
            ]
        ),
        DISPATCHER_PATH_REFUSALS,
    )

    process_races_distinct = all(
        races[name]["distinct_pids"] == processes
        for name in ("processes_explicit", "processes_dispatcher")
    )
    checks = {
        "atomic_claim_threads_explicit_single_winner": races["threads_explicit"]["single_winner"],
        "atomic_claim_processes_explicit_single_winner": (
            races["processes_explicit"]["single_winner"] and process_races_distinct
        ),
        "atomic_claim_threads_dispatcher_single_winner": races["threads_dispatcher"]["single_winner"],
        "atomic_claim_processes_dispatcher_single_winner": (
            races["processes_dispatcher"]["single_winner"] and process_races_distinct
        ),
        "atomic_claim_losers_typed_refusal": all(race["losers_typed"] for race in races.values()),
        "atomic_claim_single_attempt_and_lease": all(
            race["attempts"] == 1 and race["leases"] == 1 for race in races.values()
        ),
    }
    return {"checks": checks, "details": {"races": races}, "databases": [str(db)]}


# -- §19.2 concurrent writes -------------------------------------------------


def _writer_rows_ok(
    db_path: Path,
    result: dict[str, Any],
    *,
    heartbeats: int,
    evidence_rows: int,
) -> list[str]:
    missing: list[str] = []
    key = result.get("task_key")
    attempt_id = result.get("attempt_id")
    if result.get("outcome") != "done":
        return [f"{key}: writer did not finish: {result}"]
    task = _rows(
        db_path,
        "SELECT task_id, status, active_attempt_id FROM tasks WHERE task_key = ?",
        (key,),
    )[0]
    if task["status"] != "waiting_approval" or task["active_attempt_id"] is not None:
        missing.append(f"{key}: Ticket state is {task['status']}/{task['active_attempt_id']}")
    attempts = _rows(
        db_path,
        "SELECT attempt_id, status, is_active FROM attempts WHERE task_id = ?",
        (task["task_id"],),
    )
    if [(row["attempt_id"], row["status"], row["is_active"]) for row in attempts] != [
        (attempt_id, "waiting_approval", 0)
    ]:
        missing.append(f"{key}: Attempt rows {[tuple(row) for row in attempts]}")
    leases = _rows(
        db_path,
        "SELECT is_active, release_reason FROM runtime_leases WHERE attempt_id = ?",
        (attempt_id,),
    )
    if [(row["is_active"], row["release_reason"]) for row in leases] != [
        (0, "runtime_waiting_approval")
    ]:
        missing.append(f"{key}: lease rows {[tuple(row) for row in leases]}")
    reasons = [
        row[0]
        for row in _rows(
            db_path,
            "SELECT reason_code FROM lifecycle_events WHERE attempt_id = ? ORDER BY event_id",
            (attempt_id,),
        )
    ]
    expected_reasons = (
        ["runtime_pickup_claimed"]
        + ["runtime_lease_heartbeat"] * heartbeats
        + ["runtime_implementing", "runtime_validating", "runtime_waiting_approval"]
    )
    if reasons != expected_reasons:
        missing.append(f"{key}: lifecycle events {reasons}")
    steps = _rows(
        db_path,
        "SELECT step_name, status FROM attempt_observed_steps WHERE attempt_id = ? "
        "ORDER BY step_order",
        (attempt_id,),
    )
    if [(row[0], row[1]) for row in steps] != [(step, "passed") for step in RUNTIME_STEPS]:
        missing.append(f"{key}: ObservedSteps {[tuple(row) for row in steps]}")
    progress = _rows(
        db_path,
        "SELECT current_phase FROM attempt_progress WHERE attempt_id = ?",
        (attempt_id,),
    )
    if [row[0] for row in progress] != [RUNTIME_STEPS[-1]]:
        missing.append(f"{key}: attempt_progress {[tuple(row) for row in progress]}")
    validations = _rows(
        db_path,
        "SELECT COUNT(*) FROM task_events WHERE task_key = ? AND event_type = 'note' "
        "AND payload_json LIKE '%validation_result%'",
        (key,),
    )[0][0]
    artifacts = _rows(
        db_path, "SELECT COUNT(*) FROM task_artifacts WHERE task_key = ?", (key,)
    )[0][0]
    if validations != evidence_rows or artifacts != evidence_rows:
        missing.append(f"{key}: evidence {validations} validations, {artifacts} artifacts")
    statuses = [
        row[0]
        for row in _rows(
            db_path,
            "SELECT json_extract(payload_json, '$.status') FROM task_events "
            "WHERE task_key = ? AND event_type = 'status_changed' ORDER BY id",
            (key,),
        )
    ]
    if statuses != ["preparing", "implementing", "validating", "waiting_approval"]:
        missing.append(f"{key}: Ticket status history {statuses}")
    return missing


def rehearse_concurrent_writes(
    workdir: str | Path,
    *,
    writers: int = 4,
    heartbeats: int = 5,
    evidence_rows: int = 4,
    hold_seconds: float = 0.4,
) -> dict[str, Any]:
    fixture = create_rehearsal_fixture(Path(workdir))
    db = fixture.db_path
    keys = [f"AT-S4-WRITE-{index}" for index in range(writers)]
    for key in keys:
        add_rehearsal_task(fixture, key)
    results = run_worker_processes(
        [{"op": "hold-lock", "db_path": str(db), "hold_seconds": hold_seconds}]
        + [
            {
                "op": "write-mix",
                "db_path": str(db),
                "task_key": key,
                "owner_id": f"writer-{index}",
                "heartbeats": heartbeats,
                "evidence_rows": evidence_rows,
                "artifact_root": str(fixture.artifact_root),
            }
            for index, key in enumerate(keys)
        ]
    )
    writer_results = [item for item in results if item.get("op") == "write-mix"]
    missing: list[str] = []
    for result in writer_results:
        missing.extend(
            _writer_rows_ok(db, result, heartbeats=heartbeats, evidence_rows=evidence_rows)
        )
    integrity = integrity_errors(db)
    lifecycle = lifecycle_log_errors(db)
    contention = {
        "lock_acquisitions": sum(
            item["contention"]["lock_acquisitions"] for item in writer_results
        ),
        "busy_waits": sum(item["contention"]["busy_waits"] for item in writer_results),
        "busy_wait_seconds_total": round(
            sum(item["contention"]["busy_wait_seconds_total"] for item in writer_results), 6
        ),
        "busy_wait_seconds_max": max(
            (item["contention"]["busy_wait_seconds_max"] for item in writer_results),
            default=0.0,
        ),
        "busy_timeouts": sum(item["contention"]["busy_timeouts"] for item in writer_results),
        "lock_holder_seconds": hold_seconds,
    }
    checks = {
        "concurrent_writes_no_lost_write": (
            len(writer_results) == writers
            and len({item["pid"] for item in results}) == writers + 1
            and not missing
        ),
        "concurrent_writes_integrity_check_ok": not integrity,
        "concurrent_writes_lifecycle_log_valid": not lifecycle,
        "concurrent_writes_contention_observable": (
            contention["busy_waits"] >= 1 and contention["lock_acquisitions"] > 0
        ),
    }
    return {
        "checks": checks,
        "details": {
            "writers": writers,
            "heartbeats_per_writer": heartbeats,
            "evidence_rows_per_writer": evidence_rows,
            "missing_or_wrong": missing,
            "integrity_check": integrity or ["ok"],
            "foreign_key_violations": foreign_key_violations(db),
            "lifecycle_log_errors": lifecycle,
            "contention": contention,
        },
        "databases": [str(db)],
    }


# -- §19.3 crash recovery ----------------------------------------------------


def _parse_utc(value: str) -> float:
    from datetime import datetime, timezone

    return (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        .astimezone(timezone.utc)
        .timestamp()
    )


def rehearse_crash_recovery(
    workdir: str | Path,
    *,
    lease_ttl_seconds: int = 2,
) -> dict[str, Any]:
    fixture = create_rehearsal_fixture(Path(workdir))
    db = fixture.db_path
    key = "AT-S4-CRASH"
    add_rehearsal_task(fixture, key)
    admission = RuntimeAdmissionStore(db)

    process = start_worker_process(
        {
            "op": "crash-holder",
            "db_path": str(db),
            "task_key": key,
            "owner_id": "doomed-runtime",
            "lease_ttl_seconds": lease_ttl_seconds,
        }
    )
    try:
        holder = read_worker_line(process, timeout=60)
        time.sleep(lease_ttl_seconds + 1)
        lease_at_kill = admission.get_lease(holder["lease_id"])
        killed_at = time.time()
        os.kill(process.pid, signal.SIGKILL)
        return_code = process.wait(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    heartbeat_outlived_ttl = bool(
        lease_at_kill is not None
        and lease_at_kill.is_active
        and killed_at - _parse_utc(holder["acquired_at"]) > lease_ttl_seconds
        and _parse_utc(lease_at_kill.expires_at) > killed_at
    )

    early = reap_stale_runtime(db)
    early_lease = admission.get_lease(holder["lease_id"])
    violations = {"after_kill": ownership_violations(db)}
    remaining = lease_ttl_seconds + 0.5 - (time.time() - killed_at)
    if remaining > 0:
        time.sleep(remaining)
    running_before_reap = running_without_live_lease(db)
    reap = reap_stale_runtime(db)
    second_reap = reap_stale_runtime(db)
    reaped_lease = admission.get_lease(holder["lease_id"])
    running_after_reap = running_without_live_lease(db)
    violations["after_reap"] = ownership_violations(db)

    reset = subprocess.run(
        [
            sys.executable,
            str(PACKAGE_ROOT / "scripts" / "reset_task_status.py"),
            "--task-key",
            key,
            "--db-path",
            str(db),
            "--from-status",
            "blocked",
            "--reason",
            "SIGKILLed runtime recovered by the Step 4 rehearsal",
            "--actor",
            "step4-rehearsal-operator",
            "--confirm-reset",
        ],
        cwd=PACKAGE_ROOT,
        env=_worker_env(),
        shell=False,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        reset_payload = json.loads(reset.stdout) if reset.returncode == 0 else {}
    except json.JSONDecodeError:
        reset_payload = {}
    retry = dispatcher_preparing_claim(db, key, source="recovered-runtime")
    running_after_retry = running_without_live_lease(db)
    violations["after_retry"] = ownership_violations(db)

    stale_refused: dict[str, bool] = {}
    for action in ("heartbeat", "release"):
        try:
            if action == "heartbeat":
                admission.heartbeat(
                    holder["attempt_id"],
                    owner_id=holder["owner_id"],
                    lease_token=holder["lease_token"],
                )
            else:
                admission.release(
                    holder["attempt_id"],
                    owner_id=holder["owner_id"],
                    lease_token=holder["lease_token"],
                    attempt_status="waiting_approval",
                    task_status="waiting_approval",
                    reason_code="late_release_from_killed_owner",
                )
            stale_refused[action] = False
        except LeaseOwnershipError:
            stale_refused[action] = True
    active_leases = _rows(
        db,
        """
        SELECT runtime_leases.attempt_id, runtime_leases.owner_id FROM runtime_leases
        JOIN tasks ON tasks.task_id = runtime_leases.task_id
        WHERE tasks.task_key = ? AND runtime_leases.is_active = 1
        """,
        (key,),
    )

    old_attempt = _rows(
        db,
        "SELECT status, is_active, execution_result FROM attempts WHERE attempt_id = ?",
        (holder["attempt_id"],),
    )[0]
    old_events = [
        (row["reason_code"], row["actor"])
        for row in _rows(
            db,
            "SELECT reason_code, actor FROM lifecycle_events WHERE attempt_id = ? "
            "ORDER BY event_id",
            (holder["attempt_id"],),
        )
    ]
    old_steps = [
        (step.name, step.status)
        for step in RuntimeProgressStore(db).list_steps(holder["attempt_id"])
    ]

    checks = {
        "crash_holder_sigkilled": bool(
            return_code == -signal.SIGKILL
            and holder.get("pid") != os.getpid()
            and holder.get("task_status") == "implementing"
        ),
        "crash_lease_expired": bool(
            heartbeat_outlived_ttl
            and early.expired_attempt_ids == ()
            and early_lease is not None
            and early_lease.is_active
            and reap.expired_attempt_ids == (holder["attempt_id"],)
            and reaped_lease is not None
            and not reaped_lease.is_active
            and reaped_lease.release_reason == "runtime_lease_expired"
            and second_reap.expired_attempt_ids == ()
        ),
        "crash_ticket_recovered_via_retry": bool(
            reset.returncode == 0
            and reset_payload.get("old_attempt_id") == holder["attempt_id"]
            and retry.get("outcome") == "claimed"
            and retry.get("attempt_id") == reset_payload.get("new_attempt_id")
        ),
        "crash_nothing_running_forever": bool(
            running_before_reap == [key]
            and running_after_reap == []
            and running_after_retry == []
        ),
        "crash_no_double_ownership": bool(
            not any(violations.values())
            and all(stale_refused.values())
            and len(active_leases) == 1
            and active_leases[0]["attempt_id"] == retry.get("attempt_id")
            and active_leases[0]["owner_id"] != holder["owner_id"]
        ),
        "crash_previous_attempt_auditable": bool(
            old_attempt["status"] == "execution_aborted"
            and old_attempt["is_active"] == 0
            and old_attempt["execution_result"] == "lease_expired"
            and old_events
            and old_events[0][0] == "canonical_runtime_pickup_claimed"
            and old_events[-1] == ("runtime_lease_expired", "runtime_lease_reaper")
            and old_steps == [("Prepare", "passed"), ("Implementer", "running")]
        ),
    }
    return {
        "checks": checks,
        "details": {
            "lease_ttl_seconds": lease_ttl_seconds,
            "holder_pid": holder.get("pid"),
            "holder_return_code": return_code,
            "killed_attempt_id": holder["attempt_id"],
            "heartbeat_outlived_ttl": heartbeat_outlived_ttl,
            "early_reap": early.to_dict(),
            "reap": reap.to_dict(),
            "second_reap": second_reap.to_dict(),
            "running_without_live_lease": {
                "before_reap": running_before_reap,
                "after_reap": running_after_reap,
                "after_retry": running_after_retry,
            },
            "reset_returncode": reset.returncode,
            "reset_stderr": reset.stderr[-2000:],
            "retry": {k: v for k, v in retry.items() if k != "lease_token"},
            "ownership_violations": violations,
            "stale_owner_refused": stale_refused,
            "old_attempt": dict(old_attempt),
            "old_attempt_events": [list(item) for item in old_events],
            "old_attempt_steps": [list(item) for item in old_steps],
        },
        "databases": [str(db)],
    }


# -- the rehearsal -----------------------------------------------------------


def _prepare_output_directory(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise FileExistsError(f"rehearsal output directory must be empty: {output_dir}")


def _run_section(
    section: str,
    run: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    try:
        return run()
    except Exception as exc:  # noqa: BLE001 - recorded as a failed section.
        return {
            "checks": {name: False for name in REQUIRED_CONCURRENCY_CHECKS[section]},
            "details": {"error": f"{type(exc).__name__}: {exc}"},
            "databases": [],
        }


def run_concurrency_rehearsal(
    *,
    output_dir: str | Path,
    repo_root: str | Path = PACKAGE_ROOT,
    threads: int = 8,
    processes: int = 4,
    heartbeats: int = 5,
    evidence_rows: int = 4,
    lease_ttl_seconds: int = 2,
) -> dict[str, Any]:
    """Run §19.1-§19.3 in a fresh ``output_dir`` and write the evidence JSON."""
    output = Path(output_dir).expanduser().resolve()
    root = Path(repo_root).expanduser().resolve()
    if root != PACKAGE_ROOT:
        raise ValueError(
            f"the rehearsal can only rehearse the code it runs from ({PACKAGE_ROOT}), "
            f"not {root}"
        )
    if threads < 2 or processes < 2:
        raise ValueError("a race needs at least two threads and two processes")
    _prepare_output_directory(output)
    started_at = utc_now_iso()
    sections = {
        "19.1": _run_section(
            "19.1",
            lambda: rehearse_atomic_claim(
                output / "atomic-claim", threads=threads, processes=processes
            ),
        ),
        "19.2": _run_section(
            "19.2",
            lambda: rehearse_concurrent_writes(
                output / "concurrent-writes",
                writers=processes,
                heartbeats=heartbeats,
                evidence_rows=evidence_rows,
            ),
        ),
        "19.3": _run_section(
            "19.3",
            lambda: rehearse_crash_recovery(
                output / "crash-recovery", lease_ttl_seconds=lease_ttl_seconds
            ),
        ),
    }
    databases = [path for section in sections.values() for path in section["databases"]]
    for path in databases:
        if not Path(path).is_relative_to(output):
            raise RuntimeError(f"rehearsal opened a database outside {output}: {path}")
    checks = {
        name: bool(sections[section]["checks"].get(name))
        for section, names in REQUIRED_CONCURRENCY_CHECKS.items()
        for name in names
    }
    evidence = {
        "schema_version": CONCURRENCY_REHEARSAL_SCHEMA_VERSION,
        "rehearsal_id": f"step4-{uuid4()}",
        "started_at": started_at,
        "generated_at": utc_now_iso(),
        "repo_root": str(PACKAGE_ROOT),
        "repo_sha": git_head(PACKAGE_ROOT),
        "parameters": {
            "threads": threads,
            "processes": processes,
            "writers": processes,
            "heartbeats_per_writer": heartbeats,
            "evidence_rows_per_writer": evidence_rows,
            "lease_ttl_seconds": lease_ttl_seconds,
        },
        "disposable_database": True,
        "production_database_touched": False,
        "databases": databases,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "details": {section: result["details"] for section, result in sections.items()},
        "safety": {
            "fresh_output_directory_required": True,
            "default_state_database_used": False,
            "real_executor_invoked": False,
            "github_contacted": False,
            "capacity_control_installed": False,
        },
    }
    atomic_write_json(output / CONCURRENCY_EVIDENCE_FILENAME, evidence, indent=2, sort_keys=True)
    return json.loads(json.dumps(evidence))
