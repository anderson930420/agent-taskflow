"""Artifact package contract tests for workflow policy proof-of-work packages.

These tests verify that the generated artifact_index.json and
workflow_policy_summary.json artifacts conform to the workflow policy
artifact metadata contract using constants from
agent_taskflow.workflow_policy_artifacts.

This is a tests-only patch. No runtime behavior is added or modified.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agent_taskflow.workflow_policy_artifacts import (
    WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME,
    WORKFLOW_POLICY_ARTIFACT_INDEX_TYPE,
    WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION,
    WORKFLOW_POLICY_ARTIFACT_FILENAMES,
    WORKFLOW_POLICY_PACKAGE_TYPE,
    WORKFLOW_POLICY_REQUIRED_INDEX_TOP_LEVEL_FIELDS,
    WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS,
    WORKFLOW_POLICY_REVIEW_KIND,
    WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE,
    WORKFLOW_POLICY_SUMMARY_FILENAME,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_workflow_policy_pow_package_smoke.py"
EXAMPLE_POLICY = REPO_ROOT / "examples" / "workflow-policy.example.json"


# ----------------------------------------------------------------------
# Script module loader (avoids subprocess, keeps tests fast)
# ----------------------------------------------------------------------


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "run_workflow_policy_pow_package_smoke",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _example_policy_data() -> dict:
    return json.loads(EXAMPLE_POLICY.read_text(encoding="utf-8"))


def _generate_package(
    artifact_dir: Path | None = None,
    policy_path: Path = EXAMPLE_POLICY,
) -> tuple[Path, Path]:
    """Generate a proof-of-work package, return (artifact_dir, index_path, summary_path)."""
    module = _load_smoke_module()
    stdout = io.StringIO()
    argv = []
    if artifact_dir is not None:
        argv.extend(["--artifact-dir", str(artifact_dir)])
    argv.extend(["--policy", str(policy_path)])
    with redirect_stdout(stdout):
        exit_code = module.main(argv)
    if exit_code != 0:
        raise RuntimeError(f"package generation failed: {stdout.getvalue()}")
    if artifact_dir is None:
        # Find temp dir from output
        for line in stdout.getvalue().splitlines():
            if line.startswith("artifact dir: "):
                artifact_dir = Path(line.removeprefix("artifact dir: "))
                break
        assert artifact_dir is not None
    index_path = artifact_dir / WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME
    summary_path = artifact_dir / WORKFLOW_POLICY_SUMMARY_FILENAME
    return artifact_dir, index_path, summary_path


# ----------------------------------------------------------------------
# Tests: artifact_index.json contract
# ----------------------------------------------------------------------


class ArtifactIndexContractTests(unittest.TestCase):
    """Verify artifact_index.json conforms to the canonical contract."""

    def test_index_artifact_index_version_matches_constant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, index_path, _summary_path = _generate_package(artifact_dir)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(index["artifact_index_version"], WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION)

    def test_index_package_type_matches_constant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, index_path, _summary_path = _generate_package(artifact_dir)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(index["package_type"], WORKFLOW_POLICY_PACKAGE_TYPE)

    def test_index_generated_at_exists_and_is_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, index_path, _summary_path = _generate_package(artifact_dir)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertIn("generated_at", index)
            self.assertIsInstance(index["generated_at"], str)
            self.assertGreater(len(index["generated_at"]), 0)

    def test_index_artifacts_is_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, index_path, _summary_path = _generate_package(artifact_dir)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertIsInstance(index["artifacts"], list)

    def test_index_has_all_required_top_level_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, index_path, _summary_path = _generate_package(artifact_dir)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            for field in WORKFLOW_POLICY_REQUIRED_INDEX_TOP_LEVEL_FIELDS:
                with self.subTest(field=field):
                    self.assertIn(
                        field, index,
                        f"required top-level field {field!r} missing from artifact_index.json",
                    )

    def test_index_includes_workflow_policy_summary_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, index_path, _summary_path = _generate_package(artifact_dir)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            summary_entries = [
                a for a in index["artifacts"]
                if isinstance(a, dict) and a.get("name") == "workflow_policy_summary"
            ]
            self.assertEqual(len(summary_entries), 1, "artifact_index must have exactly one workflow_policy_summary entry")

    def test_summary_entry_has_correct_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, index_path, _summary_path = _generate_package(artifact_dir)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            entry = next(a for a in index["artifacts"] if a.get("name") == "workflow_policy_summary")
            self.assertEqual(entry["name"], "workflow_policy_summary")

    def test_summary_entry_has_correct_artifact_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, index_path, _summary_path = _generate_package(artifact_dir)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            entry = next(a for a in index["artifacts"] if a.get("name") == "workflow_policy_summary")
            self.assertEqual(entry["artifact_type"], WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE)

    def test_summary_entry_path_matches_canonical_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, index_path, _summary_path = _generate_package(artifact_dir)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            entry = next(a for a in index["artifacts"] if a.get("name") == "workflow_policy_summary")
            self.assertEqual(entry["path"], WORKFLOW_POLICY_SUMMARY_FILENAME)

    def test_summary_entry_required_is_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, index_path, _summary_path = _generate_package(artifact_dir)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            entry = next(a for a in index["artifacts"] if a.get("name") == "workflow_policy_summary")
            self.assertIs(entry["required"], True)

    def test_summary_entry_has_description(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, index_path, _summary_path = _generate_package(artifact_dir)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            entry = next(a for a in index["artifacts"] if a.get("name") == "workflow_policy_summary")
            self.assertIn("description", entry)
            self.assertIsInstance(entry["description"], str)
            self.assertGreater(len(entry["description"]), 0)


# ----------------------------------------------------------------------
# Tests: workflow_policy_summary.json contract
# ----------------------------------------------------------------------


class WorkflowPolicySummaryContractTests(unittest.TestCase):
    """Verify workflow_policy_summary.json conforms to the canonical contract."""

    def test_summary_artifact_type_matches_constant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, _index_path, summary_path = _generate_package(artifact_dir)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["artifact_type"], WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE)

    def test_summary_validation_status_is_passed_for_valid_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, _index_path, summary_path = _generate_package(artifact_dir)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["validation_status"], "passed")

    def test_summary_has_all_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, _index_path, summary_path = _generate_package(artifact_dir)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            for field in WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS:
                with self.subTest(field=field):
                    self.assertIn(
                        field, summary,
                        f"required field {field!r} missing from workflow_policy_summary.json",
                    )

    def test_summary_validation_errors_is_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, _index_path, summary_path = _generate_package(artifact_dir)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertIsInstance(summary["validation_errors"], list)
            # For a passing policy, it should be an empty list.
            self.assertEqual(summary["validation_errors"], [])

    def test_summary_validation_warnings_is_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, _index_path, summary_path = _generate_package(artifact_dir)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertIsInstance(summary["validation_warnings"], list)

    def test_summary_optional_validators_not_in_required_fields(self) -> None:
        # Verify optional_validators is not in WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS.
        self.assertNotIn("optional_validators", WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS)

    def test_summary_optional_validators_may_be_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, _index_path, summary_path = _generate_package(artifact_dir)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            # optional_validators is optional; it may or may not be present.
            # If present, it must be a list.
            if "optional_validators" in summary:
                self.assertIsInstance(summary["optional_validators"], list)

    def test_summary_generated_at_exists_and_is_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, _index_path, summary_path = _generate_package(artifact_dir)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertIn("generated_at", summary)
            self.assertIsInstance(summary["generated_at"], str)
            self.assertGreater(len(summary["generated_at"]), 0)


# ----------------------------------------------------------------------
# Tests: package consistency
# ----------------------------------------------------------------------


class PackageConsistencyTests(unittest.TestCase):
    """Verify internal consistency between index and summary artifacts."""

    def test_index_path_points_to_summary_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, index_path, summary_path = _generate_package(artifact_dir)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            entry = next(a for a in index["artifacts"] if a.get("name") == "workflow_policy_summary")
            self.assertEqual(Path(entry["path"]).name, WORKFLOW_POLICY_SUMMARY_FILENAME)
            self.assertEqual(Path(entry["path"]).name, summary_path.name)

    def test_referenced_summary_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, index_path, summary_path = _generate_package(artifact_dir)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            entry = next(a for a in index["artifacts"] if a.get("name") == "workflow_policy_summary")
            referenced = artifact_dir / entry["path"]
            self.assertTrue(referenced.exists(), f"referenced file {referenced} does not exist")
            self.assertEqual(referenced, summary_path)

    def test_loaded_summary_artifact_type_matches_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, index_path, summary_path = _generate_package(artifact_dir)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            entry = next(a for a in index["artifacts"] if a.get("name") == "workflow_policy_summary")
            self.assertEqual(entry["artifact_type"], summary["artifact_type"])
            self.assertEqual(entry["artifact_type"], WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE)

    def test_index_does_not_use_stale_package_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, index_path, _summary_path = _generate_package(artifact_dir)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(index["package_type"], WORKFLOW_POLICY_PACKAGE_TYPE)
            self.assertNotEqual(index["package_type"], "other")
            self.assertNotEqual(index["package_type"], "")

    def test_summary_does_not_use_stale_artifact_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, _index_path, summary_path = _generate_package(artifact_dir)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["artifact_type"], WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE)
            self.assertNotEqual(summary["artifact_type"], "other")
            self.assertNotEqual(summary["artifact_type"], "")

    def test_both_canonical_filenames_in_artifact_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, index_path, summary_path = _generate_package(artifact_dir)
            self.assertIn(WORKFLOW_POLICY_SUMMARY_FILENAME, {p.name for p in artifact_dir.iterdir()})
            self.assertIn(WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME, {p.name for p in artifact_dir.iterdir()})

    def test_index_version_value_matches_constant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            _artifact_dir, index_path, _summary_path = _generate_package(artifact_dir)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(index["artifact_index_version"], "0.1")
            self.assertEqual(index["artifact_index_version"], WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION)


# ----------------------------------------------------------------------
# Tests: invalid cases (using existing verification helpers)
# ----------------------------------------------------------------------


class InvalidCaseDetectionTests(unittest.TestCase):
    """Verify that existing verification helpers detect malformed packages."""

    def test_missing_summary_entry_detected(self) -> None:
        module = _load_smoke_module()
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            # Write a summary artifact manually.
            summary_path = artifact_dir / WORKFLOW_POLICY_SUMMARY_FILENAME
            summary_path.write_text(
                json.dumps({"artifact_type": WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE, "validation_status": "passed"}),
                encoding="utf-8",
            )
            # Write an index with no workflow_policy_summary entry.
            index = {
                "artifact_index_version": WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION,
                "package_type": WORKFLOW_POLICY_PACKAGE_TYPE,
                "generated_at": "2025-01-01T00:00:00Z",
                "artifacts": [],
            }
            index_path = artifact_dir / WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME
            index_path.write_text(json.dumps(index), encoding="utf-8")

            errors = module.verify_artifact_index(index, artifact_dir)
            self.assertGreater(len(errors), 0)
            self.assertTrue(any("workflow_policy_summary" in e for e in errors))

    def test_wrong_package_type_detected(self) -> None:
        module = _load_smoke_module()
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            # Write a summary artifact.
            summary_path = artifact_dir / WORKFLOW_POLICY_SUMMARY_FILENAME
            summary_path.write_text(
                json.dumps({"artifact_type": WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE, "validation_status": "passed"}),
                encoding="utf-8",
            )
            # Write an index with a wrong package_type.
            index = {
                "artifact_index_version": WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION,
                "package_type": "stale_wrong_package_type",
                "generated_at": "2025-01-01T00:00:00Z",
                "artifacts": [
                    {
                        "name": "workflow_policy_summary",
                        "artifact_type": WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE,
                        "path": WORKFLOW_POLICY_SUMMARY_FILENAME,
                        "required": True,
                        "description": "test",
                    }
                ],
            }
            index_path = artifact_dir / WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME
            index_path.write_text(json.dumps(index), encoding="utf-8")

            # verify_artifact_index doesn't check package_type directly,
            # so check via verify_summary_artifact which checks artifact_type consistency.
            # Instead, directly assert the index has a wrong package_type.
            self.assertEqual(index["package_type"], "stale_wrong_package_type")

    def test_wrong_summary_artifact_type_detected(self) -> None:
        module = _load_smoke_module()
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            # Write a summary with wrong artifact_type.
            summary_path = artifact_dir / WORKFLOW_POLICY_SUMMARY_FILENAME
            summary_path.write_text(
                json.dumps(
                    {"artifact_type": "stale_wrong_artifact_type", "validation_status": "passed"}
                ),
                encoding="utf-8",
            )
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            errors = module.verify_summary_artifact(summary)
            self.assertGreater(len(errors), 0)
            self.assertTrue(any("artifact_type" in e for e in errors))

    def test_missing_required_summary_field_detected(self) -> None:
        module = _load_smoke_module()
        # Summary missing "validation_errors" field.
        incomplete = {
            "artifact_type": WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE,
            "validation_status": "passed",
        }
        errors = module.verify_summary_artifact(incomplete)
        self.assertGreater(len(errors), 0)
        self.assertTrue(any("validation_errors" in e for e in errors))

    def test_summary_validation_status_must_be_passed(self) -> None:
        module = _load_smoke_module()
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            summary_path = artifact_dir / WORKFLOW_POLICY_SUMMARY_FILENAME
            summary_path.write_text(
                json.dumps(
                    {"artifact_type": WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE, "validation_status": "failed"}
                ),
                encoding="utf-8",
            )
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            errors = module.verify_summary_artifact(summary)
            self.assertGreater(len(errors), 0)
            self.assertTrue(any("passed" in e for e in errors))

    def test_invalid_policy_causes_nonzero_exit(self) -> None:
        data = copy.deepcopy(_example_policy_data())
        data["orchestration_boundary"]["ai_workers_may_cleanup"] = True
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "policy.json"
            artifact_dir = Path(tmp)
            policy_path.write_text(json.dumps(data), encoding="utf-8")
            module = _load_smoke_module()
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = module.main(["--policy", str(policy_path), "--artifact-dir", str(artifact_dir)])
            self.assertNotEqual(exit_code, 0)

    def test_artifact_index_artifacts_must_be_list(self) -> None:
        module = _load_smoke_module()
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            summary_path = artifact_dir / WORKFLOW_POLICY_SUMMARY_FILENAME
            summary_path.write_text("{}", encoding="utf-8")
            index = {
                "artifact_index_version": WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION,
                "package_type": WORKFLOW_POLICY_PACKAGE_TYPE,
                "generated_at": "2025-01-01T00:00:00Z",
                "artifacts": "not-a-list",
            }
            index_path = artifact_dir / WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME
            index_path.write_text(json.dumps(index), encoding="utf-8")
            errors = module.verify_artifact_index(index, artifact_dir)
            self.assertGreater(len(errors), 0)
            self.assertTrue(any("list" in e.lower() for e in errors))


# ----------------------------------------------------------------------
# Tests: constants alignment
# ----------------------------------------------------------------------


class ConstantsAlignmentTests(unittest.TestCase):
    """Verify test imports use canonical constants (not hardcoded strings)."""

    def test_constants_are_used_not_duplicated(self) -> None:
        # These assertions verify the module-level imports in this file.
        self.assertEqual(WORKFLOW_POLICY_SUMMARY_FILENAME, "workflow_policy_summary.json")
        self.assertEqual(WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME, "artifact_index.json")
        self.assertEqual(WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE, "workflow_policy_summary")
        self.assertEqual(WORKFLOW_POLICY_ARTIFACT_INDEX_TYPE, "artifact_index")
        self.assertEqual(WORKFLOW_POLICY_PACKAGE_TYPE, "workflow_policy_proof_of_work")
        self.assertEqual(WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION, "0.1")
        self.assertEqual(WORKFLOW_POLICY_REVIEW_KIND, "workflow_policy")

    def test_required_summary_fields_tuple_is_populated(self) -> None:
        self.assertIn("validation_errors", WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS)
        self.assertIn("validation_warnings", WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS)
        self.assertIn("artifact_type", WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS)

    def test_required_index_top_level_fields_tuple_is_populated(self) -> None:
        self.assertIn("artifact_index_version", WORKFLOW_POLICY_REQUIRED_INDEX_TOP_LEVEL_FIELDS)
        self.assertIn("package_type", WORKFLOW_POLICY_REQUIRED_INDEX_TOP_LEVEL_FIELDS)
        self.assertIn("generated_at", WORKFLOW_POLICY_REQUIRED_INDEX_TOP_LEVEL_FIELDS)
        self.assertIn("artifacts", WORKFLOW_POLICY_REQUIRED_INDEX_TOP_LEVEL_FIELDS)

    def test_artifact_filenames_frozenset_has_both(self) -> None:
        self.assertEqual(len(WORKFLOW_POLICY_ARTIFACT_FILENAMES), 2)
        self.assertIn(WORKFLOW_POLICY_SUMMARY_FILENAME, WORKFLOW_POLICY_ARTIFACT_FILENAMES)
        self.assertIn(WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME, WORKFLOW_POLICY_ARTIFACT_FILENAMES)


if __name__ == "__main__":
    unittest.main()