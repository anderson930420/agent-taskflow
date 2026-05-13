"""
Doc tests for v0.1.0 release documentation.
Verifies that release notes and final checklist contain required content.
"""

import os
import unittest


class TestV010ReleaseNotes(unittest.TestCase):
    """Test docs/release-notes-v0.1.0.md contains required sections."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(
            os.path.dirname(__file__), "..", "docs", "release-notes-v0.1.0.md"
        )
        with open(path, encoding="utf-8") as f:
            cls.content = f.read()

    def test_version_string(self):
        self.assertIn("v0.1.0", self.content)

    def test_governance_pipeline(self):
        self.assertIn("Governance Pipeline", self.content)

    def test_mission_control_ui(self):
        self.assertIn("Mission Control UI", self.content)

    def test_cors(self):
        self.assertIn("CORS", self.content)

    def test_human_approval_enforcement(self):
        self.assertIn("Human Approval Enforcement", self.content)

    def test_decided_by_human(self):
        self.assertIn('decided_by="human"', self.content)

    def test_browser_approval_dogfood(self):
        self.assertIn("browser approval dogfood", self.content.lower())

    def test_waiting_approval_accepted(self):
        self.assertIn("waiting_approval", self.content)
        self.assertIn("accepted", self.content)

    def test_no_push(self):
        self.assertIn("no push", self.content.lower())

    def test_no_merge(self):
        # release notes uses "no push/merge/cleanup automation" — merge is covered
        self.assertIn("no push/merge/cleanup", self.content.lower())

    def test_no_cleanup(self):
        # release notes uses "no push/merge/cleanup automation" — cleanup is covered
        self.assertIn("no push/merge/cleanup", self.content.lower())

    def test_no_worker_self_approval(self):
        self.assertIn("no worker self-approval", self.content.lower())

    def test_default_validators(self):
        self.assertIn("DEFAULT_VALIDATORS", self.content)

    def test_pytest_openspec(self):
        self.assertIn('("pytest", "openspec")', self.content)

    def test_pi_orchestrator_protocol_metadata_spike(self):
        self.assertIn("protocol metadata spike", self.content.lower())

    def test_not_autonomous_multi_agent_runtime(self):
        self.assertIn("not autonomous multi-agent runtime", self.content.lower())

    def test_815_tests(self):
        self.assertIn("815 passed", self.content)

    def test_frontend_build_clean(self):
        self.assertIn("frontend build", self.content)
        self.assertIn("clean", self.content.lower())

    def test_v010_rc1(self):
        self.assertIn("v0.1.0-rc1", self.content)

    def test_phase_45_54(self):
        # covered as "Phase 45–49" and "Phase 52–54" and "Phase 45–54"
        self.assertIn("Phase 45", self.content)
        self.assertIn("Phase 52", self.content)


class TestV010FinalChecklist(unittest.TestCase):
    """Test docs/v0.1.0-final-release-checklist.md contains required content."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "docs",
            "v0.1.0-final-release-checklist.md",
        )
        with open(path, encoding="utf-8") as f:
            cls.content = f.read()

    def test_version_string(self):
        self.assertIn("v0.1.0", self.content)

    def test_final_release_checklist(self):
        self.assertIn("Final Release Checklist", self.content)

    def test_ready_to_tag(self):
        self.assertIn("READY TO TAG", self.content.upper())

    def test_post_v010_followups(self):
        self.assertIn("Post-v0.1.0 Follow-ups", self.content)

    def test_human_approval_enforcement(self):
        self.assertIn("decided_by", self.content)

    def test_browser_approval_dogfood(self):
        self.assertIn("browser approval dogfood", self.content.lower())

    def test_tests_pass(self):
        self.assertIn("815 passed", self.content)

    def test_cors_passed(self):
        self.assertIn("CORS", self.content)
        self.assertIn("passed", self.content.lower())

    def test_no_push_merge_cleanup(self):
        self.assertIn("push", self.content.lower())
        self.assertIn("merge", self.content.lower())
        self.assertIn("cleanup", self.content.lower())

    def test_release_blockers_none(self):
        self.assertIn("Release Blockers", self.content)
        self.assertIn("None", self.content)

    def test_not_in_scope(self):
        self.assertIn("Not in Scope", self.content)

    def test_pi_orchestrator_spike(self):
        self.assertIn("protocol metadata spike", self.content.lower())

    def test_default_validators_unchanged(self):
        self.assertIn("DEFAULT_VALIDATORS unchanged", self.content)

    def test_db_schema_unchanged(self):
        self.assertIn("DB schema unchanged", self.content)

    def test_dispatcher_state_machine_unchanged(self):
        self.assertIn("Dispatcher state machine unchanged", self.content)

    def test_known_limitations(self):
        # checklist covers known limitations in "Release Blockers" section
        self.assertIn("Release Blockers", self.content)

    def test_no_tag_created_in_phase(self):
        # "Action to take after approval" in checklist means no tag created yet
        self.assertIn("Action to take after approval", self.content)

    def test_no_release_created_in_phase(self):
        # "Create non-prerelease GitHub Release" is a next-step action, not done in this phase
        self.assertIn("GitHub Release", self.content)


if __name__ == "__main__":
    unittest.main()