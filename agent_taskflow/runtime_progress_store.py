"""Attempt-scoped runtime progress writes for V1 Step 3 (SPEC §14, §14.0).

This is the only writing surface Step 3 owns. It records exactly the three
things §14 lists as continuously updated by the execution runtime:

``current_phase``
``current_activity``
``ObservedStep``

and nothing else. It never writes a Ticket status, an Attempt status, a
lifecycle event, or any §32.1 PR field; the Python control plane owns lifecycle
(§2.1) and the Step 2 watcher owns PR state (§32.1). It also never shells out,
touches git, or calls GitHub.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_taskflow.execution_observability import ExecutionObservedStep
from agent_taskflow.models import require_absolute_path, utc_now_iso
from agent_taskflow.runtime_progress import (
    AttemptProgressSnapshot,
    assert_no_progress_estimate,
    observed_step,
    runtime_step_order,
    validate_runtime_step,
)
from agent_taskflow.runtime_progress_schema import migrate_runtime_progress
from agent_taskflow.store import connect, default_db_path
from agent_taskflow.tasks import normalize_task_key


_ATTEMPT_LOOKUP_SQL = """
    SELECT
        attempts.attempt_id AS attempt_id,
        attempts.task_id AS task_id,
        attempts.attempt_number AS attempt_number,
        attempts.is_active AS is_active,
        tasks.task_key AS task_key
    FROM attempts
    JOIN tasks ON tasks.task_id = attempts.task_id
