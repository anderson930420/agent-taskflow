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
SCRIPT = REPO_ROOT / "scripts" / "run_scheduler_watcher_preview.py"


class RunSchedulerWatcherPreviewScriptTests(unittest.TestCase):
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
                title=f"Watcher CLI {task_key}",
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
        self.assertIn("--db-path", result.stdout)
        self.assertIn("--limit", result.stdout)
        self.assertIn("--include-waiting-approval", result.stdout)
        self.assertNotIn("--confirm-run-one-shot-pipeline", result.stdout)
        self.assertNotIn("--confirm-draft-pr", result.stdout)

    def test_script_preview_outputs_json(self) -> None:
        self.seed_task("AT-L8A-CLI")

        result = self.run_script("--pretty")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "dry_run_preview")
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["candidates"][0]["task_key"], "AT-L8A-CLI")
        self.assertTrue(payload["safety"]["read_only"])
        self.assertFalse(payload["safety"]["task_execution_started"])
        self.assertFalse(payload["safety"]["github_mutated"])

    def test_script_source_has_no_forbidden_calls(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        forbidden = (
            "from agent_taskflow.one_shot_task_pipeline",
            "from agent_taskflow.task_to_draft_pr_pipeline",
            "from agent_taskflow.approved_task_runner",
            "from agent_taskflow.executors",
            "from agent_taskflow.validators",
            "from agent_taskflow.branch_push",
            "from agent_taskflow.draft_pr",
            "run_one_shot_task_pipeline(",
            "run_task_to_draft_pr_pipeline(",
            "approved_task_runner",
            "--confirm-run-one-shot-pipeline",
            "--confirm-prepare-pr",
            "--confirm-github-mutations",
            "--confirm-branch-push",
            "--confirm-draft-pr",
            "subprocess.run",
            "while True",
            "threading.Thread",
            "asyncio.sleep",
            "schedule.every",
            "cron",
            "webhook",
            "poll",
            "gh pr create",
            "git push",
        )
        for needle in forbidden:
            self.assertNotIn(needle, source, needle)

    def test_invalid_limit_fails(self) -> None:
        result = self.run_script("--limit", "-1")

        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "error")
        self.assertIn("limit must be zero or positive", payload["error"])
        self.assertTrue(payload["safety"]["read_only"])


if __name__ == "__main__":
    unittest.main()
