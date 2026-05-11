from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


class ApiTests(unittest.TestCase):
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
        self._seed_data()

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
    ) -> TaskRecord:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        return TaskRecord(
            task_key=task_key,
            project=project,
            board=project,
            hermes_task_id=f"t_{task_key.lower().replace('-', '_')}",
            title=f"Task {task_key}",
            status=status,
            repo_path=self.repo_path,
            artifact_dir=artifact_dir,
        )

    def _seed_data(self) -> None:
        self.store.upsert_task(
            self.make_task("AT-0008", project="agent-taskflow", status="queued")
        )
        self.store.upsert_task(
            self.make_task("AT-0009", project="agent-taskflow", status="blocked")
        )
        self.store.upsert_task(
            self.make_task("BJ-0001", project="bullet-journal", status="queued")
        )

        run_id = self.store.create_executor_run(
            "AT-0008",
            "noop",
            model="fake-model",
            prompt_path=self.artifact_root / "AT-0008" / "implementation_prompt.md",
        )
        self.store.finish_executor_run(
            "AT-0008",
            run_id,
            executor="noop",
            status="completed",
            exit_code=0,
            summary="executor finished",
            log_path=self.artifact_root / "AT-0008" / "noop.log",
            artifacts={"log": self.artifact_root / "AT-0008" / "noop.log"},
        )

        self.store.record_task_artifact(
            "AT-0008",
            "spec",
            self.artifact_root / "AT-0008" / "spec.md",
        )

        self.store.record_validation_result(
            "AT-0008",
            "pytest",
            status="passed",
            exit_code=0,
            summary="tests passed",
            log_path=self.artifact_root / "AT-0008" / "pytest.log",
            artifacts={"log": self.artifact_root / "AT-0008" / "pytest.log"},
        )

        self.store.record_task_event(
            "AT-0008",
            "note",
            "reviewer",
            message="Approval recorded",
            payload={
                "kind": "approval_decision",
                "decision": "accepted",
                "reviewer": "human",
                "summary": "accepted after review",
                "reason": "looks good",
                "pr_url": "https://example.test/pr/8",
                "pr_number": 8,
                "merged_commit": "abc123",
                "env": {"API_TOKEN": "must-not-leak"},
                "approval_token": "must-not-leak",
            },
        )

    def assert_no_sensitive_keys(self, value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                lowered = str(key).lower()
                self.assertNotEqual(lowered, "env")
                self.assertNotEqual(lowered, "environment")
                self.assertNotIn("secret", lowered)
                if lowered != "task_key":
                    self.assertFalse(lowered.endswith("_token"), lowered)
                    self.assertNotEqual(lowered, "token")
                    self.assertNotEqual(lowered, "api_key")
                self.assert_no_sensitive_keys(item)
        elif isinstance(value, list):
            for item in value:
                self.assert_no_sensitive_keys(item)

    def test_health_returns_ok(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "ok", "service": "agent-taskflow-api"},
        )

    def test_projects_returns_list_response(self) -> None:
        response = self.client.get("/api/projects")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(
            [item["project"] for item in payload["items"]],
            ["agent-taskflow", "bullet-journal"],
        )

    def test_tasks_returns_list_response(self) -> None:
        response = self.client.get("/api/tasks")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 3)
        self.assertIn("items", payload)

    def test_tasks_can_filter_by_status(self) -> None:
        response = self.client.get("/api/tasks", params={"status": "queued"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(
            {item["task_key"] for item in payload["items"]},
            {"AT-0008", "BJ-0001"},
        )

    def test_tasks_can_filter_by_project(self) -> None:
        response = self.client.get(
            "/api/tasks",
            params={"project": "agent-taskflow"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(
            {item["project"] for item in payload["items"]},
            {"agent-taskflow"},
        )

    def test_task_detail_returns_item_response(self) -> None:
        response = self.client.get("/api/tasks/AT-0008")

        self.assertEqual(response.status_code, 200)
        item = response.json()["item"]
        self.assertEqual(item["task_key"], "AT-0008")
        self.assertEqual(item["project"], "agent-taskflow")
        self.assertEqual(item["status"], "queued")

    def test_missing_task_returns_404(self) -> None:
        response = self.client.get("/api/tasks/AT-4040")

        self.assertEqual(response.status_code, 404)
        self.assertIn("Task not found", response.json()["detail"])

    def test_executor_runs_return_metadata(self) -> None:
        response = self.client.get("/api/tasks/AT-0008/runs")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        run = payload["items"][0]
        self.assertEqual(run["task_key"], "AT-0008")
        self.assertEqual(run["executor"], "noop")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["exit_code"], 0)
        self.assertEqual(run["summary"], "executor finished")
        self.assertIsInstance(run["prompt_path"], str)
        self.assertIsInstance(run["log_path"], str)
        self.assertIsInstance(run["artifacts"]["log"], str)

    def test_artifacts_return_metadata(self) -> None:
        response = self.client.get("/api/tasks/AT-0008/artifacts")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        artifact = payload["items"][0]
        self.assertEqual(artifact["task_key"], "AT-0008")
        self.assertEqual(artifact["artifact_type"], "spec")
        self.assertIsInstance(artifact["path"], str)

    def test_validations_return_metadata(self) -> None:
        response = self.client.get("/api/tasks/AT-0008/validations")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        result = payload["items"][0]
        self.assertEqual(result["task_key"], "AT-0008")
        self.assertEqual(result["validator"], "pytest")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["exit_code"], 0)
        self.assertIsInstance(result["log_path"], str)
        self.assertIsInstance(result["artifacts"]["log"], str)

    def test_approvals_return_metadata_without_secrets(self) -> None:
        response = self.client.get("/api/tasks/AT-0008/approvals")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        decision = payload["items"][0]
        self.assertEqual(decision["task_key"], "AT-0008")
        self.assertEqual(decision["decision"], "accepted")
        self.assertEqual(decision["reviewer"], "human")
        self.assertEqual(decision["pr_number"], 8)
        self.assert_no_sensitive_keys(payload)

    def test_response_paths_are_strings(self) -> None:
        response = self.client.get("/api/tasks/AT-0008")

        self.assertEqual(response.status_code, 200)
        item = response.json()["item"]
        self.assertIsInstance(item["repo_path"], str)
        self.assertIsInstance(item["artifact_dir"], str)

    def test_api_responses_do_not_contain_env_or_secrets(self) -> None:
        for path in (
            "/api/tasks/AT-0008",
            "/api/tasks/AT-0008/runs",
            "/api/tasks/AT-0008/artifacts",
            "/api/tasks/AT-0008/validations",
            "/api/tasks/AT-0008/approvals",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assert_no_sensitive_keys(response.json())

    def test_read_only_endpoints_do_not_modify_task_status(self) -> None:
        before = self.store.get_task("AT-0008")
        self.assertIsNotNone(before)
        assert before is not None

        for path in (
            "/health",
            "/api/projects",
            "/api/tasks",
            "/api/tasks/AT-0008",
            "/api/tasks/AT-0008/runs",
            "/api/tasks/AT-0008/artifacts",
            "/api/tasks/AT-0008/validations",
            "/api/tasks/AT-0008/approvals",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)

        after = self.store.get_task("AT-0008")
        self.assertIsNotNone(after)
        assert after is not None
        self.assertEqual(after.status, before.status)

    def test_app_factory_uses_temp_db_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "custom-state.db"
            with TestClient(create_app(db_path)) as client:
                response = client.get("/api/tasks")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"items": [], "count": 0})
            self.assertTrue(db_path.exists())

    def test_invalid_status_filter_returns_400(self) -> None:
        response = self.client.get("/api/tasks", params={"status": "not-real"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid task status", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
