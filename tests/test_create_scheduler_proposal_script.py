from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord
from agent_taskflow.scheduler_proposals import (
    PROPOSAL_ARTIFACT_TYPE,
    PROPOSAL_EVENT_TYPE,
)
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "create_scheduler_proposal.py"


class CreateSchedulerProposalScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_queued(self, task_key: str = "AT-SPCLI-001") -> None:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="CLI proposal task",
                status="queued",
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )

    def _run_script(
        self,
        *args: str,
        db_path: Path | None = None,
        artifact_root: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--db-path",
                str(db_path or self.db_path),
                "--artifact-root",
                str(artifact_root or self.artifact_root),
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

    def _db_counts(self) -> dict[str, int]:
        with sqlite3.connect(self.db_path) as conn:
            return {
                "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
                "artifacts": conn.execute(
                    "SELECT COUNT(*) FROM task_artifacts"
                ).fetchone()[0],
                "worktrees": conn.execute(
                    "SELECT COUNT(*) FROM task_worktrees"
                ).fetchone()[0],
            }

    def test_json_dry_run_returns_valid_json_with_proposal_id(self) -> None:
        self._seed_queued()

        result = self._run_script("--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema_version"], "scheduler_proposal.v1")
        self.assertEqual(payload["mode"], "dry_run")
        self.assertTrue(payload["proposal_id"].startswith("proposal-"))
        kinds = [item["recommended_command_kind"] for item in payload["items"]]
        self.assertIn("create_task_execution_package", kinds)

    def test_pretty_includes_proposal_id_and_item_command_kind(self) -> None:
        self._seed_queued()

        result = self._run_script("--pretty")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Scheduler Proposal", result.stdout)
        self.assertIn("proposal_id:", result.stdout)
        self.assertIn("create_task_execution_package", result.stdout)
        self.assertIn("AT-SPCLI-001", result.stdout)

    def test_dry_run_default_does_not_mutate_db_or_disk(self) -> None:
        self._seed_queued()
        before = self._db_counts()

        result = self._run_script("--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._db_counts(), before)
        self.assertFalse((self.artifact_root / "scheduler_proposals").exists())

    def test_confirm_create_proposal_writes_artifact_and_records_evidence(self) -> None:
        self._seed_queued()
        before = self._db_counts()

        result = self._run_script("--json", "--confirm-create-proposal")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"], "confirmed")
        artifact_path = Path(payload["artifact_path"])
        self.assertTrue(artifact_path.exists())

        with sqlite3.connect(self.db_path) as conn:
            artifact_types = [
                row[0]
                for row in conn.execute(
                    "SELECT artifact_type FROM task_artifacts WHERE task_key = ?",
                    ("AT-SPCLI-001",),
                ).fetchall()
            ]
            event_types = [
                row[0]
                for row in conn.execute(
                    "SELECT event_type FROM task_events WHERE task_key = ?",
                    ("AT-SPCLI-001",),
                ).fetchall()
            ]
        self.assertEqual(artifact_types, [PROPOSAL_ARTIFACT_TYPE])
        self.assertEqual(event_types, [PROPOSAL_EVENT_TYPE])

        after = self._db_counts()
        self.assertEqual(after["artifacts"], before["artifacts"] + 1)
        self.assertEqual(after["events"], before["events"] + 1)
        self.assertEqual(after["tasks"], before["tasks"])

    def test_relative_artifact_root_returns_nonzero_with_clear_error(self) -> None:
        self._seed_queued()

        result = self._run_script(
            "--json",
            artifact_root=Path("relative/path"),
        )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("artifact_root", payload["error"])

    def test_missing_db_returns_nonzero_without_creating_db(self) -> None:
        missing = self.root / "missing" / "state.db"

        result = self._run_script("--json", db_path=missing)

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertFalse(missing.exists())

    def test_include_completed_flag_includes_completed_task_with_no_action(self) -> None:
        completed_artifact_dir = self.artifact_root / "AT-SPCLI-002"
        completed_artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-SPCLI-002",
                project="agent-taskflow",
                board="agent-taskflow",
                title="Completed CLI task",
                status="completed",
                repo_path=self.repo,
                artifact_dir=completed_artifact_dir,
            )
        )
        for artifact_type, event_type in [
            ("local_cleanup", "local_cleanup_completed"),
            ("remote_branch_cleanup", "remote_branch_cleanup_completed"),
            ("task_closeout", "task_closeout_completed"),
        ]:
            artifact_file = completed_artifact_dir / f"{artifact_type}.json"
            artifact_file.write_text(
                json.dumps({"kind": event_type}, sort_keys=True), encoding="utf-8"
            )
            self.store.record_task_artifact("AT-SPCLI-002", artifact_type, artifact_file)
            self.store.record_task_event(
                "AT-SPCLI-002",
                event_type,
                f"{artifact_type}_confirm",
                payload={"kind": event_type},
            )

        result_default = self._run_script("--json")
        result_completed = self._run_script("--json", "--include-completed")

        self.assertEqual(result_default.returncode, 0, result_default.stderr)
        self.assertEqual(result_completed.returncode, 0, result_completed.stderr)
        default_payload = json.loads(result_default.stdout)
        completed_payload = json.loads(result_completed.stdout)
        self.assertNotIn(
            "AT-SPCLI-002",
            [item["task_key"] for item in default_payload["items"]],
        )
        self.assertIn(
            "AT-SPCLI-002",
            [item["task_key"] for item in completed_payload["items"]],
        )

    def test_pretty_no_items_message(self) -> None:
        # No tasks in DB; --pretty should still succeed.
        result = self._run_script("--pretty")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Scheduler Proposal", result.stdout)
        self.assertIn("Items: (none)", result.stdout)


if __name__ == "__main__":
    unittest.main()
