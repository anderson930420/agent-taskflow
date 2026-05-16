"""Tests for scripts/run_workflow_policy_pow_package_smoke.py."""

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
SCRIPT = REPO_ROOT / "scripts" / "run_workflow_policy_pow_package_smoke.py"
EXAMPLE_POLICY = REPO_ROOT / "examples" / "workflow-policy.example.json"

# Import from the shared constants module for consistency.
from agent_taskflow.workflow_policy_artifacts import (
    WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME,
    WORKFLOW_POLICY_SUMMARY_FILENAME,
)

INDEX_FILENAME = WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME
SUMMARY_FILENAME = WORKFLOW_POLICY_SUMMARY_FILENAME


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "run_workflow_policy_pow_package_smoke",
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


class WorkflowPolicyPowPackageSmokeTests(unittest.TestCase):
    def test_smoke_succeeds_with_default_policy(self) -> None:
        exit_code, output = _run_main([])

        self.assertEqual(exit_code, 0)
        self.assertIn("Workflow policy proof-of-work package smoke", output)
        self.assertIn("status: passed", output)

    def test_artifact_dir_works(self) -> None:
        with TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "pow"

            exit_code, output = _run_main(["--artifact-dir", str(artifact_dir)])

            self.assertEqual(exit_code, 0)
            self.assertIn(f"artifact dir: {artifact_dir}", output)
            self.assertTrue((artifact_dir / INDEX_FILENAME).exists())
            self.assertTrue((artifact_dir / SUMMARY_FILENAME).exists())

    def test_keep_artifacts_preserves_default_temp_output(self) -> None:
        exit_code, output = _run_main(["--keep-artifacts"])

        self.assertEqual(exit_code, 0)
        artifact_dir_line = next(line for line in output.splitlines() if line.startswith("artifact dir: "))
        artifact_dir = Path(artifact_dir_line.removeprefix("artifact dir: "))
        self.assertTrue((artifact_dir / INDEX_FILENAME).exists())
        self.assertTrue((artifact_dir / SUMMARY_FILENAME).exists())
        self.assertIn("artifacts kept: yes", output)

    def test_keep_output_alias_preserves_default_temp_output(self) -> None:
        exit_code, output = _run_main(["--keep-output"])

        self.assertEqual(exit_code, 0)
        artifact_dir_line = next(line for line in output.splitlines() if line.startswith("artifact dir: "))
        artifact_dir = Path(artifact_dir_line.removeprefix("artifact dir: "))
        self.assertTrue((artifact_dir / INDEX_FILENAME).exists())
        self.assertTrue((artifact_dir / SUMMARY_FILENAME).exists())
        self.assertIn("artifacts kept: yes", output)

    def test_artifact_index_json_is_written(self) -> None:
        with TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "pow"

            exit_code, _output = _run_main(["--artifact-dir", str(artifact_dir)])

            self.assertEqual(exit_code, 0)
            index = json.loads((artifact_dir / INDEX_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(index["artifact_index_version"], "0.1")
            self.assertEqual(index["package_type"], "workflow_policy_proof_of_work")
            self.assertIn("generated_at", index)
            self.assertIsInstance(index["artifacts"], list)

    def test_workflow_policy_summary_json_is_written(self) -> None:
        with TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "pow"

            exit_code, _output = _run_main(["--artifact-dir", str(artifact_dir)])

            summary = json.loads((artifact_dir / SUMMARY_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(summary["artifact_type"], "workflow_policy_summary")
            self.assertEqual(summary["validation_status"], "passed")

    def test_index_references_summary_artifact(self) -> None:
        with TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "pow"

            exit_code, _output = _run_main(["--artifact-dir", str(artifact_dir)])

            index = json.loads((artifact_dir / INDEX_FILENAME).read_text(encoding="utf-8"))
            artifacts = index["artifacts"]
            summary_entries = [
                artifact for artifact in artifacts if artifact["name"] == "workflow_policy_summary"
            ]
            self.assertEqual(exit_code, 0)
            self.assertEqual(len(summary_entries), 1)
            self.assertEqual(summary_entries[0]["artifact_type"], "workflow_policy_summary")
            self.assertEqual(summary_entries[0]["path"], SUMMARY_FILENAME)
            self.assertIs(summary_entries[0]["required"], True)

    def test_referenced_artifact_path_exists(self) -> None:
        with TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "pow"

            exit_code, _output = _run_main(["--artifact-dir", str(artifact_dir)])

            index = json.loads((artifact_dir / INDEX_FILENAME).read_text(encoding="utf-8"))
            summary_entry = index["artifacts"][0]
            self.assertEqual(exit_code, 0)
            self.assertTrue((artifact_dir / summary_entry["path"]).exists())

    def test_invalid_policy_causes_nonzero_exit_and_no_complete_package(self) -> None:
        data = copy.deepcopy(_example_policy_data())
        data["orchestration_boundary"]["ai_workers_may_cleanup"] = True
        with TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "workflow-policy.json"
            artifact_dir = Path(tmp) / "pow"
            policy_path.write_text(json.dumps(data), encoding="utf-8")

            exit_code, output = _run_main(
                ["--policy", str(policy_path), "--artifact-dir", str(artifact_dir)]
            )

            self.assertNotEqual(exit_code, 0)
            self.assertFalse((artifact_dir / INDEX_FILENAME).exists())
            self.assertFalse((artifact_dir / SUMMARY_FILENAME).exists())
            self.assertIn("ai_workers_may_cleanup must be false", output)

    def test_missing_policy_causes_nonzero_exit_and_no_complete_package(self) -> None:
        with TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "missing-policy.json"
            artifact_dir = Path(tmp) / "pow"

            exit_code, output = _run_main(
                ["--policy", str(policy_path), "--artifact-dir", str(artifact_dir)]
            )

            self.assertNotEqual(exit_code, 0)
            self.assertFalse((artifact_dir / INDEX_FILENAME).exists())
            self.assertFalse((artifact_dir / SUMMARY_FILENAME).exists())
            self.assertIn("workflow policy file not found", output)

    def test_required_summary_fields_are_verified(self) -> None:
        with TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "pow"

            exit_code, output = _run_main(["--artifact-dir", str(artifact_dir)])

            summary = json.loads((artifact_dir / SUMMARY_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            for field in (
                "artifact_type",
                "schema_version",
                "source_path",
                "validation_status",
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
                self.assertIn(field, summary)
                self.assertIn(f"- {field}", output)

    def test_script_does_not_execute_external_shell_commands(self) -> None:
        with TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "pow"

            with mock.patch.object(subprocess, "run") as run:
                exit_code, output = _run_main(["--artifact-dir", str(artifact_dir)])

        run.assert_not_called()
        self.assertEqual(exit_code, 0)
        self.assertIn("status: passed", output)


if __name__ == "__main__":
    unittest.main()
