"""Tests for Level 4A scheduler confirmation verifier report helpers."""

from __future__ import annotations

import json
import sqlite3
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
    VERIFIER_REPORT_SAFETY_FLAGS,
    VERIFIER_REPORT_SCHEMA_VERSION,
    VERIFIER_REPORT_SOURCE,
    SchedulerConfirmationVerifierReportError,
    SchedulerConfirmationVerifierReportRequest,
    check_scheduler_confirmation_verifier_binding,
    create_scheduler_confirmation_verifier_report,
)
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    REPO_ROOT / "agent_taskflow" / "scheduler_confirmation_verifier_report.py"
)


class _Base(unittest.TestCase):
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
                title=f"L4A verifier report {task_key}",
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

    def _create_confirmation(self, task_key: str) -> dict[str, Any]:
        proposal = self._create_proposal(task_key)
        result = create_scheduler_confirmation_from_proposal(
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
                operator="test-operator",
                operator_note="L4A unit test",
            )
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "created")
        return result["confirmation"]

    def _build_request(
        self,
        task_key: str,
        confirmation: dict[str, Any],
        **overrides: Any,
    ) -> SchedulerConfirmationVerifierReportRequest:
        kwargs: dict[str, Any] = {
            "db_path": self.db_path,
            "artifact_root": self.artifact_root,
            "task_key": task_key,
            "confirmation_id": confirmation["confirmation_id"],
            "proposal_hash": confirmation["proposal_hash"],
            "proposal_item_id": confirmation["proposal_item_id"],
            "item_hash": confirmation["item_hash"],
            "recommended_command_kind": confirmation["recommended_command_kind"],
            "confirmation_artifact_path": Path(confirmation["artifact_path"]),
            "operator": "verifier-operator",
            "operator_note": "verifier report unit test",
        }
        kwargs.update(overrides)
        return SchedulerConfirmationVerifierReportRequest(**kwargs)

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


class DryRunTests(_Base):
    def test_dry_run_valid_confirmation_writes_nothing(self) -> None:
        task_key = "AT-L4A-DRY-001"
        confirmation = self._create_confirmation(task_key)
        before = self._db_counts()

        result = create_scheduler_confirmation_verifier_report(
            self._build_request(task_key, confirmation, dry_run=True)
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["mode"], "dry_run")
        self.assertTrue(result["would_create_verifier_report"])
        self.assertTrue(result["binding"]["verification_passed"])
        self.assertEqual(result["binding"]["reasons"], [])
        self.assertEqual(
            result["schema_version"],
            VERIFIER_REPORT_SCHEMA_VERSION,
        )
        self.assertEqual(result["source"], VERIFIER_REPORT_SOURCE)

        report = result["verifier_report"]
        self.assertEqual(report["confirmation_id"], confirmation["confirmation_id"])
        self.assertEqual(report["proposal_hash"], confirmation["proposal_hash"])
        self.assertEqual(report["proposal_item_id"], confirmation["proposal_item_id"])
        self.assertEqual(report["item_hash"], confirmation["item_hash"])
        self.assertEqual(report["operator"], "verifier-operator")
        self.assertEqual(report["operator_note"], "verifier report unit test")

        self.assertEqual(self._db_counts(), before)
        self.assertEqual(self._report_counts(task_key), {"artifacts": 0, "events": 0})
        self.assertFalse(
            (self.artifact_root / "scheduler_confirmation_verifier_reports").exists()
        )

        for key, expected in VERIFIER_REPORT_SAFETY_FLAGS.items():
            with self.subTest(key=key):
                self.assertEqual(result["safety"][key], expected)


class ConfirmedFlagTests(_Base):
    def test_confirmed_mode_requires_explicit_flag(self) -> None:
        task_key = "AT-L4A-FLAG-001"
        confirmation = self._create_confirmation(task_key)

        with self.assertRaises(SchedulerConfirmationVerifierReportError):
            create_scheduler_confirmation_verifier_report(
                self._build_request(
                    task_key,
                    confirmation,
                    dry_run=False,
                    confirm_create_verifier_report=False,
                )
            )

        self.assertEqual(self._report_counts(task_key), {"artifacts": 0, "events": 0})


class ConfirmedCreationTests(_Base):
    def test_confirmed_mode_creates_verifier_report_artifact_and_event_only(
        self,
    ) -> None:
        task_key = "AT-L4A-CRT-001"
        confirmation = self._create_confirmation(task_key)

        result = create_scheduler_confirmation_verifier_report(
            self._build_request(
                task_key,
                confirmation,
                dry_run=False,
                confirm_create_verifier_report=True,
            )
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["mode"], "confirmed")
        self.assertTrue(result["verification_passed"])

        report = result["verifier_report"]
        report_id = report["verifier_report_id"]
        artifact_path = Path(report["artifact_path"])
        self.assertEqual(
            artifact_path.parent,
            self.artifact_root / "scheduler_confirmation_verifier_reports" / report_id,
        )
        self.assertTrue(artifact_path.exists())

        on_disk = json.loads(artifact_path.read_text(encoding="utf-8"))
        for key in (
            "schema_version",
            "verifier_report_id",
            "created_at",
            "source",
            "mode",
            "task_key",
            "confirmation_id",
            "proposal_hash",
            "proposal_item_id",
            "item_hash",
            "recommended_command_kind",
            "confirmation_artifact_path",
            "proposal_artifact_path",
            "db_path",
            "artifact_root",
            "artifact_path",
            "operator",
            "operator_note",
            "verification_passed",
            "binding_summary",
            "reasons",
            "warnings",
            "checks",
            "safety",
        ):
            with self.subTest(key=key):
                self.assertIn(key, on_disk)
        self.assertEqual(on_disk["mode"], "confirmed")
        self.assertEqual(on_disk["confirmation_id"], confirmation["confirmation_id"])
        self.assertEqual(on_disk["proposal_hash"], confirmation["proposal_hash"])
        self.assertEqual(on_disk["item_hash"], confirmation["item_hash"])
        self.assertTrue(on_disk["verification_passed"])

        self.assertEqual(self._report_counts(task_key), {"artifacts": 1, "events": 1})
        events = [
            e
            for e in self.store.list_task_events(task_key)
            if e.event_type == VERIFIER_REPORT_EVENT_TYPE
        ]
        self.assertEqual(len(events), 1)
        event_payload = json.loads(events[0].payload_json or "{}")
        self.assertEqual(event_payload["kind"], VERIFIER_REPORT_EVENT_TYPE)
        self.assertEqual(event_payload["verifier_report_id"], report_id)
        self.assertTrue(event_payload["verification_passed"])
        self.assertTrue(event_payload["not_execution_permission"])
        self.assertTrue(event_payload["not_handoff"])
        self.assertTrue(event_payload["not_runtime"])
        self.assertTrue(event_payload["requires_next_gate"])

        task = self.store.get_task(task_key)
        assert task is not None
        self.assertEqual(task.status, "queued")


