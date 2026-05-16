"""Tests for scripts/run_workflow_policy_artifact_smoke.py."""

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
SCRIPT = REPO_ROOT / "scripts" / "run_workflow_policy_artifact_smoke.py"
EXAMPLE_POLICY = REPO_ROOT / "examples" / "workflow-policy.example.json"

# Import from shared constants module.
from agent_taskflow.workflow_policy_artifacts import (
    WORKFLOW_POLICY_SUMMARY_FILENAME,
    WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE,
)

ARTIFACT_FILENAME = WORKFLOW_POLICY_SUMMARY_FILENAME


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "run_workflow_policy_artifact_smoke",
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


class WorkflowPolicyArtifactSmokeTests(unittest.TestCase):
    def test_smoke_succeeds_with_default_policy(self) -> None:
        exit_code, output = _run_main([])

        self.assertEqual(exit_code, 0)
        self.assertIn("Workflow policy artifact smoke", output)
        self.assertIn("status: passed", output)

    def test_smoke_writes_expected_artifact_file(self) -> None:
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "artifact-output"

            exit_code, output = _run_main(["--output-dir", str(output_dir)])

            artifact_path = output_dir / ARTIFACT_FILENAME
            self.assertEqual(exit_code, 0)
            self.assertTrue(artifact_path.exists())
            self.assertIn(f"artifact path: {artifact_path}", output)

    def test_smoke_verifies_required_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "artifact-output"

            exit_code, output = _run_main(["--output-dir", str(output_dir)])

            artifact = json.loads((output_dir / ARTIFACT_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(artifact["artifact_type"], WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE)
            self.assertEqual(artifact["validation_status"], "passed")
            # Required fields appear as "- fieldname" (no suffix).
            for field in (
                "artifact_type",
                "schema_version",
                "source_path",
                "validation_status",
                "validation_errors",
                "validation_warnings",
                "allowed_executors",
                "required_validators",
                "path_policy",
                "workspace_policy",
                "proof_of_work",
                "human_review",
                "forbidden_actions",
                "deferred_integrations",
                "governance_invariants",
                "generated_at",
            ):
                self.assertIn(field, artifact)
                self.assertIn(f"- {field}", output)
            # optional_validators is the sole optional field, printed with "[optional]".
            self.assertIn("optional_validators", artifact)
            self.assertIn("- optional_validators  [optional]", output)

    def test_output_dir_works(self) -> None:
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "custom-output"

            exit_code, output = _run_main(["--output-dir", str(output_dir)])

            self.assertEqual(exit_code, 0)
            self.assertTrue((output_dir / ARTIFACT_FILENAME).exists())
            self.assertIn(f"output dir: {output_dir}", output)

    def test_keep_output_preserves_default_temp_output(self) -> None:
        exit_code, output = _run_main(["--keep-output"])

        self.assertEqual(exit_code, 0)
        artifact_line = next(line for line in output.splitlines() if line.startswith("artifact path: "))
        artifact_path = Path(artifact_line.removeprefix("artifact path: "))
        self.assertTrue(artifact_path.exists())
        self.assertIn("output kept: yes", output)

    def test_invalid_policy_causes_nonzero_exit(self) -> None:
        data = copy.deepcopy(_example_policy_data())
        data["orchestration_boundary"]["ai_workers_may_push"] = True
        with TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "workflow-policy.json"
            output_dir = Path(tmp) / "artifact-output"
            policy_path.write_text(json.dumps(data), encoding="utf-8")

            exit_code, output = _run_main(
                ["--policy", str(policy_path), "--output-dir", str(output_dir)]
            )

            self.assertNotEqual(exit_code, 0)
            self.assertFalse((output_dir / ARTIFACT_FILENAME).exists())
            self.assertIn("ai_workers_may_push must be false", output)

    def test_missing_policy_causes_nonzero_exit(self) -> None:
        with TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "missing-policy.json"
            output_dir = Path(tmp) / "artifact-output"

            exit_code, output = _run_main(
                ["--policy", str(policy_path), "--output-dir", str(output_dir)]
            )

            self.assertNotEqual(exit_code, 0)
            self.assertFalse((output_dir / ARTIFACT_FILENAME).exists())
            self.assertIn("workflow policy file not found", output)

    def test_script_does_not_execute_external_shell_commands(self) -> None:
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "artifact-output"

            with mock.patch.object(subprocess, "run") as run:
                exit_code, output = _run_main(["--output-dir", str(output_dir)])

        run.assert_not_called()
        self.assertEqual(exit_code, 0)
        self.assertIn("status: passed", output)


if __name__ == "__main__":
    unittest.main()
