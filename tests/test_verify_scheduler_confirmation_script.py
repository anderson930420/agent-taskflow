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
    SchedulerConfirmationRequest,
    create_scheduler_confirmation,
)
from agent_taskflow.scheduler_proposals import (
    SchedulerProposalRequest,
    create_scheduler_proposal,
)
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "verify_scheduler_confirmation.py"


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
                title=f"cli verify {task_key}",
                status="queued",
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )

    def _proposal(self, task_keys: list[str]) -> dict[str, object]:
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

    def _safe_item_id(self, proposal: dict[str, object]) -> str:
        for item in proposal["items"]:  # type: ignore[index]
            if (
                item["recommended_command_kind"] == "create_task_execution_package"
                and not item.get("consistency_warnings")
            ):
                return item["proposal_item_id"]
        raise AssertionError("no safe item available in seeded proposal")

    def _confirm(
        self, proposal: dict[str, object], item_ids: tuple[str, ...]
    ) -> dict[str, object]:
        return create_scheduler_confirmation(
            SchedulerConfirmationRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                proposal_id=proposal["proposal_id"],  # type: ignore[index]
                selected_item_ids=item_ids,
                dry_run=False,
                confirm_create_confirmation=True,
            )
        )

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

    def _run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
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


class CliJsonTests(_CliBase):
    def test_json_emits_valid_payload_for_valid_confirmation(self) -> None:
        proposal = self._proposal(["AT-CLI-VRF-OK-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))
        before = self._db_counts()

        result = self._run_script(
            "--latest",
            "--proposal-item-id", item_id,
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "valid")
        self.assertTrue(payload["verification_passed"])
        self.assertTrue(payload["eligible_for_command_specific_confirm"])
        # A verifier pass is not execution permission; these stay false.
        self.assertFalse(payload["execution_allowed"])
        self.assertFalse(payload["allowed_to_attempt"])
        self.assertFalse(payload["execution_performed"])
        self.assertFalse(payload["action_evidence_created"])
        self.assertEqual(
            payload["schema_version"], "scheduler_confirmation_verification.v1"
        )
        self.assertEqual(payload["proposal_item_id"], item_id)
        self.assertEqual(
            payload["confirmation_id"], confirmation["confirmation_id"]
        )
        self.assertEqual(self._db_counts(), before)
        self.assertTrue(payload["safety"]["dry_run_only"])
        for key in (
            "will_execute",
            "will_mutate_db",
            "will_mutate_github",
            "will_change_task_status",
            "will_start_background_worker",
        ):
            self.assertFalse(payload["safety"][key], key)

    def test_pretty_includes_identifiers(self) -> None:
        proposal = self._proposal(["AT-CLI-VRF-PRT-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))

        result = self._run_script(
            "--latest",
            "--proposal-item-id", item_id,
            "--pretty",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Scheduler Confirmation Verification", result.stdout)
        self.assertIn(item_id, result.stdout)
        self.assertIn("verification_passed:   True", result.stdout)
        self.assertIn("execution_allowed:     False", result.stdout)
        self.assertIn("status:                valid", result.stdout)
        self.assertIn(confirmation["confirmation_id"], result.stdout)  # type: ignore[index]


class CliExitCodeTests(_CliBase):
    def test_blocked_status_exits_with_2(self) -> None:
        proposal = self._proposal(["AT-CLI-VRF-EXIT-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        result = self._run_script(
            "--latest",
            "--proposal-item-id", item_id,
            "--max-age-minutes", "0",
            "--json",
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["allowed_to_attempt"])

    def test_argument_error_exits_with_1(self) -> None:
        result = self._run_script(
            "--proposal-item-id", "foo", "--json",
        )
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")

    def test_two_selectors_exits_with_1(self) -> None:
        result = self._run_script(
            "--latest",
            "--confirmation-id", "confirmation-x",
            "--proposal-item-id", "foo",
            "--json",
        )
        self.assertEqual(result.returncode, 1)


class CliReadOnlyTests(_CliBase):
    def test_cli_performs_no_db_mutation(self) -> None:
        proposal = self._proposal(["AT-CLI-VRF-NOMUT-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))
        before = self._db_counts()

        with sqlite3.connect(self.db_path) as conn:
            existing_artifact_types = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT artifact_type FROM task_artifacts"
                ).fetchall()
            }
            existing_event_types = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT event_type FROM task_events"
                ).fetchall()
            }

        result = self._run_script(
            "--latest",
            "--proposal-item-id", item_id,
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertEqual(self._db_counts(), before)
        with sqlite3.connect(self.db_path) as conn:
            new_artifact_types = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT artifact_type FROM task_artifacts"
                ).fetchall()
            }
            new_event_types = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT event_type FROM task_events"
                ).fetchall()
            }
        self.assertEqual(new_artifact_types, existing_artifact_types)
        self.assertEqual(new_event_types, existing_event_types)

        forbidden_artifacts = {
            "scheduler_confirmation_consumption",
            "task_execution_package",
            "pr_handoff",
            "pr_handoff_package",
            "draft_pr",
            "branch_push",
            "local_cleanup",
            "remote_branch_cleanup",
            "task_closeout",
        }
        forbidden_events = {
            "scheduler_confirmation_consumed",
            "task_execution_package_created",
            "branch_push_completed",
            "draft_pr_created",
            "local_cleanup_completed",
            "remote_branch_cleanup_completed",
            "task_closeout_completed",
        }
        self.assertTrue(forbidden_artifacts.isdisjoint(new_artifact_types))
        self.assertTrue(forbidden_events.isdisjoint(new_event_types))


if __name__ == "__main__":
    unittest.main()
