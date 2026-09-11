"""One idempotent runtime reaper call (V1 Step 4, SPEC §19.3).

:func:`reap_stale_runtime` is what a crashed runtime needs to become
recoverable: it expires every lease past its ``expires_at``
(``RuntimeAdmissionStore.expire_stale_leases``: the Attempt becomes
``execution_aborted``; a Ticket becomes ``failed`` (V1 Step 5, SPEC §29.2) and a
legacy task ``blocked``; both audited), then clears the
stale lock and PID markers those Attempts left behind
(``AttemptResourceManager.reap_stale_resources``). Worktrees, branches and
artifacts are kept.

Calling it again changes nothing. It never runs by itself: there is no daemon
and no cron. Step 5's scheduler loop is expected to call it, and
``scripts/reap_stale_runtime.py`` exposes it to operators.

It installs no schema. A database without the runtime-admission tables has no
lease to expire, and one without Attempt resources has no marker to reap; both
are reported as skipped.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
import sqlite3
from typing import Any

from agent_taskflow.attempt_resources import AttemptResourceManager
from agent_taskflow.models import require_absolute_path, utc_now_iso
from agent_taskflow.runtime_admission import RuntimeAdmissionStore

RUNNING_TASK_STATUSES = ("preparing", "implementing", "validating")


@dataclass(frozen=True)
class RuntimeReapResult:
    db_path: str
    expired_attempt_ids: tuple[str, ...]
    reaped_resource_attempt_ids: tuple[str, ...]
    blocked_live_pid_attempt_ids: tuple[str, ...]
    running_without_live_lease: tuple[str, ...]
    skipped: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key, value in payload.items():
            if isinstance(value, tuple):
                payload[key] = list(value)
        return payload


def _tables(db_path: Path) -> set[str]:
    if not db_path.is_file():
        return set()
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        return {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }


def running_without_live_lease(db_path: str | Path) -> list[str]:
    """Return Tickets in a running status that no live lease owns. Read-only."""
    path = require_absolute_path(db_path, "db_path")
    tables = _tables(path)
    if "tasks" not in tables:
        return []
    placeholders = ",".join("?" for _ in RUNNING_TASK_STATUSES)
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        if "runtime_leases" not in tables:
            rows = conn.execute(
                f"SELECT task_key FROM tasks WHERE status IN ({placeholders}) ORDER BY task_key",
                RUNNING_TASK_STATUSES,
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT task_key FROM tasks
                WHERE status IN ({placeholders})
                  AND NOT EXISTS (
                      SELECT 1 FROM runtime_leases
                      WHERE runtime_leases.task_id = tasks.task_id
                        AND runtime_leases.is_active = 1
                        AND julianday(runtime_leases.expires_at) > julianday(?)
                  )
                ORDER BY task_key
                """,
                (*RUNNING_TASK_STATUSES, utc_now_iso()),
            ).fetchall()
    return [str(row[0]) for row in rows]


def reap_stale_runtime(db_path: str | Path) -> RuntimeReapResult:
    """Expire stale leases, then reap stale lock/PID markers. Idempotent."""
    path = require_absolute_path(db_path, "db_path")
    tables = _tables(path)
    skipped: list[str] = []

    expired: list[str] = []
    if {"runtime_leases", "attempts", "tasks"} <= tables:
        expired = RuntimeAdmissionStore(path).expire_stale_leases()
    else:
        skipped.append("lease expiry: runtime_leases schema is not installed")

    resources = {"reaped_attempt_ids": [], "blocked_live_pid_attempt_ids": []}
    if {"attempt_resources", "runtime_leases", "attempts"} <= tables:
        resources = AttemptResourceManager(path).reap_stale_resources()
    else:
        skipped.append("resource reaping: attempt_resources schema is not installed")

    return RuntimeReapResult(
        db_path=str(path),
        expired_attempt_ids=tuple(expired),
        reaped_resource_attempt_ids=tuple(resources["reaped_attempt_ids"]),
        blocked_live_pid_attempt_ids=tuple(resources["blocked_live_pid_attempt_ids"]),
        running_without_live_lease=tuple(running_without_live_lease(path)),
        skipped=tuple(skipped),
    )


__all__ = [
    "RUNNING_TASK_STATUSES",
    "RuntimeReapResult",
    "reap_stale_runtime",
    "running_without_live_lease",
]
