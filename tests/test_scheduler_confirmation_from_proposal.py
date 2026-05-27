"""Tests for the explicit scheduler confirmation creation helper (K2)."""

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
    CONFIRMATION_ARTIFACT_TYPE,
    CONFIRMATION_EVENT_TYPE,
    CONFIRMATION_FROM_PROPOSAL_SCHEMA_VERSION,
    CONFIRMATION_FROM_PROPOSAL_SOURCE,
    CONFIRMATION_SAFETY_FLAGS,
    SchedulerConfirmationFromProposalError,
    SchedulerConfirmationFromProposalRequest,
    create_scheduler_confirmation_from_proposal,
)
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    REPO_ROOT / "agent_taskflow" / "scheduler_confirmation_from_proposal.py"
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
                title=f"K2 confirm {task_key}",
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

    def _build_request(
        self,
        task_key: str,
        proposal: dict[str, Any],
        **overrides: Any,
    ) -> SchedulerConfirmationFromProposalRequest:
        kwargs: dict[str, Any] = {
            "db_path": self.db_path,
            "artifact_root": self.artifact_root,
            "task_key": task_key,
            "proposal_item_id": proposal["proposal_item_id"],
            "proposal_hash": proposal["proposal_hash"],
            "proposal_id": proposal["proposal_id"],
            "item_hash": proposal["item_hash"],
            "recommended_command_kind": proposal["recommended_command_kind"],
            "proposal_artifact_path": Path(proposal["proposal_artifact_path"]),
            "operator": "test-operator",
            "operator_note": "K2 unit test",
        }
        kwargs.update(overrides)
        return SchedulerConfirmationFromProposalRequest(**kwargs)

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


class DryRunTests(_Base):
    def test_dry_run_valid_proposal_writes_nothing(self) -> None:
        task_key = "AT-K2-DRY-001"
        proposal = self._create_proposal(task_key)
        before = self._db_counts()

        result = create_scheduler_confirmation_from_proposal(
            self._build_request(task_key, proposal, dry_run=True)
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["mode"], "dry_run")
        self.assertTrue(result["eligible"])
        self.assertTrue(result["would_create_confirmation"])
        self.assertEqual(
            result["schema_version"],
            CONFIRMATION_FROM_PROPOSAL_SCHEMA_VERSION,
        )
        self.assertEqual(result["source"], CONFIRMATION_FROM_PROPOSAL_SOURCE)

        confirmation = result["confirmation"]
        self.assertEqual(
            confirmation["recommended_command_kind"],
            proposal["recommended_command_kind"],
        )
        self.assertEqual(confirmation["proposal_id"], proposal["proposal_id"])
        self.assertEqual(
            confirmation["proposal_hash"], proposal["proposal_hash"]
        )
        self.assertEqual(confirmation["item_hash"], proposal["item_hash"])
        self.assertEqual(confirmation["operator"], "test-operator")
        self.assertEqual(confirmation["operator_note"], "K2 unit test")

        confirmations_dir = self.artifact_root / "scheduler_confirmations"
        self.assertFalse(
            confirmations_dir.exists(),
            "dry-run must not create scheduler_confirmations directory",
        )
        self.assertEqual(self._db_counts(), before)
        self.assertEqual(
            self._confirmation_counts(task_key),
            {"artifacts": 0, "events": 0},
        )

        safety = result["safety"]
        for key, expected in CONFIRMATION_SAFETY_FLAGS.items():
            with self.subTest(key=key):
                self.assertEqual(safety[key], expected)


class ConfirmedFlagTests(_Base):
    def test_confirmed_mode_requires_explicit_flag(self) -> None:
        task_key = "AT-K2-FLAG-001"
        proposal = self._create_proposal(task_key)

        with self.assertRaises(SchedulerConfirmationFromProposalError):
            create_scheduler_confirmation_from_proposal(
                self._build_request(
                    task_key,
                    proposal,
                    dry_run=False,
                    confirm_create_confirmation=False,
                )
            )

        confirmations_dir = self.artifact_root / "scheduler_confirmations"
        self.assertFalse(confirmations_dir.exists())
        self.assertEqual(
            self._confirmation_counts(task_key),
            {"artifacts": 0, "events": 0},
        )


