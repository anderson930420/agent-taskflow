"""Global ``max_concurrent_tasks`` runtime control (V1 Step 4, SPEC §19.4, §20).

The setting is global, defaults to 1, and is stored in the runtime-control
database beside ``runtime_controls`` (see
:mod:`agent_taskflow.runtime_capacity_schema`). ``RuntimeControlStore`` and
``scripts/runtime_control.py`` expose it.

Enforcement happens inside every claim transaction
(``runtime_admission.assert_runtime_capacity_available``): the count is of
active executor leases, so an Attempt is counted from its claim until its
lease is released or reaped. An expired lease that has not been reaped yet
still holds its slot.

Raising the limit above 1 is refused unless the Step 4 rehearsal evidence
passes :func:`agent_taskflow.concurrency_gate.evaluate_concurrency_evidence`.
Lowering it to 1 never needs evidence.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any

from agent_taskflow.concurrency_gate import evaluate_concurrency_evidence
from agent_taskflow.models import require_absolute_path, utc_now_iso
from agent_taskflow.runtime_capacity_schema import (
    RUNTIME_CAPACITY_MIGRATION,
    migrate_runtime_capacity,
    runtime_capacity_deployed_in_connection,
)
from agent_taskflow.store import connect

DEFAULT_MAX_CONCURRENT_TASKS = 1
CAPACITY_SCOPE_KIND = "global"
CAPACITY_SCOPE_ID = "*"
CAPACITY_SET_REASON = "operator_capacity_set"


class RuntimeCapacityError(RuntimeError):
    """Base error for the capacity control."""


class ConcurrencyGateRefused(RuntimeCapacityError):
    """Raised when a limit above 1 lacks passing Step 4 rehearsal evidence."""

    def __init__(self, message: str, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.report = report


@dataclass(frozen=True)
class RuntimeCapacitySetting:
    max_concurrent_tasks: int
    source: str
    enforced: bool
    scope: str = CAPACITY_SCOPE_KIND
    generation: int | None = None
    requested_by: str | None = None
    requested_at: str | None = None
    evidence_path: str | None = None
    evidence_sha256: str | None = None
    evidence_repo_sha: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _default_setting(*, enforced: bool) -> RuntimeCapacitySetting:
    return RuntimeCapacitySetting(
        max_concurrent_tasks=DEFAULT_MAX_CONCURRENT_TASKS,
        source="default",
        enforced=enforced,
    )


def runtime_capacity_in_connection(conn: sqlite3.Connection) -> RuntimeCapacitySetting:
    """Return the effective setting as seen by ``conn``'s transaction."""
    if not runtime_capacity_deployed_in_connection(conn):
        return _default_setting(enforced=False)
    row = conn.execute(
        """
        SELECT * FROM runtime_capacity_controls
        WHERE scope_kind = ? AND scope_id = ?
        """,
        (CAPACITY_SCOPE_KIND, CAPACITY_SCOPE_ID),
    ).fetchone()
    if row is None:
        return _default_setting(enforced=True)
    return RuntimeCapacitySetting(
        max_concurrent_tasks=int(row["max_concurrent_tasks"]),
        source="configured",
        enforced=True,
        generation=int(row["generation"]),
        requested_by=row["requested_by"],
        requested_at=row["requested_at"],
        evidence_path=row["evidence_path"],
        evidence_sha256=row["evidence_sha256"],
        evidence_repo_sha=row["evidence_repo_sha"],
    )


