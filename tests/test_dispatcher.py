from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.dispatcher import Dispatcher
from agent_taskflow.executors.base import ExecutorContext, ExecutorResult
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.validators.base import ValidatorContext, ValidatorResult


class FakeExecutor:
    name = "fake"

    def __init__(self, status: str = "completed", summary: str | None = None) -> None:
        self.status = status
        self.summary = summary
        self.contexts: list[ExecutorContext] = []

    def run(self, context: ExecutorContext) -> ExecutorResult:
        self.contexts.append(context)
        return ExecutorResult(
            executor=self.name,
            status=self.status,
            exit_code=0 if self.status == "completed" else 1,
            summary=self.summary or f"executor {self.status}",
        )


class FakeValidator:
    def __init__(self, name: str, status: str = "passed", summary: str | None = None) -> None:
        self.name = name
        self.status = status
        self.summary = summary
        self.contexts: list[ValidatorContext] = []

    def run(self, context: ValidatorContext) -> ValidatorResult:
        self.contexts.append(context)
        return ValidatorResult(
            validator=self.name,
            status=self.status,
            exit_code=0 if self.status in {"passed", "skipped"} else 1,
            summary=self.summary or f"validator {self.status}",
        )


class DispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.worktree_path = self.repo_path / ".worktrees" / "AT-0007"
        self.artifact_dir = self.root / "artifacts" / "AT-0007"

        self.repo_path.mkdir()
        self.worktree_path.mkdir(parents=True)
        self.artifact_dir.mkdir(parents=True)

        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add_task(
        self,
        *,
        task_key: str = "AT-0007",
        status: str = "queued",
        repo_path: Path | None = None,
        worktree_path: Path | None = None,
        artifact_dir: Path | None = None,
    ) -> None:
        repo = repo_path or self.repo_path
        worktree = worktree_path or self.worktree_path
        artifacts = artifact_dir or self.artifact_dir

        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                hermes_task_id="t_at_0007",
                title="Task AT-0007",
                status=status,
                repo_path=repo,
                artifact_dir=artifacts,
            )
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=task_key,
                repo_path=repo,
                worktree_path=worktree,
                branch=f"task/{task_key}",
                base_branch="main",
                status="active",
            )
        )

    def make_dispatcher(
        self,
        *,
        executor: FakeExecutor | None = None,
        validators: dict[str, FakeValidator] | None = None,
        validator_names: tuple[str, ...] = ("pytest", "openspec"),
        default_executor: str = "fake",
    ) -> Dispatcher:
        executor = executor or FakeExecutor()
        validators = validators or {
            "pytest": FakeValidator("pytest", "passed"),
            "openspec": FakeValidator("openspec", "skipped"),
        }
        return Dispatcher(
            self.store,
            executor_registry={"fake": executor},
            validator_registry=validators,
            validators=validator_names,
            default_executor=default_executor,
            default_model="fake-model",
        )

    def event_payloads(self, task_key: str = "AT-0007") -> list[str]:
        return [event.payload_json or "" for event in self.store.list_task_events(task_key)]

    def test_queued_task_success_moves_to_waiting_approval(self) -> None:
        self.add_task()
        dispatcher = self.make_dispatcher()

        result = dispatcher.dispatch_task("AT-0007")

        task = self.store.get_task("AT-0007")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(result.status, "waiting_approval")
        self.assertEqual(task.status, "waiting_approval")
        self.assertIsNone(task.blocked_reason)
        self.assertEqual(result.executor_status, "completed")
        self.assertEqual(result.validator_statuses, {"pytest": "passed", "openspec": "skipped"})

    def test_executor_failed_blocks_task(self) -> None:
        self.add_task()
        dispatcher = self.make_dispatcher(executor=FakeExecutor("failed", "implementation failed"))

        result = dispatcher.dispatch_task("AT-0007")

        task = self.store.get_task("AT-0007")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(result.status, "blocked")
        self.assertEqual(task.status, "blocked")
        self.assertEqual(task.blocked_reason, "implementation failed")
        self.assertEqual(result.executor_status, "failed")

    def test_executor_blocked_blocks_task(self) -> None:
        self.add_task()
        dispatcher = self.make_dispatcher(executor=FakeExecutor("blocked", "missing prompt"))

        result = dispatcher.dispatch_task("AT-0007")

        task = self.store.get_task("AT-0007")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(result.status, "blocked")
        self.assertEqual(task.status, "blocked")
        self.assertEqual(task.blocked_reason, "missing prompt")
        self.assertEqual(result.executor_status, "blocked")

    def test_validator_failed_blocks_task(self) -> None:
        self.add_task()
        dispatcher = self.make_dispatcher(
            validators={"pytest": FakeValidator("pytest", "failed", "tests failed")},
            validator_names=("pytest",),
        )

        result = dispatcher.dispatch_task("AT-0007")

        task = self.store.get_task("AT-0007")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(result.status, "blocked")
        self.assertEqual(task.status, "blocked")
        self.assertEqual(task.blocked_reason, "tests failed")
        self.assertEqual(result.validator_statuses, {"pytest": "failed"})

    def test_validator_blocked_blocks_task(self) -> None:
        self.add_task()
        dispatcher = self.make_dispatcher(
            validators={"pytest": FakeValidator("pytest", "blocked", "pytest unavailable")},
            validator_names=("pytest",),
        )

        result = dispatcher.dispatch_task("AT-0007")

        task = self.store.get_task("AT-0007")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(result.status, "blocked")
        self.assertEqual(task.status, "blocked")
        self.assertEqual(task.blocked_reason, "pytest unavailable")
        self.assertEqual(result.validator_statuses, {"pytest": "blocked"})

    def test_validator_skipped_does_not_block_task(self) -> None:
        self.add_task()
        dispatcher = self.make_dispatcher(
            validators={"openspec": FakeValidator("openspec", "skipped")},
            validator_names=("openspec",),
        )

        result = dispatcher.dispatch_task("AT-0007")

        task = self.store.get_task("AT-0007")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(result.status, "waiting_approval")
        self.assertEqual(task.status, "waiting_approval")
        self.assertEqual(result.validator_statuses, {"openspec": "skipped"})

    def test_waiting_approval_task_is_skipped(self) -> None:
        self.add_task(status="waiting_approval")
        fake_executor = FakeExecutor()
        dispatcher = self.make_dispatcher(executor=fake_executor)

        result = dispatcher.dispatch_task("AT-0007")

        self.assertEqual(result.status, "skipped")
        self.assertEqual(fake_executor.contexts, [])

    def test_waiting_for_review_task_is_skipped(self) -> None:
        self.add_task(status="waiting_for_review")
        fake_executor = FakeExecutor()
        dispatcher = self.make_dispatcher(executor=fake_executor)

        result = dispatcher.dispatch_task("AT-0007")

        self.assertEqual(result.status, "skipped")
        self.assertEqual(fake_executor.contexts, [])

    def test_accepted_rejected_cleaned_tasks_are_skipped(self) -> None:
        for status in ("accepted", "rejected", "cleaned"):
            with self.subTest(status=status):
                task_key = f"AT-{len(status):04d}"
                worktree = self.repo_path / ".worktrees" / task_key
                artifacts = self.root / "artifacts" / task_key
                worktree.mkdir(parents=True, exist_ok=True)
                artifacts.mkdir(parents=True, exist_ok=True)
                self.add_task(
                    task_key=task_key,
                    status=status,
                    worktree_path=worktree,
                    artifact_dir=artifacts,
                )
                fake_executor = FakeExecutor()
                dispatcher = self.make_dispatcher(executor=fake_executor)

                result = dispatcher.dispatch_task(task_key)

                self.assertEqual(result.status, "skipped")
                self.assertEqual(fake_executor.contexts, [])

    def test_dispatcher_records_executor_run_start_and_finish(self) -> None:
        self.add_task()
        dispatcher = self.make_dispatcher()

        dispatcher.dispatch_task("AT-0007")

        payloads = self.event_payloads()
        self.assertTrue(any("executor_run_started" in payload for payload in payloads))
        self.assertTrue(any("executor_run_finished" in payload for payload in payloads))

    def test_dispatcher_records_validation_result(self) -> None:
        self.add_task()
        dispatcher = self.make_dispatcher()

        dispatcher.dispatch_task("AT-0007")

        payloads = self.event_payloads()
        self.assertTrue(any("validation_result" in payload and "pytest" in payload for payload in payloads))
        self.assertTrue(any("validation_result" in payload and "openspec" in payload for payload in payloads))

    def test_successful_task_has_no_blocked_reason(self) -> None:
        self.add_task(status="blocked")
        dispatcher = self.make_dispatcher()

        dispatcher.dispatch_task("AT-0007")

        task = self.store.get_task("AT-0007")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")
        self.assertIsNone(task.blocked_reason)

    def test_worktree_equal_to_repo_path_is_blocked(self) -> None:
        self.add_task(worktree_path=self.repo_path)
        dispatcher = self.make_dispatcher()

        result = dispatcher.dispatch_task("AT-0007")

        self.assertEqual(result.status, "blocked")
        self.assertIn("main repo path", result.blocked_reason or "")

    def test_relative_worktree_path_is_rejected(self) -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-0007",
                project="agent-taskflow",
                status="queued",
                repo_path=self.repo_path,
                artifact_dir=self.artifact_dir,
            )
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO task_worktrees (
                    task_key,
                    repo_path,
                    worktree_path,
                    branch,
                    base_branch,
                    status,
                    created_at,
                    cleaned_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "AT-0007",
                    str(self.repo_path),
                    "relative-worktree",
                    "task/AT-0007",
                    "main",
                    "active",
                    "2026-01-01T00:00:00Z",
                    None,
                ),
            )

        dispatcher = self.make_dispatcher()

        result = dispatcher.dispatch_task("AT-0007")

        self.assertEqual(result.status, "blocked")
        self.assertIn("worktree_path must be absolute", result.blocked_reason or "")

    def test_relative_artifact_dir_is_rejected(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    task_key,
                    project,
                    board,
                    hermes_task_id,
                    title,
                    status,
                    repo_path,
                    artifact_dir,
                    blocked_reason,
                    created_at,
                    updated_at,
                    last_synced_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "AT-0007",
                    "agent-taskflow",
                    "agent-taskflow",
                    "t_at_0007",
                    "Task AT-0007",
                    "queued",
                    str(self.repo_path),
                    "relative-artifacts",
                    None,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )

        dispatcher = self.make_dispatcher()

        result = dispatcher.dispatch_task("AT-0007")

        self.assertEqual(result.status, "blocked")
        self.assertIn("artifact_dir must be absolute", result.blocked_reason or "")

    def test_worktree_outside_repo_worktrees_is_blocked(self) -> None:
        outside = self.root / "outside-worktree"
        outside.mkdir()
        self.add_task(worktree_path=outside)
        dispatcher = self.make_dispatcher()

        result = dispatcher.dispatch_task("AT-0007")

        self.assertEqual(result.status, "blocked")
        self.assertIn(".worktrees", result.blocked_reason or "")

    def test_unknown_executor_blocks_task(self) -> None:
        self.add_task()
        dispatcher = self.make_dispatcher(default_executor="does-not-exist")

        result = dispatcher.dispatch_task("AT-0007")

        task = self.store.get_task("AT-0007")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(result.status, "blocked")
        self.assertEqual(task.status, "blocked")
        self.assertIn("Executor does-not-exist is unavailable", result.blocked_reason or "")

    def test_unknown_validator_blocks_task(self) -> None:
        self.add_task()
        dispatcher = self.make_dispatcher(validator_names=("does-not-exist",))

        result = dispatcher.dispatch_task("AT-0007")

        task = self.store.get_task("AT-0007")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(result.status, "blocked")
        self.assertEqual(task.status, "blocked")
        self.assertIn("Validator does-not-exist raised", result.blocked_reason or "")

    def test_task_model_is_passed_to_executor_context(self) -> None:
        self.add_task()
        fake_executor = FakeExecutor()
        dispatcher = self.make_dispatcher(executor=fake_executor)

        dispatcher.dispatch_task("AT-0007", model="model-from-call")

        self.assertEqual(fake_executor.contexts[0].model, "model-from-call")

    def test_context_worktree_and_artifact_dir_are_task_paths(self) -> None:
        self.add_task()
        fake_executor = FakeExecutor()
        fake_validator = FakeValidator("pytest", "passed")
        dispatcher = self.make_dispatcher(
            executor=fake_executor,
            validators={"pytest": fake_validator},
            validator_names=("pytest",),
        )

        dispatcher.dispatch_task("AT-0007")

        self.assertEqual(fake_executor.contexts[0].worktree_path, self.worktree_path)
        self.assertEqual(fake_executor.contexts[0].artifact_dir, self.artifact_dir)
        self.assertEqual(fake_validator.contexts[0].worktree_path, self.worktree_path)
        self.assertEqual(fake_validator.contexts[0].artifact_dir, self.artifact_dir)

    def test_opencode_requires_implementation_prompt(self) -> None:
        self.add_task()
        dispatcher = Dispatcher(
            self.store,
            executor_registry={"opencode": FakeExecutor()},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="opencode",
        )

        result = dispatcher.dispatch_task("AT-0007")

        self.assertEqual(result.status, "blocked")
        self.assertIn("implementation_prompt.md is required", result.blocked_reason or "")

    def test_dry_run_does_not_execute_or_mutate_status(self) -> None:
        self.add_task()
        fake_executor = FakeExecutor()
        dispatcher = self.make_dispatcher(executor=fake_executor)

        result = dispatcher.dispatch_task("AT-0007", dry_run=True)

        task = self.store.get_task("AT-0007")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(result.status, "skipped")
        self.assertEqual(task.status, "queued")
        self.assertEqual(fake_executor.contexts, [])


