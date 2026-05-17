from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "prepare_task_workspace.py"


class PrepareTaskWorkspaceScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.base_sha = self._init_git_repo()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.returncode != 0:
            self.fail(f"git {' '.join(args)} failed: {completed.stderr}")
        return completed

    def _init_git_repo(self) -> str:
        self._git(["init"])
        self._git(["config", "user.email", "agent-taskflow@example.invalid"])
        self._git(["config", "user.name", "Agent Taskflow"])
        (self.repo / "README.md").write_text("# test repo\n", encoding="utf-8")
        self._git(["add", "README.md"])
        self._git(["commit", "-m", "initial"])
        self._git(["branch", "-M", "main"])
        return self._git(["rev-parse", "main"]).stdout.strip()

    def _add_task(self, task_key: str = "AT-CLI-001") -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                status="queued",
                repo_path=self.repo,
                artifact_dir=self.root / "artifacts" / task_key,
            )
        )

    def _run_script(
        self,
        task_key: str = "AT-CLI-001",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--task-key",
                task_key,
                "--db-path",
                str(self.db_path),
            ],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_cli_successful_prepare_records_base_sha(self) -> None:
        self._add_task()

        completed = self._run_script()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "prepared")
        self.assertEqual(payload["base_sha"], self.base_sha)
        worktree = self.store.get_task_worktree("AT-CLI-001")
        self.assertIsNotNone(worktree)
        assert worktree is not None
        self.assertEqual(worktree.worktree_path, self.repo / ".worktrees" / "AT-CLI-001")
        self.assertEqual(worktree.branch, "task/AT-CLI-001")
        self.assertEqual(worktree.base_branch, "main")
        self.assertEqual(worktree.base_sha, self.base_sha)
        self.assertEqual(worktree.status, "active")

    def test_cli_missing_task_exits_nonzero(self) -> None:
        completed = self._run_script("AT-MISSING")

        self.assertNotEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("Task not found", payload["summary"])

    def test_cli_blocked_workspace_exits_nonzero(self) -> None:
        self._add_task()
        (self.repo / ".worktrees" / "AT-CLI-001").mkdir(parents=True)

        completed = self._run_script()

        self.assertNotEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("not registered", payload["summary"])
        self.assertIsNone(self.store.get_task_worktree("AT-CLI-001"))


if __name__ == "__main__":
    unittest.main()
