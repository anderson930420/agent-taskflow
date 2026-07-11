from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.executors.base import ExecutorContext, ExecutorResult
from agent_taskflow.lifecycle_control import (
    LifecycleTransitionError,
    RuntimeControlStore,
    RuntimePausedError,
    validate_attempt_transition,
    validate_reason_code,
)
from agent_taskflow.lifecycle_control_schema import (
    LIFECYCLE_CONTROL_MIGRATION,
    migrate_lifecycle_control,
)
from agent_taskflow.lifecycle_runtime_path import LifecycleRuntimeTaskStore
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore, connect
from agent_taskflow.validators.base import ValidatorContext, ValidatorResult


class _ResultExecutor:
    name = "fake"

    def __init__(self, result: ExecutorResult) -> None:
        self.result = result
        self.calls = 0

    def run(self, context: ExecutorContext) -> ExecutorResult:
        self.calls += 1
        return self.result


class _ResultValidator:
    name = "fake-validator"

    def __init__(self, result: ValidatorResult) -> None:
        self.result = result
        self.calls = 0

    def run(self, context: ValidatorContext) -> ValidatorResult:
        self.calls += 1
        return self.result


class LifecycleControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.repo / "README.md").write_text("test\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")
        self.db_path = self.root / "state.db"
        self.artifact_base = self.root / "artifacts" / "AT-PR6-1"
        self.artifact_base.mkdir(parents=True)
        (self.artifact_base / "issue_spec.md").write_text("issue\n", encoding="utf-8")
        base = TaskMirrorStore(self.db_path)
        base.init_db()
        base.upsert_task(
            TaskRecord(
                task_key="AT-PR6-1",
                project="agent-taskflow",
                board="agent-taskflow",
                title="Lifecycle control",
                status="queued",
                repo_path=self.repo,
                artifact_dir=self.artifact_base,
                executor="noop",
            )
        )
        self.base_store = base

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _claim(self) -> LifecycleRuntimeTaskStore:
        store = LifecycleRuntimeTaskStore(
            self.db_path,
            heartbeat_interval_seconds=60,
        )
        store.preclaim_runtime(
            "AT-PR6-1",
            source="test-runtime",
            artifact_base_root=self.artifact_base,
            worktree_root=self.repo / ".worktrees",
            base_branch="main",
        )
        workspace = store.prepare_attempt_workspace("AT-PR6-1")
        self.assertTrue(workspace.ok, workspace.summary)
        return store

    def _executor_context(self, store: LifecycleRuntimeTaskStore) -> ExecutorContext:
        resource = store.attempt_resource("AT-PR6-1")
        assert resource is not None
        return ExecutorContext(
            task_key="AT-PR6-1",
            project="agent-taskflow",
            worktree_path=resource.worktree_path,
            artifact_dir=resource.artifact_root,
            repo_root=resource.repo_path,
        )

    def _validator_context(self, store: LifecycleRuntimeTaskStore) -> ValidatorContext:
        resource = store.attempt_resource("AT-PR6-1")
        assert resource is not None
        return ValidatorContext(
            task_key="AT-PR6-1",
            project="agent-taskflow",
            worktree_path=resource.worktree_path,
            artifact_dir=resource.artifact_root,
        )

    def test_migration_is_idempotent_and_installs_graph_and_controls(self) -> None:
        migrate_lifecycle_control(self.db_path)
        migrate_lifecycle_control(self.db_path)
        with closing(connect(self.db_path)) as conn:
            migration_count = conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE name = ?",
                (LIFECYCLE_CONTROL_MIGRATION,),
            ).fetchone()[0]
            transition_count = conn.execute(
                "SELECT COUNT(*) FROM lifecycle_allowed_transitions"
            ).fetchone()[0]
            trigger = conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'trigger' AND name = 'lifecycle_attempt_transition_guard'
                """
            ).fetchone()
        self.assertEqual(migration_count, 1)
        self.assertGreater(transition_count, 20)
        self.assertIsNotNone(trigger)

    def test_runtime_transitions_update_attempt_and_task_together(self) -> None:
        store = self._claim()
        attempts = AttemptStore(self.db_path)
        store.update_task_status("AT-PR6-1", "implementing", source="test")
        attempt = attempts.get_active_attempt("AT-PR6-1")
        task = self.base_store.get_task("AT-PR6-1")
        assert attempt is not None and task is not None
        self.assertEqual(attempt.status, "implementing")
        self.assertEqual(task.status, "implementing")

        store.update_task_status("AT-PR6-1", "validating", source="test")
        attempt = attempts.get_active_attempt("AT-PR6-1")
        task = self.base_store.get_task("AT-PR6-1")
        assert attempt is not None and task is not None
        self.assertEqual(attempt.status, "validating")
        self.assertEqual(task.status, "validating")

        attempt_id = attempt.attempt_id
        store.update_task_status("AT-PR6-1", "waiting_approval", source="test")
        closed = attempts.get_attempt(attempt_id)
        task = self.base_store.get_task("AT-PR6-1")
        assert closed is not None and task is not None
        self.assertEqual(closed.status, "waiting_approval")
        self.assertEqual(closed.execution_result, "completed")
        self.assertEqual(closed.validation_result, "passed")
        self.assertEqual(task.status, "waiting_approval")

    def test_graph_rejects_backward_and_terminal_reopen(self) -> None:
        with self.assertRaises(LifecycleTransitionError):
            validate_attempt_transition("validating", "implementing")
        store = self._claim()
        store.update_task_status("AT-PR6-1", "implementing", source="test")
        claim = store.runtime_claim("AT-PR6-1")
        assert claim is not None
        with closing(connect(self.db_path)) as conn:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "illegal attempt lifecycle transition",
            ):
                with conn:
                    conn.execute(
                        "UPDATE attempts SET status = 'preparing' WHERE attempt_id = ?",
                        (claim.attempt_id,),
                    )
        store.update_task_status("AT-PR6-1", "blocked", source="test")

    def test_executor_timeout_closes_as_execution_timeout(self) -> None:
        store = self._claim()
        store.update_task_status("AT-PR6-1", "implementing", source="test")
        executor = store.wrap_executor(
            _ResultExecutor(
                ExecutorResult(
                    executor="fake",
                    status="failed",
                    summary="Executor timed out after 5 seconds.",
                )
            )
        )
        result = executor.run(self._executor_context(store))
        self.assertEqual(result.status, "failed")
        claim = store.runtime_claim("AT-PR6-1")
        assert claim is not None
        store.update_task_status(
            "AT-PR6-1",
            "blocked",
            source="test",
            blocked_reason=result.summary,
        )
        attempt = AttemptStore(self.db_path).get_attempt(claim.attempt_id)
        assert attempt is not None
        self.assertEqual(attempt.status, "execution_timeout")
        self.assertEqual(attempt.execution_result, "timed_out")

    def test_validator_failure_closes_as_validation_failed(self) -> None:
        store = self._claim()
        store.update_task_status("AT-PR6-1", "implementing", source="test")
        store.update_task_status("AT-PR6-1", "validating", source="test")
        validator = store.wrap_validator(
            _ResultValidator(
                ValidatorResult(
                    validator="fake-validator",
                    status="failed",
                    summary="policy failed",
                )
            )
        )
        result = validator.run(self._validator_context(store))
        claim = store.runtime_claim("AT-PR6-1")
        assert claim is not None
        store.update_task_status(
            "AT-PR6-1",
            "blocked",
            source="test",
            blocked_reason=result.summary,
        )
        attempt = AttemptStore(self.db_path).get_attempt(claim.attempt_id)
        assert attempt is not None
        self.assertEqual(attempt.status, "validation_failed")
        self.assertEqual(attempt.validation_result, "failed")

    def test_pause_denies_new_claim_but_does_not_abort_active_attempt(self) -> None:
        controls = RuntimeControlStore(self.db_path)
        controls.pause(actor="operator")
        with self.assertRaises(RuntimePausedError):
            self._claim()
        controls.clear(actor="operator")
        store = self._claim()
        controls.pause(actor="operator")
        store.update_task_status("AT-PR6-1", "implementing", source="test")
        attempt = AttemptStore(self.db_path).get_active_attempt("AT-PR6-1")
        assert attempt is not None
        self.assertEqual(attempt.status, "implementing")
        store.update_task_status("AT-PR6-1", "blocked", source="test")

    def test_kill_switch_aborts_cooperatively_without_invoking_executor(self) -> None:
        store = self._claim()
        store.update_task_status("AT-PR6-1", "implementing", source="test")
        claim = store.runtime_claim("AT-PR6-1")
        assert claim is not None
        RuntimeControlStore(self.db_path).request_kill(
            scope_kind="attempt",
            scope_id=claim.attempt_id,
            actor="operator",
        )
        inner = _ResultExecutor(
            ExecutorResult(executor="fake", status="completed", summary="done")
        )
        executor = store.wrap_executor(inner)
        result = executor.run(self._executor_context(store))
        self.assertEqual(result.status, "blocked")
        self.assertEqual(inner.calls, 0)
        store.update_task_status(
            "AT-PR6-1",
            "blocked",
            source="test",
            blocked_reason=result.summary,
        )
        attempt = AttemptStore(self.db_path).get_attempt(claim.attempt_id)
        assert attempt is not None
        self.assertEqual(attempt.status, "execution_aborted")
        self.assertEqual(attempt.execution_result, "aborted")
        events = AttemptStore(self.db_path).list_lifecycle_events("AT-PR6-1")
        terminal = events[-1]
        self.assertEqual(terminal.reason_code, "operator_kill_requested")
        self.assertIn('"os_signal_sent": false', terminal.metadata_json)

    def test_reason_codes_are_closed_taxonomy(self) -> None:
        self.assertEqual(validate_reason_code("executor_timeout"), "executor_timeout")
        with self.assertRaisesRegex(ValueError, "Unknown lifecycle reason_code"):
            validate_reason_code("some free form explanation")


if __name__ == "__main__":
    unittest.main()
