from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from agent_taskflow.m1_exit_gate import audit_m1_exit_gate


REPO_ROOT = Path(__file__).resolve().parents[1]
STATUS_DOC = REPO_ROOT / "docs" / "m1-exit-gate-status.md"
SCRIPT = REPO_ROOT / "scripts" / "audit_m1_exit_gate.py"


class M1ExitGateAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "scripts" / "summarize_real_scheduled_execution.py").write_text(
            "# legacy payload fallback\n",
            encoding="utf-8",
        )
        self.db = self.root / "state.db"
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        self._create_current_m1_schema()

    def _create_current_m1_schema(self) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.executescript(
                """
                CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY,
                    task_key TEXT NOT NULL,
                    is_legacy INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE attempts (
                    attempt_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE lifecycle_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    attempt_id TEXT,
                    from_status TEXT,
                    to_status TEXT NOT NULL
                );
                CREATE TABLE lifecycle_allowed_transitions (
                    entity_kind TEXT NOT NULL,
                    from_status TEXT NOT NULL,
                    to_status TEXT NOT NULL
                );
                CREATE TRIGGER lifecycle_attempt_transition_guard
                BEFORE UPDATE OF status ON attempts
                BEGIN
                    SELECT RAISE(ABORT, 'illegal attempt lifecycle transition');
                END;
                CREATE TABLE runtime_controls (
                    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('global', 'task', 'attempt')),
                    scope_id TEXT NOT NULL,
                    mode TEXT NOT NULL
                );
                CREATE TABLE runtime_control_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_kind TEXT NOT NULL,
                    scope_id TEXT NOT NULL
                );
                CREATE TABLE attempt_resources (
                    attempt_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    branch_name TEXT NOT NULL UNIQUE,
                    worktree_path TEXT NOT NULL UNIQUE,
                    artifact_root TEXT NOT NULL UNIQUE
                );
                """
            )

    def _report(self, *, with_evidence: bool = False) -> dict[str, object]:
        return audit_m1_exit_gate(
            db_path=self.db,
            repo_root=self.repo,
            evidence_dir=self.evidence if with_evidence else None,
        )

    @staticmethod
    def _gate(report: dict[str, object], name: str) -> dict[str, object]:
        gates = report["gates"]
        assert isinstance(gates, list)
        for gate in gates:
            assert isinstance(gate, dict)
            if gate["gate"] == name:
                return gate
        raise AssertionError(f"gate not found: {name}")

    def test_current_foundation_does_not_false_close_m1(self) -> None:
        report = self._report()

        self.assertTrue(report["read_only"])
        self.assertEqual(report["m1_exit_gate"], "blocked")
        self.assertFalse(report["m2_entry_allowed"])
        self.assertFalse(report["shadow_mode_ready"])
        self.assertFalse(report["auto_merge_eligible"])
        self.assertEqual(
            self._gate(report, "illegal_transition_rejection")["status"],
            "passed",
        )
        self.assertEqual(
            self._gate(report, "legacy_schema_reader_retained")["status"],
            "passed",
        )
        self.assertEqual(
            self._gate(report, "project_class_kill_switch")["status"],
            "blocked",
        )
        self.assertEqual(
            self._gate(report, "canonical_execution_path")["status"],
            "blocked",
        )

    def test_three_distinct_attempt_resources_and_replay_can_pass(self) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO tasks(task_id, task_key, is_legacy) VALUES ('task-1', 'AT-1', 0)"
            )
            for number, status in ((1, "completed"), (2, "failed"), (3, "waiting_approval")):
                attempt_id = f"attempt-{number}"
                conn.execute(
                    "INSERT INTO attempts(attempt_id, task_id, attempt_number, status) VALUES (?, 'task-1', ?, ?)",
                    (attempt_id, number, status),
                )
                conn.execute(
                    "INSERT INTO lifecycle_events(task_id, attempt_id, from_status, to_status) VALUES ('task-1', ?, NULL, ?)",
                    (attempt_id, status),
                )
                conn.execute(
                    "INSERT INTO attempt_resources(attempt_id, task_id, branch_name, worktree_path, artifact_root) VALUES (?, 'task-1', ?, ?, ?)",
                    (
                        attempt_id,
                        f"attempt/AT-1/{number}",
                        str(self.root / "worktrees" / attempt_id),
                        str(self.root / "artifacts" / attempt_id),
                    ),
                )

        report = self._report()
        self.assertEqual(
            self._gate(report, "three_attempt_artifact_isolation")["status"],
            "passed",
        )
        self.assertEqual(
            self._gate(report, "lifecycle_timeline_replay")["status"],
            "passed",
        )
        self.assertEqual(report["m1_exit_gate"], "blocked")

    def test_valid_external_evidence_is_accepted_but_cannot_hide_schema_blockers(self) -> None:
        evidence_files = {
            "production-db-copy-rehearsal.json": {
                "schema_version": "m1_production_db_copy_rehearsal.v1",
                "migration_dry_run": True,
                "integrity_check": True,
                "rollback_rehearsal": True,
            },
            "dual-write-consistency.json": {
                "schema_version": "m1_dual_write_consistency.v1",
                "observation_window_started_at": "2026-07-12T00:00:00Z",
                "observation_window_ended_at": "2026-07-12T01:00:00Z",
                "records_compared": 5,
                "mismatch_count": 0,
                "silent_failure_count": 0,
            },
            "timeout-abort-cleanup.json": {
                "schema_version": "m1_timeout_abort_cleanup.v1",
                "timeout_pid_cleared": True,
                "timeout_lock_released": True,
                "timeout_worktree_cleanup_verified": True,
                "timeout_verified_exit": True,
                "abort_pid_cleared": True,
                "abort_lock_released": True,
                "abort_worktree_cleanup_verified": True,
                "abort_verified_exit": True,
            },
            "pause-admission-rehearsal.json": {
                "schema_version": "m1_pause_admission_rehearsal.v1",
                "new_pickup_denied": True,
                "existing_attempt_not_suspended": True,
                "pause_cleared": True,
            },
            "canonical-execution-path.json": {
                "schema_version": "m1_canonical_execution_path.v1",
                "canonical_path": "ExecutionEngine",
                "parity_test_passed": False,
                "legacy_level2_rejected": True,
                "merger_requires_canonical_attempt": True,
            },
        }
        for filename, payload in evidence_files.items():
            (self.evidence / filename).write_text(
                json.dumps(payload), encoding="utf-8"
            )

        report = self._report(with_evidence=True)
        self.assertEqual(
            self._gate(report, "production_db_copy_rehearsal")["status"],
            "passed",
        )
        self.assertEqual(
            self._gate(report, "dual_write_consistency")["status"],
            "passed",
        )
        self.assertEqual(
            self._gate(report, "canonical_execution_path")["status"],
            "passed",
        )
        self.assertEqual(
            self._gate(report, "project_class_kill_switch")["status"],
            "blocked",
        )
        self.assertEqual(report["m1_exit_gate"], "blocked")

    def test_invalid_dual_write_evidence_fails_closed(self) -> None:
        (self.evidence / "dual-write-consistency.json").write_text(
            json.dumps(
                {
                    "schema_version": "m1_dual_write_consistency.v1",
                    "observation_window_started_at": "start",
                    "observation_window_ended_at": "end",
                    "records_compared": 10,
                    "mismatch_count": 1,
                    "silent_failure_count": 0,
                }
            ),
            encoding="utf-8",
        )
        report = self._report(with_evidence=True)
        gate = self._gate(report, "dual_write_consistency")
        self.assertEqual(gate["status"], "blocked")
        self.assertIn("mismatch_count", gate["summary"])

    def test_audit_does_not_modify_database(self) -> None:
        before = self.db.read_bytes()
        self._report()
        after = self.db.read_bytes()
        self.assertEqual(after, before)

    def test_cli_runs_from_source_checkout_without_site_packages(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-S",
                str(SCRIPT),
                "--db-path",
                str(self.db),
                "--repo-root",
                str(self.repo),
                "--require-passed",
            ],
            cwd=REPO_ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["m1_exit_gate"], "blocked")
        self.assertTrue(payload["read_only"])


class M1ExitGateDocumentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = STATUS_DOC.read_text(encoding="utf-8")
        cls.normalized = " ".join(cls.text.split())
        cls.lower = cls.normalized.lower()

    def test_document_records_current_status_without_premature_promotion(self) -> None:
        for phrase in (
            "m0_exit_gate = passed",
            "m1_exit_gate = blocked",
            "m2_entry_allowed = false",
            "shadow_mode_ready = false",
            "auto_merge_eligible = false",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.normalized)
        self.assertNotIn("m1_exit_gate = passed", self.lower)
        self.assertNotIn("shadow_mode_ready = true", self.lower)
        self.assertNotIn("auto_merge_eligible = true", self.lower)

    def test_document_preserves_all_roadmap_v2_gates(self) -> None:
        for phrase in (
            "Production DB-copy migration dry-run",
            "Dual-write consistency audit",
            "three non-overwriting Attempts",
            "Timeout/abort clears PID",
            "Lifecycle timeline can be reconstructed",
            "Illegal lifecycle transition is rejected",
            "Pause prevents new pickup",
            "auto-merge eligibility can be disabled immediately",
            "ExecutionEngine parity passes",
            "Legacy schema and reader remain available",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.normalized)


if __name__ == "__main__":
    unittest.main()
