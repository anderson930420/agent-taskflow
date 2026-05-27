"""Tests for the run_one_shot_task_pipeline.py CLI."""

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
SCRIPT = REPO_ROOT / "scripts" / "run_one_shot_task_pipeline.py"


def _seed_queued_task(workspace: Path, task_key: str = "AT-L7A-CLI-TEST") -> dict:
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
            title="L7A CLI test task",
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


class RunOneShotTaskPipelineScriptTests(unittest.TestCase):
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
        self.assertIn("--confirm-run-one-shot-pipeline", result.stdout)
        self.assertIn("--resume-existing", result.stdout)
        self.assertIn("--recommended-command-kind", result.stdout)
        self.assertIn("--proposal-max-items", result.stdout)
        self.assertNotIn("--allow-runtime-rerun", result.stdout)

    def test_script_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            seeded = _seed_queued_task(workspace)
            db_path = seeded["db_path"]
            artifact_root = seeded["artifact_root"]
            task_key = seeded["task_key"]

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--task-key",
                    task_key,
                    "--db-path",
                    str(db_path),
                    "--artifact-root",
                    str(artifact_root),
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
            self.assertEqual(payload["task_key"], task_key)
            self.assertTrue(payload["would_run_pipeline"])
            safety = payload.get("safety") or {}
            self.assertTrue(safety.get("dry_run"))
            self.assertFalse(safety.get("approved_task_runner_called"))
            self.assertFalse(safety.get("scheduler_loop_started"))
            self.assertFalse(safety.get("background_worker_started"))
            self.assertFalse(safety.get("automatic_task_picking_started"))

            with sqlite3.connect(db_path) as conn:
                artifact_count = conn.execute(
                    "SELECT COUNT(*) FROM task_artifacts"
                ).fetchone()[0]
                event_count = conn.execute(
                    "SELECT COUNT(*) FROM task_events"
                ).fetchone()[0]
            self.assertEqual(artifact_count, 0)
            self.assertEqual(event_count, 0)

    def test_script_dry_run_missing_task_returns_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            seeded = _seed_queued_task(workspace)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--task-key",
                    "AT-DOES-NOT-EXIST",
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
            self.assertEqual(payload["failed_stage"], "proposal")
            self.assertIn("task_missing", payload["reasons"])

    def test_source_has_no_forbidden_calls(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        forbidden_substrings = (
            "while True",
            "schedule.every",
            "asyncio.sleep",
            "threading.Thread",
            "from agent_taskflow.executors",
            "from agent_taskflow.validators",
            "from agent_taskflow.branch_push",
            "from agent_taskflow.draft_pr",
            "from agent_taskflow.local_cleanup_confirm",
            "from agent_taskflow.remote_branch_cleanup_confirm",
            "from agent_taskflow.task_closeout_confirm",
            "from agent_taskflow.github_issue",
            "from agent_taskflow.post_merge_cleanup",
            "from agent_taskflow.api",
            "git push",
            "gh pr create",
            "webhook",
            "cron",
            "background_worker",
            "automatic_task_picking",
        )
        for needle in forbidden_substrings:
            self.assertNotIn(
                needle,
                source,
                msg=f"forbidden substring {needle!r} in {SCRIPT}",
            )


if __name__ == "__main__":
    unittest.main()
