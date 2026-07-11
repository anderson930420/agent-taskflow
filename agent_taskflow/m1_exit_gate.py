"""Read-only Milestone 1 exit-gate reconciliation.

The audit intentionally separates repository/database facts from operator-supplied
rehearsal evidence.  It never migrates or mutates the target database.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any

M1_EXIT_GATE_SCHEMA_VERSION = "m1_exit_gate_audit.v1"
VALID_GATE_STATUSES = frozenset({"passed", "partial", "blocked", "not_applicable"})


@dataclass(frozen=True)
class GateResult:
    gate: str
    status: str
    summary: str
    evidence: tuple[str, ...] = ()
    next_action: str | None = None

    def __post_init__(self) -> None:
        if self.status not in VALID_GATE_STATUSES:
            raise ValueError(f"invalid gate status: {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "gate": self.gate,
            "status": self.status,
            "summary": self.summary,
            "evidence": list(self.evidence),
        }
        if self.next_action is not None:
            payload["next_action"] = self.next_action
        return payload


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _trigger_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?", (name,)
    ).fetchone() is not None


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _schema_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return "" if row is None or row["sql"] is None else str(row["sql"])


def _load_evidence(evidence_dir: Path | None, filename: str) -> tuple[dict[str, Any] | None, str]:
    if evidence_dir is None:
        return None, "evidence directory not supplied"
    path = evidence_dir / filename
    if not path.is_file():
        return None, f"missing {path}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"invalid {path}: {exc}"
    if not isinstance(payload, dict):
        return None, f"invalid {path}: top-level JSON must be an object"
    return payload, str(path)


def _external_boolean_gate(
    *,
    gate: str,
    evidence_dir: Path | None,
    filename: str,
    schema_version: str,
    required_true: tuple[str, ...],
    summary: str,
    next_action: str,
) -> GateResult:
    payload, source = _load_evidence(evidence_dir, filename)
    if payload is None:
        return GateResult(gate, "blocked", source, next_action=next_action)
    errors: list[str] = []
    if payload.get("schema_version") != schema_version:
        errors.append(f"schema_version must be {schema_version}")
    for field in required_true:
        if payload.get(field) is not True:
            errors.append(f"{field} must be true")
    if errors:
        return GateResult(
            gate,
            "blocked",
            "; ".join(errors),
            evidence=(source,),
            next_action=next_action,
        )
    return GateResult(gate, "passed", summary, evidence=(source,))


def _audit_db_copy(evidence_dir: Path | None) -> GateResult:
    return _external_boolean_gate(
        gate="production_db_copy_rehearsal",
        evidence_dir=evidence_dir,
        filename="production-db-copy-rehearsal.json",
        schema_version="m1_production_db_copy_rehearsal.v1",
        required_true=("migration_dry_run", "integrity_check", "rollback_rehearsal"),
        summary="Production database copy migration, integrity, and rollback rehearsal are recorded.",
        next_action="Run the migration on a production DB copy, verify integrity, rehearse rollback, and save the signed JSON evidence.",
    )


def _audit_dual_write(evidence_dir: Path | None) -> GateResult:
    payload, source = _load_evidence(evidence_dir, "dual-write-consistency.json")
    if payload is None:
        return GateResult(
            "dual_write_consistency",
            "blocked",
            source,
            next_action="Run a bounded dual-write observation window and save comparison counts and mismatches.",
        )
    errors: list[str] = []
    if payload.get("schema_version") != "m1_dual_write_consistency.v1":
        errors.append("schema_version must be m1_dual_write_consistency.v1")
    if not payload.get("observation_window_started_at") or not payload.get("observation_window_ended_at"):
        errors.append("observation window start/end are required")
    if not isinstance(payload.get("records_compared"), int) or payload.get("records_compared", 0) < 1:
        errors.append("records_compared must be at least 1")
    if payload.get("mismatch_count") != 0:
        errors.append("mismatch_count must be 0")
    if payload.get("silent_failure_count") != 0:
        errors.append("silent_failure_count must be 0")
    if errors:
        return GateResult(
            "dual_write_consistency",
            "blocked",
            "; ".join(errors),
            evidence=(source,),
            next_action="Repeat the observation window after resolving every mismatch or silent failure.",
        )
    return GateResult(
        "dual_write_consistency",
        "passed",
        "The recorded observation window has zero mismatches and zero silent failures.",
        evidence=(source,),
    )


def _audit_three_attempts(conn: sqlite3.Connection) -> GateResult:
    if not (_table_exists(conn, "attempts") and _table_exists(conn, "attempt_resources")):
        return GateResult(
            "three_attempt_artifact_isolation",
            "blocked",
            "attempts or attempt_resources table is missing",
            next_action="Apply the Attempt and Attempt-resource migrations before running the rehearsal.",
        )
    row = conn.execute(
        """
        SELECT task_id,
               COUNT(*) AS attempts,
               COUNT(DISTINCT attempt_id) AS attempt_ids,
               COUNT(DISTINCT artifact_root) AS artifact_roots,
               COUNT(DISTINCT worktree_path) AS worktrees,
               COUNT(DISTINCT branch_name) AS branches
        FROM attempt_resources
        GROUP BY task_id
        HAVING COUNT(*) >= 3
           AND COUNT(DISTINCT attempt_id) = COUNT(*)
           AND COUNT(DISTINCT artifact_root) = COUNT(*)
           AND COUNT(DISTINCT worktree_path) = COUNT(*)
           AND COUNT(DISTINCT branch_name) = COUNT(*)
        ORDER BY COUNT(*) DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return GateResult(
            "three_attempt_artifact_isolation",
            "partial",
            "Schema constraints exist, but no task in this database proves three isolated Attempt resources.",
            next_action="Run one disposable task through three Attempts and retain the distinct branch/worktree/artifact evidence.",
        )
    return GateResult(
        "three_attempt_artifact_isolation",
        "passed",
        f"Task {row['task_id']} has {row['attempts']} distinct Attempt resource sets.",
        evidence=(
            f"attempt_ids={row['attempt_ids']}",
            f"artifact_roots={row['artifact_roots']}",
            f"worktrees={row['worktrees']}",
            f"branches={row['branches']}",
        ),
    )


