"""Tests for create_pi_smoke_task.py.

These tests do NOT call real pi or real MiniMax.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "create_pi_smoke_task.py"


class TestCreatePiSmokeTask(unittest.TestCase):
    """Test create_pi_smoke_task.py CLI helper."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="smoke_test_"))
        self.db_path = self.tmpdir / "state.db"
        self.repo_path = self.tmpdir / "repo"
        self.artifact_root = self.tmpdir / "artifacts"
        self.task_key = "AT-PI-SMOKE-TEST"

        # Create the repo dir with .worktrees subdir
        self.repo_path.mkdir(parents=True)
        (self.repo_path / ".worktrees").mkdir()
        self.artifact_root.mkdir(parents=True)

    def _run(self, *extra_args):
        """Run the helper script; return (exit_code, stdout, stderr)."""
        import subprocess

        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        cmd = [
            "python",
            str(SCRIPT),
            "--task-key",
            self.task_key,
            "--db-path",
            str(self.db_path),
            "--repo-path",
            str(self.repo_path),
            "--artifact-root",
            str(self.artifact_root),
            *extra_args,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        return result.returncode, result.stdout, result.stderr

    # ------------------------------------------------------------------
    # --help
    # ------------------------------------------------------------------
    def test_help_flag_succeeds(self):
        """--help must exit 0 and show required flags."""
        import subprocess

        result = subprocess.run(
            ["python", str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--task-key", result.stdout)
        self.assertIn("--db-path", result.stdout)
        self.assertIn("--repo-path", result.stdout)
        self.assertIn("--artifact-root", result.stdout)

    # ------------------------------------------------------------------
    # Path safety
    # ------------------------------------------------------------------
    def test_relative_db_path_rejected(self):
        """Relative db-path must be rejected with a non-zero exit."""
        import subprocess

        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        result = subprocess.run(
            [
                "python", str(SCRIPT),
                "--task-key", self.task_key,
                "--db-path", "relative_db.db",
                "--repo-path", str(self.repo_path),
                "--artifact-root", str(self.artifact_root),
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be absolute", result.stderr)

    def test_relative_repo_path_rejected(self):
        """Relative repo-path must be rejected with a non-zero exit."""
        import subprocess

        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        result = subprocess.run(
            [
                "python", str(SCRIPT),
                "--task-key", self.task_key,
                "--db-path", str(self.db_path),
                "--repo-path", "relative_repo",
                "--artifact-root", str(self.artifact_root),
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be absolute", result.stderr)

    def test_relative_artifact_root_rejected(self):
        """Relative artifact-root must be rejected with a non-zero exit."""
        import subprocess

        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        result = subprocess.run(
            [
                "python", str(SCRIPT),
                "--task-key", self.task_key,
                "--db-path", str(self.db_path),
                "--repo-path", str(self.repo_path),
                "--artifact-root", "relative_artifacts",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be absolute", result.stderr)

    # ------------------------------------------------------------------
    # Full integration run
    # ------------------------------------------------------------------
    def test_full_flow_creates_task_worktree_and_prompt(self):
        """Full run: creates worktree dir, artifact dir, prompt, TaskRecord, TaskWorktreeRecord."""
        exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 0, stderr)

        # Check JSON output
        summary = json.loads(stdout)
        self.assertEqual(summary["task_key"], self.task_key)
        self.assertEqual(summary["executor"], "pi")
        self.assertEqual(summary["provider"], "minimax")
        self.assertEqual(summary["model"], "MiniMax-M2.7")
        self.assertEqual(summary["tools"], ["read", "write", "grep", "find", "ls"])
        self.assertEqual(summary["pi_bin"], "pi")

        # Check next_dispatch_command
        self.assertIn("run_dispatcher.py", summary["next_dispatch_command"])
        self.assertIn(self.task_key, summary["next_dispatch_command"])
        self.assertIn("--db-path", summary["next_dispatch_command"])

        # Check directories created
        worktree_path = Path(summary["worktree_path"])
        artifact_dir = Path(summary["artifact_dir"])
        prompt_path = Path(summary["prompt_path"])
        self.assertTrue(worktree_path.exists())
        self.assertTrue(artifact_dir.exists())
        self.assertTrue(prompt_path.exists())

        # Check prompt content
        content = prompt_path.read_text()
        self.assertIn("pi-real-run-smoke-ok", content)

        # Check TaskRecord in DB
        store = TaskMirrorStore(self.db_path)
        task = store.get_task(self.task_key)
        self.assertIsNotNone(task)
        self.assertEqual(task.executor, "pi")
        self.assertEqual(task.provider, "minimax")
        self.assertEqual(task.model, "MiniMax-M2.7")
        self.assertEqual(task.tools, ["read", "write", "grep", "find", "ls"])
        self.assertEqual(task.pi_bin, "pi")
        self.assertEqual(task.status, "queued")
        self.assertEqual(task.project, "agent-taskflow")

        # Check TaskWorktreeRecord in DB
        worktree = store.get_task_worktree(self.task_key)
        self.assertIsNotNone(worktree)
        self.assertEqual(worktree.branch, f"smoke/{self.task_key}")
        self.assertEqual(worktree.base_branch, "main")
        self.assertEqual(worktree.status, "active")

    def test_existing_prompt_preserved_without_overwrite_flag(self):
        """Existing prompt must be preserved unless --overwrite-prompt is set."""
        # First run creates prompt
        exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 0, stderr)
        summary = json.loads(stdout)
        prompt_path = Path(summary["prompt_path"])
        original_content = "CUSTOM CONTENT HERE\n"
        prompt_path.write_text(original_content, encoding="utf-8")

        # Second run without --overwrite-prompt must NOT change the prompt
        exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(prompt_path.read_text(), original_content)

    def test_overwrite_prompt_flag_replaces_existing_prompt(self):
        """--overwrite-prompt must replace existing prompt."""
        # First run creates prompt
        exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 0, stderr)
        summary = json.loads(stdout)
        prompt_path = Path(summary["prompt_path"])
        original_content = "CUSTOM CONTENT\n"
        prompt_path.write_text(original_content, encoding="utf-8")

        # Second run with --overwrite-prompt must replace the prompt
        exit_code, stdout, stderr = self._run("--overwrite-prompt")
        self.assertEqual(exit_code, 0, stderr)
        new_content = prompt_path.read_text()
        self.assertNotEqual(new_content, original_content)
        self.assertIn("pi-real-run-smoke-ok", new_content)

    def test_custom_flags_stored_in_db_and_json(self):
        """Custom --provider, --model, --pi-bin, --tools, --prompt-text are stored."""
        custom_prompt = "Custom smoke prompt"
        exit_code, stdout, stderr = self._run(
            "--provider", "openai",
            "--model", "gpt-4",
            "--pi-bin", "/usr/local/bin/pi",
            "--tools", "read,write,bash",
            "--prompt-text", custom_prompt,
        )
        self.assertEqual(exit_code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["provider"], "openai")
        self.assertEqual(summary["model"], "gpt-4")
        self.assertEqual(summary["pi_bin"], "/usr/local/bin/pi")
        self.assertEqual(summary["tools"], ["read", "write", "bash"])
        self.assertEqual(Path(summary["prompt_path"]).read_text(), custom_prompt)

        # Verify in DB
        store = TaskMirrorStore(self.db_path)
        task = store.get_task(self.task_key)
        self.assertEqual(task.provider, "openai")
        self.assertEqual(task.model, "gpt-4")
        self.assertEqual(task.pi_bin, "/usr/local/bin/pi")
        self.assertEqual(task.tools, ["read", "write", "bash"])

    def test_next_dispatch_command_format(self):
        """next_dispatch_command must be a valid dispatcher invocation."""
        exit_code, stdout, stderr = self._run()
        self.assertEqual(exit_code, 0, stderr)
        summary = json.loads(stdout)
        cmd = summary["next_dispatch_command"]
        self.assertTrue(cmd.startswith("python "))
        self.assertIn("run_dispatcher.py", cmd)
        self.assertIn(f"--task-key {self.task_key}", cmd)
        self.assertIn(f"--db-path {self.db_path}", cmd)

    def test_script_does_not_call_subprocess_or_pi_agent(self):
        """Script must not import subprocess or pi_agent at runtime."""
        script_text = SCRIPT.read_text()
        # These patterns must not appear as runtime calls (only in comments/docs are fine)
        self.assertNotIn("subprocess.run", script_text)
        self.assertNotIn("subprocess.Popen", script_text)
        self.assertNotIn("pi_agent", script_text)
        # MINIMAX_API_KEY must not be read in the script
        self.assertNotIn("MINIMAX_API_KEY", script_text)
        self.assertNotIn("getenv", script_text)


if __name__ == "__main__":
    unittest.main()