class NotVerifiedTests(_Base):
    def test_not_verified_does_not_write(self) -> None:
        task_key = "AT-L4A-NV-001"
        confirmation = self._create_confirmation(task_key)
        before = self._db_counts()

        result = create_scheduler_confirmation_verifier_report(
            self._build_request(
                task_key,
                confirmation,
                item_hash="0" * 64,
                dry_run=False,
                confirm_create_verifier_report=True,
            )
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "not_verified")
        self.assertFalse(result["verification_passed"])
        self.assertTrue(result["reasons"])
        self.assertEqual(self._db_counts(), before)
        self.assertEqual(self._report_counts(task_key), {"artifacts": 0, "events": 0})


class DuplicateTests(_Base):
    def test_duplicate_verifier_report_blocks_second_report(self) -> None:
        task_key = "AT-L4A-DUP-001"
        confirmation = self._create_confirmation(task_key)

        first = create_scheduler_confirmation_verifier_report(
            self._build_request(
                task_key,
                confirmation,
                dry_run=False,
                confirm_create_verifier_report=True,
            )
        )
        self.assertEqual(first["status"], "created")
        self.assertEqual(self._report_counts(task_key), {"artifacts": 1, "events": 1})

        second = create_scheduler_confirmation_verifier_report(
            self._build_request(
                task_key,
                confirmation,
                dry_run=False,
                confirm_create_verifier_report=True,
            )
        )
        self.assertFalse(second["ok"])
        self.assertEqual(second["status"], "not_verified")
        self.assertIn("duplicate_active_verifier_report", second["reasons"])
        self.assertEqual(self._report_counts(task_key), {"artifacts": 1, "events": 1})


class ArtifactSafetyTests(_Base):
    def test_verifier_report_artifact_safety_flags(self) -> None:
        task_key = "AT-L4A-SAFE-001"
        confirmation = self._create_confirmation(task_key)

        result = create_scheduler_confirmation_verifier_report(
            self._build_request(
                task_key,
                confirmation,
                dry_run=False,
                confirm_create_verifier_report=True,
            )
        )
        artifact_path = Path(result["verifier_report"]["artifact_path"])
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))

        self.assertTrue(payload["not_execution_permission"])
        self.assertTrue(payload["not_handoff"])
        self.assertTrue(payload["not_runtime"])
        self.assertTrue(payload["requires_next_gate"])
        self.assertTrue(payload["safety"]["verifier_report_created"])
        self.assertFalse(payload["safety"]["handoff_created"])
        self.assertFalse(payload["safety"]["runtime_started"])
        self.assertFalse(payload["safety"]["approved_task_runner_called"])
        self.assertFalse(payload["safety"]["executor_started"])
        self.assertFalse(payload["safety"]["validators_started"])
        self.assertFalse(payload["safety"]["github_mutated"])
        self.assertFalse(payload["safety"]["approved"])
        self.assertFalse(payload["safety"]["merged"])
        self.assertFalse(payload["safety"]["cleanup_performed"])
        self.assertFalse(payload["safety"]["background_worker_started"])

    def test_binding_helper_is_read_only(self) -> None:
        task_key = "AT-L4A-RO-001"
        confirmation = self._create_confirmation(task_key)
        before = self._db_counts()

        result = check_scheduler_confirmation_verifier_binding(
            self._build_request(task_key, confirmation)
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["verification_passed"], result)
        self.assertTrue(result["eligible_for_report"])
        self.assertEqual(result["reasons"], [])
        self.assertEqual(self._db_counts(), before)
        self.assertEqual(self._report_counts(task_key), {"artifacts": 0, "events": 0})


class SourceContractTests(unittest.TestCase):
    def test_source_does_not_import_or_call_forbidden_runtime_paths(self) -> None:
        text = MODULE_PATH.read_text(encoding="utf-8")

        forbidden_imports = (
            "from agent_taskflow.api",
            "import agent_taskflow.api",
            "from agent_taskflow.executors",
            "import agent_taskflow.executors",
            "from agent_taskflow.validators",
            "import agent_taskflow.validators",
            "from scripts",
            "import scripts",
            "mission_control",
            "mission-control",
        )
        for needle in forbidden_imports:
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)

        strict_forbidden_calls = (
            "subprocess",
            "requests.post",
            "gh pr",
            "approved_task_runner(",
            "approved_task_runner.",
            "intake_runner_handoff(",
            "runtime_execution_started(",
            "executor_run_started(",
            "validation_result(",
        )
        for needle in strict_forbidden_calls:
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