class DispatcherExecutorSelectionTests(unittest.TestCase):
    """Phase 13: dispatcher task-level executor selection tests."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.worktree_path = self.repo_path / ".worktrees" / "AT-0013"
        self.artifact_dir = self.root / "artifacts" / "AT-0013"

        self.repo_path.mkdir()
        self.worktree_path.mkdir(parents=True)
        self.artifact_dir.mkdir(parents=True)
        # Create prompt file so opencode/pi executor doesn't block
        (self.artifact_dir / "implementation_prompt.md").write_text(
            "Implement the task.", encoding="utf-8"
        )

        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add_task(
        self,
        *,
        task_key: str = "AT-0013",
        status: str = "queued",
        executor: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        tools: list[str] | None = None,
        pi_bin: str | None = None,
    ) -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                hermes_task_id=f"t_{task_key.lower().replace('-', '_')}",
                title=f"Task {task_key}",
                status=status,
                repo_path=self.repo_path,
                artifact_dir=self.artifact_dir,
                executor=executor,
                model=model,
                provider=provider,
                tools=tools,
                pi_bin=pi_bin,
            )
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=task_key,
                repo_path=self.repo_path,
                worktree_path=self.worktree_path,
                branch=f"task/{task_key}",
                base_branch="main",
                status="active",
            )
        )

    def test_task_executor_overrides_dispatcher_default_executor(self) -> None:
        """task.executor='pi' causes dispatcher to select pi even with default='manual'."""
        import unittest.mock as mock

        self.add_task(executor="pi")
        dispatcher = Dispatcher(
            self.store,
            executor_registry={},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="manual",
        )

        with mock.patch(
            "agent_taskflow.dispatcher.get_executor",
            return_value=FakeExecutor("completed", "ok"),
        ) as mock_get_exec:
            result = dispatcher.dispatch_task("AT-0013")

        self.assertEqual(result.status, "waiting_approval")
        mock_get_exec.assert_called_once()
        self.assertEqual(mock_get_exec.call_args.args[0], "pi")

    def test_task_model_is_used_when_task_has_executor_pi(self) -> None:
        """task.model is passed to executor context when executor is pi."""
        from agent_taskflow.executors.base import ExecutorContext
        captured: list[ExecutorContext] = []

        class CapturingFakeExecutor:
            name = "fake"

            def run(self, context: ExecutorContext) -> ExecutorResult:
                captured.append(context)
                return ExecutorResult(
                    executor=self.name,
                    status="completed",
                    exit_code=0,
                    summary="ok",
                )

        self.add_task(executor="fake", model="task-model-from-record")
        dispatcher = Dispatcher(
            self.store,
            executor_registry={"fake": CapturingFakeExecutor()},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="manual",
            default_model="dispatcher-default-model",
        )

        result = dispatcher.dispatch_task("AT-0013")

        self.assertEqual(result.status, "waiting_approval")
        self.assertEqual(len(captured), 1)
        # Task model should override dispatcher default_model
        self.assertEqual(captured[0].model, "task-model-from-record")

    def test_dispatcher_default_model_when_task_executor_no_model(self) -> None:
        """When task has no model, dispatcher default_model is used."""
        from agent_taskflow.executors.base import ExecutorContext
        captured: list[ExecutorContext] = []

        class CapturingFakeExecutor:
            name = "fake"

            def run(self, context: ExecutorContext) -> ExecutorResult:
                captured.append(context)
                return ExecutorResult(
                    executor=self.name,
                    status="completed",
                    exit_code=0,
                    summary="ok",
                )

        # No model set on task
        self.add_task(executor="fake")
        dispatcher = Dispatcher(
            self.store,
            executor_registry={"fake": CapturingFakeExecutor()},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="fake",
            default_model="dispatcher-default-model",
        )

        result = dispatcher.dispatch_task("AT-0013")

        self.assertEqual(result.status, "waiting_approval")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].model, "dispatcher-default-model")

    def test_task_with_executor_pi_calls_get_executor_with_provider(self) -> None:
        """task.provider is passed to get_executor when executor is pi."""
        import unittest.mock as mock

        self.add_task(
            executor="pi",
            model="minimax-01",
            provider="minimax",
        )
        dispatcher = Dispatcher(
            self.store,
            executor_registry={},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="manual",
        )

        with mock.patch(
            "agent_taskflow.dispatcher.get_executor",
            return_value=FakeExecutor("completed", "ok"),
        ) as mock_get_exec:
            result = dispatcher.dispatch_task("AT-0013")

        mock_get_exec.assert_called_once()
        call_kwargs = mock_get_exec.call_args
        # Verify provider was passed
        self.assertEqual(call_kwargs.kwargs.get("provider"), "minimax")
        self.assertEqual(call_kwargs.kwargs.get("model"), "minimax-01")

    def test_task_with_executor_pi_calls_get_executor_with_tools(self) -> None:
        """task.tools is passed to get_executor when executor is pi."""
        import unittest.mock as mock

        self.add_task(
            executor="pi",
            tools=["Read", "Write", "Bash"],
        )
        dispatcher = Dispatcher(
            self.store,
            executor_registry={},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="manual",
        )

        with mock.patch(
            "agent_taskflow.dispatcher.get_executor",
            return_value=FakeExecutor("completed", "ok"),
        ) as mock_get_exec:
            result = dispatcher.dispatch_task("AT-0013")

        mock_get_exec.assert_called_once()
        self.assertEqual(
            mock_get_exec.call_args.kwargs.get("tools"),
            ["Read", "Write", "Bash"],
        )

    def test_task_with_executor_pi_calls_get_executor_with_pi_bin(self) -> None:
        """task.pi_bin is passed to get_executor when executor is pi."""
        import unittest.mock as mock

        self.add_task(
            executor="pi",
            pi_bin="/usr/local/bin/pi",
        )
        dispatcher = Dispatcher(
            self.store,
            executor_registry={},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="manual",
        )

        with mock.patch(
            "agent_taskflow.dispatcher.get_executor",
            return_value=FakeExecutor("completed", "ok"),
        ) as mock_get_exec:
            result = dispatcher.dispatch_task("AT-0013")

        mock_get_exec.assert_called_once()
        self.assertEqual(
            mock_get_exec.call_args.kwargs.get("pi_bin"),
            "/usr/local/bin/pi",
        )

    def test_unknown_executor_still_blocks_task(self) -> None:
        """Unknown executor still blocks task (no behavior regression)."""
        self.add_task(executor="nonexistent-executor")
        dispatcher = Dispatcher(
            self.store,
            executor_registry={},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="nonexistent-executor",
        )

        result = dispatcher.dispatch_task("AT-0013")

        self.assertEqual(result.status, "blocked")
        self.assertIn("is unavailable", result.blocked_reason or "")

    def test_task_executor_none_uses_dispatcher_default(self) -> None:
        """When task.executor is None, dispatcher default is used."""
        from agent_taskflow.executors.base import ExecutorContext
        captured: list[ExecutorContext] = []

        class CapturingFakeExecutor:
            name = "fake"

            def run(self, context: ExecutorContext) -> ExecutorResult:
                captured.append(context)
                return ExecutorResult(
                    executor=self.name,
                    status="completed",
                    exit_code=0,
                    summary="ok",
                )

        # No executor set on task
        self.add_task()
        dispatcher = Dispatcher(
            self.store,
            executor_registry={"fake": CapturingFakeExecutor()},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="fake",
        )

        result = dispatcher.dispatch_task("AT-0013")

        self.assertEqual(result.status, "waiting_approval")
        self.assertEqual(len(captured), 1)

    def test_dispatch_executor_param_overrides_task_executor(self) -> None:
        """dispatcher call executor_name overrides task.executor."""
        from agent_taskflow.executors.base import ExecutorContext
        captured: list[tuple[str, ExecutorContext]] = []

        class CapturingFakeExecutor:
            name = "fake"

            def run(self, context: ExecutorContext) -> ExecutorResult:
                captured.append(("fake", context))
                return ExecutorResult(
                    executor=self.name,
                    status="completed",
                    exit_code=0,
                    summary="ok",
                )

        self.add_task(executor="other-executor")
        dispatcher = Dispatcher(
            self.store,
            executor_registry={"fake": CapturingFakeExecutor()},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="other-executor",
        )

        result = dispatcher.dispatch_task("AT-0013", executor_name="fake")

        self.assertEqual(result.status, "waiting_approval")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], "fake")


class DispatcherPiIntegrationTests(unittest.TestCase):
    """Phase 13: controlled integration smoke test using a fake pi executable.

    Does NOT call real Pi/MiniMax. Uses subprocess with a controlled fake.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.worktree_path = self.repo_path / ".worktrees" / "AT-PI01"
        self.artifact_dir = self.root / "artifacts" / "AT-PI01"

        self.repo_path.mkdir()
        self.worktree_path.mkdir(parents=True)
        self.artifact_dir.mkdir(parents=True)
        (self.artifact_dir / "implementation_prompt.md").write_text(
            "Implement the feature.", encoding="utf-8"
        )

        # Create a fake pi binary that exits 0 (success)
        # PiExecutor passes prompt content via -p, so we accept any args
        self.fake_pi = self.root / "fake_pi"
        self.fake_pi.write_text(
            "#!/bin/sh\n"
            "# Fake pi: just succeeds (exit 0) so dispatcher can proceed\n"
            "exit 0\n",
            encoding="utf-8",
        )
        self.fake_pi.chmod(0o755)

        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add_task(
        self,
        *,
        task_key: str = "AT-PI01",
        status: str = "queued",
        **task_kwargs: object,
    ) -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                hermes_task_id="t_at_pi01",
                title="Task AT-PI01",
                status=status,
                repo_path=self.repo_path,
                artifact_dir=self.artifact_dir,
                **task_kwargs,
            )
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=task_key,
                repo_path=self.repo_path,
                worktree_path=self.worktree_path,
                branch=f"task/{task_key}",
                base_branch="main",
                status="active",
            )
        )

    def test_dispatcher_pi_with_fake_binary_succeeds(self) -> None:
        """Dispatcher using pi executor with a fake pi binary completes successfully."""
        self.add_task(
            executor="pi",
            provider="minimax",
            model="minimax-01",
            tools=["Read", "Write"],
            pi_bin=str(self.fake_pi),
        )
        dispatcher = Dispatcher(
            self.store,
            executor_registry={},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="manual",
        )

        result = dispatcher.dispatch_task("AT-PI01")

        self.assertEqual(result.status, "waiting_approval")
        self.assertEqual(result.executor_status, "completed")

    def test_dispatcher_pi_fake_binary_logs_command(self) -> None:
        """Fake pi binary logs the command that was invoked."""
        self.add_task(
            executor="pi",
            provider="minimax",
            model="test-model",
            tools=["Bash"],
            pi_bin=str(self.fake_pi),
        )
        dispatcher = Dispatcher(
            self.store,
            executor_registry={},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="manual",
        )

        result = dispatcher.dispatch_task("AT-PI01")

        self.assertEqual(result.status, "waiting_approval")
        log_path = self.artifact_dir / "pi-executor.log"
        self.assertTrue(log_path.exists(), "pi-executor.log should be created")
        log_content = log_path.read_text(encoding="utf-8")
        self.assertIn("--provider", log_content)
        self.assertIn("minimax", log_content)
        self.assertIn("--model", log_content)
        self.assertIn("test-model", log_content)
        self.assertIn("--tools", log_content)
        self.assertIn("Bash", log_content)
        self.assertIn("--no-session", log_content)

    def test_dispatcher_pi_with_task_only_executor_selection(self) -> None:
        """task.executor='pi' with no extra fields still selects pi executor."""
        self.add_task(executor="pi", pi_bin=str(self.fake_pi))
        dispatcher = Dispatcher(
            self.store,
            executor_registry={},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="manual",
        )

        result = dispatcher.dispatch_task("AT-PI01")

        self.assertEqual(result.status, "waiting_approval")

    def test_dispatcher_pi_without_prompt_blocks(self) -> None:
        """pi executor blocks when implementation_prompt.md is missing."""
        (self.artifact_dir / "implementation_prompt.md").unlink()
        self.add_task(
            executor="pi",
            pi_bin=str(self.fake_pi),
        )
        dispatcher = Dispatcher(
            self.store,
            executor_registry={},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="pi",
        )

        result = dispatcher.dispatch_task("AT-PI01")

        self.assertEqual(result.status, "blocked")


