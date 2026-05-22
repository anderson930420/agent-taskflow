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
    SchedulerProposalRequest,
    create_scheduler_proposal,
)
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "review_scheduler_proposal.py"


class ReviewSchedulerProposalScriptTests(unittest.TestCase):
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

    def _seed_queued(self, task_key: str) -> None:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Review CLI task",
                status="queued",
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )

    def _record_confirmed_proposal(self, task_keys: list[str]) -> dict[str, object]:
        for key in task_keys:
            self._seed_queued(key)
        request = SchedulerProposalRequest(
            db_path=self.db_path,
            artifact_root=self.artifact_root,
            dry_run=False,
            confirm_create_proposal=True,
        )
        return create_scheduler_proposal(request)

    def _db_counts(self) -> dict[str, int]:
        with sqlite3.connect(self.db_path) as conn:
            return {
                "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                "events": conn.execute(
                    "SELECT COUNT(*) FROM task_events"
                ).fetchone()[0],
                "artifacts": conn.execute(
                    "SELECT COUNT(*) FROM task_artifacts"
                ).fetchone()[0],
                "worktrees": conn.execute(
                    "SELECT COUNT(*) FROM task_worktrees"
                ).fetchone()[0],
            }

    def _run_script(
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

    def test_list_json_returns_summary(self) -> None:
        self._record_confirmed_proposal(["AT-REVCLI-LIST-001"])

        result = self._run_script("--list", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["review_mode"], "list")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["proposal_count"], 1)
        proposal = payload["proposals"][0]
        self.assertTrue(proposal["proposal_id"].startswith("proposal-"))
        self.assertEqual(proposal["task_key_count"], 1)
        self.assertIn("AT-REVCLI-LIST-001", proposal["task_keys"])

    def test_list_pretty_includes_proposal_id(self) -> None:
        proposal = self._record_confirmed_proposal(["AT-REVCLI-LIST-002"])

        result = self._run_script("--list", "--pretty")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Scheduler Proposal Review (list)", result.stdout)
        self.assertIn(proposal["proposal_id"], result.stdout)
        self.assertIn("read_only_review:   true", result.stdout)

    def test_proposal_id_json_returns_full_review(self) -> None:
        proposal = self._record_confirmed_proposal(["AT-REVCLI-ID-001"])

        result = self._run_script(
            "--proposal-id",
            proposal["proposal_id"],
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["review_mode"], "single")
        self.assertEqual(payload["review_status"], "valid")
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["hash_valid"])
        self.assertEqual(payload["proposal_id"], proposal["proposal_id"])
        self.assertEqual(payload["proposal_hash"], proposal["proposal_hash"])
        self.assertTrue(payload["items"])

    def test_artifact_path_pretty_includes_hash_and_command_kind(self) -> None:
        proposal = self._record_confirmed_proposal(["AT-REVCLI-AP-001"])

        result = self._run_script(
            "--artifact-path",
            proposal["artifact_path"],
            "--pretty",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Scheduler Proposal Review", result.stdout)
        self.assertIn(proposal["proposal_id"], result.stdout)
        self.assertIn(proposal["proposal_hash"][:12], result.stdout)
        self.assertIn("create_task_execution_package", result.stdout)
        self.assertIn("AT-REVCLI-AP-001", result.stdout)
        self.assertIn("read_only_review:   true", result.stdout)

    def test_latest_json_returns_newest(self) -> None:
        first = self._record_confirmed_proposal(["AT-REVCLI-LAT-A"])
        second = self._record_confirmed_proposal(["AT-REVCLI-LAT-B"])

        result = self._run_script("--latest", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["proposal_id"], second["proposal_id"])
        self.assertNotEqual(payload["proposal_id"], first["proposal_id"])

    def test_invalid_hash_returns_nonzero(self) -> None:
        proposal = self._record_confirmed_proposal(["AT-REVCLI-INV-001"])
        artifact_path = Path(proposal["artifact_path"])
        on_disk = json.loads(artifact_path.read_text(encoding="utf-8"))
        on_disk["items"][0]["proposed_action"] = "MUTATED"
        artifact_path.write_text(
            json.dumps(on_disk, indent=2, sort_keys=True), encoding="utf-8"
        )

        result = self._run_script("--latest", "--json")

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["review_status"], "invalid_hash")

    def test_cli_does_not_mutate_db(self) -> None:
        self._record_confirmed_proposal(["AT-REVCLI-NOMUT-001"])
        before = self._db_counts()

        self._run_script("--latest", "--json")
        self._run_script("--list", "--json")
        self._run_script(
            "--no-items",
            "--latest",
            "--json",
        )

        self.assertEqual(self._db_counts(), before)

    def test_missing_db_returns_nonzero(self) -> None:
        missing = self.root / "missing" / "state.db"
        result = self._run_script("--list", "--json", db_path=missing)
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertFalse(missing.exists())

    def test_relative_artifact_path_returns_clean_error(self) -> None:
        result = self._run_script(
            "--artifact-path",
            "relative/scheduler_proposal.json",
            "--json",
        )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("artifact_path", payload["error"])

    def test_no_verify_hashes_returns_unverified_review(self) -> None:
        self._record_confirmed_proposal(["AT-REVCLI-NV-001"])

        result = self._run_script("--latest", "--no-verify-hashes", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["review_status"], "valid")
        self.assertIsNone(payload["hash_report"])
        for item in payload["items"]:
            self.assertIsNone(item["item_hash_valid"])

    def test_no_items_pretty_message(self) -> None:
        self._record_confirmed_proposal(["AT-REVCLI-NOITEMS-001"])

        result = self._run_script("--latest", "--no-items", "--pretty")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Items: (omitted via --no-items)", result.stdout)

    def test_no_selector_defaults_to_list(self) -> None:
        self._record_confirmed_proposal(["AT-REVCLI-NOSEL-001"])

        result = self._run_script("--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["review_mode"], "list")
        self.assertGreaterEqual(payload["proposal_count"], 1)


if __name__ == "__main__":
    unittest.main()
