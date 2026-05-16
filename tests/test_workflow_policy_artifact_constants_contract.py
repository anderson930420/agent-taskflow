"""Contract tests locking the Phase 100 doc to the Phase 101 shared constants.

This module verifies that:
- docs/workflow-policy-artifact-metadata-contract.md mentions all canonical
  constants defined in agent_taskflow.workflow_policy_artifacts.
- The code constants preserve their expected values.
- The doc contains required backward-compatibility and non-goal statements.

These are pure contract-verification tests. No runtime behavior is tested.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from agent_taskflow.workflow_policy_artifacts import (
    WORKFLOW_POLICY_SUMMARY_FILENAME,
    WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME,
    WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE,
    WORKFLOW_POLICY_ARTIFACT_INDEX_TYPE,
    WORKFLOW_POLICY_REVIEW_KIND,
    WORKFLOW_POLICY_PACKAGE_TYPE,
    WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION,
    WORKFLOW_POLICY_ARTIFACT_FILENAMES,
    WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS,
    WORKFLOW_POLICY_REQUIRED_INDEX_TOP_LEVEL_FIELDS,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "workflow-policy-artifact-metadata-contract.md"


class ConstantsContractTests(unittest.TestCase):
    """Verify code constants preserve their expected values."""

    def test_workflow_policy_summary_filename_value(self) -> None:
        self.assertEqual(WORKFLOW_POLICY_SUMMARY_FILENAME, "workflow_policy_summary.json")

    def test_workflow_policy_artifact_index_filename_value(self) -> None:
        self.assertEqual(WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME, "artifact_index.json")

    def test_workflow_policy_summary_artifact_type_value(self) -> None:
        self.assertEqual(WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE, "workflow_policy_summary")

    def test_workflow_policy_artifact_index_type_value(self) -> None:
        self.assertEqual(WORKFLOW_POLICY_ARTIFACT_INDEX_TYPE, "artifact_index")

    def test_workflow_policy_review_kind_value(self) -> None:
        self.assertEqual(WORKFLOW_POLICY_REVIEW_KIND, "workflow_policy")

    def test_workflow_policy_package_type_value(self) -> None:
        self.assertEqual(WORKFLOW_POLICY_PACKAGE_TYPE, "workflow_policy_proof_of_work")

    def test_workflow_policy_artifact_index_version_value(self) -> None:
        self.assertEqual(WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION, "0.1")

    def test_workflow_policy_artifact_filenames_contains_both(self) -> None:
        self.assertIn(WORKFLOW_POLICY_SUMMARY_FILENAME, WORKFLOW_POLICY_ARTIFACT_FILENAMES)
        self.assertIn(WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME, WORKFLOW_POLICY_ARTIFACT_FILENAMES)
        self.assertEqual(len(WORKFLOW_POLICY_ARTIFACT_FILENAMES), 2)

    def test_workflow_policy_required_summary_fields_includes_validation_errors(self) -> None:
        self.assertIn("validation_errors", WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS)

    def test_workflow_policy_required_summary_fields_includes_validation_warnings(self) -> None:
        self.assertIn("validation_warnings", WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS)

    def test_workflow_policy_required_summary_fields_excludes_optional_validators(self) -> None:
        self.assertNotIn("optional_validators", WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS)


class DocContractTests(unittest.TestCase):
    """Verify that docs/workflow-policy-artifact-metadata-contract.md mentions canonical constants."""

    @classmethod
    def setUpClass(cls) -> None:
        if not DOC_PATH.exists():
            raise FileNotFoundError(f"Contract doc not found: {DOC_PATH}")
        cls.content = DOC_PATH.read_text(encoding="utf-8")

    def test_doc_exists(self) -> None:
        self.assertTrue(DOC_PATH.exists())

    def test_doc_mentions_workflow_policy_summary_filename(self) -> None:
        self.assertIn(WORKFLOW_POLICY_SUMMARY_FILENAME, self.content)

    def test_doc_mentions_workflow_policy_artifact_index_filename(self) -> None:
        self.assertIn(WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME, self.content)

    def test_doc_mentions_workflow_policy_summary_artifact_type(self) -> None:
        self.assertIn(WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE, self.content)

    def test_doc_mentions_workflow_policy_artifact_index_artifact_type(self) -> None:
        self.assertIn(WORKFLOW_POLICY_ARTIFACT_INDEX_TYPE, self.content)

    def test_doc_mentions_workflow_policy_review_kind(self) -> None:
        self.assertIn(WORKFLOW_POLICY_REVIEW_KIND, self.content)

    def test_doc_mentions_workflow_policy_package_type(self) -> None:
        self.assertIn(WORKFLOW_POLICY_PACKAGE_TYPE, self.content)

    def test_doc_mentions_workflow_policy_artifact_index_version(self) -> None:
        self.assertIn(WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION, self.content)

    def test_doc_mentions_required_summary_fields(self) -> None:
        """Every field in WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS should be documented."""
        for field in WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, self.content, f"required field {field!r} missing from contract doc")

    def test_doc_mentions_required_index_top_level_fields(self) -> None:
        """Every field in WORKFLOW_POLICY_REQUIRED_INDEX_TOP_LEVEL_FIELDS should be documented."""
        for field in WORKFLOW_POLICY_REQUIRED_INDEX_TOP_LEVEL_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, self.content, f"required index top-level field {field!r} missing from contract doc")

    def test_doc_states_other_remains_supported(self) -> None:
        self.assertIn("other", self.content)
        self.assertIn("backward", self.content.lower())

    def test_doc_states_db_schema_unchanged(self) -> None:
        self.assertIn("schema", self.content.lower())
        self.assertIn("unchanged", self.content.lower())

    def test_doc_states_explicit_types_go_forward(self) -> None:
        # Document should mention that new artifacts should use explicit types.
        self.assertIn("explicit", self.content.lower())

    def test_doc_states_no_dispatcher_enforcement(self) -> None:
        self.assertIn("dispatcher", self.content)
        self.assertIn("enforcement", self.content.lower())

    def test_doc_states_no_executor_behavior_change(self) -> None:
        self.assertIn("executor", self.content.lower())
        self.assertIn("not add", self.content.lower())

    def test_doc_states_no_validator_registry_change(self) -> None:
        self.assertIn("validator", self.content.lower())
        self.assertIn("registry", self.content.lower())

    def test_doc_states_no_api_endpoint_change(self) -> None:
        self.assertIn("API", self.content)
        self.assertIn("endpoint", self.content)

    def test_doc_states_no_mission_control_frontend_change(self) -> None:
        self.assertIn("Mission Control", self.content)
        self.assertIn("frontend", self.content.lower())

    def test_doc_states_no_github_sync(self) -> None:
        self.assertIn("GitHub", self.content)

    def test_doc_states_no_pr_creation(self) -> None:
        self.assertIn("PR", self.content)

    def test_doc_states_no_merge_push_cleanup_automation(self) -> None:
        self.assertIn("merge", self.content.lower())
        self.assertIn("push", self.content.lower())
        self.assertIn("cleanup", self.content.lower())

    def test_doc_lists_non_goals_section(self) -> None:
        self.assertIn("Non-Goals", self.content)


if __name__ == "__main__":
    unittest.main()