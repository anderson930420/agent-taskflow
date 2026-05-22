from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.intake_runner_handoff import (
    HANDOFF_ARTIFACT_TYPE,
    HANDOFF_EVENT_TYPE,
)
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
SCRIPT = REPO_ROOT / "scripts" / "create_intake_runner_handoff.py"


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
                title=f"cli handoff {task_key}",
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

    def _confirm(
        self,
        proposal: dict[str, object],
        item_ids: tuple[str, ...],
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

    def _base_args(self, item_id: str, *extra: str) -> list[str]:
        return [
            "--db-path",
            str(self.db_path),
            "--artifact-root",
            str(self.artifact_root),
            "--proposal-item-id",
            item_id,
            *extra,
        ]


class CliHelpTests(_CliBase):
    def test_help_works(self) -> None:
        result = self._run_script("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("intake-to-runner", result.stdout.lower())
        self.assertIn("--confirm-create-handoff", result.stdout)


class CliJsonDryRunTests(_CliBase):
    def test_dry_run_json_emits_payload_and_writes_nothing(self) -> None:
        proposal = self._record_proposal(["AT-CLI-IRH-DRY-001"])
        item_id = self._first_safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        before = self._db_counts()
        result = self._run_script(
            *self._base_args(item_id, "--latest", "--json"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)

        self.assertEqual(payload["status"], "preview")
        self.assertEqual(payload["mode"], "dry_run")
        self.assertIsNone(payload["artifact_path"])
        self.assertEqual(
            payload["schema_version"], "intake_runner_handoff.v1"
        )
        self.assertTrue(payload["confirmation"]["verification_passed"])
        self.assertTrue(
            payload["confirmation"]["eligible_for_command_specific_confirm"]
        )
        self.assertFalse(payload["runner_contract"]["execution_allowed"])
        self.assertFalse(payload["runner_contract"]["executor_started"])
        self.assertFalse(
            payload["runner_contract"]["action_evidence_created"]
        )

        self.assertEqual(self._db_counts(), before)
        self.assertFalse(
            (self.artifact_root / "intake_runner_handoffs").exists()
        )


class CliConfirmedTests(_CliBase):
    def test_confirm_create_handoff_writes_artifact(self) -> None:
        proposal = self._record_proposal(["AT-CLI-IRH-CONF-001"])
        item_id = self._first_safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        result = self._run_script(
            *self._base_args(
                item_id,
                "--latest",
                "--confirm-create-handoff",
                "--json",
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)

        self.assertEqual(payload["status"], "created")
        self.assertEqual(payload["mode"], "confirmed")
        artifact_path = Path(payload["artifact_path"])
        self.assertTrue(artifact_path.exists())

        with sqlite3.connect(self.db_path) as conn:
            artifact_types = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT artifact_type FROM task_artifacts "
                    "WHERE task_key = ?",
                    ("AT-CLI-IRH-CONF-001",),
                ).fetchall()
            }
            event_types = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT event_type FROM task_events "
                    "WHERE task_key = ?",
                    ("AT-CLI-IRH-CONF-001",),
                ).fetchall()
            }

        self.assertIn(HANDOFF_ARTIFACT_TYPE, artifact_types)
        self.assertIn(HANDOFF_EVENT_TYPE, event_types)

        forbidden = {
            "task_execution_package",
            "pr_handoff",
            "draft_pr",
            "branch_push",
            "task_closeout",
            "scheduler_confirmation_consumption",
        }
        self.assertTrue(forbidden.isdisjoint(artifact_types))


class CliPrettyTests(_CliBase):
    def test_pretty_includes_runner_contract_disclaimers(self) -> None:
        proposal = self._record_proposal(["AT-CLI-IRH-PRET-001"])
        item_id = self._first_safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))

        result = self._run_script(
            *self._base_args(item_id, "--latest", "--pretty"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Intake-to-Runner Handoff", result.stdout)
        self.assertIn("verification_passed:", result.stdout)
        self.assertIn(
            "eligible_for_command_specific_confirm:", result.stdout
        )
        self.assertIn("execution_allowed:", result.stdout)
        self.assertIn("executor_started:", result.stdout)
        self.assertIn("action_evidence_created:", result.stdout)
        # The valid-path output prints status=preview and the verifier's
        # passed flags as True; the runner_contract flags must remain
        # False even on the valid path.
        self.assertIn("status:", result.stdout)
        self.assertIn("preview", result.stdout)
        self.assertIn(
            confirmation["confirmation_id"],  # type: ignore[index]
            result.stdout,
        )


class CliSelectorErrorTests(_CliBase):
    def test_missing_selector_errors(self) -> None:
        proposal = self._record_proposal(["AT-CLI-IRH-SEL-001"])
        item_id = self._first_safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        result = self._run_script(
            *self._base_args(item_id, "--json"),
        )
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("one of", payload["error"])

    def test_multiple_selectors_rejected_by_argparse(self) -> None:
        proposal = self._record_proposal(["AT-CLI-IRH-SEL-MUL-001"])
        item_id = self._first_safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))

        # argparse mutually exclusive group catches this before our code.
        result = self._run_script(
            *self._base_args(
                item_id,
                "--latest",
                "--confirmation-id",
                confirmation["confirmation_id"],  # type: ignore[index]
                "--json",
            ),
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with", result.stderr)

    def test_missing_proposal_item_id_errors(self) -> None:
        result = self._run_script(
            "--db-path",
            str(self.db_path),
            "--artifact-root",
            str(self.artifact_root),
            "--latest",
            "--json",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--proposal-item-id", result.stderr)


if __name__ == "__main__":
    unittest.main()
