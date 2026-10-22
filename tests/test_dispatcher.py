from __future__ import annotations

import sqlite3
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
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

    def test_validation_summary_uses_the_claim_observed_by_the_executor(self) -> None:
        self.add_task()
        executor = FakeExecutor()
        validator = FakeValidator("unit")

        def validate(context):
            log = context.artifact_dir / "unit.log"
            log.write_text("observed unit result")
            return ValidatorResult("unit", "passed", exit_code=0, log_path=log)

        validator.run = validate
        result = self.make_dispatcher(
            executor=executor, validators={"unit": validator}, validator_names=("unit",),
        ).dispatch_task("AT-0007")
        self.assertEqual(result.status, "waiting_approval")
        paths = list(self.root.rglob("validation-summary.json"))
        self.assertEqual(len(paths), 1)
        data = json.loads(paths[0].read_text())
        self.assertEqual(data["attempt_id"], executor.contexts[0].attempt_id)
        self.assertIsNotNone(data["attempt_id"])
        self.assertEqual(data["attempt_binding"], "runtime_claim")
        self.assertEqual(data["source"], "dispatcher")
        self.assertTrue(data["passed"])
        self.assertEqual(data["executor_run_id"], self.store.list_executor_runs("AT-0007")[0]["run_id"])
        artifacts = self.store.list_task_artifacts("AT-0007")
        self.assertTrue(any(Path(a.path) == paths[0] for a in artifacts))

    def test_stopped_executor_leaves_validators_unrun_in_summary(self) -> None:
        self.add_task()
        result = self.make_dispatcher(executor=FakeExecutor("failed")).dispatch_task("AT-0007")
        self.assertEqual(result.status, "blocked")
        data = json.loads(next(self.root.rglob("validation-summary.json")).read_text())
        self.assertEqual(data["state"], "stopped")
        self.assertFalse(data["complete"])
        self.assertTrue(all(row["started_at"] is None and row["result"] == "not_run" for row in data["validators"]))

    def test_validator_exception_preserves_partial_summary(self) -> None:
        self.add_task()
        validators = {name: FakeValidator(name) for name in ("first", "broken", "last")}
        validators["broken"].run = mock.Mock(side_effect=RuntimeError("validator crashed"))
        result = self.make_dispatcher(validators=validators, validator_names=tuple(validators)).dispatch_task("AT-0007")
        self.assertEqual(result.status, "blocked")
        data = json.loads(next(self.root.rglob("validation-summary.json")).read_text())
        self.assertEqual([row["result"] for row in data["validators"]], ["passed", "tool_error", "not_run"])
        self.assertEqual(data["validators"][1]["error"]["message"], "validator crashed")
        self.assertFalse(data["passed"])

    def test_skipped_and_missing_logs_keep_lifecycle_but_never_summary_success(self) -> None:
        self.add_task()
        result = self.make_dispatcher().dispatch_task("AT-0007")
        self.assertEqual(result.status, "waiting_approval")
        data = json.loads(next(self.root.rglob("validation-summary.json")).read_text())
        self.assertEqual([row["result"] for row in data["validators"]], ["passed", "skipped"])
        self.assertFalse(data["complete"])
        self.assertFalse(data["passed"])
        self.assertIsNone(data["validators"][1]["exit_code"])

    def test_empty_validators_keep_legacy_lifecycle_without_summary_success(self) -> None:
        self.add_task()
        result = self.make_dispatcher(validator_names=()).dispatch_task("AT-0007")
        self.assertEqual(result.status, "waiting_approval")
        data = json.loads(next(self.root.rglob("validation-summary.json")).read_text())
        self.assertEqual(data["validators"], [])
        self.assertFalse(data["passed"])

    def _assert_summary_error_recorded_and_executor_closed(self) -> None:
        errors = [json.loads(event.payload_json) for event in self.store.list_task_events("AT-0007")
                  if event.payload_json and json.loads(event.payload_json).get("kind") == "validation_summary_error"]
        self.assertEqual(len(errors), 1)
        self.assertFalse(errors[0]["complete"])
        self.assertFalse(errors[0]["passed"])
        run = self.store.list_executor_runs("AT-0007")[0]
        self.assertIsNotNone(run["status"])
        self.assertIsNotNone(run["finished_at"])
        with sqlite3.connect(self.db_path) as connection:
            self.assertIsNone(connection.execute("SELECT active_attempt_id FROM tasks WHERE task_key='AT-0007'").fetchone()[0])

    def test_summary_construction_io_error_preserves_successful_runtime_closeout(self) -> None:
        self.add_task()
        executor = FakeExecutor()
        with mock.patch("agent_taskflow.validation_summary.atomic_write_json", side_effect=OSError("summary unavailable")):
            result = self.make_dispatcher(executor=executor).dispatch_task("AT-0007")
        self.assertEqual(result.status, "waiting_approval")
        self.assertEqual(len(executor.contexts), 1)
        self.assertEqual(list(self.root.rglob("validation-summary.json")), [])
        self._assert_summary_error_recorded_and_executor_closed()

    def test_summary_registration_io_error_keeps_failing_executor_verdict(self) -> None:
        self.add_task()
        record = TaskMirrorStore.record_task_artifact
        def register(store, task_key, artifact_type, path):
            if Path(path).name == "validation-summary.json":
                raise OSError("summary index unavailable")
            return record(store, task_key, artifact_type, path)
        with mock.patch.object(TaskMirrorStore, "record_task_artifact", new=register):
            result = self.make_dispatcher(executor=FakeExecutor("failed", "original red executor")).dispatch_task("AT-0007")
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.blocked_reason, "original red executor")
        self._assert_summary_error_recorded_and_executor_closed()
        data = json.loads(next(self.root.rglob("validation-summary.json")).read_text())
        self.assertFalse(data["passed"])

    def test_summary_finish_io_error_does_not_mask_original_executor_exception(self) -> None:
        from agent_taskflow.validation_summary import atomic_write_json
        self.add_task()
        executor = FakeExecutor()
        executor.run = mock.Mock(side_effect=RuntimeError("original executor failure"))
        def write(path, payload, **kwargs):
            if payload.get("state") == "stopped":
                raise OSError("summary finish unavailable")
            return atomic_write_json(path, payload, **kwargs)
        with mock.patch("agent_taskflow.validation_summary.atomic_write_json", side_effect=write):
            result = self.make_dispatcher(executor=executor).dispatch_task("AT-0007")
        self.assertIn("original executor failure", result.blocked_reason)
        self.assertNotIn("summary finish unavailable", result.blocked_reason)
        self._assert_summary_error_recorded_and_executor_closed()

    def test_summary_error_write_does_not_mask_original_validator_exception(self) -> None:
        from agent_taskflow.validation_summary import atomic_write_json
        self.add_task()
        validator = FakeValidator("broken")
        validator.run = mock.Mock(side_effect=RuntimeError("original validator failure"))
        def write(path, payload, **kwargs):
            if Path(path).name.endswith("-error.json"):
                raise OSError("summary error output unavailable")
            return atomic_write_json(path, payload, **kwargs)
        with mock.patch("agent_taskflow.validation_summary.atomic_write_json", side_effect=write):
            result = self.make_dispatcher(validators={"broken": validator}, validator_names=("broken",)).dispatch_task("AT-0007")
        self.assertIn("original validator failure", result.blocked_reason)
        self._assert_summary_error_recorded_and_executor_closed()

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
        self.add_task(status="queued")
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

    def test_missing_worktree_record_is_blocked_without_creating_workspace(self) -> None:
        self.worktree_path.rmdir()
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-0007",
                project="agent-taskflow",
                board="agent-taskflow",
                hermes_task_id="t_at_0007",
                title="Task AT-0007",
                status="queued",
                repo_path=self.repo_path,
                artifact_dir=self.artifact_dir,
            )
        )
        dispatcher = self.make_dispatcher()

        result = dispatcher.dispatch_task("AT-0007")

        task = self.store.get_task("AT-0007")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(result.status, "blocked")
        self.assertEqual(task.status, "blocked")
        self.assertIn("Task worktree not found", result.blocked_reason or "")
        self.assertFalse(self.worktree_path.exists())

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
        provenance = fake_executor.contexts[0].launch_provenance
        self.assertEqual(provenance.canonical_execution_path, "dispatcher")
        self.assertEqual(provenance.path_source, "Dispatcher.dispatch_task")
        self.assertEqual(provenance.config_snapshot_reference.reference,
                         "dispatcher.resolved_configuration")
        self.assertIsNone(provenance.spec_reference)

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
        # A prompt file, so the executor context carries a prompt_path
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


class DispatcherCliTests(unittest.TestCase):
    def test_run_dispatcher_help_executes(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/run_dispatcher.py", "--help"],
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
            [sys.executable, "scripts/run_dispatcher.py"],
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


if __name__ == "__main__":
    unittest.main()
