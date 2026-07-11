from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from agent_taskflow.m1_db_copy_rehearsal import (
    EVIDENCE_FILENAME,
    M1_DB_COPY_REHEARSAL_SCHEMA_VERSION,
    _active_runtime_counts,
    run_m1_db_copy_rehearsal,
)
from agent_taskflow.m1_exit_gate_cli import audit_m1_exit_gate
from agent_taskflow.validator_process_schema import migrate_validator_process_lifecycle


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_m1_db_copy_rehearsal.py"


class M1DatabaseCopyRehearsalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "state.db"
        migrate_validator_process_lifecycle(self.source)
        with sqlite3.connect(self.source) as conn:
            conn.execute("CREATE TABLE rehearsal_sentinel(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO rehearsal_sentinel(value) VALUES ('preserve-me')")
        self.output = self.root / "rehearsal"

    def test_rehearsal_uses_copy_only_and_writes_accepted_evidence(self) -> None:
        source_before = self.source.read_bytes()

        evidence = run_m1_db_copy_rehearsal(
            source_db=self.source,
            output_dir=self.output,
            actor="test-operator",
            repo_root=REPO_ROOT,
        )

        self.assertEqual(self.source.read_bytes(), source_before)
        self.assertEqual(
            evidence["schema_version"], M1_DB_COPY_REHEARSAL_SCHEMA_VERSION
        )
        self.assertTrue(evidence["migration_dry_run"])
        self.assertTrue(evidence["migration_idempotent"])
        self.assertTrue(evidence["integrity_check"])
        self.assertTrue(evidence["foreign_key_check"])
        self.assertTrue(evidence["rollback_rehearsal"])
        self.assertTrue(evidence["source_quiescent"])
        self.assertFalse(evidence["source_db_mutated_by_runner"])
        self.assertEqual(
            evidence["source_snapshot"]["logical_dump_sha256"],
            evidence["restore_target"]["logical_dump_sha256"],
        )
        self.assertEqual(
            evidence["source_snapshot"]["inventory"]["row_counts"]["rehearsal_sentinel"],
            1,
        )

        evidence_path = self.output / EVIDENCE_FILENAME
        self.assertTrue(evidence_path.is_file())
        persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["rehearsal_id"], evidence["rehearsal_id"])

        report = audit_m1_exit_gate(
            db_path=self.source,
            repo_root=REPO_ROOT,
            evidence_dir=self.output,
        )
        gate = next(
            gate
            for gate in report["gates"]
            if gate["gate"] == "production_db_copy_rehearsal"
        )
        self.assertEqual(gate["status"], "passed")
        self.assertEqual(report["m1_exit_gate"], "blocked")
        self.assertFalse(report["m2_entry_allowed"])

    def test_rehearsal_refuses_nonempty_output_directory(self) -> None:
        self.output.mkdir()
        (self.output / "existing.txt").write_text("do not overwrite", encoding="utf-8")

        with self.assertRaisesRegex(FileExistsError, "must be empty"):
            run_m1_db_copy_rehearsal(
                source_db=self.source,
                output_dir=self.output,
                actor="test-operator",
                repo_root=REPO_ROOT,
            )

        self.assertEqual(
            (self.output / "existing.txt").read_text(encoding="utf-8"),
            "do not overwrite",
        )

    def test_active_runtime_counts_cover_all_authority_tables(self) -> None:
        db = self.root / "active.db"
        with sqlite3.connect(db) as conn:
            conn.executescript(
                """
                CREATE TABLE tasks(active_attempt_id TEXT);
                CREATE TABLE attempts(is_active INTEGER);
                CREATE TABLE runtime_leases(is_active INTEGER);
                CREATE TABLE executor_processes(state TEXT);
                CREATE TABLE attempt_resources(status TEXT);
                INSERT INTO tasks VALUES ('attempt-1');
                INSERT INTO attempts VALUES (1);
                INSERT INTO runtime_leases VALUES (1);
                INSERT INTO executor_processes VALUES ('running');
                INSERT INTO attempt_resources VALUES ('active');
                """
            )

        self.assertEqual(
            _active_runtime_counts(db),
            {
                "tasks_with_active_attempt": 1,
                "active_attempts": 1,
                "active_runtime_leases": 1,
                "active_managed_processes": 1,
                "active_attempt_resources": 1,
            },
        )

    def test_cli_runs_from_source_checkout_without_site_packages(self) -> None:
        cli_output = self.root / "cli-rehearsal"
        completed = subprocess.run(
            [
                sys.executable,
                "-S",
                str(SCRIPT),
                "--db-path",
                str(self.source),
                "--output-dir",
                str(cli_output),
                "--actor",
                "test-operator",
                "--repo-root",
                str(REPO_ROOT),
                "--confirm-production-copy-rehearsal",
            ],
            cwd=REPO_ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["migration_dry_run"])
        self.assertTrue(payload["rollback_rehearsal"])
        self.assertFalse(payload["production_database_modified"])
        self.assertTrue((cli_output / EVIDENCE_FILENAME).is_file())

    def test_cli_requires_explicit_confirmation(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-S",
                str(SCRIPT),
                "--db-path",
                str(self.source),
                "--output-dir",
                str(self.output),
                "--actor",
                "test-operator",
            ],
            cwd=REPO_ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stderr)
        self.assertIn("--confirm-production-copy-rehearsal", payload["error"])
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
