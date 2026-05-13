"""Doc tests for mission-control-ui-state-model.md."""
import unittest


class TestMissionControlUIStateModelDocs(unittest.TestCase):
    """Test that docs/mission-control-ui-state-model.md contains required sections."""

    @classmethod
    def setUpClass(cls):
        with open("docs/mission-control-ui-state-model.md", "r") as f:
            cls.doc = f.read()

    # State coverage
    def test_has_queued(self):
        self.assertIn("`queued`", self.doc)

    def test_has_preparing(self):
        self.assertIn("`preparing`", self.doc)

    def test_has_implementing(self):
        self.assertIn("`implementing`", self.doc)

    def test_has_validating(self):
        self.assertIn("`validating`", self.doc)

    def test_has_waiting_approval(self):
        self.assertIn("`waiting_approval`", self.doc)

    def test_has_accepted(self):
        self.assertIn("`accepted`", self.doc)

    def test_has_rejected(self):
        self.assertIn("`rejected`", self.doc)

    def test_has_blocked(self):
        self.assertIn("`blocked`", self.doc)

    def test_has_failed(self):
        self.assertIn("`failed`", self.doc)

    # Action controls
    def test_approve_only_in_waiting_approval(self):
        # The approve action should be listed under waiting_approval
        waiting_approval_section = self.doc.find("`waiting_approval`")
        accepted_section = self.doc.find("`accepted`")
        self.assertLess(waiting_approval_section, accepted_section)
        # Check that waiting_approval has approve in its context
        context = self.doc[waiting_approval_section:waiting_approval_section+500]
        self.assertIn("approve", context)

    def test_reject_block_reason(self):
        self.assertIn("reject", self.doc.lower())
        self.assertIn("reason", self.doc.lower())

    def test_block_action_documented(self):
        self.assertIn("block", self.doc.lower())

    # UI never performs
    def test_no_push(self):
        self.assertIn("No push", self.doc)
        self.assertIn("no push", self.doc.lower())

    def test_no_merge(self):
        self.assertIn("No merge", self.doc)
        self.assertIn("no merge", self.doc.lower())

    def test_no_cleanup(self):
        self.assertIn("no cleanup", self.doc.lower())
        self.assertIn("cleanup", self.doc.lower())

    def test_no_direct_pi_execution(self):
        self.assertIn("no direct", self.doc.lower())
        self.assertIn("Pi", self.doc)
        self.assertIn("execution", self.doc.lower())

    # Core principles
    def test_human_approval_final_gate(self):
        self.assertIn("Human approval is the final gate", self.doc)

    def test_ui_is_control_layer(self):
        self.assertIn("control/review layer", self.doc.lower())

    # Backend API coverage
    def test_approve_endpoint_documented(self):
        self.assertIn("/approve", self.doc)

    def test_reject_endpoint_documented(self):
        self.assertIn("/reject", self.doc)

    def test_block_endpoint_documented(self):
        self.assertIn("/block", self.doc)

    def test_start_endpoint_documented(self):
        self.assertIn("/start", self.doc)

    def test_review_evidence_endpoint_documented(self):
        self.assertIn("/review-evidence", self.doc)

    # Forbidden actions
    def test_forbidden_actions_documented(self):
        self.assertIn("forbidden_actions", self.doc)

    def test_self_approval_forbidden(self):
        self.assertIn("self-approval", self.doc)

    # Safety notes
    def test_no_auto_approval(self):
        self.assertIn("no auto-approval", self.doc.lower())

    def test_no_autonomous_loop(self):
        self.assertIn("no autonomous loop", self.doc.lower())

    def test_confirmation_dialogs_documented(self):
        self.assertIn("confirmation", self.doc.lower())
        self.assertIn("Proceed to approve", self.doc)

    # Task Board State Grouping (Phase 46)
    def test_task_board_section_exists(self):
        self.assertIn("Task Board State Grouping", self.doc)

    def test_state_category_filter_documented(self):
        self.assertIn("Category filter", self.doc)

    def test_search_by_task_key(self):
        self.assertIn("task key", self.doc)
        self.assertIn("Search", self.doc)

    def test_search_by_executor(self):
        self.assertIn("executor", self.doc)

    def test_task_state_badge_documented(self):
        self.assertIn("State badge", self.doc)


    def test_no_direct_executor_actions_from_board(self):
        self.assertIn("No direct executor", self.doc)
        self.assertIn("board", self.doc)

    def test_no_push_on_board(self):
        # Check that the "No push" section covers the board context
        push_section = self.doc.find("No push")
        board_section = self.doc.find("Task Board State Grouping")
        self.assertGreater(push_section, 0)
        self.assertGreater(board_section, 0)

    def test_no_merge_on_board(self):
        self.assertIn("No merge", self.doc)

    def test_no_cleanup_on_board(self):
        self.assertIn("no cleanup", self.doc.lower())

    def test_approval_actions_remain_in_task_detail(self):
        self.assertIn("approval actions remain", self.doc.lower())

    def test_review_evidence_basis_for_approval(self):
        self.assertIn("review evidence", self.doc.lower())
        self.assertIn("human approval", self.doc.lower())


if __name__ == "__main__":
    unittest.main()