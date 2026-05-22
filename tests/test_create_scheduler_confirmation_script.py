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
from agent_taskflow.scheduler_confirmations import (
    CONFIRMATION_ARTIFACT_TYPE,
    CONFIRMATION_EVENT_TYPE,
)
from agent_taskflow.scheduler_proposals import (
    SchedulerProposalRequest,
    create_scheduler_proposal,
)
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "create_scheduler_confirmation.py"


class _CliBase(unittest.TestCase):
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
                title=f"cli confirm {task_key}",
                status="queued",
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )

    def _record_proposal(self, task_keys: list[str]) -> dict[str, object]:
        for key in task_keys:
            self._seed_queued(key)
        return create_scheduler_proposal(
            SchedulerProposalRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                dry_run=False,
                confirm_create_proposal=True,
            )
        )

    def _first_safe_item_id(self, proposal: dict[str, object]) -> str:
        for item in proposal["items"]:  # type: ignore[index]
            if (
                item["recommended_command_kind"] == "create_task_execution_package"
                and not item.get("consistency_warnings")
            ):
                return item["proposal_item_id"]
        raise AssertionError("no safe item available in seeded proposal")

    def _db_counts(self) -> dict[str, int]:
        with sqlite3.connect(self.db_path) as conn:
            return {
                "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
                "artifacts": conn.execute(
                    "SELECT COUNT(*) FROM task_artifacts"
                ).fetchone()[0],
            }

    def _run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
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


class CliJsonDryRunTests(_CliBase):
    def test_json_dry_run_emits_valid_json(self) -> None:
        proposal = self._record_proposal(["AT-CLI-DRY-001"])
        item_id = self._first_safe_item_id(proposal)
        before = self._db_counts()

        result = self._run_script(
            "--latest", "--item-id", item_id, "--json", "--dry-run",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(payload["schema_version"], "scheduler_confirmation.v1")
        self.assertIsNone(payload["artifact_path"])
        self.assertEqual(payload["selected_items"][0]["proposal_item_id"], item_id)
        self.assertFalse(payload["safety"]["execution_allowed"])
        self.assertEqual(self._db_counts(), before)

    def test_pretty_includes_identifiers(self) -> None:
        proposal = self._record_proposal(["AT-CLI-PRET-001"])
        item_id = self._first_safe_item_id(proposal)

        result = self._run_script(
            "--latest", "--item-id", item_id, "--pretty",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Scheduler Confirmation", result.stdout)
        self.assertIn("confirmation_id", result.stdout)
        self.assertIn(item_id, result.stdout)
        self.assertIn(proposal["proposal_id"], result.stdout)  # type: ignore[index]


class CliConfirmedTests(_CliBase):
    def test_confirmed_writes_only_confirmation_evidence(self) -> None:
        proposal = self._record_proposal(["AT-CLI-CONF-001"])
        item_id = self._first_safe_item_id(proposal)

        result = self._run_script(
            "--latest",
            "--item-id", item_id,
            "--confirm-create-confirmation",
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        artifact_path = Path(payload["artifact_path"])
        self.assertTrue(artifact_path.exists())

        with sqlite3.connect(self.db_path) as conn:
            artifact_types = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT artifact_type FROM task_artifacts "
                    "WHERE task_key = ?",
                    ("AT-CLI-CONF-001",),
                ).fetchall()
            }
            event_types = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT event_type FROM task_events "
                    "WHERE task_key = ?",
                    ("AT-CLI-CONF-001",),
                ).fetchall()
            }

        # Confirmation evidence is recorded.
        self.assertIn(CONFIRMATION_ARTIFACT_TYPE, artifact_types)
        self.assertIn(CONFIRMATION_EVENT_TYPE, event_types)
        # Forbidden action evidence is NOT recorded.
        forbidden = {
            "task_execution_package",
            "pr_handoff",
            "draft_pr",
            "branch_push",
            "task_closeout",
        }
        self.assertTrue(forbidden.isdisjoint(artifact_types))


class CliErrorTests(_CliBase):
    def test_missing_item_blocks_with_error(self) -> None:
        self._record_proposal(["AT-CLI-ERR-001"])
        result = self._run_script(
            "--latest", "--item-id", "DOES-NOT-EXIST", "--json",
        )
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["ok"], False)
        self.assertIn("not found", payload["error"])
        self.assertFalse(payload["safety"]["execution_allowed"])

    def test_dry_run_does_not_mutate_db(self) -> None:
        proposal = self._record_proposal(["AT-CLI-NOMUT-001"])
        item_id = self._first_safe_item_id(proposal)
        before = self._db_counts()

        result = self._run_script(
            "--latest", "--item-id", item_id, "--dry-run", "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._db_counts(), before)

    def test_warnings_without_ack_blocks(self) -> None:
        proposal = self._record_proposal(["AT-CLI-WARN-001"])
        item_id = self._first_safe_item_id(proposal)
        artifact_path = Path(proposal["artifact_path"])
        on_disk = json.loads(artifact_path.read_text())
        from agent_taskflow.scheduler_proposals import (
            compute_item_hash,
            compute_proposal_hash,
        )

        for item in on_disk["items"]:
            if item["proposal_item_id"] == item_id:
                item["consistency_warnings"] = ["synthetic"]
                item["item_hash"] = compute_item_hash(item)
        on_disk["proposal_hash"] = compute_proposal_hash(on_disk)
        artifact_path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))

        result = self._run_script(
            "--latest", "--item-id", item_id, "--json",
        )
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertIn("consistency_warnings", payload["error"])


if __name__ == "__main__":
    unittest.main()
