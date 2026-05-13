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
        waiting_approval_section = self.doc.find("`waiting_approval`")
        accepted_section = self.doc.find("`accepted`")
        self.assertLess(waiting_approval_section, accepted_section)
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

    # Create Task and Dispatch UI (Phase 47)
    def test_create_task_and_dispatch_section_exists(self):
        self.assertIn("Create Task and Dispatch UI", self.doc)

    def test_executor_selector_documented(self):
        self.assertIn("Executor selector", self.doc)

    def test_validator_selector_documented(self):
        self.assertIn("Validator selector", self.doc)

    def test_start_dispatch_action_documented(self):
        self.assertIn("Start / Dispatch UI", self.doc)

    def test_backend_api_only_for_dispatch(self):
        self.assertIn("existing backend endpoint", self.doc)

    def test_does_not_directly_execute_pi(self):
        self.assertIn("Does not invoke Pi CLI directly", self.doc)
        self.assertIn("Does not spawn", self.doc)

    def test_no_push_in_dispatch(self):
        self.assertIn("No push", self.doc)

    def test_no_merge_in_dispatch(self):
        self.assertIn("No merge", self.doc)

    def test_no_cleanup_in_dispatch(self):
        self.assertIn("no cleanup", self.doc.lower())

    def test_no_delete_worktree_in_dispatch(self):
        self.assertIn("worktree deletion", self.doc.lower())

    def test_human_approval_final_gate_in_dispatch(self):
        self.assertIn("Human approval", self.doc)
        self.assertIn("final gate", self.doc.lower())

    def test_deterministic_validators_required(self):
        self.assertIn("deterministic validators remain required", self.doc.lower())

    def test_create_task_does_not_auto_approve(self):
        self.assertIn("Create Task Does Not Auto-Approve", self.doc)

    def test_task_detail_remains_approval_surface(self):
        self.assertIn("Task Detail Remains Approval Surface", self.doc)

    # API Health, Loading, and Evidence Preview UX (Phase 48)
    def test_api_health_section_exists(self):
        self.assertIn("API Health, Loading, and Evidence Preview UX", self.doc)

    def test_api_reachability_indicator_documented(self):
        self.assertIn("API status indicator", self.doc)
        self.assertIn("GET /health", self.doc)

    def test_loading_states_documented(self):
        self.assertIn("Loading Mission Control", self.doc)
        self.assertIn("loading.tsx", self.doc)

    def test_api_error_panel_documented(self):
        self.assertIn("ApiErrorPanel", self.doc)
        self.assertIn("API Error", self.doc)

    def test_validator_summary_card_documented(self):
        self.assertIn("ValidatorSummaryCard", self.doc)

    def test_executor_log_preview_documented(self):
        self.assertIn("ExecutorLogPanel", self.doc)
        self.assertIn("Load preview", self.doc)

    def test_artifact_previews_use_backend_api(self):
        self.assertIn("artifact preview endpoint", self.doc.lower())

    def test_never_reads_filesystem_directly(self):
        self.assertIn("does NOT read the filesystem directly", self.doc)

    def test_never_reruns_validators(self):
        self.assertIn("never reruns validators", self.doc.lower())

    def test_never_executes_pi_in_evidence(self):
        self.assertIn("No direct executor", self.doc)

    def test_no_push_in_evidence_ux(self):
        self.assertIn("No push", self.doc)

    def test_no_merge_in_evidence_ux(self):
        self.assertIn("No merge", self.doc)

    def test_no_cleanup_in_evidence_ux(self):
        self.assertIn("no cleanup", self.doc.lower())

    def test_no_delete_in_evidence_ux(self):
        self.assertIn("worktree deletion", self.doc.lower())

    # Artifact Review and Full Preview UX (Phase 49)
    def test_artifact_review_section_exists(self):
        self.assertIn("Artifact Review and Full Preview UX", self.doc)

    def test_artifact_classification_documented(self):
        self.assertIn("Artifact Classification", self.doc)
        self.assertIn("mission_contract", self.doc.lower())
        self.assertIn("executor_log", self.doc.lower())

    def test_inline_preview_documented(self):
        self.assertIn("Inline Preview", self.doc)
        self.assertIn("expand", self.doc.lower())

    def test_full_preview_modal_documented(self):
        self.assertIn("ArtifactPreviewModal", self.doc)
        self.assertIn("Modal", self.doc)

    def test_mission_contract_viewer_documented(self):
        self.assertIn("MissionContractViewer", self.doc)
        self.assertIn("forbidden actions", self.doc.lower())

    def test_pi_mission_plan_viewer_documented(self):
        self.assertIn("PiMissionPlanViewer", self.doc)
        self.assertIn("Protocol steps", self.doc)

    def test_policy_log_viewer_documented(self):
        self.assertIn("PolicyLogViewer", self.doc)
        self.assertIn("Policy check failed", self.doc)

    def test_backend_artifact_api_only(self):
        self.assertIn("artifact preview api", self.doc.lower())

    def test_no_push_in_artifact_review(self):
        self.assertIn("No push", self.doc)

    def test_no_merge_in_artifact_review(self):
        self.assertIn("No merge", self.doc)

    def test_no_cleanup_in_artifact_review(self):
        self.assertIn("no cleanup", self.doc.lower())

    def test_no_delete_in_artifact_review(self):
        self.assertIn("worktree deletion", self.doc.lower())

    def test_secret_warning_documented(self):
        self.assertIn("secret", self.doc.lower())

    def test_truncated_preview_documented(self):
        self.assertIn("truncation notice", self.doc.lower())

    # Human Approval Identity Enforcement (Phase 52)
    def test_human_approval_identity_enforcement_section_exists(self):
        self.assertIn("Human Approval Identity Enforcement", self.doc)

    def test_approval_requires_decided_by_human(self):
        self.assertIn('decided_by: "human"', self.doc)

    def test_worker_approval_rejected_in_docs(self):
        self.assertIn('"worker"` — rejected', self.doc)

    def test_pi_approval_rejected_in_docs(self):
        self.assertIn('"pi"` — rejected', self.doc)

    def test_agent_approval_rejected_in_docs(self):
        self.assertIn('"agent"` — rejected', self.doc)

    def test_system_approval_rejected_in_docs(self):
        self.assertIn('"system"` — rejected', self.doc)

    def test_worker_cannot_self_approve_in_docs(self):
        self.assertIn("Workers cannot self-approve", self.doc)

    def test_human_approval_remains_final_gate_in_docs(self):
        self.assertIn("Human approval remains the final gate", self.doc)

    def test_approval_does_not_push_in_docs(self):
        self.assertIn("does not push", self.doc)

    def test_approval_does_not_merge_in_docs(self):
        self.assertIn("does not push/merge/cleanup", self.doc)

    def test_approval_does_not_cleanup_in_docs(self):
        self.assertIn(", or clean up any branch or worktree", self.doc)

    def test_create_task_autofill_section_exists(self):
        self.assertIn("Create Task Auto-fill Behavior", self.doc)

    def test_autofill_worktree_path_documented(self):
        self.assertIn("worktree_path", self.doc)
        self.assertIn("auto-fill", self.doc.lower())
        self.assertIn("repo_path", self.doc)

    def test_autofill_artifact_dir_documented(self):
        self.assertIn("artifact_dir", self.doc)
        self.assertIn("auto-fill", self.doc.lower())
        self.assertIn("project", self.doc)

    def test_autofill_branch_documented(self):
        self.assertIn("branch", self.doc)
        self.assertIn("auto-fill", self.doc.lower())
        self.assertIn("task_key", self.doc)

    def test_autofill_task_key_formula_documented(self):
        self.assertIn('"task/" + task_key', self.doc)

    def test_autofill_user_override_preserved(self):
        self.assertIn("User overrides preserved", self.doc)
        self.assertIn("does not overwrite", self.doc)

    def test_autofill_refill_on_clear_documented(self):
        self.assertIn("Re-fill on clear", self.doc)
        self.assertIn("clears", self.doc.lower())

    def test_autofill_no_auto_submit(self):
        self.assertIn("No auto-submit", self.doc)
        self.assertIn("form submission", self.doc)

    def test_autofill_no_dispatch(self):
        self.assertIn("No dispatch", self.doc)
        self.assertIn("start/dispatch", self.doc)

    def test_autofill_no_approval(self):
        self.assertIn("No approval", self.doc)

    def test_autofill_no_push(self):
        self.assertIn("No push", self.doc)

    def test_autofill_no_merge(self):
        self.assertIn("No merge", self.doc)

    def test_autofill_no_cleanup(self):
        self.assertIn("No cleanup", self.doc)

    def test_create_task_executor_default_section_exists(self):
        self.assertIn("Create Task Executor Default", self.doc)

    def test_create_task_defaults_executor_to_pi(self):
        self.assertIn("defaults the executor to `pi`", self.doc)

    def test_create_task_executor_default_frontend_only(self):
        self.assertIn("frontend form default only", self.doc)

    def test_create_task_executor_default_backend_still_supports_opencode(self):
        self.assertIn("backend executor registry still supports opencode", self.doc)

    def test_create_task_executor_default_backend_still_supports_shell(self):
        self.assertIn("backend executor registry still supports opencode, pi, shell, and manual", self.doc)

    def test_create_task_executor_default_backend_still_supports_manual(self):
        self.assertIn("opencode, pi, shell, and manual", self.doc)

    def test_create_task_executor_default_operators_can_choose_opencode(self):
        self.assertIn("Operators can still select opencode, shell, or manual", self.doc)

    def test_create_task_executor_default_ui_does_not_run_pi(self):
        self.assertIn("UI does not directly run Pi", self.doc)

    def test_create_task_executor_default_backend_start_api(self):
        self.assertIn("backend start API", self.doc)

    def test_create_task_executor_default_no_dispatcher_change(self):
        self.assertIn("dispatcher state machine", self.doc)
        self.assertIn("does not change", self.doc)

    def test_create_task_executor_default_no_db_schema_change(self):
        self.assertIn("DB schema", self.doc)
        self.assertIn("does not change", self.doc)

    def test_create_task_executor_default_no_approval_change(self):
        self.assertIn("approval semantics", self.doc)
        self.assertIn("does not change", self.doc)

    def test_create_task_executor_default_no_default_validators_change(self):
        self.assertIn("DEFAULT_VALIDATORS", self.doc)


if __name__ == "__main__":
    unittest.main()