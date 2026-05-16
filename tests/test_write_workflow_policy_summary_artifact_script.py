"""Tests for scripts/write_workflow_policy_summary_artifact.py."""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "write_workflow_policy_summary_artifact.py"
EXAMPLE_POLICY = REPO_ROOT / "examples" / "workflow-policy.example.json"


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "write_workflow_policy_summary_artifact",
        SCRIPT,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_main(argv: list[str]) -> tuple[int, str]:
    module = _load_script_module()
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = module.main(argv)
    return exit_code, stdout.getvalue()


def _example_policy_data() -> dict:
    return json.loads(EXAMPLE_POLICY.read_text(encoding="utf-8"))


class WriteWorkflowPolicySummaryArtifactScriptTests(unittest.TestCase):
    def test_valid_default_policy_writes_artifact_and_exits_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "workflow_policy_summary.json"

            exit_code, output = _run_main(["--output", str(output_path)])

            self.assertEqual(exit_code, 0)
            self.assertTrue(output_path.exists())
            self.assertIn("artifact written", output)

    def test_optional_policy_path_works(self) -> None:
        data = _example_policy_data()
        with TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "workflow-policy.json"
            output_path = Path(tmp) / "artifact.json"
            policy_path.write_text(json.dumps(data), encoding="utf-8")

            exit_code, output = _run_main(
                ["--policy", str(policy_path), "--output", str(output_path)]
            )

            artifact = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertIn(f"source path: {policy_path}", output)
            self.assertEqual(artifact["source_path"], str(policy_path))

    def test_missing_policy_file_exits_nonzero_and_does_not_write_artifact(self) -> None:
        with TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "missing-policy.json"
            output_path = Path(tmp) / "artifact.json"

            exit_code, output = _run_main(
                ["--policy", str(policy_path), "--output", str(output_path)]
            )

            self.assertNotEqual(exit_code, 0)
            self.assertFalse(output_path.exists())
            self.assertIn("workflow policy file not found", output)

    def test_invalid_json_exits_nonzero_and_does_not_write_artifact(self) -> None:
        with TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "workflow-policy.json"
            output_path = Path(tmp) / "artifact.json"
            policy_path.write_text("{not-json", encoding="utf-8")

            exit_code, output = _run_main(
                ["--policy", str(policy_path), "--output", str(output_path)]
            )

            self.assertNotEqual(exit_code, 0)
            self.assertFalse(output_path.exists())
            self.assertIn("invalid workflow policy JSON", output)

    def test_invalid_policy_exits_nonzero_and_does_not_write_artifact(self) -> None:
        data = copy.deepcopy(_example_policy_data())
        data["human_review"]["required"] = False
        with TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "workflow-policy.json"
            output_path = Path(tmp) / "artifact.json"
            policy_path.write_text(json.dumps(data), encoding="utf-8")

            exit_code, output = _run_main(
                ["--policy", str(policy_path), "--output", str(output_path)]
            )

            self.assertNotEqual(exit_code, 0)
            self.assertFalse(output_path.exists())
            self.assertIn("human_review.required must be true", output)

    def test_output_parent_directory_is_created(self) -> None:
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "nested" / "artifacts" / "summary.json"

            exit_code, _output = _run_main(["--output", str(output_path)])

            self.assertEqual(exit_code, 0)
            self.assertTrue(output_path.exists())

    def test_artifact_includes_expected_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "workflow_policy_summary.json"

            exit_code, _output = _run_main(["--output", str(output_path)])

            artifact = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(artifact["artifact_type"], "workflow_policy_summary")
            self.assertEqual(artifact["schema_version"], "0.1")
            self.assertEqual(artifact["source_path"], "examples/workflow-policy.example.json")
            self.assertEqual(artifact["validation_status"], "passed")
            self.assertEqual(artifact["validation_errors"], [])
            self.assertEqual(artifact["validation_warnings"], [])
            self.assertEqual(artifact["allowed_executors"], ["manual", "shell", "opencode", "pi"])
            self.assertEqual(
                artifact["required_validators"],
                ["policy", "changed-files", "pytest", "typecheck", "lint"],
            )
            self.assertEqual(artifact["optional_validators"], ["openspec"])
            self.assertIn("path_policy", artifact)
            self.assertIn("workspace_policy", artifact)
            self.assertIn("proof_of_work", artifact)
            self.assertIn("human_review", artifact)
            self.assertIn("forbidden_actions", artifact)
            self.assertIn("deferred_integrations", artifact)
            self.assertIn("governance_invariants", artifact)
            self.assertIn("generated_at", artifact)

    def test_artifact_generated_at_is_parseable(self) -> None:
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "workflow_policy_summary.json"

            exit_code, _output = _run_main(["--output", str(output_path)])

            artifact = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            datetime.fromisoformat(artifact["generated_at"])

    def test_governance_invariants_include_ai_worker_flags(self) -> None:
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "workflow_policy_summary.json"

            exit_code, _output = _run_main(["--output", str(output_path)])

            artifact = json.loads(output_path.read_text(encoding="utf-8"))
            invariants = artifact["governance_invariants"]
            self.assertEqual(exit_code, 0)
            self.assertIs(invariants["ai_workers_may_schedule_tasks"], False)
            self.assertIs(invariants["ai_workers_may_approve"], False)
            self.assertIs(invariants["ai_workers_may_merge"], False)
            self.assertIs(invariants["ai_workers_may_push"], False)
            self.assertIs(invariants["ai_workers_may_cleanup"], False)

    def test_script_does_not_execute_external_commands(self) -> None:
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "workflow_policy_summary.json"

            with mock.patch.object(subprocess, "run") as run:
                exit_code, output = _run_main(["--output", str(output_path)])

        run.assert_not_called()
        self.assertEqual(exit_code, 0)
        self.assertIn("validation status: passed", output)


if __name__ == "__main__":
    unittest.main()
