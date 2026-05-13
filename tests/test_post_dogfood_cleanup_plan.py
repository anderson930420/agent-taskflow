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

    def test_has_cleanup_execution_status(self):
        self.assertIn("Cleanup Execution Status", self.doc)

    def test_cleanup_executed_date_recorded(self):
        self.assertIn("Execution date", self.doc)

    def test_cleaned_paths_documented(self):
        self.assertIn("/tmp/agent-taskflow-pi-gov-smoke-28.db", self.doc)
        self.assertIn("agent-taskflow-pi-gov-artifacts-28", self.doc)
        self.assertIn("agent-taskflow-pi-smoke-artifacts", self.doc)

    def test_worktree_cleanup_documented(self):
        self.assertIn(".worktrees/AT-PI-SMOKE-28", self.doc)
        self.assertIn(".worktrees/AT-PI-SMOKE-28-R2", self.doc)
        self.assertIn(".worktrees/AT-DOGFOOD-API-DB-PATH", self.doc)

    def test_preserved_paths_documented(self):
        self.assertIn("agent-taskflow-pi-gov-smoke-28-r2.db", self.doc)
        self.assertIn("agent-taskflow-pi-gov-artifacts-28-r2", self.doc)
        self.assertIn("agent-taskflow-dogfood-api-db-path.db", self.doc)
        self.assertIn("agent-taskflow-v0.1.0-rc1-staging", self.doc)

    def test_tag_unchanged(self):
        self.assertIn("v0.1.0-rc1 tag unchanged", self.doc.lower())

    def test_no_wildcard_cleanup(self):
        # doc should not contain broad wildcard patterns in actual commands
        self.assertNotIn("rm -rf /tmp/agent-taskflow*\n", self.doc.replace("# rm -rf", "# rm -rf"))

    def test_source_repo_not_deleted_compliance(self):
        self.assertIn("source repo clean after cleanup", self.doc.lower())

    def test_phase_43_executed_marker(self):
        self.assertIn("Phase 43", self.doc)
        self.assertIn("Executed", self.doc)

    def test_next_phase_recommendation(self):
        self.assertIn("Phase 44", self.doc)
        self.assertIn("Staging Clone", self.doc)

    def test_staging_clone_archive_decision(self):
        self.assertIn("Staging Clone Archive Decision", self.doc)

    def test_staging_clone_path_mentioned(self):
        self.assertIn("agent-taskflow-v0.1.0-rc1-staging", self.doc)

    def test_staging_clone_preserve_decision(self):
        # doc says "Preserve until v0.1.0 final release"
        self.assertIn("Preserve until v0.1.0 final release", self.doc)

    def test_staging_clone_release_reproducibility(self):
        self.assertIn("release reproducibility", self.doc)

    def test_staging_clone_detached_checkout(self):
        # doc uses "Detached checkout" (capital D)
        self.assertIn("Detached checkout", self.doc)

    def test_staging_clone_v010rc1_tag(self):
        self.assertIn("v0.1.0-rc1", self.doc)

    def test_staging_clone_2039aab(self):
        self.assertIn("2039aab", self.doc)

    def test_staging_clone_no_deletion_in_phase44(self):
        self.assertIn("No deletion performed in Phase 44", self.doc)

    def test_staging_clone_future_cleanup_command(self):
        self.assertIn("rm -rf /tmp/agent-taskflow-v0.1.0-rc1-staging", self.doc)

    def test_staging_clone_do_not_run_warning(self):
        self.assertIn("do not run", self.doc.lower())
        self.assertIn("before final release sign-off", self.doc.lower())

    def test_staging_clone_equivalent_evidence_archived(self):
        # doc says "Equivalent evidence summary is archived in docs"
        self.assertIn("Equivalent evidence summary is archived", self.doc)

    def test_phase57_post_v010_evidence_decision(self):
        self.assertIn("Post-v0.1.0 Evidence Decision", self.doc)

    def test_phase57_v010_release_url(self):
        self.assertIn("github.com/anderson930420/agent-taskflow/releases/tag/v0.1.0", self.doc)

    def test_phase57_eee67f3_tag_commit(self):
        self.assertIn("eee67f3", self.doc)

    def test_phase57_2039aab_tag_unchanged(self):
        self.assertIn("2039aab", self.doc)

    def test_phase57_evidence_decision_table(self):
        self.assertIn("Evidence Decision Table", self.doc)

    def test_phase57_r2_smoke_evidence(self):
        self.assertIn("agent-taskflow-pi-gov-smoke-28-r2.db", self.doc)
        self.assertIn("agent-taskflow-pi-gov-artifacts-28-r2", self.doc)

    def test_phase57_dogfood_evidence(self):
        self.assertIn("agent-taskflow-dogfood-api-db-path.db", self.doc)
        self.assertIn("agent-taskflow-dogfood-api-db-path-artifacts", self.doc)

    def test_phase57_staging_clone_path(self):
        self.assertIn("/tmp/agent-taskflow-v0.1.0-rc1-staging/", self.doc)

    def test_phase57_keep_until_ui_dogfood(self):
        # R2/dogfood evidence: "keep until post-v0.1.0 UI create/dispatch dogfood completes"
        self.assertIn("Keep until post-v0.1.0", self.doc)

    def test_phase57_staging_clone_safe_to_delete(self):
        self.assertIn("Safe to delete", self.doc)
        self.assertIn("v0.1.0 final release verified", self.doc)

    def test_phase57_no_evidence_deleted(self):
        self.assertIn("No evidence deleted in Phase 57", self.doc)

    def test_phase57_proposed_next_cleanup_phase(self):
        self.assertIn("Proposed Next Cleanup Phase", self.doc)

    def test_phase57_staging_clone_500m(self):
        self.assertIn("500M", self.doc)

    def test_phase57_v010_release_confirmed(self):
        self.assertIn("v0.1.0 final release confirmed", self.doc)

    def test_phase57_853_tests(self):
        self.assertIn("853 passed", self.doc)

    def test_phase57_tag_pointing_to_eee67f3(self):
        # v0.1.0 tag points to eee67f3
        self.assertIn("v0.1.0 tag commit", self.doc)
        self.assertIn("eee67f3", self.doc)

    def test_phase57_staging_clone_rationale(self):
        self.assertIn("staging clone", self.doc.lower())
        self.assertIn("safe to delete", self.doc.lower())
        self.assertIn("tag and GitHub release preserve source state", self.doc)

    def test_phase58_staging_clone_cleanup_executed(self):
        self.assertIn("Staging Clone Cleanup Execution", self.doc)

    def test_phase58_deleted_path(self):
        self.assertIn("agent-taskflow-v0.1.0-rc1-staging", self.doc)
        self.assertIn("500M", self.doc)

    def test_phase58_preserved_r2_evidence(self):
        self.assertIn("agent-taskflow-pi-gov-smoke-28-r2.db", self.doc)
        self.assertIn("agent-taskflow-pi-gov-artifacts-28-r2", self.doc)

    def test_phase58_preserved_dogfood_evidence(self):
        self.assertIn("agent-taskflow-dogfood-api-db-path.db", self.doc)
        self.assertIn("agent-taskflow-dogfood-api-db-path-artifacts", self.doc)

    def test_phase58_tags_unchanged(self):
        self.assertIn("v0.1.0", self.doc)
        self.assertIn("v0.1.0-rc1", self.doc)
        self.assertIn("eee67f3", self.doc)
        self.assertIn("2039aab", self.doc)

    def test_phase58_source_code_unchanged(self):
        self.assertIn("no source code changed", self.doc.lower())

    def test_phase58_no_r2_evidence_deleted(self):
        self.assertIn("No R2 evidence deleted", self.doc)

    def test_phase58_no_dogfood_evidence_deleted(self):
        self.assertIn("no dogfood evidence deleted", self.doc.lower())

    def test_phase58_v010_release_url(self):
        self.assertIn("github.com/anderson930420/agent-taskflow/releases/tag/v0.1.0", self.doc)

    def test_phase58_retained_until_ui_dogfood(self):
        self.assertIn("UI create/dispatch dogfood completes", self.doc)

    def test_phase58_compliance_notes(self):
        self.assertIn("Compliance", self.doc)
        self.assertIn("No wildcard cleanup", self.doc)

    def test_phase58_phase58_executed_marker(self):
        self.assertIn("Phase 58", self.doc)
        self.assertIn("Executed", self.doc)

    def test_phase64_legacy_evidence_cleanup_section(self):
        self.assertIn("Legacy Evidence Cleanup After UI Dogfood", self.doc)

    def test_phase64_executed_after_accepted(self):
        self.assertIn("AT-UI-DOGFOOD-59 reached", self.doc)
        self.assertIn("accepted", self.doc)

    def test_phase64_deleted_r2_smoke_db(self):
        self.assertIn("agent-taskflow-pi-gov-smoke-28-r2.db", self.doc)

    def test_phase64_deleted_r2_artifacts(self):
        self.assertIn("agent-taskflow-pi-gov-artifacts-28-r2", self.doc)

    def test_phase64_deleted_dogfood_db(self):
        self.assertIn("agent-taskflow-dogfood-api-db-path.db", self.doc)

    def test_phase64_deleted_dogfood_artifacts(self):
        self.assertIn("agent-taskflow-dogfood-api-db-path-artifacts", self.doc)

    def test_phase64_preserved_ui_dogfood_artifacts(self):
        self.assertIn("agent-taskflow-ui-dogfood-59-artifacts/AT-UI-DOGFOOD-59", self.doc)

    def test_phase64_supersedes_r2_api_evidence(self):
        self.assertIn("supersedes", self.doc.lower())
        self.assertIn("R2", self.doc)
        self.assertIn("API", self.doc)

    def test_phase64_api_approval_not_browser_confirm(self):
        self.assertIn("API approval", self.doc)
        self.assertIn("not a browser confirm-click", self.doc)

    def test_phase64_acceptance_note_phase53(self):
        self.assertIn("Phase 53", self.doc)
        self.assertIn("browser", self.doc.lower())

    def test_phase64_decided_by_human_enforced(self):
        self.assertIn("decided_by", self.doc)
        self.assertIn("human", self.doc)

    def test_phase64_tags_unchanged(self):
        self.assertIn("Tags Unchanged", self.doc)
        self.assertIn("v0.1.0", self.doc)
        self.assertIn("v0.1.0-rc1", self.doc)

    def test_phase64_source_code_unchanged(self):
        self.assertIn("Source code unchanged", self.doc)

    def test_phase64_no_evidence_deleted_compliance(self):
        self.assertIn("No AT-UI-DOGFOOD-59 evidence deleted", self.doc)

    def test_phase64_phase64_executed_marker(self):
        self.assertIn("Phase 64", self.doc)
        self.assertIn("Executed", self.doc)

    def test_phase64_worktree_not_deleted_note(self):
        self.assertIn(".worktrees/AT-UI-DOGFOOD-59", self.doc)

    def test_phase64_ui_dogfood_artifacts_preserved(self):
        self.assertIn("agent-taskflow-ui-dogfood-59-artifacts/AT-UI-DOGFOOD-59/", self.doc)

    def test_phase64_optional_future_work(self):
        self.assertIn("Optional future work", self.doc)


if __name__ == "__main__":
    unittest.main()