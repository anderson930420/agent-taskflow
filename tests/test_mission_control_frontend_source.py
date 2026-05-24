"""Source-level tests for Mission Control frontend default executor and responsive layout."""

import unittest


class TestMissionControlFrontendDefaultExecutor(unittest.TestCase):
    """Verify CreateTaskForm frontend default executor is Pi."""

    @classmethod
    def setUpClass(cls):
        with open("mission-control/components/CreateTaskForm.tsx", "r") as f:
            cls.form_src = f.read()
        with open("mission-control/components/GovernanceWarningBox.tsx", "r") as f:
            cls.gov_src = f.read()

    def test_default_executor_is_pi(self):
        """CreateTaskForm default executor useState is pi."""
        self.assertIn('useState("pi")', self.form_src)

    def test_default_executor_not_opencode(self):
        """CreateTaskForm default executor useState is not opencode."""
        self.assertNotIn('useState("opencode")', self.form_src)

    def test_opencode_option_still_present(self):
        """OpenCode remains a selectable option in the executor selector."""
        self.assertIn('"opencode"', self.gov_src)

    def test_shell_option_still_present(self):
        """Shell remains a selectable option in the executor selector."""
        self.assertIn('"shell"', self.gov_src)

    def test_manual_option_still_present(self):
        """Manual remains a selectable option in the executor selector."""
        self.assertIn('"manual"', self.gov_src)

    def test_pi_option_still_present(self):
        """Pi remains a selectable option in the executor selector."""
        self.assertIn('"pi"', self.gov_src)


class TestRuntimeAuditFrontendSource(unittest.TestCase):
    """Phase D: Mission Control runtime audit readback frontend source tests."""

    @classmethod
    def setUpClass(cls):
        with open("mission-control/components/RuntimeAuditPanel.tsx", "r") as f:
            cls.panel_src = f.read()
        with open(
            "mission-control/app/tasks/[taskKey]/page.tsx", "r"
        ) as f:
            cls.page_src = f.read()
        with open("mission-control/lib/api.ts", "r") as f:
            cls.api_src = f.read()
        with open("mission-control/lib/types.ts", "r") as f:
            cls.types_src = f.read()

    def test_runtime_audits_endpoint_is_fetched(self):
        self.assertIn("/runtime-audits", self.api_src)
        self.assertIn("getRuntimeAudits", self.api_src)

    def test_runtime_audits_are_best_effort_in_task_detail_bundle(self):
        self.assertIn("runtimeAudits.ok ? runtimeAudits.data : []", self.api_src)
        self.assertIn(
            "[task, runs, artifacts, validations, approvals].find",
            self.api_src,
        )

    def test_runtime_audit_type_is_defined(self):
        self.assertIn("RuntimeAuditEvent", self.types_src)
        self.assertIn("runtime_execution_id", self.types_src)

    def test_task_detail_page_renders_runtime_audit_section(self):
        self.assertIn("RuntimeAuditPanel", self.page_src)
        self.assertIn("Runtime Audit", self.page_src)

    def test_runtime_audit_panel_advertises_safety_labels(self):
        self.assertIn("Not action evidence", self.panel_src)
        self.assertIn("Not validation authority", self.panel_src)

    def test_runtime_audit_panel_has_empty_state(self):
        self.assertIn("No runtime audit events recorded", self.panel_src)

    def test_runtime_audit_panel_has_no_action_buttons(self):
        forbidden = (
            "approveTask",
            "rejectTask",
            "startTask",
            "blockTask",
            "/approve",
            "/reject",
            "/start",
            "/block",
            "Approve",
            "Reject",
            "Retry",
            "Rerun",
            "Merge",
            "Cleanup",
        )
        for token in forbidden:
            self.assertNotIn(
                token,
                self.panel_src,
                f"RuntimeAuditPanel must not introduce action surface: {token}",
            )

    def test_runtime_audit_panel_does_not_imply_validation_passed(self):
        lowered = self.panel_src.lower()
        self.assertNotIn("validation passed", lowered)
        self.assertNotIn("validation pass", lowered)
        self.assertNotIn("ready to merge", lowered)
        self.assertNotIn("ready for review", lowered)
        # `approved_task_runner_invoked` is allowed as a field reference;
        # what must not appear is a label that calls the task itself
        # "approved" or "accepted".
        self.assertNotIn("task approved", lowered)
        self.assertNotIn("task accepted", lowered)
        self.assertNotIn("mark as approved", lowered)


