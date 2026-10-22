"""Tests for the Phase J2 read-only scheduler proposal readback API."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app
from agent_taskflow.ticket_fields_schema import migrate_ticket_fields
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


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

        # The API fails closed until the explicit Step 1 migration has run.
        migrate_ticket_fields(self.db_path)
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

    def test_list_returns_200_and_empty_items_on_empty_db(self) -> None:
        response = self.client.get("/api/scheduler/proposals")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "read_only")
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["count"], 0)
        self.assertTrue(payload["safety"]["read_only"])

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


if __name__ == "__main__":
    unittest.main()