"""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _decode_metadata(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


class RuntimeProgressStore:
    """SQLite access for attempt-scoped ``ObservedStep`` and current activity."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = (
            default_db_path()
            if db_path is None
            else require_absolute_path(db_path, "db_path")
        )

    def init_db(self) -> None:
        migrate_runtime_progress(self.db_path)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _attempt_row(conn: sqlite3.Connection, attempt_id: str) -> sqlite3.Row:
        if not _table_exists(conn, "attempts"):
            raise KeyError(f"Attempt not found: {attempt_id}")
        row = conn.execute(
            f"{_ATTEMPT_LOOKUP_SQL} WHERE attempts.attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Attempt not found: {attempt_id}")
        return row

    @staticmethod
    def _steps_in_connection(
        conn: sqlite3.Connection, attempt_id: str
    ) -> tuple[ExecutionObservedStep, ...]:
        if not _table_exists(conn, "attempt_observed_steps"):
            return ()
        rows = conn.execute(
            """
            SELECT step_name, status, summary, metadata_json
            FROM attempt_observed_steps
            WHERE attempt_id = ?
            ORDER BY step_order ASC
            """,
            (attempt_id,),
        ).fetchall()
        return tuple(
            observed_step(
                row["step_name"],
                row["status"],
                summary=row["summary"],
                metadata=_decode_metadata(row["metadata_json"]),
            )
            for row in rows
        )

    @classmethod
    def _snapshot_in_connection(
        cls,
        conn: sqlite3.Connection,
        attempt_row: sqlite3.Row,
        *,
        require_recorded: bool,
    ) -> AttemptProgressSnapshot | None:
        attempt_id = attempt_row["attempt_id"]
        progress = (
            conn.execute(
                """
                SELECT current_phase, current_activity, updated_at
                FROM attempt_progress
                WHERE attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if _table_exists(conn, "attempt_progress")
            else None
        )
        steps = cls._steps_in_connection(conn, attempt_id)
        if require_recorded and progress is None and not steps:
            return None
        return AttemptProgressSnapshot(
            attempt_id=attempt_id,
            task_key=attempt_row["task_key"],
            task_id=attempt_row["task_id"],
            attempt_number=int(attempt_row["attempt_number"]),
            is_active=bool(attempt_row["is_active"]),
            current_phase=progress["current_phase"] if progress else None,
            current_activity=progress["current_activity"] if progress else None,
            updated_at=progress["updated_at"] if progress else None,
            steps=steps,
        )

    # -- writes ------------------------------------------------------------

    def record_step(
        self,
        *,
        attempt_id: str,
        step: str,
        status: str,
        summary: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ExecutionObservedStep:
        """Record one §14.1 first-level step transition for an Attempt."""

        record = observed_step(step, status, summary=summary, metadata=metadata)
        order = runtime_step_order(record.name)
        metadata_json = json.dumps(dict(record.metadata), sort_keys=True)
        now = utc_now_iso()

        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            attempt = self._attempt_row(conn, attempt_id)
            cursor = conn.execute(
                """
                UPDATE attempt_observed_steps
                SET status = ?,
                    summary = ?,
                    metadata_json = ?,
                    step_order = ?,
                    updated_at = ?
                WHERE attempt_id = ? AND step_name = ?
                """,
                (
                    record.status,
                    record.summary,
                    metadata_json,
                    order,
                    now,
                    attempt_id,
                    record.name,
                ),
            )
            if cursor.rowcount == 0:
                conn.execute(
                    """
                    INSERT INTO attempt_observed_steps (
                        attempt_id,
                        task_id,
                        step_name,
                        step_order,
                        status,
                        summary,
                        metadata_json,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        attempt["task_id"],
                        record.name,
                        order,
                        record.status,
                        record.summary,
                        metadata_json,
                        now,
                        now,
                    ),
                )
        return record

    def set_current_activity(
        self,
        *,
        attempt_id: str,
        phase: str | None = None,
        activity: str | None = None,
    ) -> None:
        """Record §14 ``current_phase`` / ``current_activity`` for an Attempt."""

        canonical_phase = None if phase is None else validate_runtime_step(phase)
        assert_no_progress_estimate(
            {"current_activity": activity},
            context=f"attempt {attempt_id!r} current activity",
        )
        now = utc_now_iso()

        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            attempt = self._attempt_row(conn, attempt_id)
            cursor = conn.execute(
                """
                UPDATE attempt_progress
                SET current_phase = ?,
                    current_activity = ?,
                    updated_at = ?
                WHERE attempt_id = ?
                """,
                (canonical_phase, activity, now, attempt_id),
            )
            if cursor.rowcount == 0:
                conn.execute(
                    """
                    INSERT INTO attempt_progress (
                        attempt_id,
                        task_id,
                        current_phase,
                        current_activity,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        attempt["task_id"],
                        canonical_phase,
                        activity,
                        now,
                        now,
                    ),
                )

    # -- reads -------------------------------------------------------------

    def get_progress(self, attempt_id: str) -> AttemptProgressSnapshot | None:
        """Return the snapshot for an Attempt, or ``None`` if nothing recorded."""

        with closing(connect(self.db_path)) as conn:
            attempt = self._attempt_row(conn, attempt_id)
            return self._snapshot_in_connection(
                conn, attempt, require_recorded=True
            )

    def list_steps(self, attempt_id: str) -> tuple[ExecutionObservedStep, ...]:
        """Return only the recorded steps for an Attempt, in §14.1 order."""

        with closing(connect(self.db_path)) as conn:
            self._attempt_row(conn, attempt_id)
            return self._steps_in_connection(conn, attempt_id)

    def snapshots_for_attempts(
        self, attempt_ids: Sequence[str]
    ) -> dict[str, AttemptProgressSnapshot]:
        """Return snapshots for many Attempts over a single connection.

        The board projection needs one snapshot per Ticket on every SSE poll;
        resolving them one at a time would open a new SQLite connection per
        Ticket per poll. Unknown attempt ids are skipped rather than raising.
        """

        wanted = [str(item) for item in attempt_ids if item]
        if not wanted:
            return {}

        snapshots: dict[str, AttemptProgressSnapshot] = {}
        with closing(connect(self.db_path)) as conn:
            if not _table_exists(conn, "attempts"):
                return {}
            for attempt_id in wanted:
                row = conn.execute(
                    f"{_ATTEMPT_LOOKUP_SQL} WHERE attempts.attempt_id = ?",
                    (attempt_id,),
                ).fetchone()
                if row is None:
                    continue
                snapshot = self._snapshot_in_connection(
                    conn, row, require_recorded=False
                )
                if snapshot is not None:
                    snapshots[attempt_id] = snapshot
        return snapshots

    def get_latest_attempt_progress(
        self, task_key: str
    ) -> AttemptProgressSnapshot | None:
        """Return the latest Attempt's snapshot (§14.0 default UI selection)."""

        normalized = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn:
            if not _table_exists(conn, "attempts"):
                return None
            row = conn.execute(
                f"""
                {_ATTEMPT_LOOKUP_SQL}
                WHERE tasks.task_key = ?
                ORDER BY attempts.attempt_number DESC
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
            if row is None:
                return None
            return self._snapshot_in_connection(conn, row, require_recorded=False)

    def list_attempt_progress(
        self, task_key: str
    ) -> tuple[AttemptProgressSnapshot, ...]:
        """Return one snapshot per Attempt, oldest first (§14.0 history)."""

        normalized = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn:
            if not _table_exists(conn, "attempts"):
                return ()
            rows = conn.execute(
                f"""
                {_ATTEMPT_LOOKUP_SQL}
                WHERE tasks.task_key = ?
                ORDER BY attempts.attempt_number ASC
                """,
                (normalized,),
            ).fetchall()
            snapshots = [
                self._snapshot_in_connection(conn, row, require_recorded=False)
                for row in rows
            ]
        return tuple(item for item in snapshots if item is not None)

    def get_attempt_progress(
        self, attempt_id: str
    ) -> AttemptProgressSnapshot | None:
        """Return an Attempt's snapshot, defaulting unrecorded fields."""

        with closing(connect(self.db_path)) as conn:
            try:
                attempt = self._attempt_row(conn, attempt_id)
            except KeyError:
                return None
            return self._snapshot_in_connection(
                conn, attempt, require_recorded=False
            )


__all__ = [
    "RuntimeProgressStore",
]
