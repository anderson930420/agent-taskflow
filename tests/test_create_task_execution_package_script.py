from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.task_execution_package import (
    EVENT_TYPE,
    IMPLEMENTATION_PROMPT_FILENAME,
    PACKAGE_FILENAME,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "create_task_execution_package.py"


class CreateTaskExecutionPackageScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.artifact_root = self.root / "artifacts"
        self.artifact_dir = self.artifact_root / "AT-EXEC-CLI-1"
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_task(self, *, task_key: str = "AT-EXEC-CLI-1", status: str = "queued") -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="CLI test task",
                status=status,
                repo_path=self.repo,
                artifact_dir=self.artifact_dir,
            )
        )

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    # 1. default/dry-run mode writes nothing
    def test_default_dry_run_writes_nothing(self) -> None:
        self._seed_task()
        completed = self._run(
            [
                "--task-key", "AT-EXEC-CLI-1",
                "--db-path", str(self.db_path),
                "--artifact-root", str(self.artifact_root),
                "--dry-run",
            ]
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "dry_run")
        self.assertFalse(payload["safety"]["db_written"])
        self.assertFalse(payload["safety"]["artifact_written"])
        self.assertFalse((self.artifact_dir / IMPLEMENTATION_PROMPT_FILENAME).exists())
        self.assertFalse((self.artifact_dir / PACKAGE_FILENAME).exists())
        events = [
            event for event in self.store.list_task_events("AT-EXEC-CLI-1")
            if event.event_type == EVENT_TYPE
        ]
        self.assertEqual(events, [])

    # 2. missing task returns non-zero and structured blocked JSON
    def test_missing_task_returns_nonzero_blocked_json(self) -> None:
        completed = self._run(
            [
                "--task-key", "AT-DOES-NOT-EXIST",
                "--db-path", str(self.db_path),
                "--artifact-root", str(self.artifact_root),
                "--dry-run",
            ]
        )
        self.assertEqual(completed.returncode, 1, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("Task not found", payload["error"])
        self.assertFalse(payload["safety"]["db_written"])

    # 3. confirmed mode writes package artifacts and returns ok
    def test_confirmed_mode_writes_package_artifacts(self) -> None:
        self._seed_task()
        completed = self._run(
            [
                "--task-key", "AT-EXEC-CLI-1",
                "--db-path", str(self.db_path),
                "--artifact-root", str(self.artifact_root),
                "--confirm-create-package",
            ]
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "confirmed")
        prompt_path = self.artifact_dir / IMPLEMENTATION_PROMPT_FILENAME
        package_path = self.artifact_dir / PACKAGE_FILENAME
        self.assertTrue(prompt_path.exists())
        self.assertTrue(package_path.exists())
        self.assertEqual(payload["implementation_prompt_path"], str(prompt_path))
        self.assertEqual(payload["package_path"], str(package_path))
        self.assertTrue(payload["safety"]["execution_package_created"])
        self.assertTrue(payload["safety"]["implementation_prompt_created"])
        events = [
            event for event in self.store.list_task_events("AT-EXEC-CLI-1")
            if event.event_type == EVENT_TYPE
        ]
        self.assertEqual(len(events), 1)

    # 4. --dry-run and --confirm-create-package conflict if both are supplied
    def test_dry_run_and_confirm_conflict(self) -> None:
        self._seed_task()
        completed = self._run(
            [
                "--task-key", "AT-EXEC-CLI-1",
                "--db-path", str(self.db_path),
                "--artifact-root", str(self.artifact_root),
                "--dry-run",
                "--confirm-create-package",
            ]
        )
        self.assertNotEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("mutually exclusive", payload["error"])
        self.assertFalse((self.artifact_dir / IMPLEMENTATION_PROMPT_FILENAME).exists())
        self.assertFalse((self.artifact_dir / PACKAGE_FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
