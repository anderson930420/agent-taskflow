"""M1-C authority, fail-closed, and canonical Attempt binding tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.execution_engine_approved_task_adapter import (
    ApprovedTaskRunnerExecutionEngineAdapter,
)
from agent_taskflow.execution_engine_contract import (
    ExecutionEngineResult,
    ExecutionEngineSafety,
)
from agent_taskflow.models import TaskRecord
from agent_taskflow.scheduler_execution_engine_authority import (
    SchedulerExecutionEngineAuthority,
)
from agent_taskflow.scheduler_execution_engine_request_builder import (
    SchedulerExecutionEngineRequestBuildInput,
    build_scheduler_execution_engine_request,
)
from agent_taskflow.store import TaskMirrorStore


TASK_KEY = "AT-M1C-AUTHORITY"


def scheduler_request(root: Path, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "repo": "anderson930420/agent-taskflow",
        "db_path": root / "state.db",
        "local_repo_path": root / "repo",
        "artifact_root": root / "artifacts",
        "executor": "noop",
        "model": None,
        "provider": None,
        "tools": None,
        "pi_bin": None,
        "command": None,
        "validators": ("pytest",),
        "worktree_root": None,
        "base_branch": "main",
        "approved_task_preflight": False,
        "operator": "m1-c-test",
        "operator_note": "deterministic authority test",
        "use_execution_engine": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def bound_result(task_key: str = TASK_KEY) -> ExecutionEngineResult:
    return ExecutionEngineResult(
        ok=True,
        task_key=task_key,
        status="waiting_approval",
        summary="canonical engine result",
        safety=ExecutionEngineSafety(),
        metadata={
            "execution_authority": "execution_engine",
            "legacy_fallback_allowed": False,
            "canonical_attempt_bound": True,
            "canonical_attempt_id": "attempt-m1c-test",
        },
    )


class RecordingEngine:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.calls: list[Any] = []
        self.result = result
        self.error = error

    def execute(self, request: Any) -> Any:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return self.result if self.result is not None else bound_result(request.task_key)


class SchedulerAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "repo").mkdir()
        (self.root / "artifacts").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def runtime(self) -> dict[str, Any]:
        return {
            "task_key": TASK_KEY,
            "handoff": {
                "handoff_artifact_path": str(self.root / "handoff.json"),
                "verifier_report_artifact_path": str(
                    self.root / "verifier.json"
                ),
            },
            "handoff_id": "handoff-m1c",
            "runtime_execution_id": "runtime-m1c",
        }

    def test_confirmed_level2_uses_engine_as_only_authority(self) -> None:
        legacy_calls: list[Any] = []

        def forbidden_legacy(**kwargs: Any) -> Any:
            legacy_calls.append(kwargs)
            raise AssertionError("legacy authority must not run")

        engine = RecordingEngine()
        authority = SchedulerExecutionEngineAuthority(
            scheduler_request(self.root),
            engine=engine,
            approved_task_runner_fn=forbidden_legacy,
        )
        payload = authority.execute_from_runtime_handoff(**self.runtime())

        self.assertTrue(payload["ok"])
        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(legacy_calls, [])
        request = engine.calls[0]
        self.assertEqual(request.metadata["execution_authority"], "execution_engine")
        self.assertIs(request.metadata["legacy_fallback_allowed"], False)
        self.assertEqual(request.lifecycle_db_path, self.root / "state.db")
        self.assertEqual(request.runtime_handoff_path, self.root / "handoff.json")

    def test_engine_failure_fails_closed_without_legacy_fallback(self) -> None:
        legacy_calls: list[Any] = []
        authority = SchedulerExecutionEngineAuthority(
            scheduler_request(self.root),
            engine=RecordingEngine(error=RuntimeError("engine unavailable")),
            approved_task_runner_fn=lambda **kwargs: legacy_calls.append(kwargs),
        )
        payload = authority.execute_from_runtime_handoff(**self.runtime())

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(legacy_calls, [])
        evidence = authority.evidence(
            {
                "ok": False,
                "status": "execution_engine_blocked",
                "repo": "anderson930420/agent-taskflow",
                "selected_task_key": TASK_KEY,
            }
        )
        self.assertFalse(evidence["legacy_fallback_allowed"])
        self.assertFalse(evidence["legacy_scheduler_authority_invoked"])
        self.assertTrue(evidence["engine_authority"])

    def test_success_without_canonical_attempt_binding_is_blocked(self) -> None:
        authority = SchedulerExecutionEngineAuthority(
            scheduler_request(self.root),
            engine=RecordingEngine(
                result=ExecutionEngineResult(
                    ok=True,
                    task_key=TASK_KEY,
                    status="waiting_approval",
                )
            ),
        )
        payload = authority.execute_from_runtime_handoff(**self.runtime())

        self.assertFalse(payload["ok"])
        self.assertIn("canonical Attempt", payload["error"])

    def test_shadow_compare_cannot_override_engine_authority(self) -> None:
        authority = SchedulerExecutionEngineAuthority(
            scheduler_request(self.root),
            engine=RecordingEngine(),
        )
        authority.execute_from_runtime_handoff(**self.runtime())
        evidence = authority.evidence(
            {
                "ok": True,
                "status": "different-status",
                "repo": "different/repo",
                "selected_task_key": "AT-DIFFERENT",
            }
        )

        self.assertFalse(evidence["shadow_compare"]["matched"])
        self.assertFalse(evidence["shadow_result_can_override_authority"])
        self.assertEqual(evidence["effective_authority"], "execution_engine")


class AdapterCanonicalAttemptBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo = self.root / "repo"
        self.artifacts = self.root / "artifacts"
        self.repo.mkdir()
        self.artifacts.mkdir()
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.store.upsert_task(
            TaskRecord(
                task_key=TASK_KEY,
                project="agent-taskflow",
                board="agent-taskflow",
                title="M1-C adapter binding",
                status="queued",
                repo_path=self.repo,
                artifact_dir=self.artifacts,
            )
        )
        self.attempts = AttemptStore(self.db_path)
        self.attempts.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def request(self, **overrides: Any) -> Any:
        values: dict[str, Any] = {
            "task_key": TASK_KEY,
            "repo": "anderson930420/agent-taskflow",
            "local_repo_path": self.repo,
            "artifact_dir": self.artifacts,
            "executor": "noop",
            "validators": ("pytest",),
            "lifecycle_db_path": self.db_path,
            "dry_run": False,
            "confirmed": True,
            "preflight": False,
            "runtime_handoff_path": self.root / "handoff.json",
        }
        values.update(overrides)
        return build_scheduler_execution_engine_request(
            SchedulerExecutionEngineRequestBuildInput(**values)
        )

    def test_adapter_binds_exact_new_canonical_attempt(self) -> None:
        approved_requests: list[Any] = []

        def canonical_runner(request: Any) -> dict[str, Any]:
            approved_requests.append(request)
            attempt = self.attempts.create_attempt(
                request.task_key,
                executor=request.executor,
            )
            self.attempts.close_attempt(
                attempt.attempt_id,
                status="waiting_approval",
                reason_code="m1c_test_complete",
                actor="m1c_test",
                execution_result="completed",
                validation_result="passed",
            )
            return {
                "ok": True,
                "status": "waiting_approval",
                "phase": "waiting_approval",
                "safety": {
                    "executor_started": True,
                    "validators_started": True,
                },
            }

        result = ApprovedTaskRunnerExecutionEngineAdapter(
            approved_task_runner=canonical_runner
        ).execute(self.request())

        self.assertTrue(result.ok)
        self.assertEqual(len(approved_requests), 1)
        self.assertEqual(approved_requests[0].db_path, self.db_path)
        self.assertTrue(approved_requests[0].confirm_approved_task)
        self.assertTrue(result.metadata["canonical_attempt_bound"])
        attempt = self.attempts.get_attempt(
            str(result.metadata["canonical_attempt_id"])
        )
        self.assertIsNotNone(attempt)
        assert attempt is not None
        self.assertFalse(attempt.is_active)
        self.assertFalse(attempt.is_legacy)
        self.assertEqual(attempt.validation_result, "passed")

    def test_adapter_rejects_success_without_new_attempt(self) -> None:
        calls: list[Any] = []

        def unbound_runner(request: Any) -> dict[str, Any]:
            calls.append(request)
            return {"ok": True, "status": "waiting_approval", "safety": {}}

        result = ApprovedTaskRunnerExecutionEngineAdapter(
            approved_task_runner=unbound_runner
        ).execute(self.request())

        self.assertEqual(len(calls), 1)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertFalse(result.metadata["canonical_attempt_bound"])

    def test_missing_handoff_fails_before_runner_invocation(self) -> None:
        calls: list[Any] = []
        adapter = ApprovedTaskRunnerExecutionEngineAdapter(
            approved_task_runner=lambda request: calls.append(request)
        )
        result = adapter.execute(self.request(runtime_handoff_path=None))

        self.assertFalse(result.ok)
        self.assertEqual(calls, [])
        self.assertIn("runtime handoff", result.summary)


if __name__ == "__main__":
    unittest.main()
