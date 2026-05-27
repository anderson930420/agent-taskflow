"""CLI tests for scripts/create_scheduler_confirmation_verifier_report.py."""

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
    SchedulerConfirmationFromProposalRequest,
    create_scheduler_confirmation_from_proposal,
)
from agent_taskflow.scheduler_confirmation_verifier_report import (
    VERIFIER_REPORT_ARTIFACT_TYPE,
    VERIFIER_REPORT_EVENT_TYPE,
)
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "create_scheduler_confirmation_verifier_report.py"


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
                title=f"L4A cli verifier report {task_key}",
                status=status,
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )

    def _create_confirmation(self, task_key: str) -> dict[str, Any]:
        self._seed_task(task_key)
        proposal_payload = create_scheduler_proposal_from_candidate(
            SchedulerCandidateProposalRequest(
                task_key=task_key,
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                dry_run=False,
                confirm_create_proposal=True,
            )
        )
        self.assertTrue(proposal_payload["ok"], proposal_payload)
        proposal = proposal_payload["proposal"]
        confirmation_payload = create_scheduler_confirmation_from_proposal(
            SchedulerConfirmationFromProposalRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                task_key=task_key,
                proposal_item_id=proposal["proposal_item_id"],
                proposal_hash=proposal["proposal_hash"],
                proposal_id=proposal["proposal_id"],
                item_hash=proposal["item_hash"],
                recommended_command_kind=proposal["recommended_command_kind"],
                proposal_artifact_path=Path(proposal["proposal_artifact_path"]),
                dry_run=False,
                confirm_create_confirmation=True,
            )
        )
        self.assertTrue(confirmation_payload["ok"], confirmation_payload)
        return confirmation_payload["confirmation"]

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

    def _report_counts(self, task_key: str) -> dict[str, int]:
        artifacts = [
            a
            for a in self.store.list_task_artifacts(task_key)
            if a.artifact_type == VERIFIER_REPORT_ARTIFACT_TYPE
        ]
        events = [
            e
            for e in self.store.list_task_events(task_key)
            if e.event_type == VERIFIER_REPORT_EVENT_TYPE
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

    def _common_args(self, task_key: str, confirmation: dict[str, Any]) -> list[str]:
        return [
            "--db-path",
            str(self.db_path),
            "--artifact-root",
            str(self.artifact_root),
            "--task-key",
            task_key,
            "--confirmation-id",
            confirmation["confirmation_id"],
            "--proposal-hash",
            confirmation["proposal_hash"],
            "--proposal-item-id",
            confirmation["proposal_item_id"],
            "--item-hash",
            confirmation["item_hash"],
            "--recommended-command-kind",
            confirmation["recommended_command_kind"],
            "--confirmation-artifact-path",
            str(confirmation["artifact_path"]),
        ]


class ScriptHelpTests(_CliBase):
    def test_script_help(self) -> None:
        result = self._run("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scheduler_confirmation_verifier_report", result.stdout)
        self.assertIn("--task-key", result.stdout)
        self.assertIn("--confirmation-id", result.stdout)
        self.assertIn("--confirm-create-verifier-report", result.stdout)


class ScriptDryRunTests(_CliBase):
    def test_script_dry_run_writes_nothing(self) -> None:
        task_key = "AT-L4A-CLI-DRY-001"
        confirmation = self._create_confirmation(task_key)
        before = self._db_counts()

        result = self._run(*self._common_args(task_key, confirmation))

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["status"], "dry_run")
        self.assertEqual(payload["mode"], "dry_run")
        self.assertTrue(payload["would_create_verifier_report"])
        self.assertTrue(payload["binding"]["verification_passed"])
        self.assertEqual(self._db_counts(), before)
        self.assertEqual(self._report_counts(task_key), {"artifacts": 0, "events": 0})
        self.assertFalse(
            (self.artifact_root / "scheduler_confirmation_verifier_reports").exists()
        )


class ScriptConfirmedTests(_CliBase):
    def test_script_confirmed_mode_creates_verifier_report(self) -> None:
        task_key = "AT-L4A-CLI-CRT-001"
        confirmation = self._create_confirmation(task_key)

        result = self._run(
            *self._common_args(task_key, confirmation),
            "--operator",
            "cli-operator",
            "--operator-note",
            "cli-test",
            "--confirm-create-verifier-report",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["status"], "created")
        self.assertEqual(payload["mode"], "confirmed")

        artifact_path = Path(payload["verifier_report"]["artifact_path"])
        self.assertTrue(artifact_path.exists())
        on_disk = json.loads(artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["operator"], "cli-operator")
        self.assertEqual(on_disk["operator_note"], "cli-test")
        self.assertEqual(on_disk["confirmation_id"], confirmation["confirmation_id"])
        self.assertEqual(on_disk["proposal_hash"], confirmation["proposal_hash"])
        self.assertEqual(on_disk["item_hash"], confirmation["item_hash"])
        self.assertTrue(on_disk["verification_passed"])

        self.assertEqual(self._report_counts(task_key), {"artifacts": 1, "events": 1})


class ScriptNotVerifiedTests(_CliBase):
    def test_script_not_verified_returns_nonzero(self) -> None:
        task_key = "AT-L4A-CLI-NV-001"
        confirmation = self._create_confirmation(task_key)
        before = self._db_counts()

        args = self._common_args(task_key, confirmation)
        idx = args.index("--item-hash")
        args[idx + 1] = "0" * 64

        result = self._run(*args, "--confirm-create-verifier-report")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "not_verified")
        self.assertTrue(payload["reasons"])

        self.assertEqual(self._db_counts(), before)
        self.assertEqual(self._report_counts(task_key), {"artifacts": 0, "events": 0})


class ScriptSourceContractTests(unittest.TestCase):
    def test_script_source_has_no_forbidden_runtime_calls(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")

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

        forbidden_calls = (
            "requests.post",
            "gh pr",
            "approved_task_runner(",
            "approved_task_runner.",
            "intake_runner_handoff(",
            "runtime_execution_started(",
            "executor_run_started(",
            "validation_result(",
        )
        for needle in forbidden_calls:
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
