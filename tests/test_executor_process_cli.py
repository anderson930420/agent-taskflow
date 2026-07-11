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


class ExecutorProcessCliTests(unittest.TestCase):
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
                task_key="AT-PR7-CLI",
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
            "scripts/migrate_executor_process_lifecycle.py",
            "--db-path",
            str(self.db_path),
        )
        self.assertTrue(payload["migration_recorded"])
        self.assertEqual(payload["active_executor_processes"], 0)
        self.assertTrue(payload["launch_isolation"]["start_new_session"])
        self.assertTrue(payload["launch_isolation"]["close_fds"])
        self.assertFalse(payload["launch_isolation"]["shell"])
        self.assertFalse(payload["launch_isolation"]["network_isolation"])
        self.assertTrue(payload["termination"]["verified_exit_required"])

    def test_status_cli_reports_empty_active_set(self) -> None:
        self._run(
            "scripts/migrate_executor_process_lifecycle.py",
            "--db-path",
            str(self.db_path),
        )
        payload = self._run(
            "scripts/terminate_executor_process.py",
            "status",
            "--db-path",
            str(self.db_path),
        )
        self.assertEqual(payload["selected_count"], 0)
        self.assertTrue(payload["all_verified_exit"])
        self.assertEqual(payload["results"], [])


if __name__ == "__main__":
    unittest.main()
