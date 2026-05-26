"""Tests for the read-only scheduler confirmation eligibility helper."""

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
from agent_taskflow.scheduler_confirmation_eligibility import (
    CONFIRMATION_ARTIFACT_TYPE,
    CONFIRMATION_EVENT_TYPE,
    ELIGIBILITY_MODE,
    ELIGIBILITY_SAFETY_FLAGS,
    ELIGIBILITY_SCHEMA_VERSION,
    SchedulerConfirmationEligibilityRequest,
    check_scheduler_confirmation_eligibility,
)
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    REPO_ROOT / "agent_taskflow" / "scheduler_confirmation_eligibility.py"
)


class SchedulerConfirmationEligibilityTests(unittest.TestCase):
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

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _seed_task(self, task_key: str, *, status: str = "queued") -> None:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title=f"Eligibility {task_key}",
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
                "worktrees": conn.execute(
                    "SELECT COUNT(*) FROM task_worktrees"
                ).fetchone()[0],
            }

    def _build_request(
        self,
        task_key: str,
        proposal: dict[str, Any],
        **overrides: Any,
    ) -> SchedulerConfirmationEligibilityRequest:
        kwargs: dict[str, Any] = {
            "db_path": self.db_path,
            "task_key": task_key,
            "proposal_item_id": proposal["proposal_item_id"],
            "proposal_hash": proposal["proposal_hash"],
            "proposal_id": proposal["proposal_id"],
            "item_hash": proposal["item_hash"],
            "recommended_command_kind": proposal["recommended_command_kind"],
            "proposal_artifact_path": Path(proposal["proposal_artifact_path"]),
        }
        kwargs.update(overrides)
        return SchedulerConfirmationEligibilityRequest(**kwargs)

    def _record_fake_confirmation(
        self,
        task_key: str,
        *,
        proposal_hash: str,
        proposal_item_id: str,
        item_hash: str,
    ) -> Path:
        confirmation_dir = (
            self.artifact_root / "scheduler_confirmations" / task_key
        )
        confirmation_dir.mkdir(parents=True, exist_ok=True)
        path = confirmation_dir / "scheduler_confirmation.json"
        path.write_text(
            json.dumps(
                {
                    "task_key": task_key,
                    "proposal_hash": proposal_hash,
                    "proposal_item_id": proposal_item_id,
                    "item_hash": item_hash,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self.store.record_task_artifact(
            task_key,
            CONFIRMATION_ARTIFACT_TYPE,
            path,
        )
        self.store.record_task_event(
            task_key,
            CONFIRMATION_EVENT_TYPE,
            "test_fixture",
            payload={
                "kind": CONFIRMATION_EVENT_TYPE,
                "task_key": task_key,
                "proposal_hash": proposal_hash,
                "proposal_item_id": proposal_item_id,
                "item_hash": item_hash,
            },
        )
        return path

    # ------------------------------------------------------------------
    # tests
    # ------------------------------------------------------------------

    def test_eligible_for_valid_existing_proposal_item(self) -> None:
        task_key = "AT-K1-001"
        proposal = self._create_proposal(task_key)

        result = check_scheduler_confirmation_eligibility(
            self._build_request(task_key, proposal)
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["schema_version"], ELIGIBILITY_SCHEMA_VERSION)
        self.assertEqual(result["mode"], ELIGIBILITY_MODE)
        self.assertEqual(result["task_key"], task_key)
        self.assertTrue(result["eligible"], result)
        self.assertEqual(result["reasons"], [])

        checks = result["checks"]
        for name in (
            "proposal_exists",
            "proposal_artifact_exists",
            "proposal_hash_matches_artifact",
            "proposal_item_id_exists",
            "item_hash_matches_selected_item",
            "task_still_exists",
            "task_status_matches_expected",
            "recommended_command_kind_matches",
            "duplicate_active_confirmation_absent",
        ):
            with self.subTest(check=name):
                self.assertTrue(checks[name])

        safety = result["safety"]
        self.assertEqual(safety, dict(ELIGIBILITY_SAFETY_FLAGS))
        self.assertTrue(safety["read_only"])
        self.assertFalse(safety["confirmation_created"])
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

        proposal_view = result["proposal"]
        self.assertEqual(proposal_view["proposal_id"], proposal["proposal_id"])
        self.assertEqual(
            proposal_view["proposal_hash"], proposal["proposal_hash"]
        )
        self.assertEqual(
            proposal_view["proposal_item_id"], proposal["proposal_item_id"]
        )
        self.assertEqual(proposal_view["item_hash"], proposal["item_hash"])
        self.assertEqual(
            proposal_view["recommended_command_kind"],
            proposal["recommended_command_kind"],
        )
        self.assertEqual(
            proposal_view["proposal_artifact_path"],
            proposal["proposal_artifact_path"],
        )

        current = result["current"]
        self.assertTrue(current["task_exists"])
        self.assertEqual(current["task_status"], "queued")
        self.assertEqual(current["expected_status"], "queued")
        self.assertEqual(current["duplicate_confirmation_count"], 0)

    def test_read_only_helper_does_not_mutate_db(self) -> None:
        task_key = "AT-K1-002"
        proposal = self._create_proposal(task_key)
        before = self._db_counts()

        check_scheduler_confirmation_eligibility(
            self._build_request(task_key, proposal)
        )
        check_scheduler_confirmation_eligibility(
            self._build_request(task_key, proposal)
        )

        self.assertEqual(self._db_counts(), before)

    def test_missing_proposal_item_is_not_eligible(self) -> None:
        task_key = "AT-K1-003"
        proposal = self._create_proposal(task_key)

        result = check_scheduler_confirmation_eligibility(
            self._build_request(
                task_key,
                proposal,
                proposal_item_id=f"{task_key}:no_such_kind",
                item_hash=None,
                recommended_command_kind=None,
            )
        )

        self.assertFalse(result["eligible"])
        self.assertIn("proposal_item_not_found", result["reasons"])
        self.assertFalse(result["checks"]["proposal_exists"])

    def test_blocks_item_hash_mismatch(self) -> None:
        task_key = "AT-K1-004"
        proposal = self._create_proposal(task_key)

        result = check_scheduler_confirmation_eligibility(
            self._build_request(
                task_key,
                proposal,
                item_hash="0" * 64,
            )
        )

        self.assertFalse(result["eligible"])
        self.assertTrue(
            "item_hash_mismatch" in result["reasons"]
            or "proposal_item_not_found" in result["reasons"],
            result["reasons"],
        )

    def test_blocks_proposal_hash_mismatch(self) -> None:
        task_key = "AT-K1-005"
        proposal = self._create_proposal(task_key)

        result = check_scheduler_confirmation_eligibility(
            self._build_request(
                task_key,
                proposal,
                proposal_hash="0" * 64,
            )
        )

        self.assertFalse(result["eligible"])
        self.assertTrue(
            "proposal_hash_mismatch" in result["reasons"]
            or "proposal_item_not_found" in result["reasons"],
            result["reasons"],
        )

    def test_blocks_task_status_mismatch(self) -> None:
        task_key = "AT-K1-006"
        proposal = self._create_proposal(task_key)

        self.store.update_task_status(
            task_key,
            "blocked",
            blocked_reason="test blocker",
        )

        result = check_scheduler_confirmation_eligibility(
            self._build_request(task_key, proposal)
        )

        self.assertFalse(result["eligible"])
        self.assertIn("task_status_mismatch", result["reasons"])
        self.assertFalse(result["checks"]["task_status_matches_expected"])
        self.assertEqual(result["current"]["task_status"], "blocked")
        self.assertEqual(result["current"]["expected_status"], "queued")

    def test_blocks_recommended_command_kind_mismatch(self) -> None:
        task_key = "AT-K1-007"
        proposal = self._create_proposal(task_key)

        result = check_scheduler_confirmation_eligibility(
            self._build_request(
                task_key,
                proposal,
                recommended_command_kind="branch_push_review",
            )
        )

        self.assertFalse(result["eligible"])
        self.assertTrue(
            "recommended_command_kind_mismatch" in result["reasons"]
            or "proposal_item_not_found" in result["reasons"],
            result["reasons"],
        )

    def test_blocks_missing_artifact_file(self) -> None:
        task_key = "AT-K1-008"
        proposal = self._create_proposal(task_key)
        artifact_path = Path(proposal["proposal_artifact_path"])

        artifact_path.unlink()

        result = check_scheduler_confirmation_eligibility(
            self._build_request(task_key, proposal)
        )

        self.assertFalse(result["eligible"])
        self.assertIn("proposal_artifact_file_missing", result["reasons"])
        self.assertFalse(result["checks"]["proposal_artifact_exists"])

    def test_blocks_duplicate_active_confirmation(self) -> None:
        task_key = "AT-K1-009"
        proposal = self._create_proposal(task_key)

        self._record_fake_confirmation(
            task_key,
            proposal_hash=proposal["proposal_hash"],
            proposal_item_id=proposal["proposal_item_id"],
            item_hash=proposal["item_hash"],
        )

        result = check_scheduler_confirmation_eligibility(
            self._build_request(task_key, proposal)
        )

        self.assertFalse(result["eligible"])
        self.assertIn("duplicate_active_confirmation", result["reasons"])
        self.assertFalse(
            result["checks"]["duplicate_active_confirmation_absent"]
        )
        self.assertGreaterEqual(
            result["current"]["duplicate_confirmation_count"], 1
        )

    def test_source_does_not_import_or_call_forbidden_runtime_paths(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")

        strict_forbidden = (
            "create_event",
            "create_draft",
            "send_email",
            "executor_run_started",
            "validation_result",
            "create_scheduler_confirmation",
            "runtime_execution_started",
            "subprocess",
            "requests.post",
            "gh pr",
        )
        for token in strict_forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

        # `approved_task_runner` is permitted only as the safety-flag KEY
        # asserting the runner was NOT invoked. It must never appear as an
        # import target or as a function call.
        self.assertNotIn("from agent_taskflow.approved_task_runner", source)
        self.assertNotIn("import agent_taskflow.approved_task_runner", source)
        self.assertNotIn("approved_task_runner(", source)
        self.assertNotIn("approved_task_runner.", source)

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
        for token in forbidden_imports:
            with self.subTest(token=token):
                self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
