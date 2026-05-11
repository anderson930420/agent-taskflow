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


if __name__ == "__main__":
    unittest.main()