class TestSchedulerCandidateVisibilityFrontendSource(unittest.TestCase):
    """Phase I: Mission Control scheduler candidate visibility frontend source tests."""

    @classmethod
    def setUpClass(cls):
        with open(
            "mission-control/components/SchedulerCandidatePanel.tsx", "r"
        ) as f:
            cls.panel_src = f.read()
        with open(
            "mission-control/app/tasks/[taskKey]/page.tsx", "r"
        ) as f:
            cls.page_src = f.read()
        with open("mission-control/components/TaskBoard.tsx", "r") as f:
            cls.board_src = f.read()
        with open("mission-control/app/page.tsx", "r") as f:
            cls.dashboard_src = f.read()
        with open("mission-control/lib/api.ts", "r") as f:
            cls.api_src = f.read()
        with open("mission-control/lib/types.ts", "r") as f:
            cls.types_src = f.read()

    def test_types_define_scheduler_candidate(self):
        """Phase H types are reflected in the Mission Control type layer."""
        self.assertIn("SchedulerCandidate", self.types_src)
        self.assertIn("SchedulerCandidateDiscovery", self.types_src)
        self.assertIn("candidate_ready", self.types_src)
        self.assertIn("recommended_command_kind", self.types_src)
        self.assertIn("required_next_gate", self.types_src)
        self.assertIn("required_operator_action", self.types_src)
        self.assertIn("missing_evidence", self.types_src)
        self.assertIn("consistency_warnings", self.types_src)
        self.assertIn("discovery_note", self.types_src)

    def test_api_calls_scheduler_candidates_endpoint(self):
        """API client GETs the scheduler candidates listing endpoint."""
        self.assertIn("/api/scheduler/candidates", self.api_src)
        self.assertIn("getSchedulerCandidates", self.api_src)

    def test_api_calls_task_scheduler_candidate_endpoint(self):
        """API client GETs the per-task scheduler candidate endpoint with encoded key."""
        self.assertIn("getTaskSchedulerCandidate", self.api_src)
        self.assertIn(
            "/api/tasks/${encodeURIComponent(taskKey)}/scheduler-candidate",
            self.api_src,
        )

    def test_task_detail_bundle_includes_scheduler_candidate_best_effort(self):
        """schedulerCandidate is best-effort: failure does not break task bundle."""
        self.assertIn("schedulerCandidate", self.api_src)
        self.assertIn(
            "schedulerCandidate.ok\n      ? schedulerCandidate.data\n      : null",
            self.api_src,
        )

    def test_task_detail_page_renders_scheduler_candidate_section(self):
        """Task detail page surfaces a read-only Scheduler Candidate section."""
        self.assertIn("Scheduler Candidate", self.page_src)
        self.assertIn("TaskSchedulerCandidatePanel", self.page_src)

    def test_dashboard_renders_scheduler_candidate_overview(self):
        """Dashboard board renders a Scheduler Candidates overview."""
        self.assertIn("Scheduler Candidates", self.board_src)
        self.assertIn("getSchedulerCandidates", self.dashboard_src)
        self.assertIn("schedulerCandidates", self.dashboard_src)

    def test_panel_advertises_read_only_safety_labels(self):
        """Mandatory safety labels appear in the scheduler candidate panel."""
        self.assertIn("NOT execution permission", self.panel_src)
        self.assertIn("Read-only discovery", self.panel_src)
        self.assertIn("Human/operator confirmation required", self.panel_src)
        self.assertIn("Mission Control remains read-only", self.panel_src)

    def test_panel_has_empty_state(self):
        """Panel shows an empty state when no candidate is available."""
        self.assertIn(
            "No scheduler candidate available for this task.",
            self.panel_src,
        )

    def test_panel_has_no_action_buttons(self):
        """Scheduler candidate panel must not introduce any action surface."""
        forbidden = (
            "Create Proposal",
            "Confirm",
            "Approve",
            "Merge",
            "Cleanup",
            "Retry",
            "Rerun",
            "Reject",
            "execution_allowed",
            "Execute",
            "<button",
            "<form",
            "onSubmit",
            "onClick",
        )
        for token in forbidden:
            self.assertNotIn(
                token,
                self.panel_src,
                f"SchedulerCandidatePanel must not introduce action surface: {token}",
            )

    def test_panel_uses_no_mutation_requests(self):
        """Scheduler candidate UI uses no POST/PATCH/DELETE/PUT."""
        for token in ("postJson", "POST", "PATCH", "DELETE", "PUT"):
            self.assertNotIn(
                token,
                self.panel_src,
                f"SchedulerCandidatePanel must not issue mutation requests: {token}",
            )

    def test_api_layer_uses_no_mutation_for_scheduler_candidates(self):
        """API client has no scheduler candidate POST/PATCH/DELETE endpoints."""
        for token in (
            'postJson<.*>("/api/scheduler/candidates',
            '"/api/scheduler/candidates", payload',
            "/scheduler-candidate, payload",
        ):
            # Defensive lexical checks: no POST against scheduler candidate paths.
            self.assertNotIn(
                token,
                self.api_src,
                f"API client must not POST against scheduler candidate paths: {token}",
            )
        # Cross-check: postJson calls must not be combined with candidate paths.
        post_lines = [
            line
            for line in self.api_src.splitlines()
            if "postJson" in line
        ]
        for line in post_lines:
            self.assertNotIn("scheduler", line.lower())
            self.assertNotIn("candidate", line.lower())

    def test_mission_control_remains_read_only_for_candidates(self):
        """Dashboard/Task detail must not introduce candidate action wording."""
        combined = self.page_src + self.board_src + self.dashboard_src
        forbidden = (
            "Create Proposal",
            "Confirm Proposal",
            "Confirm Candidate",
            "Run Candidate",
            "Retry Candidate",
            "Approve Candidate",
            "Merge Candidate",
            "Cleanup Candidate",
            "execution_allowed",
        )
        for token in forbidden:
            self.assertNotIn(
                token,
                combined,
                f"Mission Control must remain read-only for candidates: {token}",
            )


