from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from agent_taskflow.m1_exit_gate_cli import audit_m1_exit_gate


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "audit_m1_exit_gate.py"


class M1LegacyReaderAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "agent_taskflow").mkdir(parents=True)
        (self.repo / "scripts" / "summarize_real_scheduled_execution.py").write_text(
            "# thin CLI wrapper; implementation imported elsewhere\n",
            encoding="utf-8",
        )
        (self.repo / "agent_taskflow" / "real_scheduled_execution_observability.py").write_text(
            """
# Older logs fall back to the legacy tick status field.
# Malformed summaries fall back to the legacy tick payload.
# Legacy ticks remain readable.
""",
            encoding="utf-8",
        )
        self.db = self.root / "state.db"
        self._create_schema()

    def _create_schema(self) -> None:
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

    @staticmethod
    def _legacy_gate(report: dict[str, object]) -> dict[str, object]:
        gates = report["gates"]
        assert isinstance(gates, list)
        for gate in gates:
            assert isinstance(gate, dict)
            if gate.get("gate") == "legacy_schema_reader_retained":
                return gate
        raise AssertionError("legacy gate missing")

    def test_reads_fallback_implementation_module_not_thin_cli(self) -> None:
        report = audit_m1_exit_gate(db_path=self.db, repo_root=self.repo)
        gate = self._legacy_gate(report)

        self.assertEqual(gate["status"], "passed")
        self.assertNotIn("next_action", gate)
        self.assertIn("tasks.is_legacy=true", gate["evidence"])
        self.assertIn("reader_fallback_verified=true", gate["evidence"])
        self.assertEqual(report["gate_status_counts"]["passed"], 2)
        self.assertEqual(report["gate_status_counts"]["blocked"], 5)
        self.assertEqual(report["m1_exit_gate"], "blocked")

    def test_missing_legacy_marker_still_fails_closed(self) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.executescript(
                """
                ALTER TABLE tasks RENAME TO tasks_old;
                CREATE TABLE tasks(task_id TEXT PRIMARY KEY, task_key TEXT NOT NULL);
                DROP TABLE tasks_old;
                """
            )
        report = audit_m1_exit_gate(db_path=self.db, repo_root=self.repo)
        gate = self._legacy_gate(report)
        self.assertEqual(gate["status"], "blocked")
        self.assertIn("tasks.is_legacy", gate["summary"])

    def test_source_checkout_cli_uses_corrected_audit(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-S",
                str(SCRIPT),
                "--db-path",
                str(self.db),
                "--repo-root",
                str(self.repo),
            ],
            cwd=REPO_ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        gate = self._legacy_gate(payload)
        self.assertEqual(gate["status"], "passed")
        self.assertEqual(payload["gate_status_counts"]["passed"], 2)


if __name__ == "__main__":
    unittest.main()
