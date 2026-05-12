"""Tests for docs/release-notes-v0.1.0-rc1.md content.

These tests verify the release notes draft contains the statements required
by Phase 33 acceptance criteria. They do not run any workflow.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
NOTES_DOC = REPO_ROOT / "docs" / "release-notes-v0.1.0-rc1.md"


class ReleaseNotesContentTests(unittest.TestCase):
    """Verify the release notes draft contains required content."""

    @classmethod
    def setUpClass(cls):
        cls.doc = NOTES_DOC.read_text(encoding="utf-8")
        cls.lower = cls.doc.lower()

    # Basic identification
    def test_doc_identifies_as_v0_1_0_rc1(self) -> None:
        self.assertIn("v0.1.0-rc1", self.doc)

    def test_doc_says_governance_pipeline_release_candidate(self) -> None:
        self.assertIn("governance pipeline release candidate", self.lower)

    # Architecture artifacts
    def test_doc_lists_mission_contract_json(self) -> None:
        self.assertIn("mission_contract.json", self.doc)

    def test_doc_lists_pi_mission_plan_json(self) -> None:
        self.assertIn("pi_mission_plan.json", self.doc)

    def test_doc_lists_pi_mission_prompt_md(self) -> None:
        self.assertIn("pi_mission_prompt.md", self.doc)

    # Validators
    def test_doc_mentions_policy_validator(self) -> None:
        self.assertIn("PolicyCheckValidator", self.doc)

    def test_doc_mentions_typecheck_validator(self) -> None:
        self.assertIn("TypecheckValidator", self.doc)

    def test_doc_mentions_lint_validator(self) -> None:
        self.assertIn("LintValidator", self.doc)

    # Governance guarantees
    def test_doc_says_human_approval_remains_final_gate(self) -> None:
        self.assertIn("human approval is the final gate", self.lower)

    def test_doc_says_worker_cannot_approve(self) -> None:
        self.assertIn("worker cannot approve", self.lower)

    def test_doc_says_worker_cannot_push(self) -> None:
        self.assertIn("worker cannot push", self.lower)

    def test_doc_says_worker_cannot_merge(self) -> None:
        self.assertIn("worker cannot merge", self.lower)

    def test_doc_says_worker_cannot_cleanup(self) -> None:
        self.assertIn("cleanup", self.lower)

    def test_doc_lists_all_8_forbidden_actions(self) -> None:
        for action in [
            "approve",
            "push",
            "merge",
            "cleanup",
            "delete_worktree",
            "delete_branch",
            "self_approve",
            "force_push",
        ]:
            self.assertIn(action, self.lower)

    # Default validators
    def test_doc_shows_default_validators(self) -> None:
        self.assertIn('"pytest"', self.doc) and self.assertIn('"openspec"', self.doc)

    def test_doc_says_policy_typecheck_lint_are_opt_in(self) -> None:
        self.assertIn("opt-in", self.lower)

    # Validation status
    def test_doc_reports_620_tests(self) -> None:
        self.assertIn("620", self.doc)

    def test_doc_reports_real_pi_smoke_passed(self) -> None:
        self.assertIn("real pi governance smoke", self.lower)
        self.assertIn("passed", self.lower)

    def test_doc_reports_review_evidence_api_passed(self) -> None:
        self.assertIn("review evidence api", self.lower)
        self.assertIn("passed", self.lower)

    # Scope limitations
    def test_doc_says_no_db_schema_changes(self) -> None:
        self.assertIn("no db schema changes", self.lower)

    def test_doc_says_no_approval_semantic_changes(self) -> None:
        self.assertIn("no approval semantic changes", self.lower)

    def test_doc_says_no_multipi_integration(self) -> None:
        self.assertIn("no real multi-pi integration", self.lower)

    def test_doc_says_orchestrator_is_spike_not_runtime(self) -> None:
        self.assertIn("protocol metadata spike", self.lower)
        self.assertIn("not a true", self.lower)
        self.assertIn("multi-agent runtime", self.lower)

    # Suggested next steps
    def test_doc_suggests_dogfood_next_step(self) -> None:
        self.assertIn("dogfood", self.lower)

    def test_doc_suggests_create_github_release(self) -> None:
        self.assertIn("github release", self.lower)

    def test_doc_suggests_staging_smoke_from_tag(self) -> None:
        self.assertIn("staging smoke", self.lower)

    # Pi executor backend status
    def test_doc_says_pi_is_executor_backend(self) -> None:
        self.assertIn("executor backend", self.lower)
        self.assertIn("only", self.lower)

    def test_doc_says_agent_taskflow_is_control_plane(self) -> None:
        self.assertIn("governance", self.lower)
        self.assertIn("control plane", self.lower)

    # Commit hash
    def test_doc_lists_tag_and_commit(self) -> None:
        self.assertIn("2039aab", self.doc)
        self.assertIn("v0.1.0-rc1", self.doc)


if __name__ == "__main__":
    unittest.main()