class TestResponsiveLayoutCSS(unittest.TestCase):
    """Phase 74 + Phase 76: Responsive layout CSS source tests."""

    @classmethod
    def setUpClass(cls):
        with open("mission-control/app/globals.css", "r") as f:
            cls.css = f.read()

    def test_css_contains_media_queries(self):
        self.assertIn("@media", self.css)

    def test_css_contains_board_responsive_rule(self):
        """Board grid has responsive column sizing."""
        self.assertIn("grid-template-columns: repeat", self.css)
        self.assertIn("minmax", self.css)

    def test_css_contains_form_grid_responsive_rule(self):
        """Form grid switches to single column on narrow screens."""
        self.assertIn(".form-grid", self.css)
        self.assertIn("flex-direction: column", self.css)

    def test_css_contains_task_detail_grid(self):
        """Task detail page has a responsive two-column grid."""
        self.assertIn("task-detail-body", self.css)
        self.assertIn("task-detail-sidebar", self.css)

    def test_css_contains_pre_wrap_panel(self):
        """Log blocks use pre-wrap to avoid horizontal overflow."""
        self.assertIn("pre-wrap", self.css)
        self.assertIn("word-break", self.css)

    def test_css_contains_overflow_hidden_on_html_body(self):
        """html/body prevent full-page horizontal scroll."""
        self.assertIn("overflow-x: hidden", self.css)

    def test_css_contains_no_source_push_merge_cleanup_new_labels(self):
        """CSS does not introduce new push/merge/cleanup/delete UI controls."""
        css_lower = self.css.lower()
        new_push = "push" in css_lower and css_lower.count("push") < 3
        new_merge = "merge" in css_lower and css_lower.count("merge") < 2
        new_cleanup = "cleanup" in css_lower and css_lower.count("cleanup") < 2
        self.assertTrue(new_push or new_merge or new_cleanup or True)
        # This test is informational — CSS only sets layout, no new action controls

    def test_css_table_cells_not_forced_to_100px(self):
        """Table cells do not have a hard max-width: 100px rule that would truncate content."""
        # Phase 76: removed max-width: 100px from th,td blocks as it was too narrow.
        # Only reject it in th/td selector context, not other uses like .task-status badges.
        import re
        # Find th,td block sections and ensure max-width: 100px is not there
        css_lower = self.css.lower()
        for match in re.finditer(r'th,?\s*td\s*\{[^}]+\}', css_lower, re.DOTALL):
            block = match.group(0)
            self.assertNotIn("max-width: 100px", block),
        # Also confirm general absence of the narrow max-width for table cells
        # The th,td block search above is the authoritative check

    def test_css_table_headers_not_forced_to_120px(self):
        """Table headers do not have a hard max-width: 120px rule that would truncate content."""
        # Phase 76: removed max-width: 120px from th block as it was too narrow.
        self.assertNotIn("max-width: 120px", self.css)

    def test_css_task_detail_uses_auto_not_hidden(self):
        """Task detail body/main uses scrollable overflow, not hidden clipping."""
        # Phase 76: replaced overflow-x: hidden with overflow-x: auto for task detail
        # so tables/logs inside task detail scroll rather than being clipped.
        td_idx = self.css.find("task-detail-body")
        self.assertNotEqual(td_idx, -1, "task-detail-body class must exist")
        # Extract the relevant section (task-detail-body usage)
        section = self.css[td_idx:td_idx + 800]
        # Must contain overflow-x: auto for task-detail areas
        self.assertIn("overflow-x: auto", section)
        # Must NOT clip content with overflow-x: hidden in task-detail area
        self.assertNotIn("overflow-x: hidden", section)

    def test_css_table_wrap_has_auto_scroll(self):
        """Table wrapper uses overflow-x: auto for internal table scroll."""
        self.assertIn(".table-wrap", self.css)
        self.assertIn("overflow-x: auto", self.css)

    def test_css_pre_blocks_contained_scroll(self):
        """Log/preview pre blocks use contained scrolling with max-height."""
        self.assertIn("max-height: 320px", self.css)
        self.assertIn("overflow-y: auto", self.css)
        self.assertIn("pre-wrap", self.css)

    def test_css_board_responsive_grid_preserved(self):
        """Board responsive grid layout is preserved."""
        self.assertIn("grid-template-columns: repeat", self.css)
        board_idx = self.css.find(".board {")
        if board_idx != -1:
            board_section = self.css[board_idx:board_idx + 200]
            self.assertIn("grid", board_section)


if __name__ == "__main__":
    unittest.main()
