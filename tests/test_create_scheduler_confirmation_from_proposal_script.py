"""CLI tests for scripts/create_scheduler_confirmation_from_proposal.py."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.models import TaskRecord
from agent_taskflow.scheduler_candidate_proposals import (
    SchedulerCandidateProposalRequest,
    create_scheduler_proposal_from_candidate,
)
from agent_taskflow.scheduler_confirmation_from_proposal import (
    CONFIRMATION_ARTIFACT_TYPE,
    CONFIRMATION_EVENT_TYPE,
)
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "create_scheduler_confirmation_from_proposal.py"


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

    def _seed_task(self, task_key: str, *, status: str = "queued") -> None:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title=f"K2 cli confirm {task_key}",
                status=status,
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )

    def _create_proposal(self, task_key: str) -> dict[str, Any]:
        self._seed_task(task_key)
        payload = create_scheduler_proposal_from_candidate(
            SchedulerCandidateProposalRequest(
                task_key=task_key,
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                dry_run=False,
                confirm_create_proposal=True,
            )
        )
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["status"], "created")
        return payload["proposal"]

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
            }

    def _confirmation_counts(self, task_key: str) -> dict[str, int]:
        artifacts = [
            a
            for a in self.store.list_task_artifacts(task_key)
            if a.artifact_type == CONFIRMATION_ARTIFACT_TYPE
        ]
        events = [
            e
            for e in self.store.list_task_events(task_key)
            if e.event_type == CONFIRMATION_EVENT_TYPE
        ]
        return {"artifacts": len(artifacts), "events": len(events)}

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def _common_args(
        self, task_key: str, proposal: dict[str, Any]
    ) -> list[str]:
        return [
            "--db-path",
            str(self.db_path),
            "--artifact-root",
            str(self.artifact_root),
            "--task-key",
            task_key,
            "--proposal-item-id",
            proposal["proposal_item_id"],
            "--proposal-hash",
            proposal["proposal_hash"],
            "--proposal-id",
            proposal["proposal_id"],
            "--item-hash",
            proposal["item_hash"],
            "--recommended-command-kind",
            proposal["recommended_command_kind"],
            "--proposal-artifact-path",
            str(proposal["proposal_artifact_path"]),
        ]


class ScriptHelpTests(_CliBase):
    def test_script_help(self) -> None:
        result = self._run("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scheduler_confirmation", result.stdout.lower())
        self.assertIn("--task-key", result.stdout)
        self.assertIn("--proposal-item-id", result.stdout)
        self.assertIn("--confirm-create-confirmation", result.stdout)


class ScriptDryRunTests(_CliBase):
    def test_script_dry_run_writes_nothing(self) -> None:
        task_key = "AT-K2-CLI-DRY-001"
        proposal = self._create_proposal(task_key)
        before = self._db_counts()

        result = self._run(*self._common_args(task_key, proposal))

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "dry_run")
        self.assertEqual(payload["mode"], "dry_run")
        self.assertTrue(payload["would_create_confirmation"])

        self.assertEqual(self._db_counts(), before)
        self.assertEqual(
            self._confirmation_counts(task_key),
            {"artifacts": 0, "events": 0},
        )
        self.assertFalse(
            (self.artifact_root / "scheduler_confirmations").exists()
        )


class ScriptConfirmedTests(_CliBase):
    def test_script_confirmed_mode_creates_confirmation(self) -> None:
        task_key = "AT-K2-CLI-CRT-001"
        proposal = self._create_proposal(task_key)

        result = self._run(
            *self._common_args(task_key, proposal),
            "--operator",
            "cli-operator",
            "--operator-note",
            "cli-test",
            "--confirm-create-confirmation",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "created")
        self.assertEqual(payload["mode"], "confirmed")

        artifact_path = Path(payload["confirmation"]["artifact_path"])
        self.assertTrue(artifact_path.exists())
        on_disk = json.loads(artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["operator"], "cli-operator")
        self.assertEqual(on_disk["operator_note"], "cli-test")
        self.assertEqual(
            on_disk["proposal_hash"], proposal["proposal_hash"]
        )
        self.assertEqual(on_disk["item_hash"], proposal["item_hash"])

        self.assertEqual(
            self._confirmation_counts(task_key),
            {"artifacts": 1, "events": 1},
        )

    def test_script_missing_confirm_flag_does_not_write(self) -> None:
        # Without --confirm-create-confirmation the CLI must remain a
        # dry-run that writes nothing, even when every binding field is
        # provided.
        task_key = "AT-K2-CLI-FLAG-001"
        proposal = self._create_proposal(task_key)
        before = self._db_counts()

        result = self._run(*self._common_args(task_key, proposal))

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"], "dry_run")

        self.assertEqual(self._db_counts(), before)
        self.assertEqual(
            self._confirmation_counts(task_key),
            {"artifacts": 0, "events": 0},
        )


class ScriptNotEligibleTests(_CliBase):
    def test_script_not_eligible_returns_nonzero(self) -> None:
        task_key = "AT-K2-CLI-NEL-001"
        proposal = self._create_proposal(task_key)
        before = self._db_counts()

        args = self._common_args(task_key, proposal)
        # Replace the item-hash with a bogus one so eligibility fails.
        idx = args.index("--item-hash")
        args[idx + 1] = "0" * 64

        result = self._run(*args, "--confirm-create-confirmation")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "not_eligible")
        self.assertTrue(payload["reasons"])

        self.assertEqual(self._db_counts(), before)
        self.assertEqual(
            self._confirmation_counts(task_key),
            {"artifacts": 0, "events": 0},
        )


class ScriptSourceContractTests(unittest.TestCase):
    def test_script_source_has_no_forbidden_runtime_calls(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")

        strict_forbidden = (
            "executor_run_started",
            "validation_result",
            "runtime_execution_started",
            "create_verifier_report",
            "intake_runner_handoff",
            "subprocess",
            "requests.post",
            "gh pr",
        )
        for needle in strict_forbidden:
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)

        # `approved_task_runner` may only ever appear as a key reference;
        # it must never be imported or called from this CLI.
        self.assertNotIn("from agent_taskflow.approved_task_runner", text)
        self.assertNotIn("import agent_taskflow.approved_task_runner", text)
        self.assertNotIn("approved_task_runner(", text)
        self.assertNotIn("approved_task_runner.", text)

        forbidden_imports = (
            "from agent_taskflow.api",
            "import agent_taskflow.api",
            "from agent_taskflow.executors",
            "import agent_taskflow.executors",
            "from agent_taskflow.validators",
            "import agent_taskflow.validators",
            "mission_control",
            "mission-control",
        )
        for needle in forbidden_imports:
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
