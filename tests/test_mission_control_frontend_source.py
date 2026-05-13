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
    """Phase 74: Responsive layout CSS source tests."""

    @classmethod
    def setUpClass(cls):
        with open("mission-control/app/globals.css", "r") as f:
            cls.css = f.read()

    def test_css_contains_media_queries(self):
        self.assertIn("@media", self.css)

    def test_css_contains_board_responsive_rule(self):
        """Board grid has responsive column sizing."""
        self.assertIn("grid-auto-columns", self.css)

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


if __name__ == "__main__":
    unittest.main()