def _audit_cleanup(evidence_dir: Path | None) -> GateResult:
    return _external_boolean_gate(
        gate="timeout_abort_cleanup",
        evidence_dir=evidence_dir,
        filename="timeout-abort-cleanup.json",
        schema_version="m1_timeout_abort_cleanup.v1",
        required_true=(
            "timeout_pid_cleared",
            "timeout_lock_released",
            "timeout_worktree_cleanup_verified",
            "timeout_verified_exit",
            "abort_pid_cleared",
            "abort_lock_released",
            "abort_worktree_cleanup_verified",
            "abort_verified_exit",
        ),
        summary="Timeout and abort cleanup evidence covers PID, lock, worktree policy, and verified exit.",
        next_action="Run disposable timeout and abort drills and record the PID, lock, worktree, and process-group results.",
    )


def _audit_lifecycle_replay(conn: sqlite3.Connection) -> GateResult:
    if not (_table_exists(conn, "attempts") and _table_exists(conn, "lifecycle_events")):
        return GateResult(
            "lifecycle_timeline_replay",
            "blocked",
            "attempts or lifecycle_events table is missing",
            next_action="Apply the lifecycle schema migration.",
        )
    attempts = conn.execute("SELECT attempt_id, status FROM attempts ORDER BY attempt_id").fetchall()
    if not attempts:
        return GateResult(
            "lifecycle_timeline_replay",
            "partial",
            "Lifecycle schema is installed, but this database has no Attempt timeline to replay.",
            next_action="Run a disposable Attempt through a terminal state and retain its append-only timeline.",
        )
    errors: list[str] = []
    for attempt in attempts:
        events = conn.execute(
            "SELECT from_status, to_status FROM lifecycle_events WHERE attempt_id = ? ORDER BY event_id",
            (attempt["attempt_id"],),
        ).fetchall()
        if not events:
            errors.append(f"{attempt['attempt_id']}: no lifecycle events")
            continue
        previous_to: str | None = None
        for event in events:
            if previous_to is not None and event["from_status"] != previous_to:
                errors.append(f"{attempt['attempt_id']}: discontinuous event chain")
                break
            previous_to = str(event["to_status"])
        if previous_to != attempt["status"]:
            errors.append(
                f"{attempt['attempt_id']}: replay ends at {previous_to!r}, row is {attempt['status']!r}"
            )
    if errors:
        return GateResult(
            "lifecycle_timeline_replay",
            "blocked",
            "; ".join(errors[:10]),
            next_action="Repair or explicitly classify every discontinuous/missing Attempt timeline before M1 closeout.",
        )
    return GateResult(
        "lifecycle_timeline_replay",
        "passed",
        f"All {len(attempts)} Attempt timelines replay to their persisted status.",
        evidence=(f"attempts_replayed={len(attempts)}",),
    )


