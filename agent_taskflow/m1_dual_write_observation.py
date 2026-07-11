"""Bounded dual-write consistency observation for Milestone 1-B.

The production SQLite database is opened read-only and copied with SQLite's
online backup API. Disposable workloads run only on the observation copy and
exercise the real RuntimeAdmissionStore claim/release path, which writes both
legacy task_events and canonical lifecycle_events in one transaction.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.m1_db_copy_rehearsal import (
    _active_runtime_counts,
    _backup_database,
    _database_report,
)
from agent_taskflow.models import TaskRecord
from agent_taskflow.runtime_admission import RuntimeAdmissionStore
from agent_taskflow.store import TaskMirrorStore, connect
from agent_taskflow.validator_process_schema import migrate_validator_process_lifecycle

M1_DUAL_WRITE_SCHEMA_VERSION = "m1_dual_write_consistency.v1"
EVIDENCE_FILENAME = "dual-write-consistency.json"
SOURCE_SNAPSHOT_FILENAME = "source-snapshot.sqlite3"
OBSERVATION_TARGET_FILENAME = "observation-target.sqlite3"
DEFAULT_WORKLOAD_TASKS = 3
_EXPECTED_PHASES = ("preparing", "completed")


def _utc_now_precise() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _prepare_output_directory(output_dir: Path, source_db: Path) -> None:
    if output_dir == source_db or source_db in output_dir.parents:
        raise ValueError("output directory cannot be the source database or a child of it")
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise FileExistsError(
            f"dual-write observation output directory must be empty: {output_dir}"
        )


def _parse_status_payload(payload_json: str | None) -> tuple[str | None, str | None]:
    if payload_json is None:
        return None, "payload_json is null"
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        return None, f"payload_json is invalid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "payload_json is not an object"
    status = payload.get("status")
    if not isinstance(status, str) or not status:
        return None, "payload_json.status is missing"
    return status, None


def collect_dual_write_observation(
    db_path: str | Path,
    *,
    task_keys: list[str],
    claim_reason: str,
    release_reason: str,
) -> dict[str, Any]:
    """Compare legacy and canonical transition records for one bounded workload."""
    path = Path(db_path).expanduser().resolve()
    if not task_keys:
        raise ValueError("task_keys must not be empty")
    placeholders = ",".join("?" for _ in task_keys)
    lifecycle_reasons = (claim_reason, release_reason)

    with closing(connect(path)) as conn:
        legacy_rows = conn.execute(
            f"""
            SELECT id, task_key, source, message, payload_json, created_at
            FROM task_events
            WHERE task_key IN ({placeholders})
              AND event_type = 'status_changed'
            ORDER BY task_key, id
            """,
            task_keys,
        ).fetchall()
        lifecycle_rows = conn.execute(
            f"""
            SELECT lifecycle_events.event_id,
                   tasks.task_key,
                   lifecycle_events.attempt_id,
                   lifecycle_events.actor,
                   lifecycle_events.timestamp,
                   lifecycle_events.to_status,
                   lifecycle_events.reason_code
            FROM lifecycle_events
            JOIN tasks ON tasks.task_id = lifecycle_events.task_id
            WHERE tasks.task_key IN ({placeholders})
              AND lifecycle_events.reason_code IN (?, ?)
            ORDER BY tasks.task_key, lifecycle_events.event_id
            """,
            [*task_keys, *lifecycle_reasons],
        ).fetchall()
        task_rows = {
            str(row["task_key"]): row
            for row in conn.execute(
                f"""
                SELECT task_key, task_id, status, active_attempt_id
                FROM tasks WHERE task_key IN ({placeholders})
                """,
                task_keys,
            ).fetchall()
        }
        attempt_rows = {
            str(row["task_key"]): row
            for row in conn.execute(
                f"""
                SELECT tasks.task_key, attempts.attempt_id, attempts.status,
                       attempts.is_active, attempts.ended_at
                FROM tasks
                JOIN attempts ON attempts.task_id = tasks.task_id
                WHERE tasks.task_key IN ({placeholders})
                """,
                task_keys,
            ).fetchall()
        }
        lease_rows = {
            str(row["task_key"]): row
            for row in conn.execute(
                f"""
                SELECT tasks.task_key, runtime_leases.lease_id,
                       runtime_leases.is_active, runtime_leases.released_at
                FROM tasks
                JOIN runtime_leases ON runtime_leases.task_id = tasks.task_id
                WHERE tasks.task_key IN ({placeholders})
                """,
                task_keys,
            ).fetchall()
        }

    legacy_by_key_status: dict[tuple[str, str], list[dict[str, Any]]] = {}
    malformed_legacy: list[dict[str, Any]] = []
    for row in legacy_rows:
        status, error = _parse_status_payload(row["payload_json"])
        record = {
            "event_id": int(row["id"]),
            "task_key": str(row["task_key"]),
            "source": str(row["source"]),
            "message": row["message"],
            "created_at": str(row["created_at"]),
            "status": status,
        }
        if error is not None or status is None:
            record["error"] = error
            malformed_legacy.append(record)
            continue
        legacy_by_key_status.setdefault((record["task_key"], status), []).append(record)

    lifecycle_by_key_status: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in lifecycle_rows:
        record = {
            "event_id": int(row["event_id"]),
            "task_key": str(row["task_key"]),
            "attempt_id": row["attempt_id"],
            "actor": str(row["actor"]),
            "timestamp": str(row["timestamp"]),
            "status": str(row["to_status"]),
            "reason_code": str(row["reason_code"]),
        }
        lifecycle_by_key_status.setdefault(
            (record["task_key"], record["status"]), []
        ).append(record)

    comparisons: list[dict[str, Any]] = []
    mismatch_count = len(malformed_legacy)
    silent_failure_count = 0
    records_compared = 0

    for task_key in task_keys:
        for phase in _EXPECTED_PHASES:
            legacy = legacy_by_key_status.get((task_key, phase), [])
            canonical = lifecycle_by_key_status.get((task_key, phase), [])
            comparison: dict[str, Any] = {
                "task_key": task_key,
                "phase": phase,
                "legacy_count": len(legacy),
                "canonical_count": len(canonical),
                "matched": False,
                "errors": [],
            }
            if not legacy or not canonical:
                silent_failure_count += 1
                if not legacy:
                    comparison["errors"].append("legacy transition missing")
                if not canonical:
                    comparison["errors"].append("canonical transition missing")
                comparisons.append(comparison)
                continue
            if len(legacy) != 1 or len(canonical) != 1:
                mismatch_count += 1
                comparison["errors"].append("transition is not unique on both sides")
                comparisons.append(comparison)
                continue

            legacy_record = legacy[0]
            canonical_record = canonical[0]
            records_compared += 1
            checks = {
                "status_match": legacy_record["status"] == canonical_record["status"],
                "task_key_match": legacy_record["task_key"]
                == canonical_record["task_key"],
                "actor_source_match": legacy_record["source"]
                == canonical_record["actor"],
                "timestamp_match": legacy_record["created_at"]
                == canonical_record["timestamp"],
                "attempt_identity_present": isinstance(
                    canonical_record["attempt_id"], str
                )
                and bool(canonical_record["attempt_id"]),
            }
            errors = [name for name, passed in checks.items() if not passed]
            if errors:
                mismatch_count += 1
            comparison.update(
                {
                    "legacy": legacy_record,
                    "canonical": canonical_record,
                    "checks": checks,
                    "matched": not errors,
                    "errors": errors,
                }
            )
            comparisons.append(comparison)

    postconditions: list[dict[str, Any]] = []
    for task_key in task_keys:
        task = task_rows.get(task_key)
        attempt = attempt_rows.get(task_key)
        lease = lease_rows.get(task_key)
        checks = {
            "task_exists": task is not None,
            "task_completed": task is not None and task["status"] == "completed",
            "task_has_no_active_attempt": task is not None
            and task["active_attempt_id"] is None,
            "attempt_exists": attempt is not None,
            "attempt_completed": attempt is not None
            and attempt["status"] == "completed",
            "attempt_inactive": attempt is not None and int(attempt["is_active"]) == 0,
            "attempt_has_end_time": attempt is not None
            and attempt["ended_at"] is not None,
            "lease_exists": lease is not None,
            "lease_inactive": lease is not None and int(lease["is_active"]) == 0,
            "lease_has_release_time": lease is not None
            and lease["released_at"] is not None,
        }
        errors = [name for name, passed in checks.items() if not passed]
        if errors:
            mismatch_count += 1
        postconditions.append(
            {
                "task_key": task_key,
                "checks": checks,
                "matched": not errors,
                "errors": errors,
            }
        )

    expected_records = len(task_keys) * len(_EXPECTED_PHASES)
    return {
        "task_keys": task_keys,
        "expected_transition_pairs": expected_records,
        "records_compared": records_compared,
        "mismatch_count": mismatch_count,
        "silent_failure_count": silent_failure_count,
        "malformed_legacy_records": malformed_legacy,
        "comparisons": comparisons,
        "terminal_postconditions": postconditions,
        "consistent": (
            records_compared == expected_records
            and mismatch_count == 0
            and silent_failure_count == 0
        ),
    }


def run_m1_dual_write_observation(
    *,
    source_db: str | Path,
    output_dir: str | Path,
    actor: str,
    repo_root: str | Path,
    workload_tasks: int = DEFAULT_WORKLOAD_TASKS,
) -> dict[str, Any]:
    """Run the M1-B workload on a production copy and write passing evidence."""
    source = Path(source_db).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    repo = Path(repo_root).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"source database does not exist: {source}")
    if not repo.is_dir():
        raise NotADirectoryError(f"repository root does not exist: {repo}")
    normalized_actor = actor.strip()
    if not normalized_actor:
        raise ValueError("actor must not be empty")
    count = int(workload_tasks)
    if count < 1 or count > 100:
        raise ValueError("workload_tasks must be between 1 and 100")
    _prepare_output_directory(output, source)

    source_sha_before = _sha256_file(source)
    observation_id = f"m1b-{uuid4()}"
    short_id = observation_id.replace("-", "")[-12:]
    snapshot = output / SOURCE_SNAPSHOT_FILENAME
    target = output / OBSERVATION_TARGET_FILENAME
    evidence_path = output / EVIDENCE_FILENAME

    _backup_database(source, snapshot, destination_must_be_new=True)
    source_report = _database_report(snapshot)
    active_counts = _active_runtime_counts(snapshot)
    if any(active_counts.values()):
        raise RuntimeError(
            "source snapshot is not quiescent; active runtime state: "
            + json.dumps(active_counts, sort_keys=True)
        )
    _backup_database(snapshot, target, destination_must_be_new=True)
    migrate_validator_process_lifecycle(target)

    task_store = TaskMirrorStore(target)
    runtime_store = RuntimeAdmissionStore(target)
    task_keys: list[str] = []
    claim_reason = "m1b_dual_write_claim"
    release_reason = "m1b_dual_write_release"
    observation_started_at = _utc_now_precise()
    for index in range(1, count + 1):
        task_key = f"AT-M1B-{short_id}-{index:03d}"
        task_keys.append(task_key)
        artifact_dir = output / "workload-artifacts" / task_key
        task_store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="m1b-observation",
                board="m1b-observation",
                title="Disposable M1-B dual-write observation task",
                status="queued",
                repo_path=repo,
                artifact_dir=artifact_dir,
                executor="noop",
            ),
            preserve_existing_status=False,
        )
        owner = f"{normalized_actor}:m1b:{index}"
        claim = runtime_store.claim(
            task_key,
            owner_id=owner,
            executor="noop",
            policy_version="m1b-observation.v1",
            permission_profile="no-side-effects",
            artifact_root=artifact_dir,
            reason_code=claim_reason,
            metadata={"observation_id": observation_id, "workload_index": index},
        )
        runtime_store.release(
            claim.attempt_id,
            owner_id=owner,
            lease_token=claim.lease_token,
            attempt_status="completed",
            task_status="completed",
            reason_code=release_reason,
            execution_result="observation_only",
            validation_result="not_applicable",
            merge_recommendation="not_applicable",
            metadata={"observation_id": observation_id, "workload_index": index},
        )
    observation_ended_at = _utc_now_precise()

    comparison = collect_dual_write_observation(
        target,
        task_keys=task_keys,
        claim_reason=claim_reason,
        release_reason=release_reason,
    )
    if not comparison["consistent"]:
        raise RuntimeError(
            "dual-write observation failed: "
            + json.dumps(
                {
                    "records_compared": comparison["records_compared"],
                    "mismatch_count": comparison["mismatch_count"],
                    "silent_failure_count": comparison["silent_failure_count"],
                },
                sort_keys=True,
            )
        )

    target_report = _database_report(target)
    source_sha_after = _sha256_file(source)
    if source_sha_before != source_sha_after:
        raise RuntimeError("source database file changed during observation window")

    evidence = {
        "schema_version": M1_DUAL_WRITE_SCHEMA_VERSION,
        "observation_id": observation_id,
        "actor": normalized_actor,
        "observation_scope": "production-copy-disposable-workload",
        "observation_window_started_at": observation_started_at,
        "observation_window_ended_at": observation_ended_at,
        "records_compared": comparison["records_compared"],
        "mismatch_count": comparison["mismatch_count"],
        "silent_failure_count": comparison["silent_failure_count"],
        "expected_transition_pairs": comparison["expected_transition_pairs"],
        "workload_task_count": count,
        "workload_task_keys": task_keys,
        "dual_write_seam": (
            "RuntimeAdmissionStore.claim/release: task_events(status_changed) "
            "+ lifecycle_events"
        ),
        "claim_reason_code": claim_reason,
        "release_reason_code": release_reason,
        "source_db_path": str(source),
        "source_connection_mode": "read_only",
        "source_db_mutated_by_runner": False,
        "source_sha256_before": source_sha_before,
        "source_sha256_after": source_sha_after,
        "source_quiescent": not any(active_counts.values()),
        "source_active_runtime_counts": active_counts,
        "backup_method": "sqlite3.Connection.backup",
        "production_workload_executed": False,
        "observation_copy_workload_executed": True,
        "comparison": comparison,
        "artifacts": {
            "source_snapshot": str(snapshot),
            "observation_target": str(target),
            "evidence": str(evidence_path),
        },
        "source_snapshot": source_report,
        "observation_target": target_report,
        "safety": {
            "production_database_opened_read_only": True,
            "production_database_modified": False,
            "plain_file_copy_used": False,
            "disposable_tasks_written_only_to_observation_copy": True,
            "fresh_output_directory_required": True,
            "passing_evidence_requires_zero_mismatch": True,
            "passing_evidence_requires_zero_silent_failure": True,
        },
    }
    atomic_write_json(evidence_path, evidence, indent=2, sort_keys=True)
    return evidence


__all__ = [
    "DEFAULT_WORKLOAD_TASKS",
    "EVIDENCE_FILENAME",
    "M1_DUAL_WRITE_SCHEMA_VERSION",
    "collect_dual_write_observation",
    "run_m1_dual_write_observation",
]
