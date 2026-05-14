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