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

    def _create_confirmed_proposal(self, task_key: str) -> dict[str, Any]:
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
        return payload

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

    def test_confirmed_j1_proposal_appears_in_readback(self) -> None:
        self._create_confirmed_proposal("AT-J2-001")

        result = list_scheduler_proposal_readbacks(self.store)

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["task_key"], "AT-J2-001")

    def test_item_includes_proposal_id(self) -> None:
        created = self._create_confirmed_proposal("AT-J2-002")

        item = self._readback_item("AT-J2-002")

        self.assertEqual(item["proposal_id"], created["proposal"]["proposal_id"])

    def test_item_includes_proposal_hash(self) -> None:
        created = self._create_confirmed_proposal("AT-J2-003")

        item = self._readback_item("AT-J2-003")

        self.assertEqual(item["proposal_hash"], created["proposal"]["proposal_hash"])

    def test_item_includes_proposal_item_id(self) -> None:
        created = self._create_confirmed_proposal("AT-J2-004")

        item = self._readback_item("AT-J2-004")

        self.assertEqual(
            item["proposal_item_id"],
            created["proposal"]["proposal_item_id"],
        )

    def test_item_includes_item_hash(self) -> None:
        created = self._create_confirmed_proposal("AT-J2-005")

        item = self._readback_item("AT-J2-005")

        self.assertEqual(item["item_hash"], created["proposal"]["item_hash"])

    def test_item_includes_recommended_command_kind(self) -> None:
        created = self._create_confirmed_proposal("AT-J2-006")

        item = self._readback_item("AT-J2-006")

        self.assertEqual(
            item["recommended_command_kind"],
            created["proposal"]["recommended_command_kind"],
        )

    def test_item_includes_artifact_path(self) -> None:
        created = self._create_confirmed_proposal("AT-J2-007")

        item = self._readback_item("AT-J2-007")

        self.assertEqual(
            item["artifact_path"],
            created["proposal"]["proposal_artifact_path"],
        )

    def test_item_says_not_execution_permission(self) -> None:
        self._create_confirmed_proposal("AT-J2-008")

        item = self._readback_item("AT-J2-008")

        self.assertTrue(item["not_execution_permission"])
        self.assertTrue(item["safety"]["not_execution_permission"])

    def test_item_says_not_confirmation(self) -> None:
        self._create_confirmed_proposal("AT-J2-009")

        item = self._readback_item("AT-J2-009")

        self.assertTrue(item["not_confirmation"])
        self.assertTrue(item["safety"]["not_confirmation"])

    def test_item_says_requires_human_confirmation(self) -> None:
        self._create_confirmed_proposal("AT-J2-010")

        item = self._readback_item("AT-J2-010")

        self.assertTrue(item["requires_human_confirmation"])

    def test_top_level_safety_says_read_only(self) -> None:
        result = list_scheduler_proposal_readbacks(self.store)

        self.assertTrue(result["safety"]["read_only"])

    def test_top_level_safety_says_proposal_created_false(self) -> None:
        self._create_confirmed_proposal("AT-J2-011")

        result = list_scheduler_proposal_readbacks(self.store)

        self.assertFalse(result["safety"]["proposal_created"])

    def test_top_level_safety_says_no_confirmation_handoff_runtime_or_runner(self) -> None:
        self._create_confirmed_proposal("AT-J2-012")

        result = list_scheduler_proposal_readbacks(self.store)

        self.assert_top_level_safety(result)

    def test_task_key_filter_works(self) -> None:
        self._create_confirmed_proposal("AT-J2-013")
        self._create_confirmed_proposal("AT-J2-014")

        result = list_scheduler_proposal_readbacks(
            self.store,
            task_key="AT-J2-014",
        )

        self.assertEqual(result["filters"]["task_key"], "AT-J2-014")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["task_key"], "AT-J2-014")

    def test_limit_works(self) -> None:
        self._create_confirmed_proposal("AT-J2-015")
        self._create_confirmed_proposal("AT-J2-016")

        zero = list_scheduler_proposal_readbacks(self.store, limit=0)
        one = list_scheduler_proposal_readbacks(self.store, limit=1)

        self.assertEqual(zero["items"], [])
        self.assertEqual(zero["count"], 0)
        self.assertEqual(one["count"], 1)

    def test_malformed_or_missing_artifact_json_does_not_crash(self) -> None:
        created = self._create_confirmed_proposal("AT-J2-017")
        artifact_path = Path(created["proposal"]["proposal_artifact_path"])

        artifact_path.write_text("{not-json", encoding="utf-8")
        malformed = self._readback_item("AT-J2-017")
        self.assertIn("artifact_json_malformed", malformed["readback_warnings"])

        artifact_path.unlink()
        missing = self._readback_item("AT-J2-017")
        self.assertIn("artifact_file_missing", missing["readback_warnings"])

    def test_repeated_readback_does_not_mutate_db(self) -> None:
        self._create_confirmed_proposal("AT-J2-018")
        before = self._db_counts()

        list_scheduler_proposal_readbacks(self.store)
        list_task_scheduler_proposal_readbacks(self.store, "AT-J2-018")

        self.assertEqual(self._db_counts(), before)

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
            "from agent_taskflow.scheduler_confirmations",
            "import agent_taskflow.scheduler_confirmations",
            "from agent_taskflow.scheduler_confirmation_verifier",
            "from agent_taskflow.intake_runner_handoff",
            "from agent_taskflow.queued_task_handoff",
            "from agent_taskflow.approved_task_runner",
            "from agent_taskflow.executors",
            "from agent_taskflow.validators",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
