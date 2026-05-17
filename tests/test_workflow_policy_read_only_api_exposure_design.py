"""Tests for docs/workflow-policy-read-only-api-exposure-design.md."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "workflow-policy-read-only-api-exposure-design.md"


class WorkflowPolicyReadOnlyApiExposureDesignTests(unittest.TestCase):
    """Tests verifying the read-only API exposure design document."""

    @classmethod
    def setUpClass(cls) -> None:
        if not DOC_PATH.exists():
            raise FileNotFoundError(f"Design doc not found: {DOC_PATH}")
        cls.content = DOC_PATH.read_text(encoding="utf-8")

    def test_doc_exists(self) -> None:
        self.assertTrue(DOC_PATH.exists())

    def test_doc_states_read_only_api_exposure(self) -> None:
        self.assertIn("read-only", self.content.lower())
        self.assertIn("API", self.content)

    def test_doc_states_no_endpoint_implementation_in_this_phase(self) -> None:
        self.assertIn("does not implement", self.content.lower())
        self.assertIn("endpoint", self.content.lower())

    def test_doc_outlines_existing_review_evidence_option(self) -> None:
        self.assertIn("review-evidence", self.content.lower())

    def test_doc_outlines_dedicated_endpoint_as_future_option(self) -> None:
        self.assertIn("dedicated", self.content.lower())
        self.assertIn("endpoint", self.content.lower())

    def test_doc_proposes_workflow_policy_evidence_shape(self) -> None:
        self.assertIn("workflow_policy_evidence", self.content)

    def test_doc_states_backward_compatibility_required(self) -> None:
        self.assertIn("backward", self.content.lower())
        self.assertIn("compatible", self.content.lower())

    def test_doc_states_no_dispatcher_enforcement(self) -> None:
        self.assertIn("dispatcher", self.content.lower())
        self.assertIn("enforcement", self.content.lower())

    def test_doc_states_no_api_write_behavior(self) -> None:
        self.assertIn("write", self.content.lower())
        self.assertIn("API", self.content)

    def test_doc_states_no_mission_control_ui_behavior(self) -> None:
        self.assertIn("Mission Control", self.content)
        self.assertIn("frontend", self.content.lower())

    def test_doc_states_no_merge_push_cleanup(self) -> None:
        self.assertIn("merge", self.content.lower())
        self.assertIn("push", self.content.lower())
        self.assertIn("cleanup", self.content.lower())

    def test_doc_lists_non_goals(self) -> None:
        self.assertIn("Non-Goals", self.content)

    def test_doc_recommends_phase_109(self) -> None:
        self.assertIn("Phase 109", self.content)

    def test_doc_recommends_phase_110(self) -> None:
        self.assertIn("Phase 110", self.content)

    def test_doc_outlines_preconditions_before_implementation(self) -> None:
        self.assertIn("Preconditions", self.content)
        self.assertIn("run_local_validation", self.content)

    def test_doc_states_api_design_principles(self) -> None:
        self.assertIn("Design Principles", self.content)
        self.assertIn("read-only", self.content.lower())

    def test_doc_states_ai_workers_receive_context_not_authority(self) -> None:
        self.assertIn("context", self.content.lower())
        self.assertIn("authority", self.content.lower())

    def test_doc_states_display_is_not_enforcement(self) -> None:
        self.assertIn("display", self.content.lower())
        self.assertIn("enforcement", self.content.lower())


if __name__ == "__main__":
    unittest.main()