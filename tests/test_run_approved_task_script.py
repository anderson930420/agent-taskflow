from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_approved_task.py"


class RunApprovedTaskScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.worktree_root = self.root / "worktrees"
        self._init_repo()
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _init_repo(self) -> None:
        self._git("init", "-b", "main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.repo / "README.md").write_text("agent-taskflow\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial commit")

    def _add_task(self, task_key: str, *, status: str = "queued") -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title=f"Task {task_key}",
                status=status,
                repo_path=self.repo,
                artifact_dir=self.artifact_root / task_key,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )

    def run_script(self, *extra_args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--repo-path",
                str(self.repo),
                "--db-path",
                str(self.db_path),
                "--artifact-root",
                str(self.artifact_root),
                "--worktree-root",
                str(self.repo / ".worktrees"),
                "--validator",
                "policy",
                *extra_args,
            ],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def test_script_requires_task_key(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("--task-key", result.stdout)

        missing = subprocess.run(
            [sys.executable, str(SCRIPT), "--executor", "noop", "--repo-path", str(self.repo)],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("--task-key", missing.stderr)

    def test_script_requires_executor(self) -> None:
        missing = subprocess.run(
            [sys.executable, str(SCRIPT), "--task-key", "AT-GH-501", "--repo-path", str(self.repo)],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("--executor", missing.stderr)

    def test_script_requires_confirm_flag_for_non_dry_run(self) -> None:
        self._add_task("AT-GH-502")

        result = self.run_script(
            "--task-key",
            "AT-GH-502",
            "--executor",
            "noop",
            "--json",
        )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("--confirm-approved-task", payload["error"])
        self.assertFalse(payload["safety"]["human_approval_confirmed"])

    def test_script_prints_deterministic_json(self) -> None:
        self._add_task("AT-GH-503")

        result = self.run_script(
            "--task-key",
            "AT-GH-503",
            "--executor",
            "noop",
            "--confirm-approved-task",
            "--dry-run",
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["task_key"], "AT-GH-503")
        self.assertEqual(payload["executor"], "noop")
        self.assertEqual(payload["status"], "preview")
        self.assertTrue(payload["safety"]["read_only"])

    def test_script_refuses_non_queued_task(self) -> None:
        self._add_task("AT-GH-504", status="blocked")

        result = self.run_script(
            "--task-key",
            "AT-GH-504",
            "--executor",
            "noop",
            "--confirm-approved-task",
            "--json",
        )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("must be queued", payload["error"])
        self.assertFalse(payload["safety"]["task_status_changed"])

    def test_script_supports_dry_run_without_mutation(self) -> None:
        self._add_task("AT-GH-505")
        before_status = self.store.get_task("AT-GH-505").status
        before_events = len(self.store.list_task_events("AT-GH-505"))
        before_artifacts = len(self.store.list_task_artifacts("AT-GH-505"))

        result = self.run_script(
            "--task-key",
            "AT-GH-505",
            "--executor",
            "noop",
            "--dry-run",
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "preview")
        self.assertEqual(self.store.get_task("AT-GH-505").status, before_status)
        self.assertEqual(len(self.store.list_task_events("AT-GH-505")), before_events)
        self.assertEqual(len(self.store.list_task_artifacts("AT-GH-505")), before_artifacts)
        self.assertIsNone(self.store.get_task_worktree("AT-GH-505"))

    def test_script_and_runner_do_not_reference_recommendation_or_forbidden_helpers(self) -> None:
        text = (SCRIPT.read_text(encoding="utf-8") + "\n" + (REPO_ROOT / "agent_taskflow" / "approved_task_runner.py").read_text(encoding="utf-8")).lower()
        forbidden = [
            "recommend_next_tasks",
            "recommended_next_task",
            "run_recommended",
            "from_recommendation",
            "git push",
            "gh pr create",
            "gh pr merge",
            "merge_pull_request",
            "create_pull_request",
            "push_task_branch",
            "delete_worktree",
            "delete_branch",
            "cleanup(",
        ]
        for item in forbidden:
            self.assertNotIn(item, text)


if __name__ == "__main__":
    unittest.main()
