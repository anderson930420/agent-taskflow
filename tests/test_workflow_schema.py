"""Tests for the isolated workflow policy schema loader."""

from __future__ import annotations

import copy
import contextlib
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from agent_taskflow.workflow_schema import load_workflow_policy


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_POLICY = REPO_ROOT / "examples" / "workflow-policy.example.json"


def _example_data() -> dict:
    return json.loads(EXAMPLE_POLICY.read_text(encoding="utf-8"))


@contextlib.contextmanager
def _policy_file(data: dict):
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "workflow-policy.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        yield path


class WorkflowSchemaTests(unittest.TestCase):
    def test_example_workflow_policy_loads_successfully(self) -> None:
        policy = load_workflow_policy(EXAMPLE_POLICY)

        self.assertEqual(policy.schema_version, "0.1")
        self.assertEqual(policy.allowed_executors, ["manual", "shell", "opencode", "pi"])

    def test_example_workflow_policy_validates_successfully(self) -> None:
        result = load_workflow_policy(EXAMPLE_POLICY).validate()

        self.assertTrue(result.passed)
        self.assertEqual(result.errors, [])

    def test_missing_file_raises_clear_error(self) -> None:
        with self.assertRaisesRegex(FileNotFoundError, "workflow policy file not found"):
            load_workflow_policy(REPO_ROOT / "missing-workflow-policy.json")

    def test_invalid_json_raises_clear_error(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "workflow-policy.json"
            path.write_text("{not-json", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "invalid workflow policy JSON"):
                load_workflow_policy(path)

    def test_missing_required_top_level_key_fails_validation(self) -> None:
        data = _example_data()
        del data["workspace_policy"]

        with _policy_file(data) as path:
            result = load_workflow_policy(path).validate()

        self.assertFalse(result.passed)
        self.assertIn("Missing required workflow policy key: workspace_policy", result.errors)

    def test_empty_allowed_executors_fails_validation(self) -> None:
        data = _example_data()
        data["allowed_executors"] = []

        with _policy_file(data) as path:
            result = load_workflow_policy(path).validate()

        self.assertFalse(result.passed)
        self.assertIn("allowed_executors must be a non-empty list", result.errors)

    def test_ai_worker_may_approve_true_fails_validation(self) -> None:
        data = _example_data()
        data["orchestration_boundary"]["ai_workers_may_approve"] = True

        with _policy_file(data) as path:
            result = load_workflow_policy(path).validate()

        self.assertFalse(result.passed)
        self.assertIn(
            "orchestration_boundary.ai_workers_may_approve must be false",
            result.errors,
        )

    def test_human_review_required_false_fails_validation(self) -> None:
        data = _example_data()
        data["human_review"]["required"] = False

        with _policy_file(data) as path:
            result = load_workflow_policy(path).validate()

        self.assertFalse(result.passed)
        self.assertIn("human_review.required must be true", result.errors)

    def test_missing_required_forbidden_actions_fails_validation(self) -> None:
        data = _example_data()
        data["forbidden_actions"] = [
            action
            for action in data["forbidden_actions"]
            if action not in {"push", "merge", "cleanup", "self_approve"}
        ]

        with _policy_file(data) as path:
            result = load_workflow_policy(path).validate()

        self.assertFalse(result.passed)
        for action in ["push", "merge", "cleanup", "self_approve"]:
            self.assertIn(f"forbidden_actions must include {action}", result.errors)

    def test_source_path_is_preserved(self) -> None:
        policy = load_workflow_policy(EXAMPLE_POLICY)

        self.assertEqual(policy.source_path, EXAMPLE_POLICY)

    def test_raw_data_is_preserved(self) -> None:
        data = _example_data()

        with _policy_file(data) as path:
            policy = load_workflow_policy(path)

        self.assertEqual(policy.raw_data, data)

    def test_loader_does_not_execute_shell_commands(self) -> None:
        data = _example_data()

        with _policy_file(data) as path:
            with mock.patch.object(subprocess, "run") as run:
                policy = load_workflow_policy(path)
                result = policy.validate()

        run.assert_not_called()
        self.assertTrue(result.passed)

    def test_path_policy_requires_allowed_and_forbidden_paths(self) -> None:
        data = _example_data()
        data["path_policy"] = {}

        with _policy_file(data) as path:
            result = load_workflow_policy(path).validate()

        self.assertFalse(result.passed)
        self.assertIn("path_policy.allowed_paths is required", result.errors)
        self.assertIn("path_policy.forbidden_paths is required", result.errors)

    def test_allowed_executors_entries_must_be_non_empty_strings(self) -> None:
        data = _example_data()
        data["allowed_executors"] = ["manual", "", 123]

        with _policy_file(data) as path:
            result = load_workflow_policy(path).validate()

        self.assertFalse(result.passed)
        self.assertIn("allowed_executors[1] must be a non-empty string", result.errors)
        self.assertIn("allowed_executors[2] must be a non-empty string", result.errors)

    def test_required_validators_entries_must_be_non_empty_strings(self) -> None:
        data = _example_data()
        data["required_validators"] = ["policy", "  ", None]

        with _policy_file(data) as path:
            result = load_workflow_policy(path).validate()

        self.assertFalse(result.passed)
        self.assertIn("required_validators[1] must be a non-empty string", result.errors)
        self.assertIn("required_validators[2] must be a non-empty string", result.errors)

    def test_optional_validators_must_be_list_of_non_empty_strings(self) -> None:
        data = _example_data()
        data["optional_validators"] = ["openspec", ""]

        with _policy_file(data) as path:
            result = load_workflow_policy(path).validate()

        self.assertFalse(result.passed)
        self.assertIn("optional_validators[1] must be a non-empty string", result.errors)

    def test_path_policy_values_must_be_lists(self) -> None:
        data = _example_data()
        data["path_policy"]["allowed_paths"] = "src"
        data["path_policy"]["forbidden_paths"] = {"path": "secrets"}

        with _policy_file(data) as path:
            result = load_workflow_policy(path).validate()

        self.assertFalse(result.passed)
        self.assertIn("path_policy.allowed_paths must be a list", result.errors)
        self.assertIn("path_policy.forbidden_paths must be a list", result.errors)

    def test_path_policy_rejects_absolute_and_traversal_paths(self) -> None:
        data = _example_data()
        data["path_policy"]["allowed_paths"] = ["/tmp/src", "../src", "src/../tests"]
        data["path_policy"]["forbidden_paths"] = [".", "..", ""]

        with _policy_file(data) as path:
            result = load_workflow_policy(path).validate()

        self.assertFalse(result.passed)
        self.assertIn("path_policy.allowed_paths[0] must be a safe repo-relative path", result.errors)
        self.assertIn("path_policy.allowed_paths[1] must be a safe repo-relative path", result.errors)
        self.assertIn("path_policy.allowed_paths[2] must be a safe repo-relative path", result.errors)
        self.assertIn("path_policy.forbidden_paths[0] must be a safe repo-relative path", result.errors)
        self.assertIn("path_policy.forbidden_paths[1] must be a safe repo-relative path", result.errors)
        self.assertIn("path_policy.forbidden_paths[2] must be a safe repo-relative path", result.errors)

    def test_forbidden_actions_entries_must_be_non_empty_strings(self) -> None:
        data = _example_data()
        data["forbidden_actions"] = [
            "self_approve",
            "push",
            "merge",
            "cleanup",
            "",
            123,
        ]

        with _policy_file(data) as path:
            result = load_workflow_policy(path).validate()

        self.assertFalse(result.passed)
        self.assertIn("forbidden_actions[4] must be a non-empty string", result.errors)
        self.assertIn("forbidden_actions[5] must be a non-empty string", result.errors)

    def test_raw_data_is_copied_from_input_file(self) -> None:
        data = _example_data()
        original = copy.deepcopy(data)

        with _policy_file(data) as path:
            policy = load_workflow_policy(path)
        data["schema_version"] = "changed"

        self.assertEqual(policy.raw_data, original)


if __name__ == "__main__":
    unittest.main()
