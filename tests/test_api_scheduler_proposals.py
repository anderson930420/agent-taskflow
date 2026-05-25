"""Tests for the Phase J2 read-only scheduler proposal readback API."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app
from agent_taskflow.models import TaskRecord
from agent_taskflow.scheduler_candidate_proposals import (
    SchedulerCandidateProposalRequest,
    create_scheduler_proposal_from_candidate,
)
from agent_taskflow.store import TaskMirrorStore


FORBIDDEN_ARTIFACT_TYPES = (
    "scheduler_confirmation",
    "scheduler_confirmation_verifier_report",
    "verifier_report",
    "intake_runner_handoff",
    "runtime_handoff_execution",
    "validation_result",
    "approval_decision",
    "merge_recorded",
    "cleanup",
)

FORBIDDEN_EVENT_TYPES = (
    "scheduler_confirmation_created",
    "scheduler_confirmation_verifier_report",
    "verifier_report",
    "intake_runner_handoff_created",
    "runtime_preflight_finished",
    "runtime_execution_started",
    "runtime_execution_finished",
    "executor_run_started",
    "executor_run_finished",
    "validation_result",
    "approval_decision",
    "merge_recorded",
    "cleanup",
)

FORBIDDEN_PAYLOAD_MARKERS = (
    "scheduler_confirmation_created",
    "scheduler_confirmation_verifier_report",
    "intake_runner_handoff_created",
    "runtime_execution_started",
    "runtime_execution_finished",
    "approved_task_runner",
    "executor_run_started",
    "executor_run_finished",
    "validation_result",
)


class SchedulerProposalsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.artifact_root = self.root / "artifacts"
        self.repo_path.mkdir()
        self.artifact_root.mkdir()

        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

        self.client_context = TestClient(create_app(self.db_path))
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.tmp.cleanup()

    def _seed_task(self, task_key: str, *, status: str = "queued") -> None:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title=f"API proposal readback {task_key}",
                status=status,
                repo_path=self.repo_path,
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

    def _forbidden_side_effect_counts(self) -> dict[str, int]:
        with sqlite3.connect(self.db_path) as conn:
            artifact_placeholders = ",".join("?" for _ in FORBIDDEN_ARTIFACT_TYPES)
            event_placeholders = ",".join("?" for _ in FORBIDDEN_EVENT_TYPES)
            marker_clause = " OR ".join(
                "payload_json LIKE ?" for _ in FORBIDDEN_PAYLOAD_MARKERS
            )
            artifacts = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM task_artifacts
                WHERE artifact_type IN ({artifact_placeholders})
                """,
                FORBIDDEN_ARTIFACT_TYPES,
            ).fetchone()[0]
            events = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM task_events
                WHERE event_type IN ({event_placeholders})
                """,
                FORBIDDEN_EVENT_TYPES,
            ).fetchone()[0]
            payload_markers = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM task_events
                WHERE payload_json IS NOT NULL
                  AND ({marker_clause})
                """,
                tuple(f"%{marker}%" for marker in FORBIDDEN_PAYLOAD_MARKERS),
            ).fetchone()[0]
        return {
            "artifacts": artifacts,
            "events": events,
            "payload_markers": payload_markers,
        }

    def _all_dict_keys(self, value: Any) -> set[str]:
        keys: set[str] = set()
        if isinstance(value, dict):
            for key, item in value.items():
                keys.add(str(key))
                keys.update(self._all_dict_keys(item))
        elif isinstance(value, list):
            for item in value:
                keys.update(self._all_dict_keys(item))
        return keys

    def test_list_returns_200_and_empty_items_on_empty_db(self) -> None:
        response = self.client.get("/api/scheduler/proposals")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "read_only")
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["count"], 0)
        self.assertTrue(payload["safety"]["read_only"])

    def test_generated_proposal_appears_in_list_endpoint(self) -> None:
        created = self._create_confirmed_proposal("AT-J2-API-001")

        response = self.client.get("/api/scheduler/proposals")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        item = payload["items"][0]
        self.assertEqual(item["task_key"], "AT-J2-API-001")
        self.assertEqual(item["proposal_id"], created["proposal"]["proposal_id"])
        self.assertEqual(item["proposal_hash"], created["proposal"]["proposal_hash"])

    def test_list_endpoint_filters_by_task_key(self) -> None:
        self._create_confirmed_proposal("AT-J2-API-002")
        self._create_confirmed_proposal("AT-J2-API-003")

        response = self.client.get(
            "/api/scheduler/proposals",
            params={"task_key": "AT-J2-API-003"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["filters"]["task_key"], "AT-J2-API-003")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["task_key"], "AT-J2-API-003")

    def test_task_endpoint_returns_proposals_for_task(self) -> None:
        created = self._create_confirmed_proposal("AT-J2-API-004")

        response = self.client.get(
            "/api/tasks/AT-J2-API-004/scheduler-proposals"
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(
            payload["items"][0]["proposal_item_id"],
            created["proposal"]["proposal_item_id"],
        )

    def test_task_endpoint_returns_empty_for_task_without_proposals(self) -> None:
        self._seed_task("AT-J2-API-005")

        response = self.client.get(
            "/api/tasks/AT-J2-API-005/scheduler-proposals"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])
        self.assertEqual(response.json()["count"], 0)

    def test_task_endpoint_returns_404_for_unknown_task(self) -> None:
        response = self.client.get(
            "/api/tasks/AT-J2-API-MISSING/scheduler-proposals"
        )

        self.assertEqual(response.status_code, 404)

    def test_post_scheduler_proposals_returns_405(self) -> None:
        response = self.client.post("/api/scheduler/proposals", json={})

        self.assertEqual(response.status_code, 405)

    def test_patch_scheduler_proposals_returns_405(self) -> None:
        response = self.client.patch("/api/scheduler/proposals", json={})

        self.assertEqual(response.status_code, 405)

    def test_delete_scheduler_proposals_returns_405(self) -> None:
        response = self.client.delete("/api/scheduler/proposals")

        self.assertEqual(response.status_code, 405)

    def test_post_task_scheduler_proposals_returns_405(self) -> None:
        self._seed_task("AT-J2-API-006")

        response = self.client.post(
            "/api/tasks/AT-J2-API-006/scheduler-proposals",
            json={},
        )

        self.assertEqual(response.status_code, 405)

    def test_repeated_get_does_not_mutate_db_row_counts(self) -> None:
        self._create_confirmed_proposal("AT-J2-API-007")
        before = self._db_counts()

        self.client.get("/api/scheduler/proposals")
        self.client.get("/api/tasks/AT-J2-API-007/scheduler-proposals")

        self.assertEqual(self._db_counts(), before)

    def test_response_says_not_execution_permission(self) -> None:
        self._create_confirmed_proposal("AT-J2-API-008")

        payload = self.client.get("/api/scheduler/proposals").json()

        self.assertTrue(payload["safety"]["not_execution_permission"])
        self.assertTrue(payload["items"][0]["not_execution_permission"])
        self.assertTrue(payload["items"][0]["safety"]["not_execution_permission"])
        self.assertIn(
            "not execution permission",
            payload["readback_note"].lower(),
        )

    def test_response_says_proposal_is_not_confirmation(self) -> None:
        self._create_confirmed_proposal("AT-J2-API-009")

        payload = self.client.get("/api/scheduler/proposals").json()

        self.assertTrue(payload["safety"]["not_confirmation"])
        self.assertTrue(payload["items"][0]["not_confirmation"])
        self.assertIn("not confirmation", payload["readback_note"].lower())

    def test_get_does_not_create_confirmation_artifact_or_event(self) -> None:
        self._create_confirmed_proposal("AT-J2-API-010")

        self.client.get("/api/scheduler/proposals")

        counts = self._forbidden_side_effect_counts()
        self.assertEqual(counts["artifacts"], 0)
        self.assertEqual(counts["events"], 0)

    def test_get_does_not_create_verifier_report_artifact_or_event(self) -> None:
        self._create_confirmed_proposal("AT-J2-API-011")

        self.client.get("/api/scheduler/proposals")

        counts = self._forbidden_side_effect_counts()
        self.assertEqual(counts["artifacts"], 0)
        self.assertEqual(counts["events"], 0)

    def test_get_does_not_create_intake_runner_handoff_artifact_or_event(self) -> None:
        self._create_confirmed_proposal("AT-J2-API-012")

        self.client.get("/api/scheduler/proposals")

        counts = self._forbidden_side_effect_counts()
        self.assertEqual(counts["artifacts"], 0)
        self.assertEqual(counts["events"], 0)

    def test_get_does_not_create_runtime_audit_events(self) -> None:
        self._create_confirmed_proposal("AT-J2-API-013")

        self.client.get("/api/scheduler/proposals")

        self.assertEqual(self._forbidden_side_effect_counts()["events"], 0)

    def test_get_does_not_create_runner_executor_or_validator_events(self) -> None:
        self._create_confirmed_proposal("AT-J2-API-014")

        self.client.get("/api/scheduler/proposals")

        self.assertEqual(
            self._forbidden_side_effect_counts()["payload_markers"],
            0,
        )

    def test_api_does_not_expose_creation_action_fields(self) -> None:
        self._create_confirmed_proposal("AT-J2-API-015")

        payload = self.client.get("/api/scheduler/proposals").json()
        keys = self._all_dict_keys(payload)

        for forbidden_key in (
            "confirm",
            "run",
            "execute",
            "create_handoff",
            "approve",
            "merge",
            "cleanup",
            "action",
            "actions",
        ):
            self.assertNotIn(forbidden_key, keys)


if __name__ == "__main__":
    unittest.main()
