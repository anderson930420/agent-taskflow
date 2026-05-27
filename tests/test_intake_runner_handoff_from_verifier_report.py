"""Tests for Level 5A intake runner handoff helpers."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_taskflow.intake_runner_handoff_from_verifier_report import (
    HANDOFF_ARTIFACT_TYPE,
    HANDOFF_EVENT_TYPE,
    HANDOFF_SAFETY_FLAGS,
    HANDOFF_SCHEMA_VERSION,
    HANDOFF_SOURCE,
    VERIFIER_REPORT_CONSUMED_EVENT_TYPE,
    IntakeRunnerHandoffFromVerifierReportError,
    IntakeRunnerHandoffFromVerifierReportRequest,
    check_intake_runner_handoff_binding,
    create_intake_runner_handoff_from_verifier_report,
)
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
    SchedulerConfirmationVerifierReportRequest,
    create_scheduler_confirmation_verifier_report,
)
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    REPO_ROOT / "agent_taskflow" / "intake_runner_handoff_from_verifier_report.py"
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
                title=f"L5A handoff {task_key}",
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
                operator_note="L5A confirmation unit test",
            )
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "created")
        return result["confirmation"]

    def _create_verifier_report(self, task_key: str) -> dict[str, Any]:
        confirmation = self._create_confirmation(task_key)
        result = create_scheduler_confirmation_verifier_report(
            SchedulerConfirmationVerifierReportRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                task_key=task_key,
                confirmation_id=confirmation["confirmation_id"],
                proposal_hash=confirmation["proposal_hash"],
                proposal_item_id=confirmation["proposal_item_id"],
                item_hash=confirmation["item_hash"],
                recommended_command_kind=confirmation["recommended_command_kind"],
                confirmation_artifact_path=Path(confirmation["artifact_path"]),
                dry_run=False,
                confirm_create_verifier_report=True,
                operator="verifier-operator",
                operator_note="L5A verifier report unit test",
            )
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "created")
        return result["verifier_report"]

    def _build_request(
        self,
        task_key: str,
        verifier_report: dict[str, Any],
        **overrides: Any,
    ) -> IntakeRunnerHandoffFromVerifierReportRequest:
        kwargs: dict[str, Any] = {
            "db_path": self.db_path,
            "artifact_root": self.artifact_root,
            "task_key": task_key,
            "verifier_report_id": verifier_report["verifier_report_id"],
            "confirmation_id": verifier_report["confirmation_id"],
            "proposal_hash": verifier_report["proposal_hash"],
            "proposal_item_id": verifier_report["proposal_item_id"],
            "item_hash": verifier_report["item_hash"],
            "recommended_command_kind": verifier_report[
                "recommended_command_kind"
            ],
            "verifier_report_artifact_path": Path(verifier_report["artifact_path"]),
            "operator": "handoff-operator",
            "operator_note": "handoff unit test",
        }
        kwargs.update(overrides)
        return IntakeRunnerHandoffFromVerifierReportRequest(**kwargs)

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

    def _handoff_counts(self, task_key: str) -> dict[str, int]:
        artifacts = [
            a
            for a in self.store.list_task_artifacts(task_key)
            if a.artifact_type == HANDOFF_ARTIFACT_TYPE
        ]
        events = [
            e
            for e in self.store.list_task_events(task_key)
            if e.event_type == HANDOFF_EVENT_TYPE
        ]
        return {"artifacts": len(artifacts), "events": len(events)}

    def _consumption_events(self, task_key: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for event in self.store.list_task_events(task_key):
            if event.event_type != VERIFIER_REPORT_CONSUMED_EVENT_TYPE:
                continue
            events.append(json.loads(event.payload_json or "{}"))
        return events


class DryRunTests(_Base):
    def test_dry_run_valid_verifier_report_writes_nothing(self) -> None:
        task_key = "AT-L5A-DRY-001"
        verifier_report = self._create_verifier_report(task_key)
        before = self._db_counts()

        result = create_intake_runner_handoff_from_verifier_report(
            self._build_request(task_key, verifier_report, dry_run=True)
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["mode"], "dry_run")
        self.assertTrue(result["would_create_handoff"])
        self.assertTrue(result["binding"]["handoff_allowed"])
        self.assertEqual(result["binding"]["reasons"], [])
        self.assertEqual(result["schema_version"], HANDOFF_SCHEMA_VERSION)
        self.assertEqual(result["source"], HANDOFF_SOURCE)

        handoff = result["handoff"]
        self.assertEqual(
            handoff["verifier_report_id"],
            verifier_report["verifier_report_id"],
        )
        self.assertEqual(handoff["confirmation_id"], verifier_report["confirmation_id"])
        self.assertEqual(handoff["proposal_hash"], verifier_report["proposal_hash"])
        self.assertEqual(handoff["proposal_item_id"], verifier_report["proposal_item_id"])
        self.assertEqual(handoff["item_hash"], verifier_report["item_hash"])
        self.assertEqual(handoff["operator"], "handoff-operator")
        self.assertEqual(handoff["operator_note"], "handoff unit test")

        self.assertEqual(self._db_counts(), before)
        self.assertEqual(self._handoff_counts(task_key), {"artifacts": 0, "events": 0})
        self.assertEqual(self._consumption_events(task_key), [])
        self.assertFalse((self.artifact_root / "intake_runner_handoffs").exists())

        for key, expected in HANDOFF_SAFETY_FLAGS.items():
            with self.subTest(key=key):
                self.assertEqual(result["safety"][key], expected)


class ConfirmedFlagTests(_Base):
    def test_confirmed_mode_requires_explicit_flag(self) -> None:
        task_key = "AT-L5A-FLAG-001"
        verifier_report = self._create_verifier_report(task_key)

        with self.assertRaises(IntakeRunnerHandoffFromVerifierReportError):
            create_intake_runner_handoff_from_verifier_report(
                self._build_request(
                    task_key,
                    verifier_report,
                    dry_run=False,
                    confirm_create_handoff=False,
                )
            )

        self.assertEqual(self._handoff_counts(task_key), {"artifacts": 0, "events": 0})


class ConfirmedCreationTests(_Base):
    def test_confirmed_mode_creates_handoff_artifact_and_event_only(self) -> None:
        task_key = "AT-L5A-CRT-001"
        verifier_report = self._create_verifier_report(task_key)
        before = self._db_counts()

        result = create_intake_runner_handoff_from_verifier_report(
            self._build_request(
                task_key,
                verifier_report,
                dry_run=False,
                confirm_create_handoff=True,
            )
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["mode"], "confirmed")
        self.assertTrue(result["handoff_allowed"])

        handoff = result["handoff"]
        handoff_id = handoff["handoff_id"]
        artifact_path = Path(handoff["artifact_path"])
        self.assertEqual(
            artifact_path.parent,
            self.artifact_root / "intake_runner_handoffs" / handoff_id,
        )
        self.assertTrue(artifact_path.exists())

        on_disk = json.loads(artifact_path.read_text(encoding="utf-8"))
        for key in (
            "schema_version",
            "handoff_id",
            "created_at",
            "source",
            "mode",
            "task_key",
            "verifier_report_id",
            "confirmation_id",
            "proposal_hash",
            "proposal_item_id",
            "item_hash",
            "recommended_command_kind",
            "verifier_report_artifact_path",
            "confirmation_artifact_path",
            "proposal_artifact_path",
            "db_path",
            "artifact_root",
            "artifact_path",
            "operator",
            "operator_note",
            "handoff_allowed",
            "binding_summary",
            "reasons",
            "warnings",
            "checks",
            "safety",
        ):
            with self.subTest(key=key):
                self.assertIn(key, on_disk)
        self.assertEqual(on_disk["mode"], "confirmed")
        self.assertEqual(
            on_disk["verifier_report_id"],
            verifier_report["verifier_report_id"],
        )
        self.assertEqual(on_disk["confirmation_id"], verifier_report["confirmation_id"])
        self.assertEqual(on_disk["proposal_hash"], verifier_report["proposal_hash"])
        self.assertEqual(on_disk["item_hash"], verifier_report["item_hash"])
        self.assertTrue(on_disk["handoff_allowed"])

        self.assertEqual(self._handoff_counts(task_key), {"artifacts": 1, "events": 1})
        after = self._db_counts()
        self.assertEqual(after["artifacts"], before["artifacts"] + 1)
        self.assertEqual(after["events"], before["events"] + 2)
        self.assertEqual(after["tasks"], before["tasks"])
        self.assertEqual(after["worktrees"], before["worktrees"])

        events = [
            e
            for e in self.store.list_task_events(task_key)
            if e.event_type == HANDOFF_EVENT_TYPE
        ]
        self.assertEqual(len(events), 1)
        event_payload = json.loads(events[0].payload_json or "{}")
        self.assertEqual(event_payload["kind"], HANDOFF_EVENT_TYPE)
        self.assertEqual(event_payload["handoff_id"], handoff_id)
        self.assertTrue(event_payload["handoff_allowed"])
        self.assertTrue(event_payload["not_execution_permission"])
        self.assertTrue(event_payload["not_runtime"])
        self.assertFalse(event_payload["approved_task_runner_called"])
        self.assertTrue(event_payload["requires_runtime_preflight"])
        self.assertTrue(event_payload["requires_next_gate"])

        consumption_events = self._consumption_events(task_key)
        self.assertEqual(len(consumption_events), 1)
        consumed = consumption_events[0]
        self.assertEqual(consumed["kind"], VERIFIER_REPORT_CONSUMED_EVENT_TYPE)
        self.assertEqual(
            consumed["consumed_artifact_type"],
            "scheduler_confirmation_verifier_report",
        )
        self.assertEqual(consumed["consumer_artifact_type"], HANDOFF_ARTIFACT_TYPE)
        self.assertEqual(
            consumed["verifier_report_id"],
            verifier_report["verifier_report_id"],
        )
        self.assertEqual(consumed["handoff_id"], handoff_id)
        self.assertEqual(consumed["confirmation_id"], verifier_report["confirmation_id"])
        self.assertEqual(consumed["proposal_hash"], verifier_report["proposal_hash"])
        self.assertEqual(consumed["proposal_item_id"], verifier_report["proposal_item_id"])
        self.assertEqual(consumed["item_hash"], verifier_report["item_hash"])
        self.assertTrue(consumed["single_use_enforced"])
        self.assertTrue(consumed["not_approval"])
        self.assertTrue(consumed["not_merge"])
        self.assertTrue(consumed["not_cleanup"])

        task = self.store.get_task(task_key)
        assert task is not None
        self.assertEqual(task.status, "queued")


class NotAllowedTests(_Base):
    def test_not_allowed_does_not_write(self) -> None:
        task_key = "AT-L5A-NA-001"
        verifier_report = self._create_verifier_report(task_key)
        before = self._db_counts()

        result = create_intake_runner_handoff_from_verifier_report(
            self._build_request(
                task_key,
                verifier_report,
                item_hash="0" * 64,
                dry_run=False,
                confirm_create_handoff=True,
            )
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "not_allowed")
        self.assertFalse(result["handoff_allowed"])
        self.assertTrue(result["reasons"])
        self.assertEqual(self._db_counts(), before)
        self.assertEqual(self._handoff_counts(task_key), {"artifacts": 0, "events": 0})
        self.assertEqual(self._consumption_events(task_key), [])

    def test_failed_downstream_creation_does_not_consume_verifier_report(self) -> None:
        task_key = "AT-L5A-NA-002"
        verifier_report = self._create_verifier_report(task_key)

        with mock.patch.object(
            TaskMirrorStore,
            "record_task_artifact",
            side_effect=RuntimeError("artifact write failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "artifact write failed"):
                create_intake_runner_handoff_from_verifier_report(
                    self._build_request(
                        task_key,
                        verifier_report,
                        dry_run=False,
                        confirm_create_handoff=True,
                    )
                )

        self.assertEqual(self._consumption_events(task_key), [])


class DuplicateTests(_Base):
    def test_duplicate_handoff_blocks_second_handoff(self) -> None:
        task_key = "AT-L5A-DUP-001"
        verifier_report = self._create_verifier_report(task_key)

        first = create_intake_runner_handoff_from_verifier_report(
            self._build_request(
                task_key,
                verifier_report,
                dry_run=False,
                confirm_create_handoff=True,
            )
        )
        self.assertEqual(first["status"], "created")
        self.assertEqual(self._handoff_counts(task_key), {"artifacts": 1, "events": 1})

        second = create_intake_runner_handoff_from_verifier_report(
            self._build_request(
                task_key,
                verifier_report,
                dry_run=False,
                confirm_create_handoff=True,
            )
        )
        self.assertFalse(second["ok"])
        self.assertEqual(second["status"], "not_allowed")
        self.assertIn("duplicate_active_handoff", second["reasons"])
        self.assertIn(
            "scheduler_confirmation_verifier_report_already_consumed",
            second["reasons"],
        )
        self.assertEqual(
            second["binding"]["current"]["duplicate_handoff_count"],
            2,
        )
        self.assertEqual(
            second["binding"]["current"][
                "scheduler_confirmation_verifier_report_consumed_count"
            ],
            1,
        )
        self.assertEqual(self._handoff_counts(task_key), {"artifacts": 1, "events": 1})
        self.assertEqual(len(self._consumption_events(task_key)), 1)


class ArtifactSafetyTests(_Base):
    def test_handoff_artifact_safety_flags(self) -> None:
        task_key = "AT-L5A-SAFE-001"
        verifier_report = self._create_verifier_report(task_key)

        result = create_intake_runner_handoff_from_verifier_report(
            self._build_request(
                task_key,
                verifier_report,
                dry_run=False,
                confirm_create_handoff=True,
            )
        )
        artifact_path = Path(result["handoff"]["artifact_path"])
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))

        self.assertTrue(payload["not_execution_permission"])
        self.assertTrue(payload["not_runtime"])
        self.assertFalse(payload["approved_task_runner_called"])
        self.assertTrue(payload["requires_runtime_preflight"])
        self.assertTrue(payload["requires_next_gate"])
        self.assertTrue(payload["safety"]["handoff_created"])
        self.assertFalse(payload["safety"]["runtime_started"])
        self.assertFalse(payload["safety"]["approved_task_runner_called"])
        self.assertFalse(payload["safety"]["executor_started"])
        self.assertFalse(payload["safety"]["validators_started"])
        self.assertFalse(payload["safety"]["github_mutated"])
        self.assertFalse(payload["safety"]["approved"])
        self.assertFalse(payload["safety"]["merged"])
        self.assertFalse(payload["safety"]["cleanup_performed"])
        self.assertFalse(payload["safety"]["background_worker_started"])
        self.assertTrue(payload["safety"]["requires_runtime_preflight"])
        self.assertTrue(payload["safety"]["requires_next_gate"])

    def test_binding_helper_is_read_only(self) -> None:
        task_key = "AT-L5A-RO-001"
        verifier_report = self._create_verifier_report(task_key)
        before = self._db_counts()

        result = check_intake_runner_handoff_binding(
            self._build_request(task_key, verifier_report)
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["handoff_allowed"], result)
        self.assertTrue(result["eligible_for_handoff"])
        self.assertEqual(result["reasons"], [])
        self.assertEqual(self._db_counts(), before)
        self.assertEqual(self._handoff_counts(task_key), {"artifacts": 0, "events": 0})


class SourceContractTests(unittest.TestCase):
    def test_source_does_not_import_or_call_forbidden_runtime_paths(self) -> None:
        text = MODULE_PATH.read_text(encoding="utf-8")

        forbidden_imports = (
            "from agent_taskflow.api",
            "import agent_taskflow.api",
            "from agent_taskflow.approved_task_runner",
            "import agent_taskflow.approved_task_runner",
            "from agent_taskflow.queued_task_handoff",
            "import agent_taskflow.queued_task_handoff",
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
            "run_queued_task_handoff(",
            "approved_task_runner(",
            "approved_task_runner.",
            "runtime_execution_started(",
            "executor_run_started(",
            "validation_result(",
        )
        for needle in strict_forbidden_calls:
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
