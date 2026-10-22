from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.models import TaskRecord
from agent_taskflow.scheduler_proposal_readback import (
    READBACK_NOTE,
    READBACK_SAFETY_FLAGS,
    READBACK_SCHEMA_VERSION,
    list_scheduler_proposal_readbacks,
    list_task_scheduler_proposal_readbacks,
)
from agent_taskflow.scheduler_proposals import (
    PROPOSAL_ARTIFACT_TYPE,
    PROPOSAL_EVENT_TYPE,
    SCHEMA_VERSION as PROPOSAL_SCHEMA_VERSION,
)
from agent_taskflow.store import TaskMirrorStore


class SchedulerProposalReadbackTests(unittest.TestCase):
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
                title=f"Proposal readback {task_key}",
                status=status,
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )

    def _readback_item(self, task_key: str) -> dict[str, Any]:
        result = list_task_scheduler_proposal_readbacks(self.store, task_key)
        self.assertEqual(result["count"], 1)
        return result["items"][0]

    def assert_top_level_safety(self, result: dict[str, Any]) -> None:
        self.assertEqual(result["safety"], dict(READBACK_SAFETY_FLAGS))
        self.assertTrue(result["safety"]["read_only"])
        self.assertFalse(result["safety"]["proposal_created"])
        self.assertFalse(result["safety"]["confirmation_created"])
        self.assertFalse(result["safety"]["verifier_report_created"])
        self.assertFalse(result["safety"]["handoff_created"])
        self.assertFalse(result["safety"]["runtime_started"])
        self.assertFalse(result["safety"]["approved_task_runner_called"])
        self.assertFalse(result["safety"]["executor_started"])
        self.assertFalse(result["safety"]["validators_started"])

    def test_empty_readback_returns_count_zero(self) -> None:
        result = list_scheduler_proposal_readbacks(self.store)

        self.assertTrue(result["ok"])
        self.assertEqual(result["schema_version"], READBACK_SCHEMA_VERSION)
        self.assertEqual(result["mode"], "read_only")
        self.assertEqual(result["readback_note"], READBACK_NOTE)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["count"], 0)
        self.assert_top_level_safety(result)

    def test_top_level_safety_says_read_only(self) -> None:
        result = list_scheduler_proposal_readbacks(self.store)

        self.assertTrue(result["safety"]["read_only"])

    def test_event_without_artifact_row_still_returns_item(self) -> None:
        self._seed_task("AT-J2-019")
        self.store.record_task_event(
            "AT-J2-019",
            PROPOSAL_EVENT_TYPE,
            "scheduler_proposals",
            message="Scheduler proposal event only",
            payload={
                "kind": PROPOSAL_EVENT_TYPE,
                "proposal_id": "proposal-event-only",
                "proposal_hash": "a" * 64,
                "proposal_item_id": "AT-J2-019:create_task_execution_package",
                "item_hash": "b" * 64,
                "task_key": "AT-J2-019",
                "recommended_command_kind": "create_task_execution_package",
                "artifact_path": str(self.artifact_root / "missing.json"),
                "schema_version": PROPOSAL_SCHEMA_VERSION,
            },
        )

        item = self._readback_item("AT-J2-019")

        self.assertEqual(item["proposal_id"], "proposal-event-only")
        self.assertIn("artifact_row_missing", item["readback_warnings"])

    def test_artifact_without_event_uses_artifact_json_fallback(self) -> None:
        self._seed_task("AT-J2-020")
        artifact_path = self.artifact_root / "scheduler_proposals" / "fallback.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(
                {
                    "schema_version": PROPOSAL_SCHEMA_VERSION,
                    "proposal_id": "proposal-artifact-only",
                    "proposal_hash": "c" * 64,
                    "artifact_path": str(artifact_path),
                    "items": [
                        {
                            "task_key": "AT-J2-020",
                            "proposal_item_id": (
                                "AT-J2-020:create_task_execution_package"
                            ),
                            "item_hash": "d" * 64,
                            "recommended_command_kind": (
                                "create_task_execution_package"
                            ),
                        }
                    ],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self.store.record_task_artifact(
            "AT-J2-020",
            PROPOSAL_ARTIFACT_TYPE,
            artifact_path,
        )

        item = self._readback_item("AT-J2-020")

        self.assertEqual(item["proposal_id"], "proposal-artifact-only")
        self.assertEqual(item["proposal_hash"], "c" * 64)
        self.assertEqual(item["item_hash"], "d" * 64)
        self.assertIn("event_created_at", item["missing_evidence"])

    def test_readback_module_does_not_import_mutation_surfaces(self) -> None:
        module_path = (
            Path(__file__).resolve().parents[1]
            / "agent_taskflow"
            / "scheduler_proposal_readback.py"
        )
        source = module_path.read_text(encoding="utf-8")

        for forbidden in (
            "from agent_taskflow.approved_task_runner",
            "from agent_taskflow.executors",
            "from agent_taskflow.validators",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