def _audit_illegal_transition(conn: sqlite3.Connection) -> GateResult:
    installed = _trigger_exists(conn, "lifecycle_attempt_transition_guard") and _table_exists(
        conn, "lifecycle_allowed_transitions"
    )
    return GateResult(
        "illegal_transition_rejection",
        "passed" if installed else "blocked",
        (
            "SQLite transition allowlist and rejection trigger are installed."
            if installed
            else "SQLite transition allowlist or rejection trigger is missing."
        ),
        next_action=None if installed else "Apply the lifecycle-control migration and rerun the audit.",
    )


def _audit_pause(evidence_dir: Path | None, conn: sqlite3.Connection) -> GateResult:
    schema_ready = _table_exists(conn, "runtime_controls") and _table_exists(
        conn, "runtime_control_events"
    )
    if not schema_ready:
        return GateResult(
            "pause_stops_new_pickup",
            "blocked",
            "Runtime control tables are missing.",
            next_action="Apply the lifecycle-control migration.",
        )
    payload, source = _load_evidence(evidence_dir, "pause-admission-rehearsal.json")
    if payload is None:
        return GateResult(
            "pause_stops_new_pickup",
            "partial",
            "Pause persistence is installed, but no deployed no-new-pickup rehearsal is supplied.",
            next_action="Pause a disposable scope, prove a new claim is denied, clear it, and save the evidence JSON.",
        )
    if (
        payload.get("schema_version") == "m1_pause_admission_rehearsal.v1"
        and payload.get("new_pickup_denied") is True
        and payload.get("existing_attempt_not_suspended") is True
        and payload.get("pause_cleared") is True
    ):
        return GateResult(
            "pause_stops_new_pickup",
            "passed",
            "Deployed pause rehearsal denied new pickup without pretending to suspend an active Attempt.",
            evidence=(source,),
        )
    return GateResult(
        "pause_stops_new_pickup",
        "blocked",
        "Pause rehearsal evidence is incomplete or invalid.",
        evidence=(source,),
        next_action="Repeat the pause rehearsal with all required assertions.",
    )


def _audit_project_class_controls(conn: sqlite3.Connection) -> GateResult:
    sql = _schema_sql(conn, "runtime_controls").lower()
    has_project = "'project'" in sql or '"project"' in sql
    has_class = any(token in sql for token in ("'task_class'", "'class'", '"task_class"'))
    if has_project and has_class:
        return GateResult(
            "project_class_kill_switch",
            "partial",
            "Project and class scopes exist in the schema, but a deployed immediate-disable rehearsal is still required.",
            next_action="Run a disposable (project, class) eligibility-disable rehearsal and retain audit evidence.",
        )
    missing = [name for name, present in (("project", has_project), ("task_class", has_class)) if not present]
    return GateResult(
        "project_class_kill_switch",
        "blocked",
        f"runtime_controls does not support required scope(s): {', '.join(missing)}",
        next_action="Add project pause and task-class auto-merge kill-switch scopes with append-only control evidence.",
    )


