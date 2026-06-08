"""Tests for the P4-d manual ExecutionEngine runtime helpers."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_taskflow import execution_engine_manual_runtime as manual_runtime
from agent_taskflow.execution_engine_contract import (
    REQUEST_SOURCE_MANUAL,
    ExecutionEngineRequest,
    ExecutionEngineResult,
)
from agent_taskflow.execution_engine_manual_runtime import (
    build_manual_execution_engine_request,
    run_manual_execution_engine_request,
)


def build(**overrides: Any) -> ExecutionEngineRequest:
    params: dict[str, Any] = {
        "task_key": "AT-P4D",
        "repo_path": "/tmp/agent-taskflow",
        "artifact_dir": "/tmp/agent-taskflow-artifacts/AT-P4D",
        "executor": "pi",
        "validators": ("pytest", "policy"),
        "model": "claude",
        "provider": "anthropic",
        "tools": ("git", "pytest"),
        "pi_bin": "/usr/bin/pi",
        "worktree_root": "/tmp/agent-taskflow-worktrees",
    }
    params.update(overrides)
    return build_manual_execution_engine_request(**params)


class BuildManualRequestTests(unittest.TestCase):
    def test_maps_task_key_repo_and_artifact_dir(self) -> None:
        request = build()

        self.assertIsInstance(request, ExecutionEngineRequest)
        self.assertEqual(request.task_key, "AT-P4D")
        self.assertEqual(request.workspace.repo_path, Path("/tmp/agent-taskflow"))
        self.assertEqual(
            request.workspace.artifact_dir,
            Path("/tmp/agent-taskflow-artifacts/AT-P4D"),
        )
        self.assertTrue(request.workspace.repo_path.is_absolute())
        self.assertTrue(request.workspace.artifact_dir.is_absolute())

    def test_maps_executor_profile_fields(self) -> None:
        request = build()

        profile = request.executor_profile
        self.assertEqual(profile.executor, "pi")
        self.assertEqual(profile.model, "claude")
        self.assertEqual(profile.provider, "anthropic")
        self.assertEqual(profile.tools, ("git", "pytest"))
        self.assertEqual(profile.pi_bin, "/usr/bin/pi")

    def test_maps_validator_tuple(self) -> None:
        request = build(validators=["pytest", "policy", "lint"])

        self.assertEqual(
            request.validator_profile.validators,
            ("pytest", "policy", "lint"),
        )

    def test_maps_workspace_worktree_root(self) -> None:
        request = build()

        self.assertEqual(
            request.workspace.worktree_root,
            Path("/tmp/agent-taskflow-worktrees"),
        )

    def test_dry_run_default_is_true(self) -> None:
        request = build_manual_execution_engine_request(
            task_key="AT-P4D",
            repo_path="/tmp/agent-taskflow",
            artifact_dir="/tmp/agent-taskflow-artifacts/AT-P4D",
        )

        self.assertTrue(request.dry_run)

    def test_dry_run_can_be_disabled(self) -> None:
        request = build(dry_run=False)

        self.assertFalse(request.dry_run)

    def test_request_source_is_manual(self) -> None:
        request = build()

        self.assertEqual(request.source, REQUEST_SOURCE_MANUAL)
        self.assertEqual(request.source, "manual")

    def test_optional_paths_are_forwarded(self) -> None:
        request = build(
            runtime_handoff_path="/tmp/handoff.json",
            verifier_report_path="/tmp/verifier.json",
        )

        self.assertEqual(request.runtime_handoff_path, Path("/tmp/handoff.json"))
        self.assertEqual(
            request.verifier_report_path, Path("/tmp/verifier.json")
        )

    def test_relative_repo_path_is_rejected_by_contract(self) -> None:
        with self.assertRaisesRegex(ValueError, "repo_path must be absolute"):
            build(repo_path="relative/repo")

    def test_relative_artifact_dir_is_rejected_by_contract(self) -> None:
        with self.assertRaisesRegex(ValueError, "artifact_dir must be absolute"):
            build(artifact_dir="relative/artifacts")

    def test_build_does_not_check_filesystem_existence(self) -> None:
        # Paths that do not exist must still build a valid request: the helper
        # performs no filesystem existence checks during request construction.
        with mock.patch.object(
            Path, "exists", side_effect=AssertionError("Path.exists was called")
        ), mock.patch.object(
            Path, "resolve", side_effect=AssertionError("Path.resolve was called")
        ):
            request = build(
                repo_path="/nonexistent/repo/does/not/exist",
                artifact_dir="/nonexistent/artifacts/also/missing",
            )

        self.assertEqual(
            request.workspace.repo_path,
            Path("/nonexistent/repo/does/not/exist"),
        )
        self.assertEqual(
            request.workspace.artifact_dir,
            Path("/nonexistent/artifacts/also/missing"),
        )


class RunManualRequestTests(unittest.TestCase):
    def test_run_instantiates_adapter_and_calls_execute(self) -> None:
        request = build()
        sentinel = ExecutionEngineResult(
            ok=True, task_key="AT-P4D", status="dry_run"
        )
        fake_adapter = mock.Mock()
        fake_adapter.execute.return_value = sentinel

        with mock.patch.object(
            manual_runtime,
            "ApprovedTaskRunnerExecutionEngineAdapter",
            return_value=fake_adapter,
        ) as adapter_cls:
            result = run_manual_execution_engine_request(request)

        adapter_cls.assert_called_once_with()
        fake_adapter.execute.assert_called_once_with(request)

    def test_run_passes_result_through_unchanged(self) -> None:
        request = build()
        sentinel = ExecutionEngineResult(
            ok=True, task_key="AT-P4D", status="dry_run"
        )
        fake_adapter = mock.Mock()
        fake_adapter.execute.return_value = sentinel

        with mock.patch.object(
            manual_runtime,
            "ApprovedTaskRunnerExecutionEngineAdapter",
            return_value=fake_adapter,
        ):
            result = run_manual_execution_engine_request(request)

        self.assertIs(result, sentinel)


class ManualRuntimeIsolationTests(unittest.TestCase):
    def test_scheduler_and_automation_modules_do_not_use_the_facade(self) -> None:
        repo_root = Path(manual_runtime.__file__).resolve().parents[1]
        # Only the new manual runtime helper, the adapter it wraps, and the new
        # opt-in CLI are allowed to reference the engine-facade runtime symbols.
        allowed = {
            "execution_engine_manual_runtime.py",
            "execution_engine_approved_task_adapter.py",
            "run_execution_engine_approved_task.py",
        }
        tokens = (
            "execution_engine_manual_runtime",
            "run_execution_engine_approved_task",
            "ApprovedTaskRunnerExecutionEngineAdapter",
        )

        offenders: list[str] = []
        for base in (repo_root / "agent_taskflow", repo_root / "scripts"):
            for py_file in base.rglob("*.py"):
                if py_file.name in allowed:
                    continue
                text = py_file.read_text(encoding="utf-8")
                for token in tokens:
                    if token in text:
                        offenders.append(
                            f"{py_file.relative_to(repo_root)}:{token}"
                        )

        self.assertEqual(
            offenders,
            [],
            "scheduler/automation modules must not use the engine facade "
            f"runtime path: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
