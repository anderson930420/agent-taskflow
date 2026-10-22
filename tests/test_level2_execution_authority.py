"""Repository-wide Level 2 authority boundary regression tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_taskflow.approved_task_runner import (
    ApprovedTaskRunRequest,
    run_approved_task,
)
from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.dispatcher import Dispatcher
from agent_taskflow.models import TaskRecord
from agent_taskflow.scheduler_execution_engine_opt_in import (
    route_scheduler_tick_through_execution_engine,
)
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]


class _NeverCalled:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((args, kwargs))
        raise AssertionError("legacy execution callback must not run")

    def run(self, context: Any) -> Any:
        self.calls.append(context)
        raise AssertionError("legacy executor must not run")

    def execute(self, request: Any) -> Any:
        self.calls.append(request)
        raise AssertionError("historical shadow engine must not run")


class Level2ExecutionAuthorityBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.attempts = AttemptStore(self.db_path)
        self.attempts.init_db()
        self._add_task("AT-L2-AUTHORITY", level2=True)

    def _add_task(self, task_key: str, *, level2: bool) -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Authority boundary",
                status="queued",
                repo_path=self.repo,
                artifact_dir=self.artifacts / task_key,
            )
        )
        if level2:
            self.attempts.register_task_identity(
                task_key,
                task_class="canonical",
                is_legacy=False,
            )

    def _approved_request(self, task_key: str = "AT-L2-AUTHORITY") -> ApprovedTaskRunRequest:
        return ApprovedTaskRunRequest(
            task_key=task_key,
            executor="manual",
            repo_path=self.repo,
            db_path=self.db_path,
            artifact_root=self.artifacts,
            confirm_approved_task=True,
            dry_run=False,
            preflight=False,
        )

    def test_direct_legacy_runner_rejects_level2_before_executor(self) -> None:
        executor = _NeverCalled()

        result = run_approved_task(
            self._approved_request(),
            executor_registry={"manual": executor},
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "execution_authority")
        self.assertIn("canonical ExecutionEngine", result.error or "")
        self.assertEqual(executor.calls, [])

    def test_direct_script_rejects_level2(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "run_approved_task.py"),
                "--task-key",
                "AT-L2-AUTHORITY",
                "--executor",
                "manual",
                "--repo-path",
                str(self.repo),
                "--db-path",
                str(self.db_path),
                "--artifact-root",
                str(self.artifacts),
                "--confirm-approved-task",
                "--skip-preflight",
                "--json",
            ],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["phase"], "execution_authority")
        self.assertFalse(payload["safety"]["executor_started"])

    def test_dispatcher_rejects_level2_before_executor(self) -> None:
        executor = _NeverCalled()
        dispatcher = Dispatcher(
            self.store,
            executor_registry={"manual": executor},
            validators=(),
            default_executor="manual",
        )

        result = dispatcher.dispatch_task("AT-L2-AUTHORITY")

        self.assertEqual(result.status, "blocked")
        self.assertIn("canonical ExecutionEngine", result.summary)
        self.assertEqual(executor.calls, [])

    def test_historical_shadow_route_refuses_level2_execution(self) -> None:
        engine = _NeverCalled()
        block = route_scheduler_tick_through_execution_engine(
            SimpleNamespace(db_path=self.db_path),
            {
                "ok": True,
                "status": "execution_completed",
                "mode": "confirmed",
                "selected_task_key": "AT-L2-AUTHORITY",
                "safety": {},
            },
            engine=engine,
        )

        self.assertFalse(block["executed"])
        self.assertIn("forbidden_for_level2", block["reason"])
        self.assertEqual(engine.calls, [])

    def test_legacy_task_retains_supported_runner_preview(self) -> None:
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=self.repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=self.repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Authority Test"],
            cwd=self.repo,
            check=True,
        )
        (self.repo / "README.md").write_text("authority test\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=self.repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._add_task("AT-LEGACY-AUTHORITY", level2=False)
        request = self._approved_request("AT-LEGACY-AUTHORITY")
        request = ApprovedTaskRunRequest(
            **{
                **request.__dict__,
                "executor": "shell",
                "command": ("/bin/true",),
                "confirm_approved_task": False,
                "dry_run": True,
            }
        )

        result = run_approved_task(request)

        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.status, "preview")


if __name__ == "__main__":
    unittest.main()
