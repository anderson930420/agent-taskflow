"""Safe production-database-copy migration and rollback rehearsal for M1-A.

The production database is opened read-only and copied with SQLite's online
backup API.  Migrations, integrity checks, idempotency checks, and restore
operations run only against rehearsal databases in a fresh output directory.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any
import uuid

from agent_taskflow.atomic_write import atomic_write_json
from agent_taskflow.validator_process_schema import migrate_validator_process_lifecycle

M1_DB_COPY_REHEARSAL_SCHEMA_VERSION = "m1_production_db_copy_rehearsal.v1"
EVIDENCE_FILENAME = "production-db-copy-rehearsal.json"
SNAPSHOT_FILENAME = "source-snapshot.sqlite3"
MIGRATION_TARGET_FILENAME = "migration-target.sqlite3"
RESTORE_TARGET_FILENAME = "restore-target.sqlite3"

_ACTIVE_PROCESS_STATES = ("allocated", "running", "term_sent", "kill_sent")
_ACTIVE_RESOURCE_STATES = ("allocated", "active", "reap_blocked_live_pid")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone() is not None


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table)})")}


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _backup_database(source: Path, destination: Path, *, destination_must_be_new: bool) -> None:
    if destination_must_be_new and destination.exists():
        raise FileExistsError(f"backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(_read_only_connection(source)) as source_conn:
        with closing(sqlite3.connect(destination)) as destination_conn:
            source_conn.backup(destination_conn)
            destination_conn.commit()


def _integrity_report(path: Path) -> dict[str, Any]:
    with closing(_read_only_connection(path)) as conn:
        integrity_rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        foreign_key_rows = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    return {
        "integrity_rows": integrity_rows,
        "integrity_ok": integrity_rows == ["ok"],
        "foreign_key_violations": [list(row) for row in foreign_key_rows],
        "foreign_key_ok": not foreign_key_rows,
        "page_count": page_count,
        "freelist_count": freelist_count,
        "user_version": user_version,
    }


def _schema_and_row_inventory(path: Path) -> dict[str, Any]:
    with closing(_read_only_connection(path)) as conn:
        objects = [
            {
                "type": str(row["type"]),
                "name": str(row["name"]),
                "table": str(row["tbl_name"]),
                "sql_sha256": hashlib.sha256(
                    ("" if row["sql"] is None else str(row["sql"])).encode("utf-8")
                ).hexdigest(),
            }
            for row in conn.execute(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            )
        ]
        tables = [
            str(row["name"])
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]
        row_counts: dict[str, int] = {}
        for table in tables:
            row_counts[table] = int(
                conn.execute(f"SELECT COUNT(*) FROM {_quote_identifier(table)}").fetchone()[0]
            )
    canonical = json.dumps(
        {"objects": objects, "row_counts": row_counts},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "objects": objects,
        "row_counts": row_counts,
        "schema_row_inventory_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _logical_dump_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with closing(_read_only_connection(path)) as conn:
        for statement in conn.iterdump():
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def _migration_names(path: Path) -> list[str]:
    with closing(_read_only_connection(path)) as conn:
        if not _table_exists(conn, "schema_migrations"):
            return []
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM schema_migrations ORDER BY name"
            )
        ]


def _active_runtime_counts(path: Path) -> dict[str, int]:
    counts = {
        "tasks_with_active_attempt": 0,
        "active_attempts": 0,
        "active_runtime_leases": 0,
        "active_managed_processes": 0,
        "active_attempt_resources": 0,
    }
    with closing(_read_only_connection(path)) as conn:
        if "active_attempt_id" in _column_names(conn, "tasks"):
            counts["tasks_with_active_attempt"] = int(
                conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE active_attempt_id IS NOT NULL"
                ).fetchone()[0]
            )
        if "is_active" in _column_names(conn, "attempts"):
            counts["active_attempts"] = int(
                conn.execute("SELECT COUNT(*) FROM attempts WHERE is_active = 1").fetchone()[0]
            )
        if "is_active" in _column_names(conn, "runtime_leases"):
            counts["active_runtime_leases"] = int(
                conn.execute(
                    "SELECT COUNT(*) FROM runtime_leases WHERE is_active = 1"
                ).fetchone()[0]
            )
        process_columns = _column_names(conn, "executor_processes")
        if "state" in process_columns:
            placeholders = ",".join("?" for _ in _ACTIVE_PROCESS_STATES)
            counts["active_managed_processes"] = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM executor_processes WHERE state IN ({placeholders})",
                    _ACTIVE_PROCESS_STATES,
                ).fetchone()[0]
            )
        resource_columns = _column_names(conn, "attempt_resources")
        if "status" in resource_columns:
            placeholders = ",".join("?" for _ in _ACTIVE_RESOURCE_STATES)
            counts["active_attempt_resources"] = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM attempt_resources WHERE status IN ({placeholders})",
                    _ACTIVE_RESOURCE_STATES,
                ).fetchone()[0]
            )
    return counts


def _database_report(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
        "logical_dump_sha256": _logical_dump_sha256(path),
        "integrity": _integrity_report(path),
        "inventory": _schema_and_row_inventory(path),
        "schema_migrations": _migration_names(path),
    }


def _assert_valid_database(report: dict[str, Any], label: str) -> None:
    integrity = report["integrity"]
    if not integrity["integrity_ok"]:
        raise RuntimeError(f"{label} failed PRAGMA integrity_check: {integrity['integrity_rows']}")
    if not integrity["foreign_key_ok"]:
        raise RuntimeError(
            f"{label} failed PRAGMA foreign_key_check: {integrity['foreign_key_violations']}"
        )


def _prepare_output_directory(output_dir: Path, source_db: Path) -> None:
    if output_dir == source_db or source_db in output_dir.parents:
        raise ValueError("output directory cannot be the source database or a child of it")
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = list(output_dir.iterdir())
    if existing:
        raise FileExistsError(
            f"rehearsal output directory must be empty: {output_dir}"
        )


def run_m1_db_copy_rehearsal(
    *,
    source_db: str | Path,
    output_dir: str | Path,
    actor: str,
    repo_root: str | Path,
) -> dict[str, Any]:
    """Run a fail-closed M1-A rehearsal and atomically write its evidence JSON."""
    source = Path(source_db).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    repo = Path(repo_root).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"source database does not exist: {source}")
    if not repo.is_dir():
        raise NotADirectoryError(f"repository root does not exist: {repo}")
    if not actor.strip():
        raise ValueError("actor must not be empty")
    _prepare_output_directory(output, source)

    started_at = _utc_now_iso()
    rehearsal_id = f"m1a-{uuid.uuid4()}"
    snapshot = output / SNAPSHOT_FILENAME
    migration_target = output / MIGRATION_TARGET_FILENAME
    restore_target = output / RESTORE_TARGET_FILENAME
    evidence_path = output / EVIDENCE_FILENAME

    _backup_database(source, snapshot, destination_must_be_new=True)
    snapshot_report = _database_report(snapshot)
    _assert_valid_database(snapshot_report, "source snapshot")
    active_counts = _active_runtime_counts(snapshot)
    if any(active_counts.values()):
        raise RuntimeError(
            "source snapshot is not quiescent; active runtime state: "
            + json.dumps(active_counts, sort_keys=True)
        )

    _backup_database(snapshot, migration_target, destination_must_be_new=True)
    migrations_before = _migration_names(migration_target)
    migrate_validator_process_lifecycle(migration_target)
    migrated_once_report = _database_report(migration_target)
    _assert_valid_database(migrated_once_report, "migration target after first pass")

    migrate_validator_process_lifecycle(migration_target)
    migrated_twice_report = _database_report(migration_target)
    _assert_valid_database(migrated_twice_report, "migration target after second pass")
    migration_idempotent = (
        migrated_once_report["logical_dump_sha256"]
        == migrated_twice_report["logical_dump_sha256"]
        and migrated_once_report["inventory"]["schema_row_inventory_sha256"]
        == migrated_twice_report["inventory"]["schema_row_inventory_sha256"]
    )
    if not migration_idempotent:
        raise RuntimeError("migration entrypoint is not idempotent on the rehearsal copy")

    _backup_database(migration_target, restore_target, destination_must_be_new=True)
    restore_preimage_sha256 = _logical_dump_sha256(restore_target)
    _backup_database(snapshot, restore_target, destination_must_be_new=False)
    restored_report = _database_report(restore_target)
    _assert_valid_database(restored_report, "restored target")

    rollback_matches_snapshot = (
        restored_report["logical_dump_sha256"] == snapshot_report["logical_dump_sha256"]
        and restored_report["inventory"]["schema_row_inventory_sha256"]
        == snapshot_report["inventory"]["schema_row_inventory_sha256"]
    )
    if not rollback_matches_snapshot:
        raise RuntimeError("restored target does not match the pre-migration snapshot")

    evidence = {
        "schema_version": M1_DB_COPY_REHEARSAL_SCHEMA_VERSION,
        "rehearsal_id": rehearsal_id,
        "actor": actor.strip(),
        "started_at": started_at,
        "completed_at": _utc_now_iso(),
        "repo_root": str(repo),
        "source_db_path": str(source),
        "source_connection_mode": "read_only",
        "source_query_only": True,
        "source_db_mutated_by_runner": False,
        "source_active_runtime_counts": active_counts,
        "source_quiescent": not any(active_counts.values()),
        "backup_method": "sqlite3.Connection.backup",
        "migration_entrypoint": (
            "agent_taskflow.validator_process_schema."
            "migrate_validator_process_lifecycle"
        ),
        "migration_commands": [
            "migrate_validator_process_lifecycle(migration-target.sqlite3)",
            "migrate_validator_process_lifecycle(migration-target.sqlite3) # idempotency",
        ],
        "migrations_before": migrations_before,
        "migrations_after": migrated_twice_report["schema_migrations"],
        "migration_dry_run": True,
        "migration_idempotent": migration_idempotent,
        "integrity_check": all(
            report["integrity"]["integrity_ok"]
            for report in (snapshot_report, migrated_twice_report, restored_report)
        ),
        "foreign_key_check": all(
            report["integrity"]["foreign_key_ok"]
            for report in (snapshot_report, migrated_twice_report, restored_report)
        ),
        "rollback_rehearsal": rollback_matches_snapshot,
        "rollback_method": "restore source snapshot over a cloned migrated target via SQLite backup API",
        "restore_preimage_logical_dump_sha256": restore_preimage_sha256,
        "artifacts": {
            "source_snapshot": str(snapshot),
            "migration_target": str(migration_target),
            "restore_target": str(restore_target),
            "evidence": str(evidence_path),
        },
        "source_snapshot": snapshot_report,
        "migration_target": migrated_twice_report,
        "restore_target": restored_report,
        "safety": {
            "production_database_opened_read_only": True,
            "production_migration_executed": False,
            "production_restore_executed": False,
            "plain_file_copy_used": False,
            "active_runtime_allowed": False,
            "fresh_output_directory_required": True,
        },
    }
    atomic_write_json(evidence_path, evidence, indent=2, sort_keys=True)
    return evidence


__all__ = [
    "EVIDENCE_FILENAME",
    "M1_DB_COPY_REHEARSAL_SCHEMA_VERSION",
    "run_m1_db_copy_rehearsal",
]
