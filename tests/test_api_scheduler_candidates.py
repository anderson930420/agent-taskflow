"""Tests for the Phase H read-only scheduler candidate readback API."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app
from agent_taskflow.models import TaskRecord
from agent_taskflow.scheduler_candidate_discovery import (
    CANDIDATE_SAFETY_FLAGS,
    DISCOVERY_NOTE,
    DISCOVERY_SAFETY_FLAGS,
)
from agent_taskflow.store import TaskMirrorStore


TOP_LEVEL_SAFETY_REQUIRED_FALSE_FLAGS: tuple[str, ...] = (
    "db_written",
    "artifact_written",
    "proposal_created",
    "confirmation_created",
    "handoff_created",
    "verifier_report_created",
    "runtime_started",
    "approved_task_runner_called",
    "github_mutated",
    "approved",
    "merged",
    "cleanup_performed",
    "background_worker_started",
    "task_status_changed",
    "scheduler_loop_started",
)

CANDIDATE_SAFETY_REQUIRED_FALSE_FLAGS: tuple[str, ...] = (
    "proposal_created",
    "confirmation_created",
    "handoff_created",
    "runtime_started",
    "approved_task_runner_called",
    "github_mutated",
    "approved",
    "merged",
    "cleanup_performed",
    "background_worker_started",
)


class SchedulerCandidatesApiTests(unittest.TestCase):
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

    def make_task(
        self,
        task_key: str,
        *,
        project: str = "agent-taskflow",
        status: str = "queued",
        title: str | None = None,
    ) -> TaskRecord:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        return TaskRecord(
            task_key=task_key,
            project=project,
            board=project,
            title=title or f"Task {task_key}",
            status=status,
            repo_path=self.repo_path,
            artifact_dir=artifact_dir,
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-01T00:00:00Z",
        )

    def seed_task(
        self,
        task_key: str,
        *,
        project: str = "agent-taskflow",
        status: str = "queued",
    ) -> Path:
        record = self.make_task(task_key, project=project, status=status)
        self.store.upsert_task(record)
        assert record.artifact_dir is not None
        return record.artifact_dir

    def db_counts(self) -> dict[str, int]:
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

    def assert_top_level_safety_locked_down(self, payload: dict[str, Any]) -> None:
        safety = payload["safety"]
        self.assertTrue(safety["read_only"])
        for flag in TOP_LEVEL_SAFETY_REQUIRED_FALSE_FLAGS:
            self.assertFalse(safety[flag], flag)
        self.assertEqual(safety, dict(DISCOVERY_SAFETY_FLAGS))

    def assert_candidate_safety_locked_down(self, candidate: dict[str, Any]) -> None:
        safety = candidate["safety"]
        self.assertTrue(safety["read_only"])
        for flag in CANDIDATE_SAFETY_REQUIRED_FALSE_FLAGS:
            self.assertFalse(safety[flag], flag)
        self.assertEqual(safety, dict(CANDIDATE_SAFETY_FLAGS))

    # ---- list endpoint ----

    def test_list_returns_200_with_empty_candidates_when_no_tasks(self) -> None:
        response = self.client.get("/api/scheduler/candidates")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "read_only")
        self.assertEqual(payload["candidate_count"], 0)
        self.assertEqual(payload["candidates"], [])
        self.assert_top_level_safety_locked_down(payload)

    def test_list_returns_queued_task_as_candidate(self) -> None:
        self.seed_task("AT-CAND-001", status="queued")

        response = self.client.get("/api/scheduler/candidates")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["candidate_count"], 1)
        candidate = payload["candidates"][0]
        self.assertEqual(candidate["task_key"], "AT-CAND-001")
        self.assertEqual(candidate["project"], "agent-taskflow")
        self.assertEqual(candidate["status"], "queued")
        self.assertEqual(
            candidate["recommended_command_kind"], "create_task_execution_package"
        )
        self.assertTrue(candidate["candidate_ready"])
        self.assertEqual(candidate["required_next_gate"], "scheduler_proposal")
        self.assertEqual(
            candidate["required_operator_action"], "create_scheduler_proposal"
        )

    def test_list_discovery_note_states_not_execution_permission(self) -> None:
        self.seed_task("AT-CAND-001", status="queued")

        payload = self.client.get("/api/scheduler/candidates").json()

        self.assertEqual(payload["discovery_note"], DISCOVERY_NOTE)
        self.assertIn(
            "not execution permission", payload["discovery_note"].lower()
        )

    def test_list_top_level_safety_is_locked_down(self) -> None:
        self.seed_task("AT-CAND-001", status="queued")

        payload = self.client.get("/api/scheduler/candidates").json()

        self.assert_top_level_safety_locked_down(payload)

    def test_list_does_not_advertise_execution_allowed_field(self) -> None:
        self.seed_task("AT-CAND-001", status="queued")

        payload = self.client.get("/api/scheduler/candidates").json()

        self.assertNotIn("execution_allowed", payload)
        for candidate in payload["candidates"]:
            self.assertNotIn("execution_allowed", candidate)

    def test_list_candidate_safety_is_locked_down(self) -> None:
        self.seed_task("AT-CAND-001", status="queued")

        payload = self.client.get("/api/scheduler/candidates").json()
        candidate = payload["candidates"][0]
        self.assert_candidate_safety_locked_down(candidate)

    def test_list_includes_summary_with_execution_allowed_false(self) -> None:
        self.seed_task("AT-CAND-001", status="queued")

        payload = self.client.get("/api/scheduler/candidates").json()

        summary = payload["summary"]
        self.assertEqual(summary["candidate_count"], 1)
        self.assertFalse(summary["execution_allowed"])
        self.assertTrue(summary["requires_human_review"])

    def test_list_filters_by_task_key(self) -> None:
        self.seed_task("AT-CAND-001", status="queued")
        self.seed_task("AT-CAND-002", status="queued")

        payload = self.client.get(
            "/api/scheduler/candidates",
            params={"task_key": "AT-CAND-002"},
        ).json()

        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["candidates"][0]["task_key"], "AT-CAND-002")
        self.assertEqual(payload["filters"]["task_key"], "AT-CAND-002")

    def test_list_filters_by_project(self) -> None:
        self.seed_task("AT-CAND-001", status="queued", project="agent-taskflow")
        self.seed_task("BJ-CAND-001", status="queued", project="bullet-journal")

        payload = self.client.get(
            "/api/scheduler/candidates",
            params={"project": "bullet-journal"},
        ).json()

        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(
            payload["candidates"][0]["project"], "bullet-journal"
        )

    def test_list_filters_by_status(self) -> None:
        self.seed_task("AT-CAND-001", status="queued")
        self.seed_task("AT-CAND-002", status="blocked")

        payload = self.client.get(
            "/api/scheduler/candidates",
            params={"status": "blocked", "include_not_ready": True},
        ).json()

        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["candidates"][0]["status"], "blocked")
        self.assertEqual(payload["filters"]["status"], "blocked")

    def test_list_include_not_ready_filter_flag_is_passed_through(self) -> None:
        default_payload = self.client.get(
            "/api/scheduler/candidates",
        ).json()
        included_payload = self.client.get(
            "/api/scheduler/candidates",
            params={"include_not_ready": True},
        ).json()

        self.assertFalse(default_payload["filters"]["include_not_ready"])
        self.assertTrue(included_payload["filters"]["include_not_ready"])
        # Per-candidate read-only contract holds regardless of filter.
        for candidate in included_payload["candidates"]:
            self.assert_candidate_safety_locked_down(candidate)

    def test_list_include_no_action_filter_flag_is_passed_through(self) -> None:
        default_payload = self.client.get(
            "/api/scheduler/candidates",
        ).json()
        included_payload = self.client.get(
            "/api/scheduler/candidates",
            params={"include_no_action": True},
        ).json()

        self.assertFalse(default_payload["filters"]["include_no_action"])
        self.assertTrue(included_payload["filters"]["include_no_action"])
        for candidate in included_payload["candidates"]:
            self.assert_candidate_safety_locked_down(candidate)

    def test_list_limit_caps_candidate_count(self) -> None:
        for index in range(3):
            self.seed_task(f"AT-CAND-{index:03d}", status="queued")

        unlimited = self.client.get("/api/scheduler/candidates").json()
        limited = self.client.get(
            "/api/scheduler/candidates", params={"limit": 1}
        ).json()

        self.assertEqual(unlimited["candidate_count"], 3)
        self.assertEqual(limited["candidate_count"], 1)
        self.assertEqual(limited["filters"]["limit"], 1)

    def test_list_rejects_negative_limit_with_422(self) -> None:
        response = self.client.get(
            "/api/scheduler/candidates", params={"limit": -1}
        )
        self.assertEqual(response.status_code, 422)

    def test_list_rejects_invalid_status(self) -> None:
        response = self.client.get(
            "/api/scheduler/candidates",
            params={"status": "not-real"},
        )
        self.assertEqual(response.status_code, 422)

    # ---- task-specific endpoint ----

    def test_task_specific_returns_candidate(self) -> None:
        self.seed_task("AT-CAND-001", status="queued")

        response = self.client.get(
            "/api/tasks/AT-CAND-001/scheduler-candidate"
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["candidate_count"], 1)
        candidate = payload["candidates"][0]
        self.assertEqual(candidate["task_key"], "AT-CAND-001")
        self.assertEqual(
            candidate["recommended_command_kind"], "create_task_execution_package"
        )
        self.assert_candidate_safety_locked_down(candidate)
        self.assert_top_level_safety_locked_down(payload)

    def test_task_specific_returns_404_when_task_missing(self) -> None:
        response = self.client.get(
            "/api/tasks/AT-MISSING/scheduler-candidate"
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("Task not found", response.json()["detail"])

    def test_task_specific_includes_not_ready_candidate(self) -> None:
        self.seed_task("AT-CAND-001", status="blocked")

        response = self.client.get(
            "/api/tasks/AT-CAND-001/scheduler-candidate"
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertGreaterEqual(payload["candidate_count"], 1)

    # ---- read-only behavior ----

    def test_list_does_not_mutate_db(self) -> None:
        self.seed_task("AT-CAND-001", status="queued")
        before = self.db_counts()

        for _ in range(3):
            response = self.client.get("/api/scheduler/candidates")
            self.assertEqual(response.status_code, 200)

        after = self.db_counts()
        self.assertEqual(after, before)

    def test_list_does_not_mutate_task_status(self) -> None:
        self.seed_task("AT-CAND-001", status="queued")
        before = self.store.get_task("AT-CAND-001")
        assert before is not None

        self.client.get("/api/scheduler/candidates")
        self.client.get("/api/tasks/AT-CAND-001/scheduler-candidate")

        after = self.store.get_task("AT-CAND-001")
        assert after is not None
        self.assertEqual(after.status, before.status)

    def test_list_does_not_mutate_task_events_or_artifacts(self) -> None:
        self.seed_task("AT-CAND-001", status="queued")
        with sqlite3.connect(self.db_path) as conn:
            events_before = conn.execute(
                "SELECT COUNT(*) FROM task_events"
            ).fetchone()[0]
            artifacts_before = conn.execute(
                "SELECT COUNT(*) FROM task_artifacts"
            ).fetchone()[0]

        self.client.get("/api/scheduler/candidates")
        self.client.get("/api/tasks/AT-CAND-001/scheduler-candidate")

        with sqlite3.connect(self.db_path) as conn:
            events_after = conn.execute(
                "SELECT COUNT(*) FROM task_events"
            ).fetchone()[0]
            artifacts_after = conn.execute(
                "SELECT COUNT(*) FROM task_artifacts"
            ).fetchone()[0]

        self.assertEqual(events_after, events_before)
        self.assertEqual(artifacts_after, artifacts_before)

    def test_validations_endpoint_unchanged_after_candidate_calls(self) -> None:
        self.seed_task("AT-CAND-001", status="queued")
        self.client.get("/api/scheduler/candidates")
        self.client.get("/api/tasks/AT-CAND-001/scheduler-candidate")

        response = self.client.get("/api/tasks/AT-CAND-001/validations")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": [], "count": 0})

    # ---- method guards ----

    def test_list_rejects_post(self) -> None:
        response = self.client.post("/api/scheduler/candidates", json={})
        self.assertEqual(response.status_code, 405)

    def test_list_rejects_patch(self) -> None:
        response = self.client.patch("/api/scheduler/candidates", json={})
        self.assertEqual(response.status_code, 405)

    def test_list_rejects_delete(self) -> None:
        response = self.client.delete("/api/scheduler/candidates")
        self.assertEqual(response.status_code, 405)

    def test_list_rejects_put(self) -> None:
        response = self.client.put("/api/scheduler/candidates", json={})
        self.assertEqual(response.status_code, 405)

    def test_task_specific_rejects_post(self) -> None:
        self.seed_task("AT-CAND-001", status="queued")
        response = self.client.post(
            "/api/tasks/AT-CAND-001/scheduler-candidate", json={}
        )
        self.assertEqual(response.status_code, 405)

    def test_task_specific_rejects_patch(self) -> None:
        self.seed_task("AT-CAND-001", status="queued")
        response = self.client.patch(
            "/api/tasks/AT-CAND-001/scheduler-candidate", json={}
        )
        self.assertEqual(response.status_code, 405)

    def test_task_specific_rejects_delete(self) -> None:
        self.seed_task("AT-CAND-001", status="queued")
        response = self.client.delete(
            "/api/tasks/AT-CAND-001/scheduler-candidate"
        )
        self.assertEqual(response.status_code, 405)


if __name__ == "__main__":
    unittest.main()
