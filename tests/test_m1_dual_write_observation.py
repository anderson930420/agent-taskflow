from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from agent_taskflow.m1_db_copy_rehearsal import run_m1_db_copy_rehearsal
from agent_taskflow.m1_dual_write_observation import (
    EVIDENCE_FILENAME,
    M1_DUAL_WRITE_SCHEMA_VERSION,
    collect_dual_write_observation,
    run_m1_dual_write_observation,
)
from agent_taskflow.m1_exit_gate_cli import audit_m1_exit_gate
from agent_taskflow.models import TaskRecord
from agent_taskflow.runtime_admission import RuntimeAdmissionStore
from agent_taskflow.store import TaskMirrorStore, connect
from agent_taskflow.validator_process_schema import migrate_validator_process_lifecycle


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_m1_dual_write_observation.py"


class M1DualWriteObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "state.db"
        migrate_validator_process_lifecycle(self.source)
        with sqlite3.connect(self.source) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.output = self.root / "m1b"

    def _run(self, *, workload_tasks: int = 3) -> dict[str, object]:
        return run_m1_dual_write_observation(
            source_db=self.source,
            output_dir=self.output,
            actor="test-operator",
            repo_root=REPO_ROOT,
            workload_tasks=workload_tasks,
        )

    def test_observation_compares_real_claim_release_dual_writes(self) -> None:
        source_before = self.source.read_bytes()
        evidence = self._run(workload_tasks=3)

        self.assertEqual(self.source.read_bytes(), source_before)
        self.assertEqual(evidence["schema_version"], M1_DUAL_WRITE_SCHEMA_VERSION)
        self.assertEqual(evidence["observation_scope"], "production-copy-disposable-workload")
        self.assertEqual(evidence["workload_task_count"], 3)
        self.assertEqual(evidence["records_compared"], 6)
        self.assertEqual(evidence["expected_transition_pairs"], 6)
        self.assertEqual(evidence["mismatch_count"], 0)
        self.assertEqual(evidence["silent_failure_count"], 0)
        self.assertTrue(evidence["source_quiescent"])
        self.assertFalse(evidence["source_db_mutated_by_runner"])
        self.assertFalse(evidence["production_workload_executed"])
        self.assertTrue(evidence["observation_copy_workload_executed"])
        self.assertTrue((self.output / EVIDENCE_FILENAME).is_file())

        comparison = evidence["comparison"]
        assert isinstance(comparison, dict)
        self.assertTrue(comparison["consistent"])
        for record in comparison["comparisons"]:
            self.assertTrue(record["matched"], record)
        for record in comparison["terminal_postconditions"]:
            self.assertTrue(record["matched"], record)

        report = audit_m1_exit_gate(
            db_path=self.source,
            repo_root=REPO_ROOT,
            evidence_dir=self.output,
        )
        gate = next(
            gate for gate in report["gates"] if gate["gate"] == "dual_write_consistency"
        )
        self.assertEqual(gate["status"], "passed")
        self.assertEqual(report["m1_exit_gate"], "blocked")
        self.assertFalse(report["m2_entry_allowed"])

    def test_missing_legacy_counterpart_is_a_silent_failure(self) -> None:
        evidence = self._run(workload_tasks=1)
        task_key = evidence["workload_task_keys"][0]
        target = Path(evidence["artifacts"]["observation_target"])
        with connect(target) as conn:
            conn.execute(
                """
                DELETE FROM task_events
                WHERE id = (
                    SELECT id FROM task_events
                    WHERE task_key = ? AND event_type = 'status_changed'
                    ORDER BY id LIMIT 1
                )
                """,
                (task_key,),
            )
        comparison = collect_dual_write_observation(
            target,
            task_keys=[task_key],
            claim_reason=evidence["claim_reason_code"],
            release_reason=evidence["release_reason_code"],
        )
        self.assertFalse(comparison["consistent"])
        self.assertEqual(comparison["records_compared"], 1)
        self.assertEqual(comparison["silent_failure_count"], 1)

    def test_actor_source_disagreement_is_a_mismatch(self) -> None:
        evidence = self._run(workload_tasks=1)
        task_key = evidence["workload_task_keys"][0]
        target = Path(evidence["artifacts"]["observation_target"])
        with connect(target) as conn:
            conn.execute(
                """
                UPDATE task_events SET source = 'wrong-owner'
                WHERE id = (
                    SELECT id FROM task_events
                    WHERE task_key = ? AND event_type = 'status_changed'
                    ORDER BY id LIMIT 1
                )
                """,
                (task_key,),
            )
        comparison = collect_dual_write_observation(
            target,
            task_keys=[task_key],
            claim_reason=evidence["claim_reason_code"],
            release_reason=evidence["release_reason_code"],
        )
        self.assertFalse(comparison["consistent"])
        self.assertGreaterEqual(comparison["mismatch_count"], 1)
        preparing = next(
            item for item in comparison["comparisons"] if item["phase"] == "preparing"
        )
        self.assertIn("actor_source_match", preparing["errors"])

    def test_refuses_active_production_snapshot(self) -> None:
        store = TaskMirrorStore(self.source)
        store.upsert_task(
            TaskRecord(
                task_key="AT-M1B-ACTIVE",
                project="test",
                status="queued",
                repo_path=REPO_ROOT,
            ),
            preserve_existing_status=False,
        )
        RuntimeAdmissionStore(self.source).claim(
            "AT-M1B-ACTIVE",
            owner_id="active-owner",
            reason_code="test_active_claim",
        )

        with self.assertRaisesRegex(RuntimeError, "not quiescent"):
            self._run(workload_tasks=1)
        self.assertFalse((self.output / EVIDENCE_FILENAME).exists())

    def test_refuses_nonempty_output_directory(self) -> None:
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(FileExistsError, "must be empty"):
            self._run(workload_tasks=1)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_workload_count_is_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 1 and 100"):
            self._run(workload_tasks=0)


