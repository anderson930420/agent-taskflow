"""Repository-wide Level 2 authority boundary regression tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_taskflow.approved_task_runner import (
    ApprovedTaskRunRequest,
    run_approved_task,
)
from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.dispatcher import Dispatcher
from agent_taskflow.execution_engine_approved_task_adapter import (
    ApprovedTaskRunnerExecutionEngineAdapter,
)
from agent_taskflow.execution_engine_manual_runtime import (
    build_manual_execution_engine_request,
)
from agent_taskflow.models import TaskRecord
from agent_taskflow.one_shot_task_pipeline import (
    OneShotTaskPipelineRequest,
    run_one_shot_task_pipeline,
)
from agent_taskflow.queued_task_handoff import (
    QueuedTaskHandoffRequest,
    run_queued_task_handoff,
)
from agent_taskflow.runtime_handoff_execution_from_handoff import (
    RuntimeHandoffExecutionRequest,
    run_runtime_handoff_execution_from_handoff,
)
from agent_taskflow.scheduler_execution_engine_opt_in import (
    route_scheduler_tick_through_execution_engine,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.task_to_draft_pr_pipeline import (
    TaskToDraftPRPipelineRequest,
    canonical_attempt_binding_error,
    run_task_to_draft_pr_pipeline,
)


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

    def test_queued_handoff_rejects_level2_injected_runner(self) -> None:
        runner = _NeverCalled()
        request = QueuedTaskHandoffRequest(
            task_key="AT-L2-AUTHORITY",
            executor="manual",
            repo_path=self.repo,
            db_path=self.db_path,
            artifact_root=self.artifacts,
            dry_run=False,
            confirm_handoff=True,
            intake_runner_handoff_artifact_path=self.root / "handoff.json",
        )

        result = run_queued_task_handoff(
            request,
            store=self.store,
            approved_task_runner=runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "execution_authority")
        self.assertEqual(runner.calls, [])

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

    def test_manual_engine_facade_cannot_skip_level2_contract(self) -> None:
        runner = _NeverCalled()
        request = replace(
            build_manual_execution_engine_request(
                task_key="AT-L2-AUTHORITY",
                repo_path=self.repo,
                artifact_dir=self.artifacts,
                dry_run=False,
            ),
            lifecycle_db_path=self.db_path,
        )

        result = ApprovedTaskRunnerExecutionEngineAdapter(
            approved_task_runner=runner
        ).execute(request)

        self.assertFalse(result.ok)
        self.assertIn("Level 2 ExecutionEngine contract", result.summary)
        self.assertEqual(runner.calls, [])

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

    def test_runtime_handoff_rejects_arbitrary_level2_callback(self) -> None:
        runner = _NeverCalled()
        # Caller-controlled marker attributes are not authority capabilities.
        runner.__level2_execution_engine_authority__ = True
        request = RuntimeHandoffExecutionRequest(
            db_path=self.db_path,
            artifact_root=self.artifacts,
            task_key="AT-L2-AUTHORITY",
            handoff_id="handoff-l2",
            dry_run=False,
            confirm_run_approved_task_runner=True,
        )

        result = run_runtime_handoff_execution_from_handoff(
            request,
            approved_task_runner_fn=runner,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "execution_authority_blocked")
        self.assertEqual(runner.calls, [])

    def test_one_shot_rejects_arbitrary_level2_callback_before_writes(self) -> None:
        runner = _NeverCalled()
        before_events = len(self.store.list_task_events("AT-L2-AUTHORITY"))
        result = run_one_shot_task_pipeline(
            OneShotTaskPipelineRequest(
                db_path=self.db_path,
                artifact_root=self.artifacts,
                task_key="AT-L2-AUTHORITY",
                dry_run=False,
                confirm_run_one_shot_pipeline=True,
            ),
            approved_task_runner_fn=runner,
        )

        self.assertFalse(result["ok"])
        self.assertIn(
            "level2_execution_engine_authority_required", result["reasons"]
        )
        self.assertEqual(runner.calls, [])
        self.assertEqual(
            before_events,
            len(self.store.list_task_events("AT-L2-AUTHORITY")),
        )

    def test_task_pipeline_rejects_inconsistent_level2_authority(self) -> None:
        result = {
            "stages": {
                "runtime_execution": {
                    "execution_authority": "legacy_scheduler",
                    "canonical_attempt_bound": False,
                    "canonical_attempt_id": None,
                }
            }
        }

        error = canonical_attempt_binding_error(
            result,
            db_path=self.db_path,
            task_key="AT-L2-AUTHORITY",
        )

        self.assertEqual(error, "execution_engine_authority_required_for_level2")

    def test_task_to_draft_pr_rejects_injected_level2_runner(self) -> None:
        runner = _NeverCalled()
        result = run_task_to_draft_pr_pipeline(
            TaskToDraftPRPipelineRequest(
                db_path=self.db_path,
                artifact_root=self.artifacts,
                task_key="AT-L2-AUTHORITY",
                dry_run=False,
                confirm_run_one_shot_pipeline=True,
                confirm_prepare_pr=True,
                confirm_github_mutations=True,
                confirm_branch_push=True,
                confirm_draft_pr=True,
            ),
            approved_task_runner_fn=runner,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_stage"], "one_shot")
        self.assertEqual(runner.calls, [])

    def test_task_pipeline_verifies_exact_attempt_even_when_newer_exists(self) -> None:
        attempt_a = self.attempts.create_attempt("AT-L2-AUTHORITY")
        self.attempts.close_attempt(
            attempt_a.attempt_id,
            status="waiting_approval",
            reason_code="attempt_a_complete",
            actor="authority_test",
            execution_result="completed",
            validation_result="passed",
        )
        attempt_b = self.attempts.create_attempt("AT-L2-AUTHORITY")
        self.attempts.close_attempt(
            attempt_b.attempt_id,
            status="waiting_approval",
            reason_code="attempt_b_complete",
            actor="authority_test",
            execution_result="completed",
            validation_result="passed",
        )
        result = {
            "stages": {
                "runtime_execution": {
                    "execution_authority": "execution_engine",
                    "canonical_attempt_bound": True,
                    "canonical_attempt_id": attempt_a.attempt_id,
                }
            }
        }

        error = canonical_attempt_binding_error(
            result,
            db_path=self.db_path,
            task_key="AT-L2-AUTHORITY",
        )

        self.assertIsNone(error)
        self.assertNotEqual(attempt_a.attempt_id, attempt_b.attempt_id)

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