def count_active_executor_leases_in_connection(conn: sqlite3.Connection) -> int:
    """Count active executor leases, expired-but-unreaped ones included."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runtime_leases'"
    ).fetchone()
    if table is None:
        return 0
    return int(
        conn.execute("SELECT COUNT(*) FROM runtime_leases WHERE is_active = 1").fetchone()[0]
    )


def _read_only_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def read_runtime_capacity(db_path: str | Path) -> RuntimeCapacitySetting:
    """Read the setting without creating, migrating or writing anything."""
    path = require_absolute_path(db_path, "db_path")
    if not path.is_file():
        return _default_setting(enforced=False)
    with closing(_read_only_connection(path)) as conn:
        return runtime_capacity_in_connection(conn)


def _require_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max_concurrent_tasks must be an integer")
    if value < 1:
        raise ValueError("max_concurrent_tasks must be >= 1")
    return value


def set_max_concurrent_tasks(
    db_path: str | Path,
    value: int,
    *,
    actor: str,
    evidence_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    metadata: dict[str, Any] | None = None,
) -> RuntimeCapacitySetting:
    """Set the global limit; above 1 requires passing Step 4 rehearsal evidence.

    A refusal raises :class:`ConcurrencyGateRefused` before anything is
    written, so it neither installs the control nor changes enforcement.
    """
    path = require_absolute_path(db_path, "db_path")
    limit = _require_limit(value)
    normalized_actor = (actor or "").strip()
    if not normalized_actor:
        raise ValueError("actor must not be empty")

    report: dict[str, Any] | None = None
    if limit > 1:
        if evidence_path is None:
            report = {
                "gate": "blocked",
                "errors": [
                    "evidence_path is required to raise max_concurrent_tasks above 1"
                ],
                "read_only": True,
            }
            raise ConcurrencyGateRefused(
                "Raising max_concurrent_tasks above 1 requires Step 4 rehearsal "
                "evidence (--evidence-path)",
                report,
            )
        report = evaluate_concurrency_evidence(evidence_path, repo_root=repo_root)
        if report["gate"] != "passed":
            raise ConcurrencyGateRefused(
                "Step 4 rehearsal evidence did not pass the concurrency gate: "
                + "; ".join(report["errors"]),
                report,
            )

    migrate_runtime_capacity(path)
    now = utc_now_iso()
    evidence_fields = (
        (
            report["evidence_path"],
            report["evidence_sha256"],
            report["evidence_schema_version"],
            report["evidence_repo_sha"],
        )
        if limit > 1 and report is not None
        else (None, None, None, None)
    )
    event_metadata = dict(metadata or {})
    if limit > 1 and report is not None:
        event_metadata["gate"] = {
            "schema_version": report["schema_version"],
            "audited_repo_sha": report["audited_repo_sha"],
        }
    with closing(connect(path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            """
            SELECT max_concurrent_tasks, generation FROM runtime_capacity_controls
            WHERE scope_kind = ? AND scope_id = ?
            """,
            (CAPACITY_SCOPE_KIND, CAPACITY_SCOPE_ID),
        ).fetchone()
        generation = 1 if previous is None else int(previous["generation"]) + 1
        conn.execute(
            """
            INSERT INTO runtime_capacity_controls(
                scope_kind, scope_id, max_concurrent_tasks, evidence_path,
                evidence_sha256, evidence_schema_version, evidence_repo_sha,
                reason_code, requested_by, requested_at, generation, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_kind, scope_id) DO UPDATE SET
                max_concurrent_tasks = excluded.max_concurrent_tasks,
                evidence_path = excluded.evidence_path,
                evidence_sha256 = excluded.evidence_sha256,
                evidence_schema_version = excluded.evidence_schema_version,
                evidence_repo_sha = excluded.evidence_repo_sha,
                reason_code = excluded.reason_code,
                requested_by = excluded.requested_by,
                requested_at = excluded.requested_at,
                generation = excluded.generation,
                metadata_json = excluded.metadata_json
            """,
            (
                CAPACITY_SCOPE_KIND,
                CAPACITY_SCOPE_ID,
                limit,
                *evidence_fields,
                CAPACITY_SET_REASON,
                normalized_actor,
                now,
                generation,
                json.dumps(event_metadata, sort_keys=True),
            ),
        )
        conn.execute(
            """
            INSERT INTO runtime_capacity_control_events(
                scope_kind, scope_id, from_max_concurrent_tasks,
                to_max_concurrent_tasks, evidence_path, evidence_sha256,
                reason_code, actor, generation, timestamp, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                CAPACITY_SCOPE_KIND,
                CAPACITY_SCOPE_ID,
                None if previous is None else int(previous["max_concurrent_tasks"]),
                limit,
                evidence_fields[0],
                evidence_fields[1],
                CAPACITY_SET_REASON,
                normalized_actor,
                generation,
                now,
                json.dumps(event_metadata, sort_keys=True),
            ),
        )
    return read_runtime_capacity(path)


def list_runtime_capacity_events(db_path: str | Path) -> list[dict[str, Any]]:
    """Return the append-only capacity audit trail, oldest first."""
    path = require_absolute_path(db_path, "db_path")
    if not path.is_file():
        return []
    with closing(_read_only_connection(path)) as conn:
        if not runtime_capacity_deployed_in_connection(conn):
            return []
        rows = conn.execute(
            "SELECT * FROM runtime_capacity_control_events ORDER BY event_id"
        ).fetchall()
    events = []
    for row in rows:
        event = dict(row)
        event["metadata"] = json.loads(event.pop("metadata_json") or "{}")
        events.append(event)
    return events


__all__ = [
    "CAPACITY_SCOPE_KIND",
    "CAPACITY_SET_REASON",
    "DEFAULT_MAX_CONCURRENT_TASKS",
    "RUNTIME_CAPACITY_MIGRATION",
    "ConcurrencyGateRefused",
    "RuntimeCapacityError",
    "RuntimeCapacitySetting",
    "count_active_executor_leases_in_connection",
    "list_runtime_capacity_events",
    "read_runtime_capacity",
    "runtime_capacity_in_connection",
    "set_max_concurrent_tasks",
]