class DispatcherCliTests(unittest.TestCase):
    def test_run_dispatcher_help_executes(self) -> None:
        result = subprocess.run(
            ["python3", "scripts/run_dispatcher.py", "--help"],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("--task-key", result.stdout)
        self.assertIn("--validators", result.stdout)

    def test_run_dispatcher_missing_task_key_fails_with_usage(self) -> None:
        result = subprocess.run(
            ["python3", "scripts/run_dispatcher.py"],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage:", result.stderr.lower())
        self.assertIn("--task-key", result.stderr)




# ----------------------------------------------------------------------
# Phase 20: Mission Contract integration tests
# ----------------------------------------------------------------------


class DispatcherMissionContractTests(unittest.TestCase):
    """Tests that dispatcher writes mission_contract.json before executor runs."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.worktree_path = self.repo_path / ".worktrees" / "AT-MC01"
        self.artifact_dir = self.root / "artifacts" / "AT-MC01"

        self.repo_path.mkdir()
        self.worktree_path.mkdir(parents=True)
        self.artifact_dir.mkdir(parents=True)

        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add_task(
        self,
        *,
        task_key: str = "AT-MC01",
        status: str = "queued",
        executor: str = "noop",
        title: str = "Mission contract test",
    ) -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                hermes_task_id=f"t_{task_key.lower().replace('-', '_')}",
                title=title,
                status=status,
                repo_path=self.repo_path,
                artifact_dir=self.artifact_dir,
                executor=executor,
            )
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=task_key,
                repo_path=self.repo_path,
                worktree_path=self.worktree_path,
                branch=f"task/{task_key}",
                base_branch="main",
                status="active",
            )
        )

    def test_dispatcher_writes_mission_contract_before_executor(self) -> None:
        """dispatch_task writes mission_contract.json before executor runs."""
        self.add_task()
        dispatcher = Dispatcher(
            self.store,
            executor_registry={"noop": FakeExecutor("completed")},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="noop",
        )

        contract_path = self.artifact_dir / "mission_contract.json"
        self.assertFalse(contract_path.exists())

        result = dispatcher.dispatch_task("AT-MC01")

        self.assertEqual(result.status, "waiting_approval")
        self.assertTrue(contract_path.exists(), "mission_contract.json must exist")

    def test_mission_contract_contains_executor_name(self) -> None:
        """The contract reflects the selected executor name."""
        self.add_task(executor="manual")
        dispatcher = Dispatcher(
            self.store,
            executor_registry={"manual": FakeExecutor("completed")},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="manual",
        )

        dispatcher.dispatch_task("AT-MC01")

        import json
        contract_path = self.artifact_dir / "mission_contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        self.assertEqual(contract["executor"], "manual")

    def test_mission_contract_contains_validators(self) -> None:
        """The contract reflects the dispatcher's selected validators."""
        self.add_task()
        dispatcher = Dispatcher(
            self.store,
            executor_registry={"noop": FakeExecutor("completed")},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="noop",
        )

        dispatcher.dispatch_task("AT-MC01")

        import json
        contract_path = self.artifact_dir / "mission_contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        self.assertIn("pytest", contract["required_validators"])

    def test_mission_contract_has_human_approval_required_true(self) -> None:
        """human_approval_required is always true."""
        self.add_task()
        dispatcher = Dispatcher(
            self.store,
            executor_registry={"noop": FakeExecutor("completed")},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="noop",
        )

        dispatcher.dispatch_task("AT-MC01")

        import json
        contract_path = self.artifact_dir / "mission_contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        self.assertTrue(contract["human_approval_required"])

    def test_mission_contract_has_all_forbidden_actions(self) -> None:
        """forbidden_actions includes all required governance prohibitions."""
        self.add_task()
        dispatcher = Dispatcher(
            self.store,
            executor_registry={"noop": FakeExecutor("completed")},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="noop",
        )

        dispatcher.dispatch_task("AT-MC01")

        import json
        contract_path = self.artifact_dir / "mission_contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        required_actions = {
            "approve", "push", "merge", "cleanup",
            "delete_worktree", "delete_branch", "self_approve", "force_push"
        }
        self.assertTrue(required_actions.issubset(set(contract["forbidden_actions"])))

    def test_mission_contract_has_governance_rules(self) -> None:
        """The contract includes a governance_rules list."""
        self.add_task()
        dispatcher = Dispatcher(
            self.store,
            executor_registry={"noop": FakeExecutor("completed")},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="noop",
        )

        dispatcher.dispatch_task("AT-MC01")

        import json
        contract_path = self.artifact_dir / "mission_contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        self.assertIn("governance_rules", contract)
        self.assertTrue(len(contract["governance_rules"]) > 0)

    def test_mission_contract_contains_paths(self) -> None:
        """The contract contains repo_path, worktree_path, and artifact_dir."""
        self.add_task()
        dispatcher = Dispatcher(
            self.store,
            executor_registry={"noop": FakeExecutor("completed")},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="noop",
        )

        dispatcher.dispatch_task("AT-MC01")

        import json
        contract_path = self.artifact_dir / "mission_contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        self.assertIn("repo_path", contract)
        self.assertIn("worktree_path", contract)
        self.assertIn("artifact_dir", contract)

    def test_dry_run_does_not_write_mission_contract(self) -> None:
        """dry_run does not write mission_contract.json."""
        self.add_task()
        dispatcher = Dispatcher(
            self.store,
            executor_registry={"noop": FakeExecutor("completed")},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="noop",
        )

        contract_path = self.artifact_dir / "mission_contract.json"
        self.assertFalse(contract_path.exists())

        result = dispatcher.dispatch_task("AT-MC01", dry_run=True)

        self.assertEqual(result.status, "skipped")
        self.assertFalse(contract_path.exists())


class DispatcherPolicyIntegrationTests(unittest.TestCase):
    """Tests that the policy validator can validate dispatcher-produced artifacts."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.worktree_path = self.repo_path / ".worktrees" / "AT-PI01"
        self.artifact_dir = self.root / "artifacts" / "AT-PI01"

        self.repo_path.mkdir()
        self.worktree_path.mkdir(parents=True)
        self.artifact_dir.mkdir(parents=True)

        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add_task(
        self,
        *,
        task_key: str = "AT-PI01",
        status: str = "queued",
        executor: str = "noop",
    ) -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                hermes_task_id=f"t_{task_key.lower().replace('-', '_')}",
                title=f"Task {task_key}",
                status=status,
                repo_path=self.repo_path,
                artifact_dir=self.artifact_dir,
                executor=executor,
            )
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=task_key,
                repo_path=self.repo_path,
                worktree_path=self.worktree_path,
                branch=f"task/{task_key}",
                base_branch="main",
                status="active",
            )
        )

    def test_policy_validator_passes_on_dispatcher_artifact_dir(self) -> None:
        """PolicyCheckValidator passes on a dispatcher-produced artifact_dir."""
        self.add_task()
        dispatcher = Dispatcher(
            self.store,
            executor_registry={"noop": FakeExecutor("completed")},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="noop",
        )

        result = dispatcher.dispatch_task("AT-PI01")
        self.assertEqual(result.status, "waiting_approval")

        # Now run policy validator against the same artifact_dir
        from agent_taskflow.validators.policy import PolicyCheckValidator
        from agent_taskflow.validators.base import ValidatorContext

        policy_ctx = ValidatorContext(
            task_key="AT-PI01",
            project="agent-taskflow",
            worktree_path=self.worktree_path,
            artifact_dir=self.artifact_dir,
        )
        policy_result = PolicyCheckValidator(scan_artifacts=True).run(policy_ctx)
        self.assertEqual(policy_result.status, "passed")

    def test_policy_validator_fails_on_suspicious_executor_log(self) -> None:
        """PolicyCheckValidator fails when executor log contains forbidden action."""
        self.add_task()

        # First dispatch normally so the contract is written
        dispatcher = Dispatcher(
            self.store,
            executor_registry={"noop": FakeExecutor("completed")},
            validator_registry={"pytest": FakeValidator("pytest", "passed")},
            validators=("pytest",),
            default_executor="noop",
        )
        result = dispatcher.dispatch_task("AT-PI01")
        self.assertEqual(result.status, "waiting_approval")

        # Simulate an executor log with a forbidden action (not a .log file,
        # since policy validator skips .log — use a plain .txt instead)
        executor_log = self.artifact_dir / "executor-work-log.txt"
        executor_log.write_text(
            "Task executed successfully.\n"
            "git push origin main\n",
            encoding="utf-8",
        )

        from agent_taskflow.validators.policy import PolicyCheckValidator
        from agent_taskflow.validators.base import ValidatorContext

        policy_ctx = ValidatorContext(
            task_key="AT-PI01",
            project="agent-taskflow",
            worktree_path=self.worktree_path,
            artifact_dir=self.artifact_dir,
        )
        policy_result = PolicyCheckValidator(scan_artifacts=True).run(policy_ctx)
        self.assertEqual(policy_result.status, "failed")
        self.assertIn("git push", policy_result.summary or "")

    def test_mission_contract_does_not_trigger_false_positive(self) -> None:
        """mission_contract.json listing forbidden_actions does not fail policy."""
        self.add_task()

        # Write contract manually to include forbidden_actions (normal case)
        import json
        contract_path = self.artifact_dir / "mission_contract.json"
        contract = {
            "schema_version": "1",
            "task_key": "AT-PI01",
            "goal": "Task AT-PI01",
            "repo_path": str(self.repo_path),
            "worktree_path": str(self.worktree_path),
            "artifact_dir": str(self.artifact_dir),
            "executor": "noop",
            "required_validators": ["pytest"],
            "forbidden_actions": [
                "approve", "push", "merge", "cleanup",
                "delete_worktree", "delete_branch",
                "self_approve", "force_push"
            ],
            "expected_artifacts": ["executor_log"],
            "human_approval_required": True,
            "governance_rules": ["Worker cannot approve.", "Worker cannot push."],
        }
        contract_path.write_text(json.dumps(contract, indent=2), encoding="utf-8")

        from agent_taskflow.validators.policy import PolicyCheckValidator
        from agent_taskflow.validators.base import ValidatorContext

        policy_ctx = ValidatorContext(
            task_key="AT-PI01",
            project="agent-taskflow",
            worktree_path=self.worktree_path,
            artifact_dir=self.artifact_dir,
        )
        policy_result = PolicyCheckValidator(scan_artifacts=True).run(policy_ctx)
        self.assertEqual(policy_result.status, "passed")

    def test_policy_validator_skips_own_log(self) -> None:
        """policy-validate.log does not trigger false positives."""
        self.add_task()

        # Write contract
        import json
        contract_path = self.artifact_dir / "mission_contract.json"
        contract_path.write_text(
            json.dumps({
                "schema_version": "1",
                "task_key": "AT-PI01",
                "goal": "Task",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
                "executor": "noop",
                "required_validators": ["pytest"],
                "forbidden_actions": [
                    "approve", "push", "merge", "cleanup",
                    "delete_worktree", "delete_branch",
                    "self_approve", "force_push"
                ],
                "expected_artifacts": ["executor_log"],
                "human_approval_required": True,
                "governance_rules": ["Worker cannot approve.", "Worker cannot push."],
            }),
            encoding="utf-8",
        )

        # Write policy validator's own log (containing suspicious text)
        policy_log = self.artifact_dir / "policy-validate.log"
        policy_log.write_text(
            "Validator: policy\n"
            "FAILURE: git push detected in executor-work-log.txt\n",
            encoding="utf-8",
        )

        from agent_taskflow.validators.policy import PolicyCheckValidator
        from agent_taskflow.validators.base import ValidatorContext

        policy_ctx = ValidatorContext(
            task_key="AT-PI01",
            project="agent-taskflow",
            worktree_path=self.worktree_path,
            artifact_dir=self.artifact_dir,
        )
        policy_result = PolicyCheckValidator(scan_artifacts=True).run(policy_ctx)
        # Should pass because policy-validate.log is skipped
        self.assertEqual(policy_result.status, "passed")

    def test_policy_validator_skips_pytest_log(self) -> None:
        """pytest.log does not trigger false positives."""
        self.add_task()

        import json
        contract_path = self.artifact_dir / "mission_contract.json"
        contract_path.write_text(
            json.dumps({
                "schema_version": "1",
                "task_key": "AT-PI01",
                "goal": "Task",
                "repo_path": str(self.repo_path),
                "worktree_path": str(self.worktree_path),
                "artifact_dir": str(self.artifact_dir),
                "executor": "noop",
                "required_validators": ["pytest"],
                "forbidden_actions": [
                    "approve", "push", "merge", "cleanup",
                    "delete_worktree", "delete_branch",
                    "self_approve", "force_push"
                ],
                "expected_artifacts": ["executor_log"],
                "human_approval_required": True,
                "governance_rules": ["Worker cannot approve.", "Worker cannot push."],
            }),
            encoding="utf-8",
        )

        # Write pytest log containing suspicious text
        pytest_log = self.artifact_dir / "pytest.log"
        pytest_log.write_text(
            "WARNING: git push should not be used in this project.\n",
            encoding="utf-8",
        )

        from agent_taskflow.validators.policy import PolicyCheckValidator
        from agent_taskflow.validators.base import ValidatorContext

        policy_ctx = ValidatorContext(
            task_key="AT-PI01",
            project="agent-taskflow",
            worktree_path=self.worktree_path,
            artifact_dir=self.artifact_dir,
        )
        policy_result = PolicyCheckValidator(scan_artifacts=True).run(policy_ctx)
        # Should pass because pytest.log is skipped
        self.assertEqual(policy_result.status, "passed")


if __name__ == "__main__":
    unittest.main()
