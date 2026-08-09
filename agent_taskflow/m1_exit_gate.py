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
import subprocess
from typing import Any

from agent_taskflow.project_class_control_schema import (
    PROJECT_CLASS_CONTROLS_MIGRATION,
)

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


_PROJECT_CLASS_CONTROL_REQUIRED_SEMANTICS = (
    "project_pause_denied_new_pickup",
    "project_pause_did_not_abort_existing_attempt",
    "project_pause_cleared",
    "task_class_initially_control_permitted",
    "task_class_disable_applied",
    "task_class_eligibility_denied_immediately",
    "task_class_disable_cleared",
    "task_class_disable_did_not_abort_existing_attempt",
    "unrelated_project_unaffected",
    "unrelated_task_class_unaffected",
    "append_only_control_evidence_verified",
    "operator_attribution_verified",
    "alternate_level2_entrypoint_denied",
)


def _audit_project_class_controls(
    evidence_dir: Path | None,
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
) -> GateResult:
    sql = _schema_sql(conn, "runtime_controls").lower()
    has_project = "'project'" in sql or '"project"' in sql
    has_class = any(token in sql for token in ("'task_class'", "'class'", '"task_class"'))
    tables_ready = _table_exists(conn, "runtime_controls") and _table_exists(
        conn, "runtime_control_events"
    )
    migration_ready = _table_exists(conn, "schema_migrations") and conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name = ?",
        (PROJECT_CLASS_CONTROLS_MIGRATION,),
    ).fetchone() is not None
    if not tables_ready or not has_project or not has_class or not migration_ready:
        missing: list[str] = []
        if not tables_ready:
            missing.append("control tables")
        if not has_project:
            missing.append("project scope")
        if not has_class:
            missing.append("task_class scope")
        if not migration_ready:
            missing.append(PROJECT_CLASS_CONTROLS_MIGRATION)
        return GateResult(
            "project_class_kill_switch",
            "blocked",
            f"Project/class control implementation is not deployed: {', '.join(missing)}",
            next_action=(
                "Apply the project/class control migration, preserving existing "
                "runtime control history."
            ),
        )

    payload, source = _load_evidence(
        evidence_dir, "project-class-control-rehearsal.json"
    )
    if payload is None:
        return GateResult(
            "project_class_kill_switch",
            "partial",
            "Project/class scopes are deployed, but authoritative disable rehearsal evidence is missing.",
            next_action=(
                "Run the project/class control rehearsal against this deployed "
                "database and retain its evidence."
            ),
        )

    errors: list[str] = []
    if payload.get("schema_version") != "m1_project_class_controls.v1":
        errors.append("schema_version must be m1_project_class_controls.v1")
    if payload.get("migration") != PROJECT_CLASS_CONTROLS_MIGRATION:
        errors.append(f"migration must be {PROJECT_CLASS_CONTROLS_MIGRATION}")
    if payload.get("repo_sha") != _git_head(repo_root):
        errors.append("repo_sha must match the audited repository HEAD")
    raw_evidence_db = payload.get("database_path")
    evidence_db: Path | None = None
    if not isinstance(raw_evidence_db, str) or not raw_evidence_db.strip():
        errors.append("database_path must identify the disposable rehearsal database")
    else:
        try:
            evidence_db = Path(raw_evidence_db).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            errors.append("database_path must identify the disposable rehearsal database")
    if evidence_db is not None:
        if not evidence_db.is_file():
            errors.append("disposable rehearsal database does not exist")
        else:
            try:
                with closing(_connect_read_only(evidence_db)) as evidence_conn:
                    evidence_sql = _schema_sql(
                        evidence_conn, "runtime_controls"
                    ).lower()
                    evidence_schema_ready = (
                        _table_exists(evidence_conn, "runtime_control_events")
                        and "'project'" in evidence_sql
                        and "'task_class'" in evidence_sql
                        and _table_exists(evidence_conn, "schema_migrations")
                        and evidence_conn.execute(
                            "SELECT 1 FROM schema_migrations WHERE name = ?",
                            (PROJECT_CLASS_CONTROLS_MIGRATION,),
                        ).fetchone()
                        is not None
                    )
            except sqlite3.DatabaseError:
                evidence_schema_ready = False
            if not evidence_schema_ready:
                errors.append(
                    "disposable rehearsal database must contain the deployed "
                    "project/class control schema"
                )
    if payload.get("production_database_modified") is not False:
        errors.append("production_database_modified must be false")
    if payload.get("real_executor_invoked") is not False:
        errors.append("real_executor_invoked must be false")
    if payload.get("actual_auto_merge_enabled") is not False:
        errors.append("actual_auto_merge_enabled must be false")
    if payload.get("task_class_control_scope") != "class_global":
        errors.append("task_class_control_scope must be class_global")
    fixture = payload.get("fixture_identifiers")
    if not isinstance(fixture, dict):
        errors.append("fixture_identifiers must be an object")
    else:
        projects = fixture.get("projects")
        task_classes = fixture.get("task_classes")
        if (
            not isinstance(projects, list)
            or not all(isinstance(item, str) and item.strip() for item in projects)
            or len(set(projects)) < 2
        ):
            errors.append("fixture must contain at least two projects")
        if (
            not isinstance(task_classes, list)
            or not all(
                isinstance(item, str) and item.strip() for item in task_classes
            )
            or len(set(task_classes)) < 2
        ):
            errors.append("fixture must contain at least two task classes")
    for field in _PROJECT_CLASS_CONTROL_REQUIRED_SEMANTICS:
        if payload.get(field) is not True:
            errors.append(f"{field} must be true")
    if errors:
        return GateResult(
            "project_class_kill_switch",
            "blocked",
            "; ".join(errors),
            evidence=(source,),
            next_action="Repeat the project/class control rehearsal with valid DB- and SHA-bound evidence.",
        )
    return GateResult(
        "project_class_kill_switch",
        "passed",
        "Project admission pause and class-global governance disable are deployed, immediate, isolated, and append-only audited.",
        evidence=(source,),
    )


