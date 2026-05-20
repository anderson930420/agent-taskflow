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
    IMPLEMENTATION_PROMPT_FILENAME,
    PACKAGE_FILENAME,
    TaskExecutionPackageRequest,
    create_task_execution_package,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_queued_task_handoff.py"


class RunQueuedTaskHandoffScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.artifact_root = self.root / "artifacts"
        self.artifact_dir = self.artifact_root / "AT-HANDOFF-CLI-1"
        self.worktree_root = self.root / "worktrees"
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_task(self) -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-HANDOFF-CLI-1",
                project="agent-taskflow",
                board="agent-taskflow",
                title="CLI handoff test",
                status="queued",
                repo_path=self.repo,
                artifact_dir=self.artifact_dir,
            )
        )

    def _create_valid_package(self) -> None:
        create_task_execution_package(
            TaskExecutionPackageRequest(
                task_key="AT-HANDOFF-CLI-1",
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                dry_run=False,
                confirm=True,
            ),
            store=self.store,
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

    def _base_args(self) -> list[str]:
        return [
            "--task-key", "AT-HANDOFF-CLI-1",
            "--executor", "shell",
            "--repo-path", str(self.repo),
            "--db-path", str(self.db_path),
            "--artifact-root", str(self.artifact_root),
            "--worktree-root", str(self.worktree_root),
            "--base-branch", "main",
            "--validator", "pytest",
            "--skip-preflight",
        ]

    # 1. Default dry-run verifies package and writes/runs nothing.
    def test_default_dry_run_verifies_without_running(self) -> None:
        self._seed_task()
        self._create_valid_package()
        completed = self._run(self._base_args() + ["--dry-run"])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "preview")
        self.assertTrue(payload["package"]["verified"])
        self.assertFalse(payload["safety"]["approved_task_runner_started"])
        self.assertFalse(payload["safety"]["workspace_prepared"])
        self.assertFalse(payload["safety"]["executor_started"])
        # The prompt and package files exist (the package writer placed them).
        self.assertTrue((self.artifact_dir / IMPLEMENTATION_PROMPT_FILENAME).exists())
        self.assertTrue((self.artifact_dir / PACKAGE_FILENAME).exists())
        # No worktree was created.
        self.assertFalse(self.worktree_root.exists())

    # 2. Missing task returns non-zero structured blocked JSON.
    def test_missing_task_returns_nonzero_blocked_json(self) -> None:
        completed = self._run(self._base_args() + ["--dry-run"])
        self.assertEqual(completed.returncode, 1, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["phase"], "selection")
        self.assertIn("Task not found", payload["error"])
        self.assertFalse(payload["safety"]["approved_task_runner_started"])

    # 3. Missing package returns non-zero structured blocked JSON.
    def test_missing_package_returns_nonzero_blocked_json(self) -> None:
        self._seed_task()
        completed = self._run(self._base_args() + ["--dry-run"])
        self.assertEqual(completed.returncode, 1, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["phase"], "package_verification")
        self.assertIn("Task execution package is missing", payload["error"])

    # 4. --dry-run and --confirm-handoff conflict.
    def test_dry_run_and_confirm_handoff_conflict(self) -> None:
        self._seed_task()
        self._create_valid_package()
        completed = self._run(self._base_args() + ["--dry-run", "--confirm-handoff"])
        self.assertNotEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["phase"], "cli")
        self.assertIn("mutually exclusive", payload["error"])
        # No worktree was created by this conflicting CLI call.
        self.assertFalse(self.worktree_root.exists())

    # 5. CLI emits well-formed JSON in dry-run path.
    def test_default_dry_run_emits_pretty_json_by_default(self) -> None:
        self._seed_task()
        self._create_valid_package()
        completed = self._run(self._base_args() + ["--dry-run"])
        self.assertEqual(completed.returncode, 0)
        # Pretty-printed by default: stdout contains newlines.
        self.assertIn("\n", completed.stdout.strip())
        # Compact mode produces single-line JSON.
        compact = self._run(self._base_args() + ["--dry-run", "--json"])
        self.assertEqual(compact.returncode, 0)
        self.assertEqual(compact.stdout.strip().count("\n"), 0)


if __name__ == "__main__":
    unittest.main()
