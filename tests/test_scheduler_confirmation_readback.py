"""Tests for the K3 read-only scheduler confirmation readback helper."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord
from agent_taskflow.scheduler_confirmation_from_proposal import (
    CONFIRMATION_ARTIFACT_TYPE,
    CONFIRMATION_EVENT_TYPE,
)
from agent_taskflow.scheduler_confirmation_readback import (
    CONFIRMATION_READBACK_SAFETY_FLAGS,
    SchedulerConfirmationReadbackError,
    list_scheduler_confirmation_readbacks,
    list_task_scheduler_confirmation_readbacks,
)
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "agent_taskflow" / "scheduler_confirmation_readback.py"


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
                title=f"K3 readback {task_key}",
                status=status,
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )


class HelperReadbackTests(_Base):

    def test_empty_readback_returns_count_zero(self) -> None:
        result = list_scheduler_confirmation_readbacks(self.store)

        self.assertTrue(result["ok"])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["safety"], dict(CONFIRMATION_READBACK_SAFETY_FLAGS))


class LimitTests(_Base):
    def test_negative_limit_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            list_scheduler_confirmation_readbacks(self.store, limit=-1)
        with self.assertRaises(ValueError):
            list_task_scheduler_confirmation_readbacks(
                self.store, "AT-K3-LIM-001", limit=-2
            )


class DegradedEvidenceTests(_Base):

    def test_event_without_artifact_row_or_file_returns_warning(self) -> None:
        task_key = "AT-K3-DEG-003"
        self._seed_task(task_key)
        missing_path = self.artifact_root / "scheduler_confirmations" / "missing.json"

        self.store.record_task_event(
            task_key,
            CONFIRMATION_EVENT_TYPE,
            "scheduler_confirmation_from_proposal",
            message="Scheduler confirmation event only",
            payload={
                "kind": CONFIRMATION_EVENT_TYPE,
                "confirmation_id": "confirmation-event-only",
                "proposal_id": "proposal-event-only",
                "proposal_hash": "a" * 64,
                "proposal_item_id": f"{task_key}:create_task_execution_package",
                "item_hash": "b" * 64,
                "task_key": task_key,
                "recommended_command_kind": "create_task_execution_package",
                "artifact_path": str(missing_path),
                "schema_version": "scheduler_confirmation_from_proposal.v1",
                "not_execution_permission": True,
                "not_verifier_report": True,
                "not_handoff": True,
                "not_runtime": True,
                "requires_next_gate": True,
            },
        )

        result = list_task_scheduler_confirmation_readbacks(self.store, task_key)

        self.assertEqual(result["count"], 1)
        item = result["items"][0]
        self.assertEqual(item["confirmation_id"], "confirmation-event-only")
        self.assertIn("artifact_row_missing", item["readback_warnings"])
        self.assertIn("artifact_file_missing", item["readback_warnings"])

    def test_artifact_without_event_still_appears(self) -> None:
        task_key = "AT-K3-DEG-004"
        self._seed_task(task_key)
        artifact_path = (
            self.artifact_root / "scheduler_confirmations" / "fallback.json"
        )
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(
                {
                    "schema_version": "scheduler_confirmation_from_proposal.v1",
                    "confirmation_id": "confirmation-artifact-only",
                    "proposal_id": "proposal-artifact-only",
                    "proposal_hash": "c" * 64,
                    "proposal_item_id": (
                        f"{task_key}:create_task_execution_package"
                    ),
                    "item_hash": "d" * 64,
                    "recommended_command_kind": "create_task_execution_package",
                    "task_key": task_key,
                    "artifact_path": str(artifact_path),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self.store.record_task_artifact(
            task_key,
            CONFIRMATION_ARTIFACT_TYPE,
            artifact_path,
        )

        result = list_task_scheduler_confirmation_readbacks(self.store, task_key)

        self.assertEqual(result["count"], 1)
        item = result["items"][0]
        self.assertEqual(item["confirmation_id"], "confirmation-artifact-only")
        self.assertEqual(item["proposal_hash"], "c" * 64)
        self.assertEqual(item["item_hash"], "d" * 64)
        self.assertIn("event_created_at", item["missing_evidence"])


class SourceContractTests(unittest.TestCase):
    def test_source_does_not_import_or_call_forbidden_write_paths(self) -> None:
        text = MODULE_PATH.read_text(encoding="utf-8")

        forbidden_strings = (
            "create_scheduler_confirmation_from_proposal",
            "confirm_create_confirmation",
            "executor_run_started",
            "validation_result",
            "runtime_execution_started",
            "create_verifier_report",
            "intake_runner_handoff",
            "subprocess",
            "requests.post",
            "gh pr",
        )
        for needle in forbidden_strings:
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)

        # `approved_task_runner` is permitted only as part of the safety-flag
        # key `approved_task_runner_called` asserting the runner was NOT
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


class ReadbackErrorTests(unittest.TestCase):
    def test_readback_error_subclass(self) -> None:
        self.assertTrue(issubclass(SchedulerConfirmationReadbackError, RuntimeError))


if __name__ == "__main__":
    unittest.main()
