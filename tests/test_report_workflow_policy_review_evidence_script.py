"""Tests for scripts/report_workflow_policy_review_evidence.py."""

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

from agent_taskflow.workflow_policy_artifacts import (
    WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME,
    WORKFLOW_POLICY_SUMMARY_FILENAME,
    WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE,
    WORKFLOW_POLICY_ARTIFACT_INDEX_TYPE,
    WORKFLOW_POLICY_PACKAGE_TYPE,
    WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION,
    WORKFLOW_POLICY_REVIEW_KIND,
    WORKFLOW_POLICY_ARTIFACT_FILENAMES,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "report_workflow_policy_review_evidence.py"
EXAMPLE_POLICY = REPO_ROOT / "examples" / "workflow-policy.example.json"


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "report_workflow_policy_review_evidence",
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


# ----------------------------------------------------------------------
# Test that the script module imports redirect_stdout correctly
# ----------------------------------------------------------------------


class ReportWorkflowPolicyReviewEvidenceScriptTests(unittest.TestCase):
    """Tests for the report workflow policy review evidence script."""

    def test_default_generate_mode_exits_zero(self) -> None:
        exit_code, output = _run_main(["--keep-artifacts"])
        self.assertEqual(exit_code, 0)
        self.assertIn("workflow_policy_review_evidence", output)

    def test_artifact_dir_works(self) -> None:
        with TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "report"
            exit_code, output = _run_main(["--artifact-dir", str(artifact_dir), "--keep-artifacts"])
            self.assertEqual(exit_code, 0)
            self.assertTrue((artifact_dir / WORKFLOW_POLICY_SUMMARY_FILENAME).exists())
            self.assertTrue((artifact_dir / WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME).exists())

    def test_output_writes_json_report(self) -> None:
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "report.json"
            artifact_dir = Path(tmp) / "report"
            exit_code, output = _run_main([
                "--artifact-dir", str(artifact_dir),
                "--output", str(output_path),
            ])
            self.assertEqual(exit_code, 0)
            self.assertTrue(output_path.exists())
            report = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertIn("report_type", report)

    def test_no_generate_reads_existing_artifacts(self) -> None:
        with TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "report"
            artifact_dir.mkdir(parents=True)
            # Pre-generate artifacts using the smoke module.
            smoke_spec = importlib.util.spec_from_file_location(
                "run_workflow_policy_pow_package_smoke",
                REPO_ROOT / "scripts" / "run_workflow_policy_pow_package_smoke.py",
            )
            assert smoke_spec is not None and smoke_spec.loader is not None
            smoke = importlib.util.module_from_spec(smoke_spec)
            sys.modules[smoke_spec.name] = smoke
            smoke_spec.loader.exec_module(smoke)
            smoke.main(["--artifact-dir", str(artifact_dir)])

            # Now run report with --no-generate.
            exit_code, _output = _run_main(["--artifact-dir", str(artifact_dir), "--no-generate"])
            self.assertEqual(exit_code, 0)

    def test_no_generate_fails_when_artifacts_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "missing"
            artifact_dir.mkdir(parents=True)
            exit_code, _output = _run_main(["--artifact-dir", str(artifact_dir), "--no-generate"])
            self.assertNotEqual(exit_code, 0)

    def test_invalid_policy_exits_nonzero(self) -> None:
        with TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "policy.json"
            policy_path.write_text("not valid json", encoding="utf-8")
            exit_code, _output = _run_main(["--policy", str(policy_path)])
            self.assertNotEqual(exit_code, 0)

    def test_report_includes_report_type(self) -> None:
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "report.json"
            exit_code, _output = _run_main([
                "--artifact-dir", str(Path(tmp) / "report"),
                "--output", str(output_path),
            ])
            self.assertEqual(exit_code, 0)
            report = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(report["report_type"], "workflow_policy_review_evidence")

    def test_report_includes_artifacts_with_workflow_policy_kind(self) -> None:
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "report.json"
            exit_code, _output = _run_main([
                "--artifact-dir", str(Path(tmp) / "report"),
                "--output", str(output_path),
            ])
            self.assertEqual(exit_code, 0)
            report = json.loads(output_path.read_text(encoding="utf-8"))
            artifacts = report.get("artifacts", [])
            self.assertGreater(len(artifacts), 0)
            workflow_artifacts = [
                a for a in artifacts if a["kind"] == WORKFLOW_POLICY_REVIEW_KIND
            ]
            self.assertEqual(len(workflow_artifacts), 2)
            names = {a["name"] for a in workflow_artifacts}
            self.assertIn(WORKFLOW_POLICY_SUMMARY_FILENAME, names)
            self.assertIn(WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME, names)

    def test_report_includes_workflow_policy_summary_content(self) -> None:
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "report.json"
            exit_code, _output = _run_main([
                "--artifact-dir", str(Path(tmp) / "report"),
                "--output", str(output_path),
            ])
            self.assertEqual(exit_code, 0)
            report = json.loads(output_path.read_text(encoding="utf-8"))
            summary = report.get("workflow_policy_summary", {})
            self.assertIn("artifact_type", summary)
            self.assertIn("schema_version", summary)
            self.assertIn("validation_status", summary)
            self.assertIn("allowed_executors", summary)
            self.assertIn("required_validators", summary)

    def test_report_includes_artifact_index_content(self) -> None:
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "report.json"
            exit_code, _output = _run_main([
                "--artifact-dir", str(Path(tmp) / "report"),
                "--output", str(output_path),
            ])
            self.assertEqual(exit_code, 0)
            report = json.loads(output_path.read_text(encoding="utf-8"))
            index = report.get("artifact_index", {})
            self.assertIn("artifact_index_version", index)
            self.assertIn("package_type", index)
            self.assertIn("artifacts", index)

    def test_report_generation_does_not_mutate_artifacts(self) -> None:
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "report.json"
            artifact_dir = Path(tmp) / "report"
            _run_main(["--artifact-dir", str(artifact_dir), "--output", str(output_path)])

            summary_path = artifact_dir / WORKFLOW_POLICY_SUMMARY_FILENAME
            index_path = artifact_dir / WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME
            before_summary = summary_path.read_bytes()
            before_index = index_path.read_bytes()

            _run_main(["--artifact-dir", str(artifact_dir), "--no-generate", "--output", str(output_path)])

            after_summary = summary_path.read_bytes()
            after_index = index_path.read_bytes()


            self.assertEqual(before_summary, after_summary)
            self.assertEqual(before_index, after_index)

    def test_no_approval_merge_push_cleanup_artifacts_created(self) -> None:
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "report.json"
            artifact_dir = Path(tmp) / "report"
            _run_main(["--artifact-dir", str(artifact_dir), "--output", str(output_path)])

            files = {p.name for p in artifact_dir.iterdir()}
            for name in files:
                with self.subTest(name=name):
                    self.assertNotIn("approve", name.lower())
                    self.assertNotIn("merge", name.lower())
                    self.assertNotIn("push", name.lower())
                    self.assertNotIn("cleanup", name.lower())

    def test_script_does_not_execute_external_shell_commands(self) -> None:
        with TemporaryDirectory() as tmp:
            with mock.patch.object(subprocess, "run") as run:
                _run_main(["--artifact-dir", str(Path(tmp) / "report")])
            run.assert_not_called()

    def test_report_includes_all_required_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "report.json"
            exit_code, _output = _run_main([
                "--artifact-dir", str(Path(tmp) / "report"),
                "--output", str(output_path),
            ])
            self.assertEqual(exit_code, 0)
            report = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertIn("report_type", report)
            self.assertIn("generated_at", report)
            self.assertIn("artifact_dir", report)
            self.assertIn("source_policy_path", report)
            self.assertIn("validation_status", report)
            self.assertIn("artifacts", report)
            self.assertIn("workflow_policy_summary", report)
            self.assertIn("artifact_index", report)

    def test_keep_artifacts_preserves_temp_output(self) -> None:
        exit_code, output = _run_main(["--keep-artifacts"])
        self.assertEqual(exit_code, 0)
        self.assertIn("workflow_policy_review_evidence", output)

    def test_keep_output_alias_works(self) -> None:
        exit_code, _output = _run_main(["--keep-output"])
        self.assertEqual(exit_code, 0)

    def test_report_artifacts_have_size_bytes(self) -> None:
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "report.json"
            _run_main(["--artifact-dir", str(Path(tmp) / "report"), "--output", str(output_path)])
            report = json.loads(output_path.read_text(encoding="utf-8"))
            for artifact in report["artifacts"]:
                self.assertIn("size_bytes", artifact)
                self.assertGreater(artifact["size_bytes"], 0)

    def test_report_workflow_policy_artifacts_are_not_validator_or_executor_logs(self) -> None:
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "report.json"
            _run_main(["--artifact-dir", str(Path(tmp) / "report"), "--output", str(output_path)])
            report = json.loads(output_path.read_text(encoding="utf-8"))
            for artifact in report["artifacts"]:
                if artifact["name"] in WORKFLOW_POLICY_ARTIFACT_FILENAMES:
                    self.assertEqual(artifact["kind"], WORKFLOW_POLICY_REVIEW_KIND)
                    self.assertFalse(artifact["is_validator_log"])
                    self.assertFalse(artifact["is_executor_log"])
                    self.assertFalse(artifact["is_mission_contract"])

    def test_report_workflow_policy_summary_has_required_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "report.json"
            _run_main(["--artifact-dir", str(Path(tmp) / "report"), "--output", str(output_path)])
            report = json.loads(output_path.read_text(encoding="utf-8"))
            summary = report["workflow_policy_summary"]
            self.assertEqual(summary["artifact_type"], WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE)
            self.assertEqual(summary["validation_status"], "passed")
            self.assertIn("validation_errors", summary)
            self.assertIn("validation_warnings", summary)
            self.assertIn("allowed_executors", summary)
            self.assertIn("required_validators", summary)

    def test_report_artifact_index_has_required_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "report.json"
            _run_main(["--artifact-dir", str(Path(tmp) / "report"), "--output", str(output_path)])
            report = json.loads(output_path.read_text(encoding="utf-8"))
            index = report["artifact_index"]
            self.assertEqual(index["artifact_index_version"], WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION)
            self.assertEqual(index["package_type"], WORKFLOW_POLICY_PACKAGE_TYPE)
            self.assertIsInstance(index["artifacts"], list)
            entry = next(a for a in index["artifacts"] if a["name"] == "workflow_policy_summary")
            self.assertEqual(entry["artifact_type"], WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE)
            self.assertEqual(entry["path"], WORKFLOW_POLICY_SUMMARY_FILENAME)
            self.assertIs(entry["required"], True)


if __name__ == "__main__":
    unittest.main()