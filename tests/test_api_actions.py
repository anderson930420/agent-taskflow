from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Sequence

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app
from agent_taskflow.dispatcher import DispatcherResult
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore


class FakeDispatcher:
    def __init__(self, result_status: str = "waiting_approval") -> None:
        self.calls: list[dict[str, Any]] = []
        self.result_status = result_status

    def dispatch_task(
        self,
        task_key: str,
        *,
        executor_name: str | None = None,
        model: str | None = None,
        dry_run: bool = False,
    ) -> DispatcherResult:
        self.calls.append(
            {
                "task_key": task_key,
                "executor_name": executor_name,
                "model": model,
                "dry_run": dry_run,
            }
        )
        summary = (
            "Task dispatched successfully"
            if self.result_status != "blocked"
            else "Executor failed"
        )
        return DispatcherResult(
            task_key=task_key,
            status=self.result_status,
            summary=summary,
            executor_status="completed" if self.result_status != "blocked" else "failed",
            validator_statuses={"pytest": "passed"} if self.result_status != "blocked" else {},
            blocked_reason="Executor failed" if self.result_status == "blocked" else None,
        )


class ApiActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.worktree_path = self.repo_path / ".worktrees" / "AT-0009"
        self.artifact_dir = self.root / "artifacts" / "AT-0009"
        self.repo_path.mkdir()
        self.artifact_dir.mkdir(parents=True)

        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

        self.dispatchers: list[FakeDispatcher] = []
        self.dispatcher_factory_calls: list[dict[str, Any]] = []

        def dispatcher_factory(
            store: TaskMirrorStore,
            validators: Sequence[str],
        ) -> FakeDispatcher:
            dispatcher = FakeDispatcher()
            self.dispatchers.append(dispatcher)
            self.dispatcher_factory_calls.append(
                {"store_db_path": store.db_path, "validators": tuple(validators)}
            )
            return dispatcher

        self.client_context = TestClient(
            create_app(self.db_path, dispatcher_factory=dispatcher_factory)
        )
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.tmp.cleanup()

    def task_payload(
        self,
        task_key: str = "AT-0009",
        *,
        repo_path: str | None = None,
        worktree_path: str | None = None,
        artifact_dir: str | None = None,
    ) -> dict[str, Any]:
        return {
            "task_key": task_key,
            "project": "agent-taskflow",
            "repo_path": repo_path or str(self.repo_path),
            "worktree_path": worktree_path or str(self.worktree_path),
            "artifact_dir": artifact_dir or str(self.artifact_dir),
            "executor": "opencode",
            "model": "fake-model",
            "validator": "pytest",
        }

    def add_task(
        self,
        task_key: str = "AT-0009",
        *,
        status: str = "queued",
    ) -> None:
        artifact_dir = self.root / "artifacts" / task_key
        worktree_path = self.repo_path / ".worktrees" / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                hermes_task_id=f"t_{task_key.lower().replace('-', '_')}",
                title=f"Task {task_key}",
                status=status,
                repo_path=self.repo_path,
                artifact_dir=artifact_dir,
            )
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=task_key,
                repo_path=self.repo_path,
                worktree_path=worktree_path,
                branch=f"task/{task_key}",
                base_branch="main",
                status="active",
            )
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

    def test_create_task_creates_queued_task_and_worktree_record(self) -> None:
        response = self.client.post("/api/tasks", json=self.task_payload())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "create")
        self.assertEqual(payload["task_key"], "AT-0009")
        self.assertEqual(payload["status"], "queued")

        task = self.store.get_task("AT-0009")
        worktree = self.store.get_task_worktree("AT-0009")
        self.assertIsNotNone(task)
        self.assertIsNotNone(worktree)
        assert task is not None
        assert worktree is not None
        self.assertEqual(task.status, "queued")
        self.assertEqual(task.repo_path, self.repo_path)
        self.assertEqual(task.artifact_dir, self.artifact_dir)
        self.assertEqual(worktree.worktree_path, self.worktree_path)

    def test_duplicate_task_key_returns_conflict(self) -> None:
        self.add_task()

        response = self.client.post("/api/tasks", json=self.task_payload())

        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["action"], "create")
        self.assertIn("already exists", payload["message"])

    def test_create_rejects_relative_repo_path(self) -> None:
        response = self.client.post(
            "/api/tasks",
            json=self.task_payload(repo_path="relative/repo"),
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("repo_path must be absolute", response.json()["detail"])

    def test_create_rejects_relative_worktree_path(self) -> None:
        response = self.client.post(
            "/api/tasks",
            json=self.task_payload(worktree_path="relative/worktree"),
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("worktree_path must be absolute", response.json()["detail"])

    def test_create_rejects_relative_artifact_dir(self) -> None:
        response = self.client.post(
            "/api/tasks",
            json=self.task_payload(artifact_dir="relative/artifacts"),
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("artifact_dir must be absolute", response.json()["detail"])

    def test_create_rejects_worktree_equal_to_main_repo(self) -> None:
        response = self.client.post(
            "/api/tasks",
            json=self.task_payload(worktree_path=str(self.repo_path)),
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("main repo path", response.json()["detail"])

    def test_create_rejects_worktree_outside_repo_worktrees(self) -> None:
        response = self.client.post(
            "/api/tasks",
            json=self.task_payload(worktree_path=str(self.root / "outside")),
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn(".worktrees", response.json()["detail"])

    def test_create_does_not_call_dispatcher_or_worker(self) -> None:
        response = self.client.post("/api/tasks", json=self.task_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.dispatcher_factory_calls, [])
        self.assertEqual(self.dispatchers, [])

    def test_start_missing_task_returns_404(self) -> None:
        response = self.client.post("/api/tasks/AT-4040/start", json={})

        self.assertEqual(response.status_code, 404)
        self.assertIn("Task not found", response.json()["detail"])

    def test_start_calls_fake_dispatcher_and_returns_result(self) -> None:
        self.add_task()

        response = self.client.post(
            "/api/tasks/AT-0009/start",
            json={
                "validators": ["pytest"],
                "executor": "manual",
                "model": "fake-model",
                "dry_run": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "start")
        self.assertEqual(payload["status"], "waiting_approval")
        self.assertEqual(payload["item"]["executor_status"], "completed")
        self.assertEqual(len(self.dispatchers), 1)
        self.assertEqual(self.dispatcher_factory_calls[0]["validators"], ("pytest",))
        self.assertEqual(
            self.dispatchers[0].calls,
            [
                {
                    "task_key": "AT-0009",
                    "executor_name": "manual",
                    "model": "fake-model",
                    "dry_run": True,
                }
            ],
        )

    def test_start_terminal_statuses_are_rejected_without_dispatcher(self) -> None:
        for status in ("accepted", "rejected", "cleaned"):
            with self.subTest(status=status):
                task_key = f"AT-{len(status):04d}"
                self.add_task(task_key, status=status)

                response = self.client.post(f"/api/tasks/{task_key}/start", json={})

                self.assertEqual(response.status_code, 409)
                self.assertFalse(response.json()["ok"])

        self.assertEqual(self.dispatcher_factory_calls, [])

    def test_approve_waiting_approval_records_decision_and_accepts(self) -> None:
        self.add_task(status="waiting_approval")

        response = self.client.post(
            "/api/tasks/AT-0009/approve",
            json={"decided_by": "human", "notes": "looks good"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "accepted")
        task = self.store.get_task("AT-0009")
        approvals = self.store.list_approval_decisions("AT-0009")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "accepted")
        self.assertEqual(approvals[-1]["decision"], "accepted")
        self.assertEqual(approvals[-1]["decided_by"], "human")
        self.assertEqual(approvals[-1]["notes"], "looks good")

    def test_approve_rejects_worker_identity(self) -> None:
        self.add_task(status="waiting_approval")

        response = self.client.post(
            "/api/tasks/AT-0009/approve",
            json={"decided_by": "worker", "notes": "auto-approved"},
        )

        self.assertEqual(response.status_code, 422)
        task = self.store.get_task("AT-0009")
        approvals = self.store.list_approval_decisions("AT-0009")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")
        self.assertEqual(approvals, [])

    def test_approve_rejects_pi_identity(self) -> None:
        self.add_task(status="waiting_approval")

        response = self.client.post(
            "/api/tasks/AT-0009/approve",
            json={"decided_by": "pi", "notes": "pi self-approve"},
        )

        self.assertEqual(response.status_code, 422)
        task = self.store.get_task("AT-0009")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")
        self.assertEqual(self.store.list_approval_decisions("AT-0009"), [])

    def test_approve_rejects_agent_identity(self) -> None:
        self.add_task(status="waiting_approval")

        response = self.client.post(
            "/api/tasks/AT-0009/approve",
            json={"decided_by": "agent", "notes": "agent self-approve"},
        )

        self.assertEqual(response.status_code, 422)
        task = self.store.get_task("AT-0009")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")
        self.assertEqual(self.store.list_approval_decisions("AT-0009"), [])

    def test_approve_rejects_system_identity(self) -> None:
        self.add_task(status="waiting_approval")

        response = self.client.post(
            "/api/tasks/AT-0009/approve",
            json={"decided_by": "system", "notes": "system self-approve"},
        )

        self.assertEqual(response.status_code, 422)
        task = self.store.get_task("AT-0009")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")
        self.assertEqual(self.store.list_approval_decisions("AT-0009"), [])

    def test_approve_rejects_empty_decided_by(self) -> None:
        self.add_task(status="waiting_approval")

        response = self.client.post(
            "/api/tasks/AT-0009/approve",
            json={"decided_by": "", "notes": "empty identity"},
        )

        self.assertEqual(response.status_code, 422)
        task = self.store.get_task("AT-0009")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")
        self.assertEqual(self.store.list_approval_decisions("AT-0009"), [])

    def test_approve_rejects_missing_decided_by(self) -> None:
        self.add_task(status="waiting_approval")

        response = self.client.post(
            "/api/tasks/AT-0009/approve",
            json={"notes": "no identity"},
        )

        self.assertEqual(response.status_code, 422)
        task = self.store.get_task("AT-0009")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")
        self.assertEqual(self.store.list_approval_decisions("AT-0009"), [])

    def test_reject_still_works_after_human_identity_enforcement(self) -> None:
        self.add_task(status="waiting_approval")

        response = self.client.post(
            "/api/tasks/AT-0009/reject",
            json={"decided_by": "human", "notes": "needs changes"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "rejected")
        task = self.store.get_task("AT-0009")
        approvals = self.store.list_approval_decisions("AT-0009")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "rejected")
        self.assertEqual(approvals[-1]["decision"], "rejected")
        self.assertEqual(approvals[-1]["decided_by"], "human")
        self.assertEqual(approvals[-1]["notes"], "needs changes")

    def test_block_still_works_after_human_identity_enforcement(self) -> None:
        self.add_task(status="queued")

        response = self.client.post(
            "/api/tasks/AT-0009/block",
            json={"blocked_reason": "missing approval token"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        task = self.store.get_task("AT-0009")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "blocked")
        self.assertEqual(task.blocked_reason, "missing approval token")

    def test_approve_queued_task_is_rejected(self) -> None:
        self.add_task(status="queued")

        response = self.client.post(
            "/api/tasks/AT-0009/approve",
            json={"decided_by": "human", "notes": "not ready"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["ok"])
        task = self.store.get_task("AT-0009")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "queued")
        self.assertEqual(self.store.list_approval_decisions("AT-0009"), [])

    def test_reject_blocked_task_is_allowed(self) -> None:
        self.add_task(status="blocked")

        response = self.client.post(
            "/api/tasks/AT-0009/reject",
            json={"decided_by": "human", "notes": "blocked too long"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "rejected")

    def test_block_task_updates_status_and_reason(self) -> None:
        self.add_task(status="queued")

        response = self.client.post(
            "/api/tasks/AT-0009/block",
            json={"blocked_reason": "missing approval token"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        task = self.store.get_task("AT-0009")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "blocked")
        self.assertEqual(task.blocked_reason, "missing approval token")

    def test_block_requires_reason(self) -> None:
        self.add_task(status="queued")

        response = self.client.post(
            "/api/tasks/AT-0009/block",
            json={"blocked_reason": "   "},
        )

        self.assertEqual(response.status_code, 422)
        task = self.store.get_task("AT-0009")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "queued")

    def test_validate_endpoint_is_explicitly_not_implemented(self) -> None:
        self.add_task(status="queued")

        response = self.client.post(
            "/api/tasks/AT-0009/validate",
            json={"validators": ["pytest"]},
        )

        self.assertEqual(response.status_code, 501)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["action"], "validate")
        self.assertIn("not implemented", payload["message"])
        self.assertEqual(self.dispatcher_factory_calls, [])

    def test_action_responses_do_not_contain_secrets(self) -> None:
        response = self.client.post("/api/tasks", json=self.task_payload())
        self.assertEqual(response.status_code, 200)
        self.assert_no_sensitive_keys(response.json())

        block_response = self.client.post(
            "/api/tasks/AT-0009/block",
            json={"blocked_reason": "manual block"},
        )
        self.assertEqual(block_response.status_code, 200)
        self.assert_no_sensitive_keys(block_response.json())

    def test_tests_use_temp_db_not_default_state_db(self) -> None:
        self.client.post("/api/tasks", json=self.task_payload())

        self.assertTrue(self.db_path.exists())
        self.assertTrue(str(self.store.db_path).startswith(str(self.root)))
        self.assertNotIn(".agent-taskflow/state.db", str(self.store.db_path))


if __name__ == "__main__":
    unittest.main()
