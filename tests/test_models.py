from __future__ import annotations

import unittest
from pathlib import Path

from agent_taskflow.models import (
    TaskArtifactRecord,
    TaskRecord,
    TaskWorktreeRecord,
    require_absolute_path,
    utc_now_iso,
    validate_task_status,
)


class ModelValidationTests(unittest.TestCase):
    def test_valid_task_status_is_accepted(self) -> None:
        self.assertEqual(validate_task_status("blocked"), "blocked")

    def test_invalid_task_status_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid task status"):
            validate_task_status("not-a-real-status")

    def test_absolute_path_is_accepted(self) -> None:
        self.assertEqual(
            require_absolute_path("/home/ubuntu/agent-taskflow", "repo_path"),
            Path("/home/ubuntu/agent-taskflow"),
        )

    def test_relative_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "repo_path must be absolute"):
            require_absolute_path("relative/path", "repo_path")

    def test_utc_now_iso_returns_utc_iso_string(self) -> None:
        value = utc_now_iso()
        self.assertTrue(value)
        self.assertTrue(value.endswith("Z"))

    def test_task_record_validates_paths(self) -> None:
        record = TaskRecord(
            task_key=" AT-0003 ",
            project="agent-taskflow",
            status="blocked",
            repo_path="/home/ubuntu/agent-taskflow",
            artifact_dir="/home/ubuntu/.hermes/task-artifacts/AT-0003",
        )

        self.assertEqual(record.task_key, "AT-0003")
        self.assertEqual(record.repo_path, Path("/home/ubuntu/agent-taskflow"))
        self.assertEqual(
            record.artifact_dir,
            Path("/home/ubuntu/.hermes/task-artifacts/AT-0003"),
        )

    def test_artifact_record_rejects_relative_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "path must be absolute"):
            TaskArtifactRecord(
                task_key="AT-0003",
                artifact_type="spec",
                path="relative/spec.md",
            )

    def test_worktree_record_rejects_relative_worktree_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "worktree_path must be absolute"):
            TaskWorktreeRecord(
                task_key="AT-0003",
                repo_path="/home/ubuntu/agent-taskflow",
                worktree_path=".worktrees/AT-0003",
                branch="task/AT-0003",
                status="active",
            )

    def test_worktree_record_accepts_optional_base_sha(self) -> None:
        record = TaskWorktreeRecord(
            task_key="AT-0003",
            repo_path="/home/ubuntu/agent-taskflow",
            worktree_path="/home/ubuntu/agent-taskflow/.worktrees/AT-0003",
            branch="task/AT-0003",
            base_branch="main",
            base_sha="abc123",
            status="active",
        )

        self.assertEqual(record.base_sha, "abc123")


class TaskRecordExecutorFieldsTests(unittest.TestCase):
    """Phase 13: TaskRecord executor selection fields."""

    def test_task_record_accepts_executor_field(self) -> None:
        record = TaskRecord(
            task_key="AT-0013",
            project="agent-taskflow",
            status="queued",
            repo_path="/home/ubuntu/agent-taskflow",
            executor="pi",
        )
        self.assertEqual(record.executor, "pi")

    def test_task_record_accepts_model_field(self) -> None:
        record = TaskRecord(
            task_key="AT-0013",
            project="agent-taskflow",
            status="queued",
            repo_path="/home/ubuntu/agent-taskflow",
            model="minimax-01",
        )
        self.assertEqual(record.model, "minimax-01")

    def test_task_record_accepts_provider_field(self) -> None:
        record = TaskRecord(
            task_key="AT-0013",
            project="agent-taskflow",
            status="queued",
            repo_path="/home/ubuntu/agent-taskflow",
            provider="minimax",
        )
        self.assertEqual(record.provider, "minimax")

    def test_task_record_accepts_tools_field(self) -> None:
        record = TaskRecord(
            task_key="AT-0013",
            project="agent-taskflow",
            status="queued",
            repo_path="/home/ubuntu/agent-taskflow",
            tools=["Read", "Write", "Bash"],
        )
        self.assertEqual(record.tools, ["Read", "Write", "Bash"])

    def test_task_record_accepts_pi_bin_field(self) -> None:
        record = TaskRecord(
            task_key="AT-0013",
            project="agent-taskflow",
            status="queued",
            repo_path="/home/ubuntu/agent-taskflow",
            pi_bin="/usr/local/bin/pi",
        )
        self.assertEqual(record.pi_bin, "/usr/local/bin/pi")

    def test_task_record_all_executor_fields_combined(self) -> None:
        record = TaskRecord(
            task_key="AT-0013",
            project="agent-taskflow",
            status="queued",
            repo_path="/home/ubuntu/agent-taskflow",
            executor="pi",
            model="minimax-01",
            provider="minimax",
            tools=["Read", "Write"],
            pi_bin="pi",
        )
        self.assertEqual(record.executor, "pi")
        self.assertEqual(record.model, "minimax-01")
        self.assertEqual(record.provider, "minimax")
        self.assertEqual(record.tools, ["Read", "Write"])
        self.assertEqual(record.pi_bin, "pi")

    def test_task_record_executor_fields_default_to_none(self) -> None:
        record = TaskRecord(
            task_key="AT-0013",
            project="agent-taskflow",
            status="queued",
            repo_path="/home/ubuntu/agent-taskflow",
        )
        self.assertIsNone(record.executor)
        self.assertIsNone(record.model)
        self.assertIsNone(record.provider)
        self.assertIsNone(record.tools)
        self.assertIsNone(record.pi_bin)

    def test_task_record_tools_must_be_list_of_strings(self) -> None:
        # Empty list is fine
        record = TaskRecord(
            task_key="AT-0013",
            project="agent-taskflow",
            status="queued",
            repo_path="/home/ubuntu/agent-taskflow",
            tools=[],
        )
        self.assertEqual(record.tools, [])


if __name__ == "__main__":
    unittest.main()
