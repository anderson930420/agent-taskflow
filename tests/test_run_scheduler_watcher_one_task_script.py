"""Tests for scripts/run_scheduler_watcher_one_task.py."""

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
SCRIPT = REPO_ROOT / "scripts" / "run_scheduler_watcher_one_task.py"


class RunSchedulerWatcherOneTaskScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def seed_task(self, task_key: str, *, status: str = "queued") -> None:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title=f"Watcher one-task CLI {task_key}",
                status=status,
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--db-path",
                str(self.db_path),
                "--artifact-root",
                str(self.artifact_root),
                *args,
            ],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def test_script_help(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in (
            "--db-path",
            "--artifact-root",
            "--limit",
            "--task-key",
            "--select-first-candidate",
            "--confirm-select-first-candidate",
            "--confirm-run-watcher-one-task",
            "--confirm-run-one-shot-pipeline",
            "--confirm-prepare-pr",
            "--confirm-github-mutations",
            "--confirm-branch-push",
            "--confirm-draft-pr",
            "--resume-existing",
            "--resume-pr-preparation",
        ):
            self.assertIn(flag, result.stdout, flag)
        for forbidden in (
            "--background",
            "--daemon",
            "--cron",
            "--webhook",
            "--poll",
            "--batch-size",
            "--approve",
            "--merge",
            "--cleanup",
            "--delete-branch",
            "--delete-worktree",
        ):
            self.assertNotIn(forbidden, result.stdout, forbidden)

    def test_script_dry_run_preview(self) -> None:
        self.seed_task("AT-L8B-CLI")

        result = self.run_script("--pretty")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "dry_run")
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(payload["preview"]["candidate_count"], 1)
        self.assertTrue(payload["safety"]["preview_only"])
        self.assertFalse(payload["safety"]["task_to_draft_pr_pipeline_called"])

    def test_script_confirmed_requires_selection(self) -> None:
        self.seed_task("AT-L8B-CLI")

        result = self.run_script(
            "--confirm-run-watcher-one-task",
            "--confirm-run-one-shot-pipeline",
            "--confirm-prepare-pr",
            "--confirm-github-mutations",
            "--confirm-branch-push",
            "--confirm-draft-pr",
            "--json",
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failed_stage"], "selection")
        self.assertIn("selection_required", payload["reasons"])

    def test_script_has_selection_flags(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("--task-key", source)
        self.assertIn("--select-first-candidate", source)
        self.assertIn("--confirm-select-first-candidate", source)
        self.assertIn("--confirm-run-watcher-one-task", source)

    def test_source_has_no_forbidden_calls(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        forbidden = (
            "while True",
            "schedule.every",
            "asyncio.sleep",
            "threading.Thread",
            "Thread(",
            "subprocess.run",
            "from agent_taskflow.github_issue",
            "ingest_github",
            "discover_github",
            "from agent_taskflow.dispatcher",
            "from agent_taskflow.api",
            "from agent_taskflow.local_cleanup_confirm",
            "from agent_taskflow.remote_branch_cleanup_confirm",
            "from agent_taskflow.task_closeout_confirm",
            "merge_pull_request",
            "record_approval_decision(",
            "delete_worktree",
            "git push",
            "gh pr create",
            "--background",
            "--daemon",
            "--cron",
            "--webhook",
            "--poll",
            "--batch-size",
        )
        for needle in forbidden:
            self.assertNotIn(needle, source, needle)


if __name__ == "__main__":
    unittest.main()
