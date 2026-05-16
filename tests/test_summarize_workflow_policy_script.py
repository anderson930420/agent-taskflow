"""Tests for scripts/summarize_workflow_policy.py."""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "summarize_workflow_policy.py"
EXAMPLE_POLICY = REPO_ROOT / "examples" / "workflow-policy.example.json"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("summarize_workflow_policy", SCRIPT)
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


class SummarizeWorkflowPolicyScriptTests(unittest.TestCase):
    def test_default_example_policy_summary_exits_zero(self) -> None:
        exit_code, output = _run_main([])

        self.assertEqual(exit_code, 0)
        self.assertIn("Workflow policy summary", output)
        self.assertIn("source path: examples/workflow-policy.example.json", output)
        self.assertIn("validation status: passed", output)

    def test_optional_path_argument_works(self) -> None:
        data = _example_policy_data()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "workflow-policy.json"
            path.write_text(json.dumps(data), encoding="utf-8")

            exit_code, output = _run_main([str(path)])

        self.assertEqual(exit_code, 0)
        self.assertIn(f"source path: {path}", output)
        self.assertIn("validation status: passed", output)

    def test_invalid_policy_exits_nonzero(self) -> None:
        data = copy.deepcopy(_example_policy_data())
        data["orchestration_boundary"]["ai_workers_may_approve"] = True
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "workflow-policy.json"
            path.write_text(json.dumps(data), encoding="utf-8")

            exit_code, output = _run_main([str(path)])

        self.assertNotEqual(exit_code, 0)
        self.assertIn("validation status: failed", output)
        self.assertIn("ai_workers_may_approve must be false", output)

    def test_missing_file_exits_nonzero(self) -> None:
        missing = REPO_ROOT / "does-not-exist-workflow-policy.json"

        exit_code, output = _run_main([str(missing)])

        self.assertNotEqual(exit_code, 0)
        self.assertIn("validation status: failed", output)
        self.assertIn("workflow policy file not found", output)

    def test_invalid_json_exits_nonzero(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "workflow-policy.json"
            path.write_text("{not-json", encoding="utf-8")

            exit_code, output = _run_main([str(path)])

        self.assertNotEqual(exit_code, 0)
        self.assertIn("validation status: failed", output)
        self.assertIn("invalid workflow policy JSON", output)

    def test_output_includes_policy_metadata(self) -> None:
        exit_code, output = _run_main([])

        self.assertEqual(exit_code, 0)
        self.assertIn("source path: examples/workflow-policy.example.json", output)
        self.assertIn("schema_version: 0.1", output)
        self.assertIn("allowed_executors: manual, shell, opencode, pi", output)
        self.assertIn(
            "required_validators: policy, changed-files, pytest, typecheck, lint",
            output,
        )
        self.assertIn("optional_validators: openspec", output)
        self.assertIn("path_policy:", output)
        self.assertIn("workspace_policy:", output)
        self.assertIn("proof_of_work required_artifacts:", output)
        self.assertIn("human_review required: True", output)
        self.assertIn("human_review allowed_decisions: approve, reject, rerun, block", output)
        self.assertIn("forbidden_actions:", output)
        self.assertIn("deferred_integrations:", output)

    def test_output_includes_governance_invariant_summary(self) -> None:
        exit_code, output = _run_main([])

        self.assertEqual(exit_code, 0)
        self.assertIn("governance invariant summary:", output)
        self.assertIn("AI workers may not schedule tasks: True", output)
        self.assertIn("AI workers may not approve: True", output)
        self.assertIn("AI workers may not merge: True", output)
        self.assertIn("AI workers may not push: True", output)
        self.assertIn("AI workers may not cleanup: True", output)

    def test_script_does_not_execute_external_commands(self) -> None:
        data = _example_policy_data()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "workflow-policy.json"
            path.write_text(json.dumps(data), encoding="utf-8")

            with mock.patch.object(subprocess, "run") as run:
                exit_code, output = _run_main([str(path)])

        run.assert_not_called()
        self.assertEqual(exit_code, 0)
        self.assertIn("validation status: passed", output)


if __name__ == "__main__":
    unittest.main()
