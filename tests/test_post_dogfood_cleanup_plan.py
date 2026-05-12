"""Doc tests for post-dogfood cleanup plan."""
import unittest


class TestPostDogfoodCleanupPlanDocs(unittest.TestCase):
    """Test that docs/post-dogfood-cleanup-plan.md contains required sections."""

    @classmethod
    def setUpClass(cls):
        with open("docs/post-dogfood-cleanup-plan.md", "r") as f:
            cls.doc = f.read()

    def test_has_title(self):
        self.assertIn("Post-Dogfood Cleanup Plan", self.doc)

    def test_has_current_repo_state(self):
        self.assertIn("Current Repository State", self.doc)

    def test_has_preserved_evidence(self):
        self.assertIn("Preserved Evidence", self.doc)

    def test_has_safe_to_delete(self):
        self.assertIn("Safe to Delete Later", self.doc)

    def test_has_do_not_delete(self):
        self.assertIn("Do Not Delete", self.doc)

    def test_has_proposed_cleanup_commands(self):
        self.assertIn("Proposed Cleanup Commands", self.doc)

    def test_has_recommended_cleanup_order(self):
        self.assertIn("Recommended Cleanup Order", self.doc)

    def test_has_risks(self):
        self.assertIn("Risks", self.doc)

    def test_mentions_dogfood_task(self):
        self.assertIn("AT-DOGFOOD-API-DB-PATH", self.doc)

    def test_mentions_staging_task(self):
        self.assertIn("AT-PI-STAGING-RC1", self.doc)

    def test_mentions_smoke_r2_task(self):
        self.assertIn("AT-PI-SMOKE-28-R2", self.doc)

    def test_mentions_release_tag(self):
        self.assertIn("v0.1.0-rc1", self.doc)

    def test_commands_not_executed_in_this_phase(self):
        self.assertIn("do not run in this phase", self.doc.lower())

    def test_mentions_git_worktree_remove(self):
        self.assertIn("git worktree remove", self.doc)

    def test_mentions_tmp_agent_taskflow(self):
        self.assertIn("/tmp/agent-taskflow", self.doc)

    def test_source_repo_not_deleted(self):
        self.assertIn("source repo", self.doc.lower())
        self.assertIn("do not delete", self.doc.lower())

    def test_mentions_smoke_task_r2_db(self):
        self.assertIn("agent-taskflow-pi-gov-smoke-28-r2.db", self.doc)

    def test_mentions_dogfood_db(self):
        self.assertIn("agent-taskflow-dogfood-api-db-path.db", self.doc)

    def test_mentions_staging_staging(self):
        self.assertIn("agent-taskflow-staging-rc1.db", self.doc)

    def test_worktrees_classified(self):
        self.assertIn("AT-PI-SMOKE-28", self.doc)
        self.assertIn("AT-PI-SMOKE-28-R2", self.doc)
        self.assertIn("AT-DOGFOOD-API-DB-PATH", self.doc)

    def test_cleanup_commands_are_commented(self):
        # cleanup commands should be shown as examples/not run, not as live commands
        lines = self.doc.split("\n")
        in_proposed_section = False
        for line in lines:
            if "Proposed Cleanup Commands" in line:
                in_proposed_section = True
            if in_proposed_section and line.strip().startswith("git worktree remove"):
                self.assertTrue(
                    "#" in line or "||" in line or "only" in line.lower(),
                    f"Cleanup command should be commented or safe: {line.strip()}",
                )

    def test_preserved_evidence_has_two_tasks(self):
        # should have both AT-PI-SMOKE-28-R2 and AT-DOGFOOD-API-DB-PATH
        self.assertIn("AT-PI-SMOKE-28-R2", self.doc)
        self.assertIn("AT-DOGFOOD-API-DB-PATH", self.doc)

    def test_next_phase_recommendation(self):
        self.assertIn("Phase 43", self.doc)
        self.assertIn("Execute Approved Cleanup Plan", self.doc)


if __name__ == "__main__":
    unittest.main()