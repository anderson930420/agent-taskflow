"""Tests for scripts/push_task_branch.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "push_task_branch.py"
MODULE = REPO_ROOT / "agent_taskflow" / "branch_push.py"


class PushTaskBranchScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.db_path = self.root / "state.db"
        self.artifact_dir = self.root / "artifacts" / "AT-PUSH-CLI"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self._create_git_fixture()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=self.repo,
            shell=False,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _create_git_fixture(self) -> None:
        self.repo.mkdir(parents=True)
        subprocess.run(
            ["git", "init"],
            cwd=self.repo,
            shell=False,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._git("config", "user.email", "operator@example.com")
        self._git("config", "user.name", "Operator")
        (self.repo / "README.md").write_text("initial\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")
        self._git("branch", "-m", "main")
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            shell=False,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()
        self._git("switch", "-c", "task/AT-PUSH-CLI")
        (self.repo / "feature.txt").write_text("feature\n", encoding="utf-8")
        self._git("add", "feature.txt")
        self._git("commit", "-m", "feature")

        self.artifact_dir.mkdir(parents=True)
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-PUSH-CLI",
                project="agent-taskflow",
                board="agent-taskflow",
                title="Branch push CLI test",
                status="waiting_approval",
                repo_path=self.repo,
                artifact_dir=self.artifact_dir,
            )
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key="AT-PUSH-CLI",
                repo_path=self.repo,
                worktree_path=self.repo,
                branch="task/AT-PUSH-CLI",
                base_branch="main",
                base_sha=base_sha,
                status="active",
            )
        )

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_help_succeeds(self) -> None:
        result = self._run("--help")

        self.assertEqual(result.returncode, 0)
        self.assertIn("--confirm-push", result.stdout)

    def test_cli_dry_run_succeeds_with_fixture(self) -> None:
        result = self._run(
            "--task-key",
            "AT-PUSH-CLI",
            "--db-path",
            str(self.db_path),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse(payload["pushed"])
        self.assertFalse(payload["github_mutated"])
        self.assertEqual(payload["ahead_count"], 1)
        self.assertIn("git push --set-upstream origin task/AT-PUSH-CLI", payload["command_preview"])

    def test_cli_missing_task_exits_nonzero_with_json(self) -> None:
        result = self._run(
            "--task-key",
            "AT-MISSING",
            "--db-path",
            str(self.db_path),
        )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("Task not found", payload["summary"])

    def test_cli_dirty_worktree_exits_nonzero_with_json(self) -> None:
        (self.repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

        result = self._run(
            "--task-key",
            "AT-PUSH-CLI",
            "--db-path",
            str(self.db_path),
        )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("worktree has uncommitted changes", payload["summary"])

    def test_static_safety_forbidden_commands_are_absent(self) -> None:
        text = MODULE.read_text(encoding="utf-8") + "\n" + SCRIPT.read_text(encoding="utf-8")
        forbidden = [
            "git push --force",
            "git push -f",
            "gh pr create",
            "gh pr merge",
            "gh pr review --approve",
            "gh issue edit",
            "git merge",
            "git rebase",
            "git branch -d",
            "git branch -D",
            "git worktree remove",
            "git reset --hard",
            "git add",
            "git commit",
            "git stash",
            "shell=True",
            "cleanup automation",
            "webhook",
            "polling",
        ]
        for token in forbidden:
            self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
