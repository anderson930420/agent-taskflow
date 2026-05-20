from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.queued_task_handoff import (
    APPROVED_TASK_STATUS,
    QueuedTaskHandoffRequest,
    QueuedTaskHandoffResult,
    run_queued_task_handoff,
)
from agent_taskflow.task_execution_package import (
    IMPLEMENTATION_PROMPT_FILENAME,
    PACKAGE_FILENAME,
    SCHEMA_VERSION,
    TaskExecutionPackageRequest,
    create_task_execution_package,
)


@dataclass
class FakeApprovedTaskRunnerResult:
    ok: bool
    status: str
    phase: str
    task_key: str
    executor: str
    dry_run: bool = False
    preflight: dict[str, Any] = field(default_factory=dict)
    workspace: dict[str, Any] = field(default_factory=dict)
    executor_run: dict[str, Any] = field(default_factory=dict)
    validators: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    safety: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "phase": self.phase,
            "task_key": self.task_key,
            "executor": self.executor,
            "dry_run": self.dry_run,
            "preflight": self.preflight,
            "workspace": self.workspace,
            "executor_run": self.executor_run,
            "validators": self.validators,
            "artifacts": self.artifacts,
            "summary": self.summary,
            "safety": self.safety,
            "error": self.error,
        }


class _RunnerSpy:
    def __init__(self, result: FakeApprovedTaskRunnerResult) -> None:
        self.result = result
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def __call__(self, request: Any, **kwargs: Any) -> FakeApprovedTaskRunnerResult:
        self.calls.append((request, kwargs))
        return self.result


def _waiting_approval_runner_result(task_key: str, executor: str) -> FakeApprovedTaskRunnerResult:
    return FakeApprovedTaskRunnerResult(
        ok=True,
        status=APPROVED_TASK_STATUS,
        phase=APPROVED_TASK_STATUS,
        task_key=task_key,
        executor=executor,
        safety={
            "workspace_prepared": True,
            "executor_started": True,
            "validators_started": True,
            "db_written": True,
            "artifact_written": True,
            "branch_pushed": False,
            "pr_created": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
        },
    )


def _blocked_runner_result(task_key: str, executor: str, error: str) -> FakeApprovedTaskRunnerResult:
    return FakeApprovedTaskRunnerResult(
        ok=False,
        status="blocked",
        phase="executor",
        task_key=task_key,
        executor=executor,
        error=error,
        safety={
            "workspace_prepared": True,
            "executor_started": True,
            "validators_started": False,
            "db_written": True,
            "artifact_written": True,
            "branch_pushed": False,
            "pr_created": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
        },
    )


class QueuedTaskHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.artifact_root = self.root / "artifacts"
        self.artifact_dir = self.artifact_root / "AT-HANDOFF-1"
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_task(
        self,
        *,
        task_key: str = "AT-HANDOFF-1",
        status: str = "queued",
    ) -> TaskRecord:
        task = TaskRecord(
            task_key=task_key,
            project="agent-taskflow",
            board="agent-taskflow",
            title="Handoff test task",
            status=status,
            repo_path=self.repo,
            artifact_dir=self.artifact_dir,
        )
        self.store.upsert_task(task)
        return task

    def _create_valid_package(self) -> None:
        create_task_execution_package(
            TaskExecutionPackageRequest(
                task_key="AT-HANDOFF-1",
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                dry_run=False,
                confirm=True,
            ),
            store=self.store,
        )

    def _request(self, **overrides: Any) -> QueuedTaskHandoffRequest:
        kwargs: dict[str, Any] = {
            "task_key": "AT-HANDOFF-1",
            "executor": "shell",
            "repo_path": self.repo,
            "db_path": self.db_path,
            "artifact_root": self.artifact_root,
            "worktree_root": self.root / "worktrees",
            "base_branch": "main",
            "validators": ("pytest",),
            "command": ("echo", "noop"),
            "preflight": False,
            "dry_run": True,
            "confirm_handoff": False,
        }
        kwargs.update(overrides)
        return QueuedTaskHandoffRequest(**kwargs)

    # 1. Blocks when task does not exist.
    def test_blocks_when_task_missing(self) -> None:
        result = run_queued_task_handoff(self._request())
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.phase, "selection")
        self.assertIn("Task not found", result.error or "")

    # 2. Blocks when task status is not queued.
    def test_blocks_when_status_not_queued(self) -> None:
        self._seed_task(status="waiting_approval")
        self._create_valid_package()
        # Switch task to waiting_approval after package creation so the
        # store row reflects the non-queued status at handoff time.
        # (status starts as 'waiting_approval' in seed; reset it explicitly.)
        self.store.update_task_status(
            "AT-HANDOFF-1",
            "waiting_approval",
            source="test",
        )
        result = run_queued_task_handoff(self._request())
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.phase, "selection")
        self.assertIn("status=", result.error or "")

    # 3. Blocks when task_execution_package.json is missing.
    def test_blocks_when_package_missing(self) -> None:
        self._seed_task()
        result = run_queued_task_handoff(self._request())
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.phase, "package_verification")
        self.assertIn("Task execution package is missing", result.error or "")
        self.assertFalse(result.safety["package_verified"])

    # 4. Blocks when implementation_prompt.md is missing.
    def test_blocks_when_prompt_missing(self) -> None:
        self._seed_task()
        self._create_valid_package()
        (self.artifact_dir / IMPLEMENTATION_PROMPT_FILENAME).unlink()
        result = run_queued_task_handoff(self._request())
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "package_verification")
        self.assertIn("Implementation prompt is missing", result.error or "")

    # 5. Blocks when package JSON is invalid.
    def test_blocks_when_package_json_invalid(self) -> None:
        self._seed_task()
        self._create_valid_package()
        (self.artifact_dir / PACKAGE_FILENAME).write_text("{not json", encoding="utf-8")
        result = run_queued_task_handoff(self._request())
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "package_verification")
        self.assertIn("not valid JSON", result.error or "")

    # 6. Blocks when package schema_version is wrong.
    def test_blocks_when_schema_version_wrong(self) -> None:
        self._seed_task()
        self._create_valid_package()
        package_path = self.artifact_dir / PACKAGE_FILENAME
        data = json.loads(package_path.read_text(encoding="utf-8"))
        data["schema_version"] = "task_execution_package.vBOGUS"
        package_path.write_text(json.dumps(data), encoding="utf-8")
        result = run_queued_task_handoff(self._request())
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "package_verification")
        self.assertIn("schema_version", result.error or "")
        self.assertIn(SCHEMA_VERSION, result.error or "")

    # 7. Blocks when package task_key mismatches.
    def test_blocks_when_task_key_mismatch(self) -> None:
        self._seed_task()
        self._create_valid_package()
        package_path = self.artifact_dir / PACKAGE_FILENAME
        data = json.loads(package_path.read_text(encoding="utf-8"))
        data["task_key"] = "AT-WRONG-1"
        package_path.write_text(json.dumps(data), encoding="utf-8")
        result = run_queued_task_handoff(self._request())
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "package_verification")
        self.assertIn("task_key", result.error or "")

    # 8. Dry-run verifies package and does not call approved_task_runner.
    def test_dry_run_verifies_without_calling_runner(self) -> None:
        self._seed_task()
        self._create_valid_package()
        spy = _RunnerSpy(_waiting_approval_runner_result("AT-HANDOFF-1", "shell"))
        result = run_queued_task_handoff(
            self._request(),
            approved_task_runner=spy,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "preview")
        self.assertEqual(result.phase, "preview")
        self.assertTrue(result.package["verified"])
        self.assertEqual(result.package["schema_version"], SCHEMA_VERSION)
        self.assertEqual(spy.calls, [])
        self.assertFalse(result.safety["approved_task_runner_started"])
        self.assertFalse(result.safety["handoff_confirmed"])
        self.assertFalse(result.safety["workspace_prepared"])
        self.assertFalse(result.safety["executor_started"])
        self.assertIsNone(result.runner_result)

    # 9. Confirmed mode calls approved_task_runner only after package verification.
    def test_confirmed_mode_calls_runner_after_verification(self) -> None:
        self._seed_task()
        self._create_valid_package()
        spy = _RunnerSpy(_waiting_approval_runner_result("AT-HANDOFF-1", "shell"))
        result = run_queued_task_handoff(
            self._request(dry_run=False, confirm_handoff=True),
            approved_task_runner=spy,
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(spy.calls), 1)
        request_arg, kwargs = spy.calls[0]
        # Runner is called after verification, with verified package
        self.assertTrue(result.package["verified"])
        # A store handle is forwarded to the runner so it does not open
        # a separate connection that races our seeded one.
        self.assertIn("store", kwargs)
        passed_store = kwargs["store"]
        self.assertEqual(passed_store.db_path, self.store.db_path)
        # Match runner's expected request shape
        self.assertEqual(request_arg.task_key, "AT-HANDOFF-1")
        self.assertEqual(request_arg.executor, "shell")
        self.assertEqual(request_arg.repo_path, self.repo)

    # 10. Confirmed mode passes confirm_approved_task=True to approved_task_runner.
    def test_confirmed_mode_passes_confirm_approved_task(self) -> None:
        self._seed_task()
        self._create_valid_package()
        spy = _RunnerSpy(_waiting_approval_runner_result("AT-HANDOFF-1", "shell"))
        run_queued_task_handoff(
            self._request(dry_run=False, confirm_handoff=True),
            approved_task_runner=spy,
        )
        request_arg, _ = spy.calls[0]
        self.assertTrue(request_arg.confirm_approved_task)
        self.assertFalse(request_arg.dry_run)

    # 11. Confirmed mode wraps waiting_approval runner result as ok.
    def test_confirmed_mode_wraps_waiting_approval_as_ok(self) -> None:
        self._seed_task()
        self._create_valid_package()
        runner_result = _waiting_approval_runner_result("AT-HANDOFF-1", "shell")
        spy = _RunnerSpy(runner_result)
        result = run_queued_task_handoff(
            self._request(dry_run=False, confirm_handoff=True),
            approved_task_runner=spy,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, APPROVED_TASK_STATUS)
        self.assertEqual(result.phase, APPROVED_TASK_STATUS)
        self.assertIsNotNone(result.runner_result)
        self.assertTrue(result.safety["approved_task_runner_started"])
        self.assertTrue(result.safety["workspace_prepared"])
        self.assertTrue(result.safety["executor_started"])
        self.assertTrue(result.safety["validators_started"])
        # Strict non-goals
        self.assertFalse(result.safety["branch_pushed"])
        self.assertFalse(result.safety["pr_created"])
        self.assertFalse(result.safety["merged"])
        self.assertFalse(result.safety["approved"])
        self.assertFalse(result.safety["cleanup_performed"])
        self.assertFalse(result.safety["background_worker_started"])

    # 12. Confirmed mode wraps blocked runner result as not ok.
    def test_confirmed_mode_wraps_blocked_as_not_ok(self) -> None:
        self._seed_task()
        self._create_valid_package()
        runner_result = _blocked_runner_result(
            "AT-HANDOFF-1",
            "shell",
            "Executor shell raised RuntimeError",
        )
        spy = _RunnerSpy(runner_result)
        result = run_queued_task_handoff(
            self._request(dry_run=False, confirm_handoff=True),
            approved_task_runner=spy,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.phase, "runner")
        self.assertIn("RuntimeError", result.error or "")
        self.assertTrue(result.safety["approved_task_runner_started"])
        self.assertTrue(result.safety["handoff_confirmed"])

    # 13. Request rejects dry_run=True with confirm_handoff=True.
    def test_request_rejects_dry_run_with_confirm(self) -> None:
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            QueuedTaskHandoffRequest(
                task_key="AT-HANDOFF-1",
                executor="shell",
                repo_path=self.repo,
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                dry_run=True,
                confirm_handoff=True,
            )

    # 14. Request rejects dry_run=False with confirm_handoff=False.
    def test_request_rejects_non_dry_run_without_confirm(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires confirm_handoff=True"):
            QueuedTaskHandoffRequest(
                task_key="AT-HANDOFF-1",
                executor="shell",
                repo_path=self.repo,
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                dry_run=False,
                confirm_handoff=False,
            )


class QueuedTaskHandoffResultTests(unittest.TestCase):
    def test_result_to_dict_is_serializable(self) -> None:
        result = QueuedTaskHandoffResult(
            ok=True,
            status="preview",
            phase="preview",
            task_key="AT-HANDOFF-1",
            executor="shell",
            dry_run=True,
            package={"verified": True},
            handoff={"confirmed": False, "approved_task_runner_invoked": False},
            runner_result=None,
            safety={"read_only": True},
            error=None,
        )
        payload = result.to_dict()
        # Must JSON round-trip cleanly
        json.dumps(payload)


if __name__ == "__main__":
    unittest.main()
