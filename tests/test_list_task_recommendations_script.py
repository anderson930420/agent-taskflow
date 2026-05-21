from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "list_task_recommendations.py"


class ListTaskRecommendationsScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "state.db"
        self.artifact_dir = self.root / "artifacts" / "AT-CLI-001"
        self.artifact_dir.mkdir(parents=True)
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def seed_queued_task(self) -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-CLI-001",
                project="agent-taskflow",
                board="agent-taskflow",
                title="CLI recommendation task",
                status="queued",
                repo_path=self.repo,
                artifact_dir=self.artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )

    def run_script(
        self,
        *args: str,
        db_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--db-path",
                str(db_path or self.db_path),
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

    def db_counts(self) -> dict[str, int]:
        with sqlite3.connect(self.db_path) as conn:
            return {
                "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
                "artifacts": conn.execute("SELECT COUNT(*) FROM task_artifacts").fetchone()[0],
                "worktrees": conn.execute("SELECT COUNT(*) FROM task_worktrees").fetchone()[0],
            }

    def test_json_returns_valid_json(self) -> None:
        self.seed_queued_task()

        result = self.run_script("--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["items"][0]["task_key"], "AT-CLI-001")
        self.assertEqual(
            payload["items"][0]["recommended_command_kind"],
            "create_task_execution_package",
        )

    def test_pretty_includes_task_key_status_and_recommendation(self) -> None:
        self.seed_queued_task()

        result = self.run_script("--pretty")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AT-CLI-001", result.stdout)
        self.assertIn("queued", result.stdout)
        self.assertIn("Create Task Execution Package", result.stdout)

    def test_task_key_filter_shows_one_task(self) -> None:
        self.seed_queued_task()
        second_artifact_dir = self.root / "artifacts" / "AT-CLI-002"
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-CLI-002",
                project="agent-taskflow",
                board="agent-taskflow",
                title="Other CLI task",
                status="queued",
                repo_path=self.repo,
                artifact_dir=second_artifact_dir,
            )
        )

        result = self.run_script("--json", "--task-key", "AT-CLI-001")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["task_key"], "AT-CLI-001")

    def test_script_does_not_mutate_db(self) -> None:
        self.seed_queued_task()
        before_counts = self.db_counts()
        before_status = self.store.get_task("AT-CLI-001").status

        result = self.run_script("--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.db_counts(), before_counts)
        self.assertEqual(self.store.get_task("AT-CLI-001").status, before_status)

    def test_missing_db_exits_nonzero_without_creating_db(self) -> None:
        missing = self.root / "missing" / "state.db"

        result = self.run_script("--json", db_path=missing)

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertFalse(missing.exists())

    def seed_stale_completed_task(self) -> None:
        artifact_dir = self.root / "artifacts" / "AT-CLI-WARN"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-CLI-WARN",
                project="agent-taskflow",
                board="agent-taskflow",
                title="Stale worktree task",
                status="completed",
                repo_path=self.repo,
                artifact_dir=artifact_dir,
            )
        )
        missing_path = self.repo / ".worktrees" / "AT-CLI-WARN"
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key="AT-CLI-WARN",
                repo_path=self.repo,
                worktree_path=missing_path,
                branch="task/AT-CLI-WARN",
                base_branch="main",
                base_sha="base-sha",
                status="active",
            )
        )
        cleanup_payload = {
            "kind": "local_cleanup_completed",
            "artifact_type": "local_cleanup",
            "task_key": "AT-CLI-WARN",
            "cleanup_scope": "local",
        }
        cleanup_path = artifact_dir / "local_cleanup.json"
        cleanup_path.write_text(
            json.dumps(cleanup_payload, sort_keys=True), encoding="utf-8"
        )
        self.store.record_task_artifact(
            "AT-CLI-WARN", "local_cleanup", cleanup_path
        )
        self.store.record_task_event(
            "AT-CLI-WARN",
            "local_cleanup_completed",
            "local_cleanup_confirm",
            payload=cleanup_payload,
        )
        remote_payload = {
            "kind": "remote_branch_cleanup_completed",
            "artifact_type": "remote_branch_cleanup",
            "task_key": "AT-CLI-WARN",
            "cleanup_scope": "remote_branch",
        }
        remote_path = artifact_dir / "remote_branch_cleanup.json"
        remote_path.write_text(
            json.dumps(remote_payload, sort_keys=True), encoding="utf-8"
        )
        self.store.record_task_artifact(
            "AT-CLI-WARN", "remote_branch_cleanup", remote_path
        )
        self.store.record_task_event(
            "AT-CLI-WARN",
            "remote_branch_cleanup_completed",
            "remote_branch_cleanup_confirm",
            payload=remote_payload,
        )
        closeout_payload = {
            "kind": "task_closeout_completed",
            "artifact_type": "task_closeout",
            "task_key": "AT-CLI-WARN",
        }
        closeout_path = artifact_dir / "task_closeout.json"
        closeout_path.write_text(
            json.dumps(closeout_payload, sort_keys=True), encoding="utf-8"
        )
        self.store.record_task_artifact(
            "AT-CLI-WARN", "task_closeout", closeout_path
        )
        self.store.record_task_event(
            "AT-CLI-WARN",
            "task_closeout_completed",
            "task_closeout_confirm",
            payload=closeout_payload,
        )

    def test_json_includes_consistency_warnings(self) -> None:
        self.seed_stale_completed_task()

        result = self.run_script("--json", "--task-key", "AT-CLI-WARN")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        item = payload["items"][0]
        self.assertEqual(item["task_key"], "AT-CLI-WARN")
        self.assertEqual(item["recommended_command_kind"], "no_action")
        self.assertIn("consistency_warnings", item)
        self.assertTrue(item["consistency_warnings"])
        self.assertTrue(
            any(
                "physical worktree path is missing" in w
                for w in item["consistency_warnings"]
            ),
            item["consistency_warnings"],
        )
        self.assertGreater(payload["summary"]["warning_count"], 0)

    def test_pretty_shows_warnings_block_when_present(self) -> None:
        self.seed_stale_completed_task()

        result = self.run_script("--pretty", "--task-key", "AT-CLI-WARN")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Warnings:", result.stdout)
        self.assertIn("physical worktree path is missing", result.stdout)

    def test_pretty_omits_warnings_block_when_none(self) -> None:
        self.seed_queued_task()

        result = self.run_script("--pretty")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Warnings:", result.stdout)

    def test_warning_path_does_not_mutate_db(self) -> None:
        self.seed_stale_completed_task()
        before_counts = self.db_counts()

        result = self.run_script("--json", "--task-key", "AT-CLI-WARN")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.db_counts(), before_counts)


if __name__ == "__main__":
    unittest.main()