def _audit_canonical_path(evidence_dir: Path | None) -> GateResult:
    payload, source = _load_evidence(evidence_dir, "canonical-execution-path.json")
    if payload is None:
        return GateResult(
            "canonical_execution_path",
            "blocked",
            source,
            next_action="Provide a passing ExecutionEngine parity report or proof that legacy execution is rejected for every Level 2-eligible class.",
        )
    valid = (
        payload.get("schema_version") == "m1_canonical_execution_path.v1"
        and payload.get("canonical_path") == "ExecutionEngine"
        and (
            payload.get("parity_test_passed") is True
            or payload.get("legacy_level2_rejected") is True
        )
        and payload.get("merger_requires_canonical_attempt") is True
    )
    if not valid:
        return GateResult(
            "canonical_execution_path",
            "blocked",
            "Canonical-path evidence does not prove parity or legacy rejection plus merger binding.",
            evidence=(source,),
            next_action="Complete ExecutionEngine parity/enforcement and regenerate the evidence.",
        )
    return GateResult(
        "canonical_execution_path",
        "passed",
        "ExecutionEngine is recorded as the only trusted Level 2 path, with parity or legacy rejection and merger binding.",
        evidence=(source,),
    )


def _audit_legacy_retention(conn: sqlite3.Connection, repo_root: Path) -> GateResult:
    columns = _column_names(conn, "tasks")
    reader = repo_root / "scripts" / "summarize_real_scheduled_execution.py"
    reader_retains_fallback = False
    if reader.is_file():
        try:
            text = reader.read_text(encoding="utf-8").lower()
            reader_retains_fallback = "legacy" in text and "fallback" in text
        except OSError:
            reader_retains_fallback = False
    passed = "is_legacy" in columns and reader_retains_fallback
    return GateResult(
        "legacy_schema_reader_retained",
        "passed" if passed else "blocked",
        (
            "Legacy task marker and legacy observability fallback reader are retained."
            if passed
            else "Legacy schema marker or fallback reader could not be verified."
        ),
        evidence=(str(reader),) if reader.is_file() else (),
        next_action=None if passed else "Restore the legacy schema marker and reader fallback until M1 is formally closed.",
    )


def audit_m1_exit_gate(
    *,
    db_path: str | Path,
    repo_root: str | Path,
    evidence_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return a deterministic, read-only M1 gate report."""
    db = Path(db_path).expanduser().resolve()
    repo = Path(repo_root).expanduser().resolve()
    evidence = None if evidence_dir is None else Path(evidence_dir).expanduser().resolve()
    if not db.is_file():
        raise FileNotFoundError(f"database does not exist: {db}")
    if not repo.is_dir():
        raise NotADirectoryError(f"repository root does not exist: {repo}")

    with closing(_connect_read_only(db)) as conn:
        gates = [
            _audit_db_copy(evidence),
            _audit_dual_write(evidence),
            _audit_three_attempts(conn),
            _audit_cleanup(evidence),
            _audit_lifecycle_replay(conn),
            _audit_illegal_transition(conn),
            _audit_pause(evidence, conn),
            _audit_project_class_controls(conn),
            _audit_canonical_path(evidence),
            _audit_legacy_retention(conn, repo),
        ]

    counts = {status: 0 for status in sorted(VALID_GATE_STATUSES)}
    for gate in gates:
        counts[gate.status] += 1
    overall = "passed" if counts["passed"] == len(gates) else (
        "blocked" if counts["blocked"] else "partial"
    )
    return {
        "schema_version": M1_EXIT_GATE_SCHEMA_VERSION,
        "db_path": str(db),
        "repo_root": str(repo),
        "evidence_dir": None if evidence is None else str(evidence),
        "read_only": True,
        "gate_status_counts": counts,
        "gates": [gate.to_dict() for gate in gates],
        "m1_exit_gate": overall,
        "m2_entry_allowed": overall == "passed",
        "shadow_mode_ready": False,
        "auto_merge_eligible": False,
        "next_required_actions": [
            gate.next_action for gate in gates if gate.next_action is not None
        ],
    }


__all__ = [
    "GateResult",
    "M1_EXIT_GATE_SCHEMA_VERSION",
    "VALID_GATE_STATUSES",
    "audit_m1_exit_gate",
]
