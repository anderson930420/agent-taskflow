"""Tests for the P4-c ApprovedTaskRunner ExecutionEngine adapter."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_taskflow import execution_engine_approved_task_adapter as adapter_module
from agent_taskflow.approved_task_runner import ApprovedTaskRunRequest
from agent_taskflow.execution_engine_approved_task_adapter import (
    ApprovedTaskRunnerExecutionEngineAdapter,
)
from agent_taskflow.execution_engine_contract import (
    ExecutionEngine,
    ExecutionEngineArtifactRef,
    ExecutionEngineExecutorProfile,
    ExecutionEngineRequest,
    ExecutionEngineResult,
    ExecutionEngineValidatorProfile,
    ExecutionEngineWorkspaceProfile,
)


def make_request(**overrides: Any) -> ExecutionEngineRequest:
    values: dict[str, Any] = {
        "task_key": "AT-P4C",
        "dry_run": True,
        "preflight": True,
        "executor_profile": ExecutionEngineExecutorProfile(
            executor="pi",
            model="claude",
            provider="anthropic",
            tools=("git", "pytest"),
            pi_bin="/usr/bin/pi",
        ),
        "validator_profile": ExecutionEngineValidatorProfile(
            validators=("pytest", "policy"),
        ),
        "workspace": ExecutionEngineWorkspaceProfile(
            repo_path=Path("/tmp/agent-taskflow"),
            artifact_dir=Path("/tmp/agent-taskflow-artifacts/AT-P4C"),
            worktree_root=Path("/tmp/agent-taskflow-worktrees"),
        ),
    }
    values.update(overrides)
    return ExecutionEngineRequest(**values)  # type: ignore[arg-type]


def success_result(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "status": "waiting_approval",
        "phase": "waiting_approval",
        "task_key": "AT-P4C",
        "executor": "pi",
        "dry_run": False,
        "preflight": {"ran": True, "ok": True, "status": "passed"},
        "workspace": {"prepared": True, "summary": "Workspace prepared."},
        "executor_run": {"started": True, "ok": True, "summary": "ok"},
        "validators": [{"name": "pytest", "ok": True, "status": "passed"}],
        "summary": {"final_task_status": "waiting_approval"},
        "safety": {"executor_started": True, "validators_started": True},
        "next_allowed_actions": ["operator_review", "approve_or_reject"],
        "artifacts": [
            {"kind": "mission_contract", "path": "/tmp/x/contract.json"},
        ],
    }
    payload.update(overrides)
    return payload


def run_adapter(
    request: ExecutionEngineRequest,
    runner_result: Any,
) -> tuple[ApprovedTaskRunRequest, ExecutionEngineResult, mock.Mock]:
    captured: dict[str, ApprovedTaskRunRequest] = {}

    def fake_run(approved_request: ApprovedTaskRunRequest) -> Any:
        captured["request"] = approved_request
        return runner_result

    with mock.patch.object(
        adapter_module, "run_approved_task", side_effect=fake_run
    ) as patched:
        result = ApprovedTaskRunnerExecutionEngineAdapter().execute(request)
    return captured.get("request"), result, patched  # type: ignore[return-value]


class AdapterProtocolTests(unittest.TestCase):
    def test_adapter_exposes_execute_and_satisfies_protocol(self) -> None:
        adapter = ApprovedTaskRunnerExecutionEngineAdapter()

        self.assertTrue(callable(getattr(adapter, "execute", None)))
        self.assertIsInstance(adapter, ExecutionEngine)


class AdapterRequestMappingTests(unittest.TestCase):
    def test_maps_core_request_fields(self) -> None:
        approved, _, patched = run_adapter(make_request(), success_result())

        patched.assert_called_once()
        self.assertIsInstance(approved, ApprovedTaskRunRequest)
        self.assertEqual(approved.task_key, "AT-P4C")
        self.assertEqual(approved.executor, "pi")
        self.assertEqual(approved.repo_path, Path("/tmp/agent-taskflow"))
        self.assertEqual(
            approved.artifact_root,
            Path("/tmp/agent-taskflow-artifacts/AT-P4C"),
        )
        self.assertEqual(
            approved.worktree_root, Path("/tmp/agent-taskflow-worktrees")
        )

    def test_forwards_executor_profile(self) -> None:
        approved, _, _ = run_adapter(make_request(), success_result())

        self.assertEqual(approved.model, "claude")
        self.assertEqual(approved.provider, "anthropic")
        self.assertEqual(approved.tools, ("git", "pytest"))
        self.assertEqual(approved.pi_bin, "/usr/bin/pi")

    def test_forwards_validator_tuple(self) -> None:
        approved, _, _ = run_adapter(make_request(), success_result())

        self.assertEqual(approved.validators, ("pytest", "policy"))

    def test_forwards_dry_run_and_preflight(self) -> None:
        request = make_request(dry_run=False, preflight=False)

        approved, _, _ = run_adapter(request, success_result())

        self.assertFalse(approved.dry_run)
        self.assertFalse(approved.preflight)


class AdapterResultMappingTests(unittest.TestCase):
    def test_success_result_maps_to_ok_result(self) -> None:
        _, result, _ = run_adapter(make_request(), success_result())

        self.assertIsInstance(result, ExecutionEngineResult)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "waiting_approval")
        self.assertEqual(result.task_key, "AT-P4C")

    def test_blocked_result_maps_to_blocked_result(self) -> None:
        runner_result = {
            "ok": False,
            "status": "blocked",
            "summary": "Task is blocked.",
        }

        _, result, _ = run_adapter(make_request(), runner_result)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.summary, "Task is blocked.")

    def test_status_defaults_to_blocked_when_absent(self) -> None:
        _, result, _ = run_adapter(make_request(), {"ok": False})

        self.assertEqual(result.status, "blocked")

    def test_safety_defaults_conservative_without_payload(self) -> None:
        _, result, _ = run_adapter(make_request(), {"ok": True, "status": "x"})

        safety = result.safety
        self.assertTrue(safety.human_review_required)
        self.assertFalse(safety.executor_started)
        self.assertFalse(safety.validator_started)
        self.assertFalse(safety.approved)
        self.assertFalse(safety.merged)
        self.assertFalse(safety.cleanup_performed)
        self.assertFalse(safety.branch_deleted)
        self.assertFalse(safety.worktree_deleted)
        self.assertFalse(safety.background_worker_started)
        self.assertTrue(safety.one_task_only)
        self.assertTrue(safety.execution_only)

    def test_safety_fields_copied_when_present(self) -> None:
        runner_result = success_result(
            safety={
                "executor_started": True,
                "validators_started": True,
                "merged": False,
                "approved": False,
            }
        )

        _, result, _ = run_adapter(make_request(), runner_result)

        self.assertTrue(result.safety.executor_started)
        self.assertTrue(result.safety.validator_started)
        # Conservative defaults remain for non-reported governance evidence.
        self.assertFalse(result.safety.merged)
        self.assertFalse(result.safety.approved)
        self.assertTrue(result.safety.human_review_required)

    def test_next_operator_action_uses_first_allowed_action(self) -> None:
        _, result, _ = run_adapter(make_request(), success_result())

        self.assertEqual(result.next_operator_action, "operator_review")

    def test_next_operator_action_is_none_when_absent(self) -> None:
        runner_result = success_result()
        runner_result.pop("next_allowed_actions")

        _, result, _ = run_adapter(make_request(), runner_result)

        self.assertIsNone(result.next_operator_action)

    def test_metadata_does_not_mutate_runner_result(self) -> None:
        runner_result = success_result()

        _, result, _ = run_adapter(make_request(), runner_result)

        self.assertEqual(result.metadata["runner_status"], "waiting_approval")
        # Original runner result untouched.
        self.assertEqual(runner_result["status"], "waiting_approval")
        self.assertNotIn("adapter", runner_result)


class AdapterArtifactMappingTests(unittest.TestCase):
    def test_maps_dict_artifact_payload(self) -> None:
        runner_result = success_result(
            artifacts={
                "mission_contract": "/tmp/x/contract.json",
                "worker_log": "/tmp/x/log.txt",
            }
        )

        _, result, _ = run_adapter(make_request(), runner_result)

        self.assertEqual(len(result.artifacts), 2)
        for ref in result.artifacts:
            self.assertIsInstance(ref, ExecutionEngineArtifactRef)
            self.assertIsInstance(ref.path, Path)
        by_type = {ref.artifact_type: ref.path for ref in result.artifacts}
        self.assertEqual(
            by_type["mission_contract"], Path("/tmp/x/contract.json")
        )
        self.assertEqual(by_type["worker_log"], Path("/tmp/x/log.txt"))

    def test_maps_list_artifact_payload_and_skips_invalid(self) -> None:
        runner_result = success_result(
            artifacts=[
                {"artifact_type": "mission_contract", "path": "/tmp/x/c.json"},
                {"kind": "worker_log", "path": "/tmp/x/log.txt"},
                {"artifact_type": "broken", "path": ""},
                {"artifact_type": "missing"},
            ]
        )

        _, result, _ = run_adapter(make_request(), runner_result)

        by_type = {ref.artifact_type: ref.path for ref in result.artifacts}
        self.assertEqual(len(result.artifacts), 2)
        self.assertEqual(by_type["mission_contract"], Path("/tmp/x/c.json"))
        self.assertEqual(by_type["worker_log"], Path("/tmp/x/log.txt"))


class AdapterErrorHandlingTests(unittest.TestCase):
    def test_runner_exception_maps_to_blocked_failed_result(self) -> None:
        def boom(_request: ApprovedTaskRunRequest) -> Any:
            raise RuntimeError("approved runner exploded")

        with mock.patch.object(
            adapter_module, "run_approved_task", side_effect=boom
        ):
            result = ApprovedTaskRunnerExecutionEngineAdapter().execute(
                make_request()
            )

        self.assertIsInstance(result, ExecutionEngineResult)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(len(result.steps), 1)
        self.assertEqual(result.steps[0].name, "approved_task_runner")
        self.assertEqual(result.steps[0].status, "failed")
        self.assertEqual(result.metadata["error_type"], "RuntimeError")
        self.assertEqual(
            result.metadata["error_message"], "approved runner exploded"
        )
        # Safety stays conservative on failure.
        self.assertTrue(result.safety.human_review_required)
        self.assertFalse(result.safety.executor_started)


class AdapterRuntimeIsolationTests(unittest.TestCase):
    def test_no_runtime_module_imports_adapter(self) -> None:
        repo_root = Path(adapter_module.__file__).resolve().parents[1]
        adapter_stem = "execution_engine_approved_task_adapter"
        # The adapter itself, plus the explicit, opt-in importers behind the
        # engine facade: the P4-d manual runtime helper and the P5-d scheduler
        # opt-in helper (off by default, confirmed-mode only). No other
        # scheduler/automation module may import the adapter.
        allowed = {
            f"{adapter_stem}.py",
            "execution_engine_manual_runtime.py",
            "scheduler_execution_engine_opt_in.py",
        }
        offenders: list[str] = []
        for base in (repo_root / "agent_taskflow", repo_root / "scripts"):
            for py_file in base.rglob("*.py"):
                if py_file.name in allowed:
                    continue
                if adapter_stem in py_file.read_text(encoding="utf-8"):
                    offenders.append(str(py_file.relative_to(repo_root)))

        self.assertEqual(
            offenders,
            [],
            "only the P4-d manual runtime helper may import the P4-c adapter: "
            f"{offenders}",
        )


if __name__ == "__main__":
    unittest.main()
