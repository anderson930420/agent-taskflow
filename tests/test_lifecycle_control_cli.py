from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]


class LifecycleControlCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        repo = self.root / "repo"
        repo.mkdir()
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        store = TaskMirrorStore(self.db_path)
        store.init_db()
        store.upsert_task(
            TaskRecord(
                task_key="AT-PR6-CLI",
                project="agent-taskflow",
                status="queued",
                repo_path=repo,
                artifact_dir=artifacts,
            )
        )

    def _run(self, *args: str) -> dict[str, object]:
        completed = subprocess.run(
            [sys.executable, "-S", *args],
            cwd=REPO_ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return json.loads(completed.stdout)

    def test_migration_cli_runs_without_site_packages(self) -> None:
        payload = self._run(
            "scripts/migrate_lifecycle_control.py",
            "--db-path",
            str(self.db_path),
        )
        self.assertTrue(payload["migration_recorded"])
        self.assertTrue(payload["transition_guard_installed"])
        self.assertGreater(payload["attempt_transition_count"], 20)
        self.assertEqual(payload["effective_global_mode"], "running")
        self.assertFalse(payload["os_signals_sent"])

    def test_pause_kill_and_clear_cli(self) -> None:
        self._run(
            "scripts/migrate_lifecycle_control.py",
            "--db-path",
            str(self.db_path),
        )
        paused = self._run(
            "scripts/runtime_control.py",
            "pause",
            "--db-path",
            str(self.db_path),
            "--actor",
            "test-operator",
        )
        self.assertEqual(paused["effective_mode"], "paused")
        self.assertEqual(paused["control"]["reason_code"], "operator_pause_requested")

        killed = self._run(
            "scripts/runtime_control.py",
            "kill",
            "--db-path",
            str(self.db_path),
            "--scope-kind",
            "task",
            "--scope-id",
            "AT-PR6-CLI",
            "--actor",
            "test-operator",
        )
        self.assertEqual(killed["effective_mode"], "kill_requested")
        self.assertFalse(killed["os_signals_sent"])

        cleared = self._run(
            "scripts/runtime_control.py",
            "clear",
            "--db-path",
            str(self.db_path),
            "--scope-kind",
            "task",
            "--scope-id",
            "AT-PR6-CLI",
            "--actor",
            "test-operator",
        )
        self.assertEqual(cleared["control"]["mode"], "running")


if __name__ == "__main__":
    unittest.main()
