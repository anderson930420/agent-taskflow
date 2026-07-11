from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.models import TaskRecord
from agent_taskflow.reset_lineage_schema import migrate_reset_lineage
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]


class ResetLineageCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.artifacts = self.root / "artifacts" / "AT-PR8-CLI"
        self.artifacts.mkdir(parents=True)
        self.task_key = "AT-PR8-CLI"
        store = TaskMirrorStore(self.db_path)
        store.init_db()
        store.upsert_task(
            TaskRecord(
                task_key=self.task_key,
                project="agent-taskflow",
                status="blocked",
                repo_path=self.repo,
                artifact_dir=self.artifacts,
                blocked_reason="retry required",
            )
        )
        migrate_reset_lineage(self.db_path)
        attempts = AttemptStore(self.db_path)
        old = attempts.create_attempt(self.task_key, status="created")
        attempts.close_attempt(
            old.attempt_id,
            status="blocked",
            reason_code="runtime_governance_blocked",
            actor="test",
            execution_result="blocked",
        )
        self.old_attempt_id = old.attempt_id
        store.update_task_status(
            self.task_key,
            "blocked",
            blocked_reason="retry required",
        )

    def test_migration_cli_runs_without_site_packages(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-S",
                "scripts/migrate_reset_lineage.py",
                "--db-path",
                str(self.db_path),
            ],
            cwd=REPO_ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["migration_recorded"])
        self.assertTrue(payload["compare_and_set"]["one_winner"])
        self.assertTrue(payload["runtime_claim"]["reserved_attempt_adoption"])
        self.assertFalse(payload["runtime_claim"]["second_retry_identity_created"])

    def test_reset_cli_runs_without_site_packages_and_reports_binding(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-S",
                "scripts/reset_task_status.py",
                "--task-key",
                self.task_key,
                "--db-path",
                str(self.db_path),
                "--from-status",
                "blocked",
                "--reason",
                "operator retry",
                "--request-id",
                "cli-request-one",
                "--expected-reset-generation",
                "0",
                "--expected-old-attempt-id",
                self.old_attempt_id,
                "--actor",
                "cli-test",
                "--confirm-reset",
            ],
            cwd=REPO_ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["mutated"])
        self.assertEqual(payload["old_attempt_id"], self.old_attempt_id)
        self.assertTrue(payload["new_attempt_id"].startswith("attempt-"))
        self.assertEqual(payload["committed_reset_generation"], 1)
        self.assertFalse(payload["idempotent_replay"])
        self.assertIsNotNone(payload["audit_artifact_path"])


if __name__ == "__main__":
    unittest.main()
