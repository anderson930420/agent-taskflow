"""Source-level tests for Mission Control frontend default executor."""

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
        # Options are defined in GovernanceWarningBox, imported as EXECUTOR_OPTIONS
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


if __name__ == "__main__":
    unittest.main()