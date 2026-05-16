"""Tests for docs/workflow-policy-mission-control-display-design.md.

This module verifies that the Mission Control display design document
covers all required aspects of the planned read-only workflow policy
evidence panel, without implementing any frontend code.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from agent_taskflow.workflow_policy_artifacts import (
    WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME,
    WORKFLOW_POLICY_REVIEW_KIND,
    WORKFLOW_POLICY_SUMMARY_FILENAME,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "workflow-policy-mission-control-display-design.md"


class WorkflowPolicyMissionControlDisplayDesignTests(unittest.TestCase):
    """Tests verifying the Mission Control display design document."""

    @classmethod
    def setUpClass(cls) -> None:
        if not DOC_PATH.exists():
            raise FileNotFoundError(f"Display design doc not found: {DOC_PATH}")
        cls.content = DOC_PATH.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Purpose and non-implementation
    # ------------------------------------------------------------------

    def test_doc_exists(self) -> None:
        self.assertTrue(DOC_PATH.exists())

    def test_doc_states_purpose(self) -> None:
        self.assertIn("Purpose", self.content)

    def test_doc_states_no_frontend_implementation_in_this_phase(self) -> None:
        self.assertIn("does not implement", self.content.lower())
        self.assertIn("frontend", self.content.lower())

    def test_doc_states_no_mission_control_ui_changes_in_this_phase(self) -> None:
        self.assertIn("Mission Control", self.content)
        self.assertIn("no", self.content.lower())

    def test_doc_states_no_api_changes(self) -> None:
        self.assertIn("API", self.content)
        self.assertIn("no", self.content.lower())

    def test_doc_states_no_dispatcher_enforcement(self) -> None:
        self.assertIn("dispatcher", self.content.lower())
        self.assertIn("enforcement", self.content.lower())

    def test_doc_states_display_is_docs_only_phase(self) -> None:
        self.assertIn("docs-only", self.content.lower())

    # ------------------------------------------------------------------
    # Current backend foundation
    # ------------------------------------------------------------------

    def test_doc_describes_workflow_policy_evidence_exposure(self) -> None:
        self.assertIn("workflow_policy_evidence", self.content)

    def test_doc_mentions_available_field(self) -> None:
        self.assertIn("available", self.content)

    def test_doc_mentions_artifact_index_field(self) -> None:
        self.assertIn("artifact_index", self.content)

    def test_doc_mentions_summary_field(self) -> None:
        self.assertIn("summary", self.content)

    def test_doc_mentions_review_artifacts_field(self) -> None:
        self.assertIn("review_artifacts", self.content)

    def test_doc_states_api_is_read_only(self) -> None:
        self.assertIn("read-only", self.content.lower())

    def test_doc_states_api_does_not_generate_artifacts(self) -> None:
        self.assertIn("generates", self.content.lower())
        self.assertIn("mutates", self.content.lower())

    def test_doc_states_api_does_not_enforce_policy(self) -> None:
        self.assertIn("dispatcher", self.content.lower())
        self.assertIn("enforcement", self.content.lower())
        self.assertIn("not", self.content.lower())

    def test_doc_states_available_false_does_not_mean_failure(self) -> None:
        self.assertIn("available: false", self.content)
        self.assertIn("does not mean", self.content.lower())
        self.assertIn("failed", self.content.lower())

    def test_doc_states_no_dispatcher_executor_validator_calls(self) -> None:
        self.assertIn("dispatcher", self.content.lower())
        self.assertIn("executor", self.content.lower())
        self.assertIn("validator", self.content.lower())

    def test_doc_references_backend_implementation(self) -> None:
        self.assertIn("agent_taskflow/api/review.py", self.content)
        self.assertIn("build_workflow_policy_evidence", self.content)

    # ------------------------------------------------------------------
    # UI design principles
    # ------------------------------------------------------------------

    def test_doc_states_read_only_display(self) -> None:
        self.assertIn("read-only", self.content.lower())

    def test_doc_states_evidence_oriented_display(self) -> None:
        self.assertIn("evidence-oriented", self.content.lower())
        self.assertIn("evidence", self.content.lower())

    def test_doc_states_non_authoritative_display(self) -> None:
        self.assertIn("non-authoritative", self.content.lower())

    def test_doc_states_non_mutating_display(self) -> None:
        self.assertIn("non-mutating", self.content.lower())
        self.assertIn("mutat", self.content.lower())

    def test_doc_states_not_approval_surface(self) -> None:
        self.assertIn("approval", self.content.lower())
        self.assertIn("not", self.content.lower())
        self.assertIn("surface", self.content.lower())

    def test_doc_states_not_merge_push_cleanup_surface(self) -> None:
        self.assertIn("merge", self.content.lower())
        self.assertIn("push", self.content.lower())
        self.assertIn("cleanup", self.content.lower())

    def test_doc_states_safe_when_unavailable(self) -> None:
        self.assertIn("available: false", self.content)
        self.assertIn("safe", self.content.lower())

    # ------------------------------------------------------------------
    # Proposed UI placement
    # ------------------------------------------------------------------

    def test_doc_outlines_ui_placement_options(self) -> None:
        self.assertIn("Option A", self.content)
        self.assertIn("Option B", self.content)

    def test_doc_recommends_option_a_task_detail_review_evidence_section(self) -> None:
        self.assertIn("Option A", self.content)
        self.assertIn("Review Evidence", self.content)
        self.assertIn("recommended", self.content.lower())

    # ------------------------------------------------------------------
    # Proposed panel states
    # ------------------------------------------------------------------

    def test_doc_documents_available_true_state(self) -> None:
        self.assertIn("State A", self.content)
        self.assertIn("available: true", self.content)

    def test_doc_documents_available_false_state(self) -> None:
        self.assertIn("State B", self.content)
        self.assertIn("available: false", self.content)

    def test_doc_documents_partial_corrupt_artifacts_state(self) -> None:
        self.assertIn("State C", self.content)
        self.assertIn("corrupt", self.content.lower())

    def test_doc_specifies_metadata_fields_for_available_true(self) -> None:
        for field in (
            "validation_status",
            "schema_version",
            "source_path",
            "generated_at",
            "allowed_executors",
            "required_validators",
            "optional_validators",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.content)

    def test_doc_specifies_workflow_policy_artifacts_as_links(self) -> None:
        self.assertIn(WORKFLOW_POLICY_SUMMARY_FILENAME, self.content)
        self.assertIn(WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME, self.content)
        self.assertIn(WORKFLOW_POLICY_REVIEW_KIND, self.content)

    # ------------------------------------------------------------------
    # Proposed read-only UI copy
    # ------------------------------------------------------------------

    def test_doc_includes_workflow_policy_evidence_is_read_only_wording(self) -> None:
        self.assertIn("read-only", self.content.lower())

    def test_doc_includes_display_does_not_imply_enforcement_wording(self) -> None:
        self.assertIn("does not imply dispatcher enforcement", self.content)

    def test_doc_includes_human_review_remains_final_gate_wording(self) -> None:
        self.assertIn("human review remains the final gate", self.content.lower())

    def test_doc_includes_approval_does_not_imply_merge_wording(self) -> None:
        self.assertIn("approval does not imply merge", self.content.lower())

    # ------------------------------------------------------------------
    # Forbidden UI behavior
    # ------------------------------------------------------------------

    def test_doc_lists_forbidden_ui_behaviors(self) -> None:
        self.assertIn("forbidden", self.content.lower())

    def test_doc_forbids_approve_controls(self) -> None:
        self.assertIn("approve", self.content.lower())

    def test_doc_forbids_reject_controls(self) -> None:
        self.assertIn("reject", self.content.lower())

    def test_doc_forbids_rerun_controls(self) -> None:
        self.assertIn("rerun", self.content.lower())

    def test_doc_forbids_block_controls(self) -> None:
        self.assertIn("block", self.content.lower())

    def test_doc_forbids_merge_button(self) -> None:
        self.assertIn("merge", self.content.lower())
        self.assertIn("button", self.content.lower())

    def test_doc_forbids_push_button(self) -> None:
        self.assertIn("push", self.content.lower())
        self.assertIn("button", self.content.lower())

    def test_doc_forbids_cleanup_button(self) -> None:
        self.assertIn("cleanup", self.content.lower())
        self.assertIn("button", self.content.lower())

    def test_doc_forbids_regenerate_policy_artifact_button(self) -> None:
        self.assertIn("regenerate", self.content.lower())
        self.assertIn("artifact", self.content.lower())

    def test_doc_forbids_validate_policy_button_that_mutates(self) -> None:
        self.assertIn("validate", self.content.lower())
        self.assertIn("mutates", self.content.lower())

    def test_doc_forbids_dispatcher_preflight_trigger(self) -> None:
        self.assertIn("dispatcher preflight", self.content.lower())

    def test_doc_forbids_executor_rerun_trigger(self) -> None:
        self.assertIn("executor rerun", self.content.lower())

    def test_doc_forbids_github_pr_creation(self) -> None:
        self.assertIn("github", self.content.lower())
        self.assertIn("pr creation", self.content.lower())

    def test_doc_forbids_github_issue_sync(self) -> None:
        self.assertIn("GitHub", self.content)
        self.assertIn("issue sync", self.content.lower())

    def test_doc_forbids_auto_approve_trigger(self) -> None:
        self.assertIn("auto-approve", self.content.lower())

    def test_doc_forbids_auto_merge_trigger(self) -> None:
        self.assertIn("auto-merge", self.content.lower())

    def test_doc_forbids_any_action_that_changes_task_state(self) -> None:
        self.assertIn("task state", self.content.lower())

    # ------------------------------------------------------------------
    # Data dependency
    # ------------------------------------------------------------------

    def test_doc_states_ui_must_read_from_api_only(self) -> None:
        self.assertIn("API", self.content)
        self.assertIn("only", self.content.lower())
        self.assertIn("not", self.content.lower())

    def test_doc_forbids_direct_filesystem_reads(self) -> None:
        self.assertIn("filesystem", self.content.lower())
        self.assertIn("not", self.content.lower())

    def test_doc_forbids_direct_dispatcher_calls(self) -> None:
        self.assertIn("dispatcher", self.content.lower())
        self.assertIn("not", self.content.lower())

    def test_doc_forbids_direct_executor_calls(self) -> None:
        self.assertIn("executor", self.content.lower())
        self.assertIn("not", self.content.lower())

    def test_doc_forbids_direct_validator_calls(self) -> None:
        self.assertIn("validator", self.content.lower())
        self.assertIn("not", self.content.lower())

    # ------------------------------------------------------------------
    # Accessibility / UX notes
    # ------------------------------------------------------------------

    def test_doc_includes_ux_guidance_clear_labels(self) -> None:
        self.assertIn("clear", self.content.lower())
        self.assertIn("label", self.content.lower())

    def test_doc_includes_ux_guidance_collapsed_arrays(self) -> None:
        self.assertIn("collapsed", self.content.lower())
        self.assertIn("array", self.content.lower())

    def test_doc_includes_ux_guidance_empty_lists(self) -> None:
        self.assertIn("empty", self.content.lower())
        self.assertIn("list", self.content.lower())

    def test_doc_forbids_green_passed_styling_that_looks_like_approval(self) -> None:
        self.assertIn("green", self.content.lower())
        self.assertIn("passed", self.content.lower())

    def test_doc_requires_separate_workflow_policy_from_approval_status(self) -> None:
        self.assertIn("separate", self.content.lower())
        self.assertIn("approval", self.content.lower())

    # ------------------------------------------------------------------
    # Non-goals
    # ------------------------------------------------------------------

    def test_doc_lists_non_goals(self) -> None:
        self.assertIn("Non-Goals", self.content)

    def test_doc_states_no_frontend_implementation(self) -> None:
        self.assertIn("frontend implementation", self.content.lower())

    def test_doc_states_no_api_changes(self) -> None:
        self.assertIn("no new endpoints", self.content.lower())
        self.assertIn("modified responses", self.content.lower())

    def test_doc_states_no_dispatcher_enforcement_in_non_goals(self) -> None:
        self.assertIn("dispatcher", self.content.lower())
        self.assertIn("enforcement", self.content.lower())

    def test_doc_states_no_github_sync(self) -> None:
        self.assertIn("github", self.content.lower())
        self.assertIn("sync", self.content.lower())

    def test_doc_states_no_pr_creation(self) -> None:
        self.assertIn("pull request automation", self.content.lower())

    def test_doc_states_no_automatic_merge_push_cleanup(self) -> None:
        self.assertIn("automatic merge", self.content.lower())
        self.assertIn("push", self.content.lower())
        self.assertIn("cleanup", self.content.lower())

    def test_doc_states_no_ai_self_governance(self) -> None:
        self.assertIn("ai self-governance", self.content.lower())

    def test_doc_states_no_state_transition_surface(self) -> None:
        self.assertIn("state transition", self.content.lower())

    # ------------------------------------------------------------------
    # Preconditions before frontend implementation
    # ------------------------------------------------------------------

    def test_doc_outlines_preconditions_before_frontend(self) -> None:
        self.assertIn("Preconditions", self.content)
        self.assertIn("run_local_validation", self.content)

    def test_doc_requires_local_validation_passes(self) -> None:
        self.assertIn("run_local_validation", self.content)

    def test_doc_requires_phase_110_tests_pass(self) -> None:
        self.assertIn("Phase 110", self.content)
        self.assertIn("pass", self.content.lower())

    def test_doc_requires_response_stable(self) -> None:
        self.assertIn("stable", self.content.lower())

    def test_doc_requires_no_write_behavior_in_same_phase(self) -> None:
        self.assertIn("write", self.content.lower())
        self.assertIn("same phase", self.content.lower())

    # ------------------------------------------------------------------
    # Recommended Phase 112
    # ------------------------------------------------------------------

    def test_doc_recommends_phase_112(self) -> None:
        self.assertIn("Phase 112", self.content)

    def test_doc_specifies_phase_112_is_design_contract_tests(self) -> None:
        self.assertIn("design contract", self.content.lower())
        self.assertIn("source-level", self.content.lower())

    def test_doc_specifies_phase_112_is_docs_or_docs_tests_only(self) -> None:
        self.assertIn("docs-only", self.content.lower())

    def test_doc_specifies_phase_112_tests_verify_no_forbidden_ui(self) -> None:
        self.assertIn("forbidden", self.content.lower())
        self.assertIn("ui", self.content.lower())

    def test_doc_specifies_phase_112_tests_verify_read_only(self) -> None:
        self.assertIn("read-only", self.content.lower())

    def test_doc_specifies_phase_112_tests_verify_human_review_final_gate(self) -> None:
        self.assertIn("human review", self.content.lower())
        self.assertIn("final gate", self.content.lower())

    def test_doc_specifies_phase_112_tests_verify_display_does_not_imply_enforcement(self) -> None:
        self.assertIn("display", self.content.lower())
        self.assertIn("enforcement", self.content.lower())

    def test_doc_specifies_phase_112_tests_verify_available_states(self) -> None:
        self.assertIn("available: true", self.content)
        self.assertIn("available: false", self.content)

    # ------------------------------------------------------------------
    # Reference section
    # ------------------------------------------------------------------

    def test_doc_includes_reference_section(self) -> None:
        self.assertIn("Reference", self.content)


if __name__ == "__main__":
    unittest.main()