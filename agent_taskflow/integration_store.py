"""Step 2 integration persistence (V1 Master Spec §32.1).

Two storage tiers, kept deliberately separate:

* ``task_pr_state`` — exactly the §32.1 Ticket PR fields. Step 2 creates the
  table and is its only writer; every other component reads it and must
  tolerate ``None``.
* Step-2-private tables — integration state that §32.1 does not list
  (``previous_integrated_base_sha``, ``new_target_sha``), the per-repo queue
  and lock, and validator / review / conflict evidence.

This module never mutates task status, never touches git or GitHub, and never
removes anything.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from agent_taskflow.integration_schema import (
    TICKET_PR_FIELD_NAMES,
    default_pr_state,
    normalize_repo,
    pr_url_repo_marker,
    repo_from_pr_url,
    validate_pr_state,
)
from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import TaskMirrorStore, connect, init_db
from agent_taskflow.tasks import normalize_task_key


__all__ = ["IntegrationStore", "IntegrationStoreError"]


class IntegrationStoreError(RuntimeError):
    """Raised when integration state cannot be read or written safely."""


_PRIVATE_STATE_FIELDS = (
    "previous_integrated_base_sha",
    "new_target_sha",
    "behind_count",
    "last_integration_run_id",
    "last_integration_status",
    "trigger_task_key",
    "merge_verified_at",
    "closed_unmerged_at",
    "cleanup_confirmed_at",
)

_BOOL_PR_FIELDS = ("pr_merged", "reintegration_required")


def _loads(raw: str | None, fallback: Any) -> Any:
    if raw is None:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


class IntegrationStore:
    """SQLite-backed Step 2 integration state."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        store: TaskMirrorStore | None = None,
    ) -> None:
        self._store = store or TaskMirrorStore(db_path)
        self.db_path = self._store.db_path

    @property
    def task_store(self) -> TaskMirrorStore:
        return self._store

    def init_db(self) -> None:
        """Create the shared schema, including the Step 2 tables."""
        init_db(self.db_path)

    # -- §32.1 public Ticket PR fields ------------------------------------
    def get_pr_state(self, task_key: str) -> dict[str, Any]:
        """Return the §32.1 fields, falling back to the spec defaults."""
        key = normalize_task_key(task_key)
        columns = ", ".join(TICKET_PR_FIELD_NAMES)
        with closing(connect(self.db_path)) as conn:
            row = conn.execute(
                f"SELECT {columns} FROM task_pr_state WHERE task_key = ?",
                (key,),
            ).fetchone()

        state = default_pr_state()
        if row is None:
            return state
        for name in TICKET_PR_FIELD_NAMES:
            value = row[name]
            if name in _BOOL_PR_FIELDS:
                state[name] = bool(value)
            elif value is not None:
                state[name] = value
        return state

    def update_pr_state(self, task_key: str, **fields: Any) -> dict[str, Any]:
        """Write §32.1 fields. This is the only writer of ``task_pr_state``."""
        key = normalize_task_key(task_key)
        validated = validate_pr_state(fields)
        if not validated:
            return self.get_pr_state(key)

        stored = {
            name: (int(value) if name in _BOOL_PR_FIELDS and value is not None else value)
            for name, value in validated.items()
        }
        assignments = ", ".join(f"{name} = ?" for name in stored)
        with closing(connect(self.db_path)) as conn, conn:
            self._require_task(conn, key)
            self._ensure_pr_row(conn, key)
            conn.execute(
                f"UPDATE task_pr_state SET {assignments} WHERE task_key = ?",
                (*stored.values(), key),
            )
        return self.get_pr_state(key)

    def increment_reintegration_count(self, task_key: str) -> int:
        """Increment §32.1 ``reintegration_count`` and return the new value."""
        key = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn, conn:
            self._require_task(conn, key)
            self._ensure_pr_row(conn, key)
            conn.execute(
                """
                UPDATE task_pr_state
                SET reintegration_count = COALESCE(reintegration_count, 0) + 1
                WHERE task_key = ?
                """,
                (key,),
            )
        return int(self.get_pr_state(key)["reintegration_count"])

    def list_pr_states(self) -> list[dict[str, Any]]:
        """Return every recorded PR state, keyed by task."""
        with closing(connect(self.db_path)) as conn:
            keys = [row["task_key"] for row in conn.execute("SELECT task_key FROM task_pr_state")]
        return [{"task_key": key, **self.get_pr_state(key)} for key in keys]

    def list_integration_task_keys(self) -> list[str]:
        """Return every task key that has any Step 2 integration record.

        Metrics must see a Ticket that produced conflict or validator evidence
        even when it never reached a PR, so this unions the evidence tables
        rather than reading ``task_pr_state`` alone.
        """
        with closing(connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT task_key FROM task_pr_state
                UNION
                SELECT task_key FROM integration_validator_evidence
                UNION
                SELECT task_key FROM integration_conflict_evidence
                UNION
                SELECT task_key FROM task_integration_state
                """
            ).fetchall()
        return sorted(row["task_key"] for row in rows)

    def list_open_pr_states(self, repo: str) -> list[dict[str, Any]]:
        """Return the §32.0 pick-up set for one repository's watcher tick.

            repo == tick repo AND pr_number IS NOT NULL AND pr_state = 'open'

        Not scoped by status. A PR's repository is read from its own §32.1
        ``pr_url``: the PR number alone is not an identity, because the same
        number can be open in two repositories at once. The repository filter
        runs in SQL, so another repository's rows are never loaded; each
        returned row's URL is then parsed and compared exactly.
        """
        wanted = normalize_repo(repo)
        columns = ", ".join(TICKET_PR_FIELD_NAMES)
        with closing(connect(self.db_path)) as conn:
            rows = conn.execute(
                f"""
                SELECT task_key, {columns}
                FROM task_pr_state
                WHERE pr_number IS NOT NULL
                  AND pr_state = 'open'
                  AND pr_url IS NOT NULL
                  AND instr(lower(pr_url), ?) > 0
                ORDER BY task_key
                """,
                (pr_url_repo_marker(wanted),),
            ).fetchall()

        states: list[dict[str, Any]] = []
        for row in rows:
            if repo_from_pr_url(row["pr_url"]) != wanted:
                continue
            state = default_pr_state()
            for name in TICKET_PR_FIELD_NAMES:
                value = row[name]
                if name in _BOOL_PR_FIELDS:
                    state[name] = bool(value)
                elif value is not None:
                    state[name] = value
            states.append({"task_key": row["task_key"], **state})
        return states

    # -- Step-2-private integration state ---------------------------------
    def get_integration_state(self, task_key: str) -> dict[str, Any]:
        key = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM task_integration_state WHERE task_key = ?",
                (key,),
            ).fetchone()
        state: dict[str, Any] = {name: None for name in _PRIVATE_STATE_FIELDS}
        if row is not None:
            for name in _PRIVATE_STATE_FIELDS:
                state[name] = row[name]
        return state

    def update_integration_state(self, task_key: str, **fields: Any) -> dict[str, Any]:
        key = normalize_task_key(task_key)
        unknown = sorted(set(fields) - set(_PRIVATE_STATE_FIELDS))
        if unknown:
            raise ValueError(
                f"Unknown integration state field(s): {', '.join(unknown)}"
            )
        if not fields:
            return self.get_integration_state(key)

        now = utc_now_iso()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with closing(connect(self.db_path)) as conn, conn:
            self._require_task(conn, key)
            conn.execute(
                """
                INSERT OR IGNORE INTO task_integration_state (task_key, updated_at)
                VALUES (?, ?)
                """,
                (key, now),
            )
            conn.execute(
                f"""
                UPDATE task_integration_state
                SET {assignments}, updated_at = ?
                WHERE task_key = ?
                """,
                (*fields.values(), now, key),
            )
        return self.get_integration_state(key)

    # -- queue ------------------------------------------------------------
    def enqueue(
        self,
        task_key: str,
        *,
        repo: str,
        enqueued_at: str | None = None,
        source: str = "integration_queue",
        priority: str | None = None,
    ) -> None:
        """Add a ticket to its repo queue, keeping the first entry timestamp."""
        key = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn, conn:
            self._require_task(conn, key)
            conn.execute(
                """
                INSERT OR IGNORE INTO integration_queue (
                    task_key, repo, enqueued_at, source, priority
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (key, repo, enqueued_at or utc_now_iso(), source, priority),
            )

    def list_queue(self, repo: str) -> list[dict[str, Any]]:
        """Return one repo's queue in FIFO order (§22.1)."""
        with closing(connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT id, task_key, repo, enqueued_at, source, priority
                FROM integration_queue
                WHERE repo = ?
                ORDER BY enqueued_at ASC, id ASC
                """,
                (repo,),
            ).fetchall()
        return [dict(row) for row in rows]

    def dequeue(self, task_key: str) -> None:
        key = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("DELETE FROM integration_queue WHERE task_key = ?", (key,))

    def is_queued(self, task_key: str) -> bool:
        key = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT 1 FROM integration_queue WHERE task_key = ?", (key,)
            ).fetchone()
        return row is not None

    # -- per-repo lock ----------------------------------------------------
    def acquire_integration_lock(self, repo: str, *, owner: str) -> bool:
        """Take the per-repo integration lock. Returns False if already held."""
        with closing(connect(self.db_path)) as conn, conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO integration_locks (repo, owner, acquired_at)
                VALUES (?, ?, ?)
                """,
                (repo, owner, utc_now_iso()),
            )
            return cursor.rowcount == 1

    def release_integration_lock(self, repo: str, *, owner: str) -> bool:
        """Release the lock only if ``owner`` still holds it."""
        with closing(connect(self.db_path)) as conn, conn:
            cursor = conn.execute(
                "DELETE FROM integration_locks WHERE repo = ? AND owner = ?",
                (repo, owner),
            )
            return cursor.rowcount == 1

    def get_integration_lock(self, repo: str) -> dict[str, Any] | None:
        with closing(connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT repo, owner, acquired_at FROM integration_locks WHERE repo = ?",
                (repo,),
            ).fetchone()
        return dict(row) if row is not None else None

    # -- evidence ---------------------------------------------------------
    def record_validator_evidence(
        self,
        task_key: str,
        *,
        integration_run_id: str,
        validator: str,
        command: Sequence[str],
        status: str,
        exit_code: int | None,
        output: str | None,
        branch_sha: str | None,
        target_sha: str | None,
        diff_context: str | None,
    ) -> None:
        """Persist one §29 validator result with its full failure context."""
        key = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO integration_validator_evidence (
                    task_key, integration_run_id, validator, command_json,
                    status, exit_code, output, branch_sha, target_sha,
                    diff_context, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    integration_run_id,
                    validator,
                    json.dumps(list(command)),
                    status,
                    exit_code,
                    output,
                    branch_sha,
                    target_sha,
                    diff_context,
                    utc_now_iso(),
                ),
            )

    def list_validator_evidence(self, task_key: str) -> list[dict[str, Any]]:
        key = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT * FROM integration_validator_evidence
                WHERE task_key = ? ORDER BY id ASC
                """,
                (key,),
            ).fetchall()
        results = []
        for row in rows:
            entry = dict(row)
            entry["command"] = _loads(entry.pop("command_json"), [])
            results.append(entry)
        return results

    def record_review_evidence(
        self,
        task_key: str,
        *,
        pr_number: int | None,
        pr_url: str | None,
        review_decision: str | None,
        reviewer: str | None,
        reviewed_at: str | None,
        reviewed_head_sha: str | None,
        comments: Iterable[Mapping[str, Any]] | None,
    ) -> None:
        """Persist §33.2 review evidence as retry context for the next Attempt."""
        key = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO integration_review_evidence (
                    task_key, pr_number, pr_url, review_decision, reviewer,
                    reviewed_at, reviewed_head_sha, comments_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    pr_number,
                    pr_url,
                    review_decision,
                    reviewer,
                    reviewed_at,
                    reviewed_head_sha,
                    json.dumps([dict(comment) for comment in (comments or [])]),
                    utc_now_iso(),
                ),
            )

    def list_review_evidence(self, task_key: str) -> list[dict[str, Any]]:
        key = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT * FROM integration_review_evidence
                WHERE task_key = ? ORDER BY id ASC
                """,
                (key,),
            ).fetchall()
        results = []
        for row in rows:
            entry = dict(row)
            entry["comments"] = _loads(entry.pop("comments_json"), [])
            results.append(entry)
        return results

    def record_conflict_evidence(
        self,
        task_key: str,
        *,
        integration_run_id: str,
        resolver: str,
        resolved: bool,
        conflict_hunks: Iterable[Mapping[str, Any]] | None,
        explanation: str | None,
    ) -> None:
        """Persist §27.2.1 conflict hunks and the resolver's explanation."""
        key = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO integration_conflict_evidence (
                    task_key, integration_run_id, resolver, resolved,
                    conflict_hunks_json, explanation, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    integration_run_id,
                    resolver,
                    int(bool(resolved)),
                    json.dumps([dict(hunk) for hunk in (conflict_hunks or [])]),
                    explanation,
                    utc_now_iso(),
                ),
            )

    def list_conflict_evidence(self, task_key: str) -> list[dict[str, Any]]:
        key = normalize_task_key(task_key)
        with closing(connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT * FROM integration_conflict_evidence
                WHERE task_key = ? ORDER BY id ASC
                """,
                (key,),
            ).fetchall()
        results = []
        for row in rows:
            entry = dict(row)
            entry["conflict_hunks"] = _loads(entry.pop("conflict_hunks_json"), [])
            entry["resolved"] = bool(entry["resolved"])
            results.append(entry)
        return results

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _require_task(conn: sqlite3.Connection, task_key: str) -> None:
        row = conn.execute(
            "SELECT 1 FROM tasks WHERE task_key = ?", (task_key,)
        ).fetchone()
        if row is None:
            raise IntegrationStoreError(f"Task not found: {task_key}")

    @staticmethod
    def _ensure_pr_row(conn: sqlite3.Connection, task_key: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO task_pr_state (task_key) VALUES (?)",
            (task_key,),
        )
