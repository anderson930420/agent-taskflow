"""Tests for the first real executor dogfood report."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT = REPO_ROOT / "docs" / "first-real-executor-dogfood-report.md"
WORKFLOW = REPO_ROOT / "WORKFLOW.md"


class FirstRealExecutorDogfoodReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = REPORT.read_text(encoding="utf-8")
        cls.report_lower = cls.report.lower()

    def test_report_file_exists(self) -> None:
        self.assertTrue(REPORT.is_file())

    def test_report_mentions_core_run_identifiers(self) -> None:
        required = [
            "AT-DOGFOOD-REAL-001",
            "Issue #14",
            "PR #15",
            "Pi executor",
            "waiting_approval",
            "branch push",
            "draft PR",
            "human review remains final gate",
        ]
        for phrase in required:
            self.assertIn(phrase, self.report)

    def test_report_includes_evidence_paths(self) -> None:
        required = [
            "issue_spec.md",
            "pr_handoff.json",
            "branch_push.json",
            "draft_pr.json",
        ]
        for phrase in required:
            self.assertIn(phrase, self.report)

    def test_report_includes_validation_summary(self) -> None:
        required = [
            "missing pytest",
            "pytest: passed",
            "openspec: skipped",
            "run_local_validation.py` passed",
        ]
        for phrase in required:
            self.assertIn(phrase, self.report_lower)

    def test_report_includes_safety_boundaries(self) -> None:
        required = [
            "no auto-merge",
            "no auto-approval",
            "no cleanup automation",
            "no force push",
            "no direct main edit",
            "executor did not push",
            "executor did not create pr",
        ]
        for phrase in required:
            self.assertIn(phrase, self.report_lower)

    def test_report_includes_lessons_learned(self) -> None:
        self.assertIn("## Lessons learned", self.report)
        self.assertIn("semi-automatic loop is now real", self.report_lower)
        self.assertIn("validators should fail fast", self.report_lower)

    def test_report_includes_recommended_next_phase(self) -> None:
        self.assertIn("## Recommended next phase", self.report)
        self.assertIn("Real Executor Preflight Dependency Check", self.report)
        self.assertIn("Mission Control Evidence Readback for Dogfood Artifacts", self.report)

    def test_static_safety_no_executable_dangerous_commands(self) -> None:
        forbidden = [
            "gh pr merge",
            "gh pr review --approve",
            "git push --force",
            "git push -f",
            "git merge",
            "git rebase",
            "git reset --hard",
            "git branch -d",
            "git branch -D",
            "git worktree remove",
            "cleanup automation command",
        ]
        for phrase in forbidden:
            self.assertNotIn(phrase, self.report)

    def test_workflow_references_report(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        workflow_lower = workflow.lower()
        self.assertIn("First Real Executor Dogfood Report", workflow)
        self.assertIn("docs/first-real-executor-dogfood-report.md", workflow)
        self.assertIn("first real pi", workflow_lower)
        self.assertIn("executor dogfood run", workflow_lower)


if __name__ == "__main__":
    unittest.main()
