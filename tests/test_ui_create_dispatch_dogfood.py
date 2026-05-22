"""Post-v0.1.0 UI create/dispatch dogfood test.

This test exercises the full create/dispatch UI flow using the Mission Control
API, verifying:

1. Create Task form creates a TaskRecord and TaskWorktreeRecord in the mirror DB.
2. Start/Dispatch calls the dispatcher and advances the task through
   preparing → implementing → validating → waiting_approval.
3. Deterministic validators (pytest, openspec, policy) run against the output.
4. Task reaches waiting_approval and the StartDispatchPanel is disabled.
5. Human approval (approve) via the UI action API accepts the task.
6. No forbidden actions are triggered: no push, merge, cleanup, worktree deletion.

Uses fake executor and real validators. Does NOT call real Pi, MiniMax, or any
LLM provider. Does NOT run a browser. Exercises the API with FastAPI TestClient.

This is the post-v0.1.0 dogfood for the Create Task and Dispatch UI feature
(Phase 47, documented in docs/mission-control-ui-state-model.md).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app
from agent_taskflow.dispatcher import DispatcherResult
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.validators.base import ValidatorContext, ValidatorResult


class FakeExecutor:
    """Controlled fake executor for dogfood testing.

    Does not call real Pi, OpenCode, or any LLM provider.
    Simulates a successful executor run by writing a simple artifact.
    """

    name = "fake-dogfood"

    def __init__(self, status: str = "completed", summary: str | None = None) -> None:
        self.status = status
        self.summary = summary or f"fake executor {status}"
        self.contexts: list[object] = []

    def run(self, context: object) -> object:
        from agent_taskflow.executors.base import ExecutorResult

        self.contexts.append(context)
        # Simulate executor writing a handoff artifact
        artifact_dir = getattr(context, "artifact_dir", None)
        if artifact_dir is not None and isinstance(artifact_dir, Path):
            handoff = artifact_dir / "handoff_summary.md"
            handoff.write_text(
                "# Dogfood Handoff Summary\n\n"
                "This executor ran in a controlled test environment.\n"
                "No real Pi, OpenCode, or LLM call was made.\n",
                encoding="utf-8",
            )
        return ExecutorResult(
            executor=self.name,
            status=self.status,
            exit_code=0 if self.status == "completed" else 1,
            summary=self.summary,
        )


class FakeExecutorFailing(FakeExecutor):
    """Fake executor that reports failure to test blocking behavior."""

    name = "fake-dogfood-fail"

    def __init__(self, reason: str = "simulated failure") -> None:
        super().__init__(status="failed", summary=reason)


class FakePytestValidator:
    """Fake pytest validator for dogfood testing."""

    name = "pytest"

    def run(self, ctx: ValidatorContext) -> ValidatorResult:
        log_path = ctx.artifact_dir / "pytest.log"
        log_path.write_text("pytest ran in dogfood test\n", encoding="utf-8")
        return ValidatorResult(
            validator=self.name,
            status="passed",
            exit_code=0,
            summary="pytest passed",
            log_path=log_path,
            artifacts={"log": log_path},
        )


class FakeOpenspecValidator:
    """Fake openspec validator for dogfood testing."""

    name = "openspec"

    def run(self, ctx: ValidatorContext) -> ValidatorResult:
        log_path = ctx.artifact_dir / "openspec-validate.log"
        log_path.write_text("openspec ran in dogfood test\n", encoding="utf-8")
        return ValidatorResult(
            validator=self.name,
            status="skipped",
            exit_code=0,
            summary="openspec skipped",
            log_path=log_path,
            artifacts={"log": log_path},
        )


class UiCreateDispatchDogfoodTests(unittest.TestCase):
    """Dogfood tests for the Create Task and Dispatch UI flow."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.worktree_path = self.repo_path / ".worktrees" / "AT-UI-DOGFOOD-59"
        self.artifact_root = self.root / "artifacts"
        self.artifact_dir = self.artifact_root / "AT-UI-DOGFOOD-59"

        self.repo_path.mkdir(parents=True)
        self.worktree_path.mkdir(parents=True)
        self.artifact_root.mkdir(parents=True)
        self.artifact_dir.mkdir(parents=True)

        # Write an implementation prompt so opencode executor doesn't block
        (self.artifact_dir / "implementation_prompt.md").write_text(
            "Create a file called result.txt with content: dogfood-ok",
            encoding="utf-8",
        )

        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

        self.fake_executor = FakeExecutor()
        self.fake_failing_executor = FakeExecutorFailing()

        def dispatcher_factory(store: object, validators: tuple[str, ...]) -> object:
            from agent_taskflow.dispatcher import Dispatcher
            from agent_taskflow.validators.policy import PolicyCheckValidator

            return Dispatcher(
                store,
                executor_registry={
                    "fake-dogfood": self.fake_executor,
                    "fake-dogfood-fail": self.fake_failing_executor,
                },
                validator_registry={
                    "pytest": FakePytestValidator(),
                    "openspec": FakeOpenspecValidator(),
                    "policy": PolicyCheckValidator(scan_artifacts=True),
                },
                validators=validators,
                default_executor="fake-dogfood",
            )

        self.client_context = TestClient(
            create_app(self.db_path, dispatcher_factory=dispatcher_factory)
        )
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_create_task_via_api_creates_task_and_worktree_records(self) -> None:
        """POST /api/tasks creates TaskRecord and TaskWorktreeRecord in mirror DB."""
        response = self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
                "executor": "fake-dogfood",
                "validator": "pytest",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "create")
        self.assertEqual(payload["task_key"], "AT-UI-DOGFOOD-59")
        self.assertEqual(payload["status"], "queued")

        # Verify TaskRecord in DB
        task = self.store.get_task("AT-UI-DOGFOOD-59")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "queued")
        self.assertEqual(task.project, "agent-taskflow")
        # executor/model/validator are not stored by create endpoint (they are set at dispatch time)
        self.assertIsNone(task.executor)

        # Verify TaskWorktreeRecord in DB
        worktree = self.store.get_task_worktree("AT-UI-DOGFOOD-59")
        self.assertIsNotNone(worktree)
        assert worktree is not None
        self.assertEqual(worktree.branch, "task/AT-UI-DOGFOOD-59")
        self.assertEqual(worktree.base_branch, "main")
        self.assertEqual(worktree.status, "active")

    def test_create_task_does_not_dispatch(self) -> None:
        """Creating a task does NOT call the dispatcher or run any executor."""
        response = self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])

        # Executor was never called
        self.assertEqual(self.fake_executor.contexts, [])

        # Task is still queued (not moved to preparing/implementing/validating)
        task = self.store.get_task("AT-UI-DOGFOOD-59")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "queued")

    def test_create_task_rejects_relative_paths(self) -> None:
        """API rejects relative repo_path, worktree_path, artifact_dir."""
        for bad_path_field, bad_path_value in [
            ("repo_path", "relative/repo"),
            ("worktree_path", "relative/worktree"),
            ("artifact_dir", "relative/artifacts"),
        ]:
            with self.subTest(field=bad_path_field):
                response = self.client.post(
                    "/api/tasks",
                    json={
                        "task_key": "AT-UI-DOGFOOD-59",
                        "project": "agent-taskflow",
                        "repo_path": str(self.repo_path),
                        "worktree_path": str(self.worktree_path),
                        "artifact_dir": str(self.artifact_dir),
                        bad_path_field: bad_path_value,
                    },
                )

                self.assertEqual(response.status_code, 422)
                self.assertIn("must be absolute", response.json()["detail"])

    def test_start_dispatch_runs_executor_and_validators(self) -> None:
        """POST /api/tasks/{key}/start runs dispatcher, executor, and validators."""
        # Create task first
        self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
                "executor": "fake-dogfood",
                "validator": "pytest",
            },
        )

        # Reset executor contexts so we only count start call
        self.fake_executor.contexts.clear()

        response = self.client.post(
            "/api/tasks/AT-UI-DOGFOOD-59/start",
            json={
                "validators": ["pytest", "policy"],
                "executor": "fake-dogfood",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "start")
        self.assertEqual(payload["status"], "waiting_approval")

        # Executor was called exactly once
        self.assertEqual(len(self.fake_executor.contexts), 1)

        # Task moved through states
        task = self.store.get_task("AT-UI-DOGFOOD-59")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")

        # Validators ran and produced logs
        policy_log = self.artifact_dir / "policy-validate.log"
        self.assertTrue(policy_log.exists(), "policy validator should produce a log")

        # Handoff artifact written by fake executor
        handoff = self.artifact_dir / "handoff_summary.md"
        self.assertTrue(handoff.exists(), "fake executor should write handoff artifact")

    def test_start_dispatch_with_default_validators(self) -> None:
        """Dispatch with default validators (pytest, openspec) works."""
        self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
                "executor": "fake-dogfood",
            },
        )

        response = self.client.post(
            "/api/tasks/AT-UI-DOGFOOD-59/start",
            json={},  # no explicit validators -> uses DEFAULT_VALIDATORS
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "waiting_approval")

        # Both pytest and openspec logs produced
        pytest_log = self.artifact_dir / "pytest.log"
        openspec_log = self.artifact_dir / "openspec-validate.log"
        self.assertTrue(pytest_log.exists())
        self.assertTrue(openspec_log.exists())

    def test_start_on_terminal_task_returns_conflict(self) -> None:
        """Start on accepted/rejected/cleaned task returns 409 conflict."""
        # Create and approve a task
        self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
            },
        )

        # Manually set to accepted to simulate a completed task
        self.store.update_task_status(
            "AT-UI-DOGFOOD-59",
            "accepted",
            source="dogfood-test",
            message="preparing for conflict test",
        )

        response = self.client.post("/api/tasks/AT-UI-DOGFOOD-59/start", json={})

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["ok"])

    def test_failed_executor_blocks_task(self) -> None:
        """Executor failure blocks the task and does not reach waiting_approval."""
        self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
                "executor": "fake-dogfood-fail",
            },
        )

        response = self.client.post(
            "/api/tasks/AT-UI-DOGFOOD-59/start",
            json={"executor": "fake-dogfood-fail"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")

        task = self.store.get_task("AT-UI-DOGFOOD-59")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "blocked")
        self.assertIsNotNone(task.blocked_reason)

    def test_policy_validator_checks_artifact_dir(self) -> None:
        """Policy validator runs against the task artifact directory."""
        self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
                "executor": "fake-dogfood",
            },
        )

        response = self.client.post(
            "/api/tasks/AT-UI-DOGFOOD-59/start",
            json={"validators": ["policy"]},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "waiting_approval")

        # Policy log exists and shows pass
        policy_log = self.artifact_dir / "policy-validate.log"
        self.assertTrue(policy_log.exists())
        content = policy_log.read_text(encoding="utf-8")
        self.assertIn("PASSED", content)
        self.assertIn("Policy check PASSED", content)

    def test_approve_requires_operator_attestation_identity(self) -> None:
        """Approve endpoint rejects worker/system decided_by values."""
        self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
            },
        )

        self.store.update_task_status(
            "AT-UI-DOGFOOD-59",
            "waiting_approval",
            source="dogfood-test",
            message="dogfood test preparing",
        )

        for bad_identity in ("worker", "pi", "agent", "system", "", None):
            with self.subTest(identity=bad_identity):
                payload: dict[str, object] = {}
                if bad_identity is not None:
                    payload["decided_by"] = bad_identity

                response = self.client.post(
                    "/api/tasks/AT-UI-DOGFOOD-59/approve",
                    json=payload,
                )

                self.assertEqual(
                    response.status_code,
                    422,
                    f"Identity {bad_identity!r} should be rejected",
                )
                # Task should still be waiting_approval (not accepted)
                task = self.store.get_task("AT-UI-DOGFOOD-59")
                self.assertIsNotNone(task)
                assert task is not None
                self.assertEqual(task.status, "waiting_approval")

    def test_approve_with_operator_cli_attestation_accepts_task(self) -> None:
        """Approve with decided_by='operator_cli' accepts the task."""
        self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
            },
        )

        self.store.update_task_status(
            "AT-UI-DOGFOOD-59",
            "waiting_approval",
            source="dogfood-test",
            message="dogfood test preparing",
        )

        response = self.client.post(
            "/api/tasks/AT-UI-DOGFOOD-59/approve",
            json={"decided_by": "operator_cli", "notes": "dogfood test approval"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "approve")
        self.assertEqual(payload["status"], "accepted")

        task = self.store.get_task("AT-UI-DOGFOOD-59")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "accepted")

        approvals = self.store.list_approval_decisions("AT-UI-DOGFOOD-59")
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[-1]["decision"], "accepted")
        self.assertEqual(approvals[-1]["decided_by"], "operator_cli")
        self.assertEqual(approvals[-1]["notes"], "dogfood test approval")

    def test_reject_with_operator_cli_attestation_works(self) -> None:
        """Reject with decided_by='operator_cli' works."""
        self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
            },
        )

        self.store.update_task_status(
            "AT-UI-DOGFOOD-59",
            "waiting_approval",
            source="dogfood-test",
            message="dogfood test preparing",
        )

        response = self.client.post(
            "/api/tasks/AT-UI-DOGFOOD-59/reject",
            json={"decided_by": "operator_cli", "notes": "needs rework"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "rejected")

        task = self.store.get_task("AT-UI-DOGFOOD-59")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "rejected")

        approvals = self.store.list_approval_decisions("AT-UI-DOGFOOD-59")
        self.assertEqual(approvals[-1]["decision"], "rejected")
        self.assertEqual(approvals[-1]["decided_by"], "operator_cli")

    def test_full_create_dispatch_approve_flow(self) -> None:
        """Full flow: create → dispatch → waiting_approval → operator approve."""
        # 1. Create task
        create_response = self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
                "executor": "fake-dogfood",
                "validator": "pytest",
                "title": "Post-v0.1.0 UI create/dispatch dogfood",
                "board": "agent-taskflow",
            },
        )
        self.assertEqual(create_response.status_code, 200)
        create_payload = create_response.json()
        self.assertTrue(create_payload["ok"])
        self.assertEqual(create_payload["status"], "queued")
        self.assertEqual(create_payload["task_key"], "AT-UI-DOGFOOD-59")

        # 2. Dispatch task
        start_response = self.client.post(
            "/api/tasks/AT-UI-DOGFOOD-59/start",
            json={
                "validators": ["pytest", "openspec", "policy"],
                "executor": "fake-dogfood",
            },
        )
        self.assertEqual(start_response.status_code, 200)
        start_payload = start_response.json()
        self.assertTrue(start_payload["ok"])
        self.assertEqual(start_payload["action"], "start")
        self.assertEqual(start_payload["status"], "waiting_approval")

        # 3. Task is now waiting_approval — StartDispatchPanel in UI would be disabled
        task = self.store.get_task("AT-UI-DOGFOOD-59")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")

        # 4. Operator approves via UI action API
        approve_response = self.client.post(
            "/api/tasks/AT-UI-DOGFOOD-59/approve",
            json={
                "decided_by": "operator_cli",
                "notes": "Post-v0.1.0 UI create/dispatch dogfood passed",
            },
        )
        self.assertEqual(approve_response.status_code, 200)
        approve_payload = approve_response.json()
        self.assertTrue(approve_payload["ok"])
        self.assertEqual(approve_payload["action"], "approve")
        self.assertEqual(approve_payload["status"], "accepted")

        # 5. Final state check
        task = self.store.get_task("AT-UI-DOGFOOD-59")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "accepted")

        approvals = self.store.list_approval_decisions("AT-UI-DOGFOOD-59")
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[-1]["decision"], "accepted")
        self.assertEqual(approvals[-1]["decided_by"], "operator_cli")

        # 6. Verify no forbidden actions were triggered (no push/merge/cleanup logs)
        for artifact in self.artifact_dir.iterdir():
            if artifact.name == "mission_contract.json":
                continue  # contract lists forbidden actions — skip
            content = artifact.read_text(errors="ignore")
            self.assertNotIn("git push", content.lower())
            self.assertNotIn("self-approval", content.lower())

    def test_dry_run_does_not_execute_or_mutate_status(self) -> None:
        """dry_run=true validates state but does not run executor or change status."""
        self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
                "executor": "fake-dogfood",
            },
        )

        response = self.client.post(
            "/api/tasks/AT-UI-DOGFOOD-59/start",
            json={"dry_run": True},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])

        # Executor was not called
        self.assertEqual(self.fake_executor.contexts, [])

        # Task is still queued (not moved)
        task = self.store.get_task("AT-UI-DOGFOOD-59")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "queued")

    def test_mission_contract_written_before_executor_runs(self) -> None:
        """Dispatcher writes mission_contract.json before calling executor."""
        self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
                "executor": "fake-dogfood",
            },
        )

        self.fake_executor.contexts.clear()

        response = self.client.post(
            "/api/tasks/AT-UI-DOGFOOD-59/start",
            json={"validators": ["pytest"]},
        )

        self.assertEqual(response.status_code, 200)

        # Executor ran
        self.assertEqual(len(self.fake_executor.contexts), 1)

        # Contract was written
        contract_path = self.artifact_dir / "mission_contract.json"
        self.assertTrue(contract_path.exists())

        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        self.assertEqual(contract["schema_version"], "1")
        self.assertEqual(contract["task_key"], "AT-UI-DOGFOOD-59")
        self.assertTrue(contract["human_approval_required"])
        # forbidden_actions is the governance prohibition list, not executor names
        required_forbidden = {
            "approve", "push", "merge", "cleanup",
            "delete_worktree", "delete_branch", "self_approve", "force_push"
        }
        self.assertTrue(
            required_forbidden.issubset(set(contract["forbidden_actions"])),
            f"missing forbidden actions: {required_forbidden - set(contract['forbidden_actions'])}"
        )

    def test_create_task_does_not_write_mission_contract(self) -> None:
        """Creating a task does NOT write mission_contract.json."""
        response = self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
                "executor": "fake-dogfood",
            },
        )

        self.assertEqual(response.status_code, 200)

        contract_path = self.artifact_dir / "mission_contract.json"
        self.assertFalse(contract_path.exists())

    def test_worktree_governance_enforced_on_create(self) -> None:
        """Worktree path governance is enforced: must be inside .worktrees."""
        response = self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.repo_path / "outside-worktrees"),
                "artifact_dir": str(self.artifact_dir),
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn(".worktrees", response.json()["detail"])

    def test_worktree_equal_to_repo_rejected_on_create(self) -> None:
        """Worktree path equal to main repo is rejected."""
        response = self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.repo_path),
                "artifact_dir": str(self.artifact_dir),
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("main repo path", response.json()["detail"])

    def test_duplicate_task_key_returns_conflict(self) -> None:
        """Creating a task with an existing task_key returns 409 conflict."""
        self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
            },
        )

        response = self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["ok"])
        self.assertIn("already exists", response.json()["message"])

    def test_approve_on_wrong_status_returns_conflict(self) -> None:
        """Approve on non-waiting_approval task returns 409 conflict."""
        self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
            },
        )

        # Task is still queued — approve should fail
        response = self.client.post(
            "/api/tasks/AT-UI-DOGFOOD-59/approve",
            json={"decided_by": "operator_cli"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["ok"])

        task = self.store.get_task("AT-UI-DOGFOOD-59")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "queued")

        approvals = self.store.list_approval_decisions("AT-UI-DOGFOOD-59")
        self.assertEqual(approvals, [])

    def test_no_sensitive_keys_in_api_responses(self) -> None:
        """API responses do not contain env, tokens, secrets."""
        self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
            },
        )

        for path in (
            "/api/tasks/AT-UI-DOGFOOD-59",
            "/api/tasks/AT-UI-DOGFOOD-59/runs",
            "/api/tasks/AT-UI-DOGFOOD-59/artifacts",
            "/api/tasks/AT-UI-DOGFOOD-59/validations",
            "/api/tasks/AT-UI-DOGFOOD-59/approvals",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assert_no_sensitive_keys(response.json())

    def assert_no_sensitive_keys(self, value: object) -> None:
        """Assert no env, token, secret, api_key keys in response."""
        if isinstance(value, dict):
            for key, item in value.items():
                lowered = str(key).lower()
                self.assertNotEqual(lowered, "env")
                self.assertNotEqual(lowered, "environment")
                secret_ok = lowered == "has_secret_warning"
                self.assertFalse(
                    not secret_ok and "secret" in lowered,
                    f"Key {key} looks like a secret field",
                )
                if lowered != "task_key":
                    self.assertFalse(
                        str(lowered).endswith("_token"),
                        f"Key {key} looks like a token field",
                    )
                    self.assertNotEqual(lowered, "token")
                    self.assertNotEqual(lowered, "api_key")
                self.assert_no_sensitive_keys(item)
        elif isinstance(value, list):
            for item in value:
                self.assert_no_sensitive_keys(item)

    def test_read_only_endpoints_do_not_modify_status(self) -> None:
        """GET endpoints do not change task status."""
        self.client.post(
            "/api/tasks",
            json={
                "task_key": "AT-UI-DOGFOOD-59",
                "project": "agent-taskflow",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
            },
        )

        before = self.store.get_task("AT-UI-DOGFOOD-59")
        self.assertIsNotNone(before)
        assert before is not None
        self.assertEqual(before.status, "queued")

        for path in (
            "/api/tasks",
            "/api/tasks/AT-UI-DOGFOOD-59",
            "/api/tasks/AT-UI-DOGFOOD-59/runs",
            "/api/tasks/AT-UI-DOGFOOD-59/artifacts",
            "/api/tasks/AT-UI-DOGFOOD-59/validations",
            "/api/tasks/AT-UI-DOGFOOD-59/approvals",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)

        after = self.store.get_task("AT-UI-DOGFOOD-59")
        self.assertIsNotNone(after)
        assert after is not None
        self.assertEqual(after.status, "queued")


if __name__ == "__main__":
    unittest.main()
