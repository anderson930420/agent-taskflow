"""Tests for the behavior-free ExecutionEngine contract (P4-b)."""

from __future__ import annotations

import unittest
from dataclasses import fields
from pathlib import Path

from agent_taskflow.execution_engine_contract import (
    ExecutionEngine,
    ExecutionEngineArtifactRef,
    ExecutionEngineExecutorProfile,
    ExecutionEngineRequest,
    ExecutionEngineResult,
    ExecutionEngineSafety,
    ExecutionEngineStepResult,
    ExecutionEngineValidatorProfile,
    ExecutionEngineWorkspaceProfile,
    to_json_dict,
)


def make_request(**overrides: object) -> ExecutionEngineRequest:
    values: dict[str, object] = {
        "task_key": "AT-P4B",
        "executor_profile": ExecutionEngineExecutorProfile(executor="manual"),
        "validator_profile": ExecutionEngineValidatorProfile(
            validators=("pytest",)
        ),
        "workspace": ExecutionEngineWorkspaceProfile(
            repo_path=Path("/tmp/agent-taskflow"),
            artifact_dir=Path("/tmp/agent-taskflow-artifacts/AT-P4B"),
        ),
    }
    values.update(overrides)
    return ExecutionEngineRequest(**values)  # type: ignore[arg-type]


class ExecutionEngineContractTests(unittest.TestCase):
    def test_request_constructs_with_absolute_paths(self) -> None:
        request = make_request()

        self.assertEqual(request.task_key, "AT-P4B")
        self.assertTrue(request.workspace.repo_path.is_absolute())
        self.assertTrue(request.workspace.artifact_dir.is_absolute())

    def test_empty_task_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "task_key must not be empty"):
            make_request(task_key="  ")

    def test_empty_executor_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "executor must not be empty"):
            ExecutionEngineExecutorProfile(executor="  ")

    def test_tools_normalize_to_tuple(self) -> None:
        profile = ExecutionEngineExecutorProfile(
            executor="pi",
            tools=["git", "pytest"],  # type: ignore[arg-type]
        )

        self.assertEqual(profile.tools, ("git", "pytest"))

    def test_validators_normalize_to_tuple(self) -> None:
        profile = ExecutionEngineValidatorProfile(
            validators=["pytest", "changed-files"],  # type: ignore[arg-type]
        )

        self.assertEqual(profile.validators, ("pytest", "changed-files"))

    def test_safety_defaults_are_conservative(self) -> None:
        safety = ExecutionEngineSafety()

        self.assertTrue(safety.human_review_required)
        self.assertFalse(safety.approved)
        self.assertFalse(safety.merged)
        self.assertFalse(safety.github_mutated)
        self.assertFalse(safety.issue_closed)
        self.assertFalse(safety.cleanup_performed)
        self.assertFalse(safety.branch_deleted)
        self.assertFalse(safety.worktree_deleted)
        self.assertFalse(safety.daemon_started)
        self.assertFalse(safety.scheduler_loop_started)
        self.assertFalse(safety.multi_task_batch_started)
        self.assertTrue(safety.one_task_only)
        self.assertTrue(safety.execution_only)

    def test_serialization_converts_paths_and_tuples(self) -> None:
        request = make_request(
            executor_profile=ExecutionEngineExecutorProfile(
                executor="pi",
                tools=("git", "pytest"),
            )
        )

        payload = to_json_dict(request)

        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["workspace"]["repo_path"], "/tmp/agent-taskflow")
        self.assertEqual(payload["executor_profile"]["tools"], ["git", "pytest"])

    def test_serialization_does_not_mutate_dataclass(self) -> None:
        metadata = {"labels": ["contract"]}
        request = make_request(metadata=metadata)

        payload = to_json_dict(request)
        payload["metadata"]["labels"].append("serialized-copy")

        self.assertEqual(request.metadata["labels"], ["contract"])
        self.assertEqual(metadata, {"labels": ["contract"]})

    def test_protocol_can_be_implemented_by_fake_engine(self) -> None:
        class FakeEngine:
            def execute(
                self,
                request: ExecutionEngineRequest,
            ) -> ExecutionEngineResult:
                return ExecutionEngineResult(
                    ok=True,
                    task_key=request.task_key,
                    status="dry_run",
                    steps=(
                        ExecutionEngineStepResult(
                            name="preflight",
                            status="passed",
                        ),
                    ),
                    artifacts=(
                        ExecutionEngineArtifactRef(
                            artifact_type="summary",
                            path=request.workspace.artifact_dir / "summary.json",
                        ),
                    ),
                )

        engine: ExecutionEngine = FakeEngine()
        result = engine.execute(make_request())

        self.assertIsInstance(engine, ExecutionEngine)
        self.assertIsInstance(result, ExecutionEngineResult)
        self.assertTrue(result.ok)
        self.assertEqual(result.task_key, "AT-P4B")

    def test_request_omits_destructive_and_publication_fields(self) -> None:
        request_fields = {item.name for item in fields(ExecutionEngineRequest)}
        forbidden_fields = {
            "approve",
            "approved",
            "merge",
            "merged",
            "cleanup",
            "cleanup_performed",
            "archive",
            "closeout",
            "create_pr",
            "publish_pr",
            "pr_publication",
            "branch_deleted",
            "worktree_deleted",
        }

        self.assertTrue(request_fields.isdisjoint(forbidden_fields))


if __name__ == "__main__":
    unittest.main()