class ConfirmedCreationTests(_Base):
    def test_confirmed_mode_creates_confirmation_artifact_and_event_only(
        self,
    ) -> None:
        task_key = "AT-K2-CRT-001"
        proposal = self._create_proposal(task_key)

        result = create_scheduler_confirmation_from_proposal(
            self._build_request(
                task_key,
                proposal,
                dry_run=False,
                confirm_create_confirmation=True,
            )
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["mode"], "confirmed")
        self.assertTrue(result["eligible"])

        confirmation = result["confirmation"]
        confirmation_id = confirmation["confirmation_id"]
        artifact_path = Path(confirmation["artifact_path"])
        expected_dir = (
            self.artifact_root
            / "scheduler_confirmations"
            / confirmation_id
        )
        self.assertEqual(artifact_path.parent, expected_dir)
        self.assertTrue(artifact_path.exists())

        on_disk = json.loads(artifact_path.read_text(encoding="utf-8"))
        for key in (
            "confirmation_id",
            "proposal_id",
            "proposal_hash",
            "proposal_item_id",
            "item_hash",
            "recommended_command_kind",
            "proposal_artifact_path",
            "task_key",
            "schema_version",
            "operator",
            "operator_note",
            "eligibility_summary",
        ):
            with self.subTest(key=key):
                self.assertIn(key, on_disk)

        self.assertEqual(on_disk["proposal_hash"], proposal["proposal_hash"])
        self.assertEqual(on_disk["item_hash"], proposal["item_hash"])
        self.assertEqual(
            on_disk["proposal_item_id"], proposal["proposal_item_id"]
        )
        self.assertEqual(
            on_disk["recommended_command_kind"],
            proposal["recommended_command_kind"],
        )

        counts = self._confirmation_counts(task_key)
        self.assertEqual(counts, {"artifacts": 1, "events": 1})

        # Only confirmation artifact/event are added — proposal evidence
        # already existed before this call.
        events = [
            e
            for e in self.store.list_task_events(task_key)
            if e.event_type == CONFIRMATION_EVENT_TYPE
        ]
        self.assertEqual(len(events), 1)
        event_payload = json.loads(events[0].payload_json or "{}")
        self.assertEqual(event_payload["kind"], CONFIRMATION_EVENT_TYPE)
        self.assertEqual(
            event_payload["confirmation_id"], confirmation_id
        )
        self.assertTrue(event_payload["not_execution_permission"])
        self.assertTrue(event_payload["not_verifier_report"])
        self.assertTrue(event_payload["not_handoff"])
        self.assertTrue(event_payload["not_runtime"])
        self.assertTrue(event_payload["requires_next_gate"])

        # Task status remains untouched; no runtime/executor/validator side
        # effects.
        task = self.store.get_task(task_key)
        assert task is not None
        self.assertEqual(task.status, "queued")
        non_confirmation_events = [
            e
            for e in self.store.list_task_events(task_key)
            if e.event_type
            not in (CONFIRMATION_EVENT_TYPE, "scheduler_proposal_created")
        ]
        for e in non_confirmation_events:
            payload = json.loads(e.payload_json or "{}")
            self.assertNotIn(payload.get("kind"), {
                "executor_run_started",
                "executor_run_finished",
                "validation_result",
                "runtime_execution_started",
            })

        safety = result["safety"]
        self.assertTrue(safety["confirmation_created"])
        self.assertFalse(safety["verifier_report_created"])
        self.assertFalse(safety["handoff_created"])
        self.assertFalse(safety["runtime_started"])
        self.assertFalse(safety["approved_task_runner_called"])
        self.assertFalse(safety["executor_started"])
        self.assertFalse(safety["validators_started"])
        self.assertFalse(safety["github_mutated"])
        self.assertFalse(safety["approved"])
        self.assertFalse(safety["merged"])
        self.assertFalse(safety["cleanup_performed"])
        self.assertFalse(safety["background_worker_started"])
        self.assertTrue(safety["not_execution_permission"])
        self.assertTrue(safety["not_verifier_report"])
        self.assertTrue(safety["not_handoff"])
        self.assertTrue(safety["not_runtime"])
        self.assertTrue(safety["requires_next_gate"])


