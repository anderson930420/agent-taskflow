"""API coverage for read-only executor metadata exposure."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


class ExecutorMetadataApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db_path = root / "taskflow.db"
        self.repo_path = root / "repo"
        self.artifact_dir = root / "artifacts"
        self.repo_path.mkdir()
        self.artifact_dir.mkdir()

        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-0016",
                project="agent-taskflow",
                board="mission-control",
                hermes_task_id="hermes-0016",
                title="Show executor metadata",
                status="queued",
                repo_path=self.repo_path,
                artifact_dir=self.artifact_dir,
                executor="pi",
                model="MiniMax-M2.7",
                provider="minimax",
                tools=["read", "write"],
                pi_bin="/usr/local/bin/pi",
            )
        )
        self.client = TestClient(create_app(db_path=self.db_path))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_task_detail_includes_executor_metadata(self) -> None:
        response = self.client.get("/api/tasks/AT-0016")
        self.assertEqual(response.status_code, 200)

        item = response.json()["item"]
        self.assertEqual(item["executor"], "pi")
        self.assertEqual(item["model"], "MiniMax-M2.7")
        self.assertEqual(item["provider"], "minimax")
        self.assertEqual(item["tools"], ["read", "write"])
        self.assertEqual(item["pi_bin"], "/usr/local/bin/pi")

    def test_task_list_includes_executor_metadata(self) -> None:
        response = self.client.get("/api/tasks")
        self.assertEqual(response.status_code, 200)

        items = response.json()["items"]
        item = next(task for task in items if task["task_key"] == "AT-0016")
        self.assertEqual(item["executor"], "pi")
        self.assertEqual(item["model"], "MiniMax-M2.7")
        self.assertEqual(item["provider"], "minimax")
        self.assertEqual(item["tools"], ["read", "write"])
        self.assertEqual(item["pi_bin"], "/usr/local/bin/pi")

    def test_task_response_does_not_include_secret_fields(self) -> None:
        response = self.client.get("/api/tasks/AT-0016")
        self.assertEqual(response.status_code, 200)

        item = response.json()["item"]
        forbidden = {
            "env",
            "environment",
            "secret",
            "secrets",
            "token",
            "api_key",
            "access_token",
            "refresh_token",
            "password",
            "authorization",
            "MINIMAX_API_KEY",
            "OPENCODE_API_KEY",
        }
        self.assertTrue(forbidden.isdisjoint(item.keys()))


if __name__ == "__main__":
    unittest.main()