_CANONICAL_PATH_REQUIRED_SEMANTICS = (
    "scheduler_level2_engine_authoritative",
    "direct_legacy_level2_entry_blocked",
    "alternate_level2_entrypoints_engine_or_fail_closed",
    "injected_runner_level2_bypass_blocked",
    "engine_canonical_attempt_verified_in_store",
    "downstream_exact_attempt_binding_verified",
    "pr_handoff_exact_attempt_binding_verified",
    "engine_failure_legacy_fallback_blocked",
    "legacy_reader_compatibility_retained",
)

_CANONICAL_PATH_REQUIRED_ADVERSARIAL_CHECKS = (
    "nonexistent_attempt_rejected",
    "wrong_task_attempt_rejected",
    "nonterminal_attempt_rejected",
)


def _git_head(repo_root: Path) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        shell=False,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _audit_canonical_path(
    evidence_dir: Path | None,
    repo_root: Path,
) -> GateResult:
    payload, source = _load_evidence(evidence_dir, "canonical-execution-path.json")
    if payload is None:
        return GateResult(
            "canonical_execution_path",
            "blocked",
            source,
            next_action="Provide a passing ExecutionEngine parity report or proof that legacy execution is rejected for every Level 2-eligible class.",
        )
    checks = payload.get("checks")
    adversarial = payload.get("adversarial_attempt_checks")
    canonical_attempt_id = payload.get("canonical_attempt_id")
    valid = (
        payload.get("schema_version") == "m1_canonical_execution_path.v2"
        and payload.get("canonical_path") == "ExecutionEngine"
        and payload.get("repo_sha") == _git_head(repo_root)
        and payload.get("deterministic_fixture") is True
        and payload.get("production_db_mutated") is False
        and payload.get("real_executor_invoked") is False
        and isinstance(canonical_attempt_id, str)
        and bool(canonical_attempt_id.strip())
        and isinstance(checks, dict)
        and isinstance(adversarial, dict)
        and all(
            payload.get(name) is True and checks.get(name) is True
            for name in _CANONICAL_PATH_REQUIRED_SEMANTICS
        )
        and all(
            adversarial.get(name) is True
            for name in _CANONICAL_PATH_REQUIRED_ADVERSARIAL_CHECKS
        )
    )
    if not valid:
        return GateResult(
            "canonical_execution_path",
            "blocked",
            "Canonical-path evidence does not prove the repository-wide Level 2 authority and exact-Attempt contract for this repository SHA.",
            evidence=(source,),
            next_action="Complete ExecutionEngine parity/enforcement and regenerate the evidence.",
        )
    return GateResult(
        "canonical_execution_path",
        "passed",
        "ExecutionEngine is proven as the repository-wide Level 2 authority with exact store-verified Attempt propagation and no legacy fallback.",
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
            _audit_project_class_controls(
                evidence,
                conn,
                repo_root=repo,
            ),
            _audit_canonical_path(evidence, repo),
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