class M1DualWriteObservationCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "state.db"
        migrate_validator_process_lifecycle(self.source)
        self.m1a = self.root / "m1a"
        run_m1_db_copy_rehearsal(
            source_db=self.source,
            output_dir=self.m1a,
            actor="test-operator",
            repo_root=REPO_ROOT,
        )
        self.output = self.root / "m1b"

    def test_cli_runs_under_python_s_and_carries_forward_m1a_evidence(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-S",
                str(SCRIPT),
                "--db-path",
                str(self.source),
                "--output-dir",
                str(self.output),
                "--prior-evidence-dir",
                str(self.m1a),
                "--repo-root",
                str(REPO_ROOT),
                "--actor",
                "test-operator",
                "--workload-tasks",
                "2",
                "--confirm-production-copy-observation",
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
        self.assertEqual(payload["records_compared"], 4)
        self.assertEqual(payload["mismatch_count"], 0)
        self.assertEqual(payload["silent_failure_count"], 0)
        self.assertFalse(payload["production_database_modified"])
        self.assertTrue(Path(payload["evidence_path"]).is_file())
        self.assertTrue(Path(payload["carried_m1a_evidence_path"]).is_file())

        report = audit_m1_exit_gate(
            db_path=self.source,
            repo_root=REPO_ROOT,
            evidence_dir=self.output,
        )
        statuses = {gate["gate"]: gate["status"] for gate in report["gates"]}
        self.assertEqual(statuses["production_db_copy_rehearsal"], "passed")
        self.assertEqual(statuses["dual_write_consistency"], "passed")

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
                "--prior-evidence-dir",
                str(self.m1a),
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
        self.assertIn("--confirm-production-copy-observation", payload["error"])
        self.assertFalse(self.output.exists())

    def test_cli_rejects_nonpassing_m1a_evidence_before_creating_output(self) -> None:
        prior = json.loads(
            (self.m1a / "production-db-copy-rehearsal.json").read_text(
                encoding="utf-8"
            )
        )
        prior["rollback_rehearsal"] = False
        (self.m1a / "production-db-copy-rehearsal.json").write_text(
            json.dumps(prior), encoding="utf-8"
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-S",
                str(SCRIPT),
                "--db-path",
                str(self.source),
                "--output-dir",
                str(self.output),
                "--prior-evidence-dir",
                str(self.m1a),
                "--actor",
                "test-operator",
                "--confirm-production-copy-observation",
            ],
            cwd=REPO_ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(completed.returncode, 1)
        payload = json.loads(completed.stderr)
        self.assertIn("rollback_rehearsal must be true", payload["error"])
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