class NotEligibleTests(_Base):
    def test_wrong_item_hash_does_not_write(self) -> None:
        task_key = "AT-K2-NEL-001"
        proposal = self._create_proposal(task_key)
        before = self._db_counts()

        result = create_scheduler_confirmation_from_proposal(
            self._build_request(
                task_key,
                proposal,
                item_hash="0" * 64,
                dry_run=False,
                confirm_create_confirmation=True,
            )
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "not_eligible")
        self.assertFalse(result["eligible"])
        self.assertTrue(result["reasons"], "should report at least one reason")
        self.assertIsNone(result["confirmation"])
        self.assertEqual(self._db_counts(), before)
        self.assertEqual(
            self._confirmation_counts(task_key),
            {"artifacts": 0, "events": 0},
        )
        confirmations_dir = self.artifact_root / "scheduler_confirmations"
        self.assertFalse(confirmations_dir.exists())

    def test_status_mismatch_does_not_write(self) -> None:
        task_key = "AT-K2-NEL-002"
        proposal = self._create_proposal(task_key)
        # Move the task out of queued after the proposal is recorded.
        self.store.update_task_status(task_key, "in_progress", source="test")
        before = self._db_counts()

        result = create_scheduler_confirmation_from_proposal(
            self._build_request(
                task_key,
                proposal,
                dry_run=False,
                confirm_create_confirmation=True,
            )
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "not_eligible")
        self.assertIn("task_status_mismatch", result["reasons"])
        self.assertEqual(self._db_counts(), before)
        self.assertEqual(
            self._confirmation_counts(task_key),
            {"artifacts": 0, "events": 0},
        )


class DuplicateConfirmationTests(_Base):
    def test_duplicate_confirmation_blocks_second_confirmation(self) -> None:
        task_key = "AT-K2-DUP-001"
        proposal = self._create_proposal(task_key)

        first = create_scheduler_confirmation_from_proposal(
            self._build_request(
                task_key,
                proposal,
                dry_run=False,
                confirm_create_confirmation=True,
            )
        )
        self.assertEqual(first["status"], "created")
        self.assertEqual(
            self._confirmation_counts(task_key),
            {"artifacts": 1, "events": 1},
        )

        second = create_scheduler_confirmation_from_proposal(
            self._build_request(
                task_key,
                proposal,
                dry_run=False,
                confirm_create_confirmation=True,
            )
        )
        self.assertFalse(second["ok"])
        self.assertEqual(second["status"], "not_eligible")
        self.assertIn(
            "duplicate_active_confirmation",
            second["reasons"],
        )
        self.assertEqual(
            self._confirmation_counts(task_key),
            {"artifacts": 1, "events": 1},
        )


class ArtifactSemanticsTests(_Base):
    def test_confirmation_artifact_is_not_execution_permission(self) -> None:
        task_key = "AT-K2-SEM-001"
        proposal = self._create_proposal(task_key)

        result = create_scheduler_confirmation_from_proposal(
            self._build_request(
                task_key,
                proposal,
                dry_run=False,
                confirm_create_confirmation=True,
            )
        )
        self.assertEqual(result["status"], "created")
        artifact_path = Path(result["confirmation"]["artifact_path"])
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))

        self.assertTrue(payload["not_execution_permission"])
        self.assertTrue(payload["not_verifier_report"])
        self.assertTrue(payload["not_handoff"])
        self.assertTrue(payload["not_runtime"])
        self.assertTrue(payload["requires_next_gate"])

        for flag, expected in CONFIRMATION_SAFETY_FLAGS.items():
            with self.subTest(flag=flag):
                if flag == "confirmation_created":
                    self.assertTrue(payload["safety"][flag])
                else:
                    self.assertEqual(payload["safety"][flag], expected)


class SourceContractTests(unittest.TestCase):
    def test_source_does_not_import_or_call_forbidden_runtime_paths(
        self,
    ) -> None:
        text = MODULE_PATH.read_text(encoding="utf-8")

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

        # `approved_task_runner` is permitted only as the safety-flag key
        # `approved_task_runner_called` asserting the runner was NOT
        # invoked. It must never appear as an import target or as a call.
        self.assertNotIn("from agent_taskflow.approved_task_runner", text)
        self.assertNotIn("import agent_taskflow.approved_task_runner", text)
        self.assertNotIn("approved_task_runner(", text)
        self.assertNotIn("approved_task_runner.", text)

        forbidden_imports = (
            "from scripts",
            "import scripts",
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
