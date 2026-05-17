"""Tests for docs/workflow-policy-read-only-exposure-plan.md."""

from __future__ import annotations

import unittest
from pathlib import Path

from agent_taskflow.workflow_policy_artifacts import (
    WORKFLOW_POLICY_SUMMARY_FILENAME,
    WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME,
    WORKFLOW_POLICY_REVIEW_KIND,
    WORKFLOW_POLICY_PACKAGE_TYPE,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "workflow-policy-read-only-exposure-plan.md"


class WorkflowPolicyReadOnlyExposurePlanTests(unittest.TestCase):
    """Tests verifying the read-only exposure plan document."""

    @classmethod
    def setUpClass(cls) -> None:
        if not DOC_PATH.exists():
            raise FileNotFoundError(f"Exposure plan doc not found: {DOC_PATH}")
        cls.content = DOC_PATH.read_text(encoding="utf-8")

    def test_doc_exists(self) -> None:
        self.assertTrue(DOC_PATH.exists())

    def test_doc_states_read_only_exposure(self) -> None:
        self.assertIn("read-only", self.content.lower())

    def test_doc_mentions_workflow_policy_summary_json(self) -> None:
        self.assertIn(WORKFLOW_POLICY_SUMMARY_FILENAME, self.content)

    def test_doc_mentions_artifact_index_json(self) -> None:
        self.assertIn(WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME, self.content)

    def test_doc_mentions_workflow_policy_kind(self) -> None:
        self.assertIn(WORKFLOW_POLICY_REVIEW_KIND, self.content)

    def test_doc_mentions_workflow_policy_proof_of_work_package_type(self) -> None:
        self.assertIn(WORKFLOW_POLICY_PACKAGE_TYPE, self.content)

    def test_doc_states_no_api_endpoint_changes(self) -> None:
        self.assertIn("API", self.content)
        self.assertIn("endpoint", self.content)
        self.assertIn("not add", self.content)

    def test_doc_states_no_mission_control_ui_changes(self) -> None:
        self.assertIn("Mission Control", self.content)
        self.assertIn("UI", self.content)

    def test_doc_states_no_dispatcher_enforcement(self) -> None:
        self.assertIn("dispatcher", self.content.lower())
        self.assertIn("enforcement", self.content.lower())

    def test_doc_states_no_merge_push_cleanup(self) -> None:
        self.assertIn("merge", self.content.lower())
        self.assertIn("push", self.content.lower())
        self.assertIn("cleanup", self.content.lower())

    def test_doc_outlines_staged_exposure_path(self) -> None:
        self.assertIn("Stage", self.content)

    def test_doc_recommends_phase_106(self) -> None:
        self.assertIn("Phase 106", self.content)

    def test_doc_lists_non_goals(self) -> None:
        self.assertIn("Non-Goals", self.content)

    def test_doc_lists_exposure_goals(self) -> None:
        self.assertIn("Exposure Goals", self.content)

    def test_doc_states_read_only_evidence_only(self) -> None:
        self.assertIn("evidence", self.content.lower())

    def test_doc_states_human_review_final_gate(self) -> None:
        self.assertIn("human", self.content.lower())
        self.assertIn("review", self.content.lower())
        self.assertIn("final gate", self.content.lower())


if __name__ == "__main__":
    unittest.main()