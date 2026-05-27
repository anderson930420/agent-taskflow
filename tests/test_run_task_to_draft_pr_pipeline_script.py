"""Tests for scripts/run_task_to_draft_pr_pipeline.py."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_task_to_draft_pr_pipeline.py"


def _seed_queued_task(workspace: Path, task_key: str = "AT-L7D-CLI-TEST") -> dict:
    db_path = workspace / "state.db"
    repo_path = workspace / "repo"
    artifact_root = workspace / "artifacts"
    repo_path.mkdir(parents=True, exist_ok=True)
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_dir = artifact_root / task_key
    artifact_dir.mkdir(parents=True, exist_ok=True)

    store = TaskMirrorStore(db_path)
    store.init_db()
    store.upsert_task(
        TaskRecord(
            task_key=task_key,
            project="agent-taskflow",
            board="agent-taskflow",
            title="L7D CLI test task",
            status="queued",
            repo_path=repo_path,
            artifact_dir=artifact_dir,
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-01T00:00:00Z",
        )
    )
    return {
        "db_path": db_path,
        "artifact_root": artifact_root,
        "task_key": task_key,
    }


class RunTaskToDraftPRPipelineScriptTests(unittest.TestCase):
    def test_script_help(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--task-key", result.stdout)
        self.assertIn("--db-path", result.stdout)
        self.assertIn("--artifact-root", result.stdout)
        self.assertIn("--recommended-command-kind", result.stdout)
        self.assertIn("--proposal-max-items", result.stdout)
        self.assertIn("--resume-existing", result.stdout)
        self.assertIn("--resume-pr-preparation", result.stdout)
        self.assertIn("--confirm-run-one-shot-pipeline", result.stdout)
        self.assertIn("--confirm-prepare-pr", result.stdout)
        self.assertIn("--confirm-github-mutations", result.stdout)
        self.assertIn("--confirm-branch-push", result.stdout)
        self.assertIn("--confirm-draft-pr", result.stdout)

    def test_script_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seeded = _seed_queued_task(Path(tmp))
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--task-key",
                    str(seeded["task_key"]),
                    "--db-path",
                    str(seeded["db_path"]),
                    "--artifact-root",
                    str(seeded["artifact_root"]),
                ],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                env={"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "dry_run")
            self.assertEqual(payload["mode"], "dry_run")
            self.assertTrue(payload["would_run_task_to_draft_pr"])
            self.assertTrue(payload["safety"]["dry_run"])
            self.assertFalse(payload["safety"]["approved_task_runner_called"])
            self.assertFalse(payload["safety"]["github_mutated"])
            self.assertFalse(payload["safety"]["branch_pushed"])
            self.assertFalse(payload["safety"]["draft_pr_created"])

            with sqlite3.connect(Path(str(seeded["db_path"]))) as conn:
                artifact_count = conn.execute(
                    "SELECT COUNT(*) FROM task_artifacts"
                ).fetchone()[0]
                event_count = conn.execute(
                    "SELECT COUNT(*) FROM task_events"
                ).fetchone()[0]
            self.assertEqual(artifact_count, 0)
            self.assertEqual(event_count, 0)

    def test_script_requires_all_confirm_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seeded = _seed_queued_task(Path(tmp), task_key="AT-L7D-CLI-FLAGS")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--task-key",
                    str(seeded["task_key"]),
                    "--db-path",
                    str(seeded["db_path"]),
                    "--artifact-root",
                    str(seeded["artifact_root"]),
                    "--confirm-run-one-shot-pipeline",
                ],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                env={"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(result.returncode, 1, result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["failed_stage"], "pr_preparation")
            self.assertIn("--confirm-prepare-pr", payload["reasons"][0])
            self.assertIn("--confirm-github-mutations", payload["reasons"][0])
            self.assertIn("--confirm-branch-push", payload["reasons"][0])
            self.assertIn("--confirm-draft-pr", payload["reasons"][0])
            self.assertFalse(payload["safety"]["approved_task_runner_called"])
            self.assertFalse(payload["safety"]["github_mutated"])

            with sqlite3.connect(Path(str(seeded["db_path"]))) as conn:
                artifact_count = conn.execute(
                    "SELECT COUNT(*) FROM task_artifacts"
                ).fetchone()[0]
                event_count = conn.execute(
                    "SELECT COUNT(*) FROM task_events"
                ).fetchone()[0]
            self.assertEqual(artifact_count, 0)
            self.assertEqual(event_count, 0)

    def test_source_has_no_forbidden_calls(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        forbidden = (
            "from agent_taskflow.approved_task_runner",
            "from agent_taskflow.executors",
            "from agent_taskflow.validators",
            "from agent_taskflow.api",
            "from agent_taskflow.branch_push",
            "from agent_taskflow.draft_pr",
            "local_cleanup_confirm",
            "remote_branch_cleanup_confirm",
            "task_closeout_confirm",
            "while True",
            "threading.Thread",
            "asyncio.sleep",
            "schedule.every",
            "subprocess.run",
            "gh pr create",
            "git push",
        )
        for needle in forbidden:
            self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
