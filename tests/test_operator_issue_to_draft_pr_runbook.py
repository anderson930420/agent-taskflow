"""Tests for the operator issue-to-draft-PR dogfood runbook."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs" / "operator-issue-to-draft-pr-dogfood.md"
WORKFLOW = REPO_ROOT / "WORKFLOW.md"


def _executable_code_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    in_block = False
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("```"):
            fence = line.strip().removeprefix("```").strip()
            if in_block:
                blocks.append("\n".join(current))
                current = []
                in_block = False
                continue
            in_block = fence in {"bash", "sh", "shell"}
            continue
        if in_block:
            current.append(line)
    return blocks


class OperatorIssueToDraftPrRunbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = RUNBOOK.read_text(encoding="utf-8")
        cls.doc_lower = cls.doc.lower()

    def test_runbook_file_exists(self) -> None:
        self.assertTrue(RUNBOOK.is_file())

    def test_runbook_includes_current_core_workflow(self) -> None:
        required = [
            "issue ingestion",
            "workspace preparation",
            "dispatcher",
            "validation",
            "review evidence",
            "pr handoff",
            "dry-run",
            "fake-gh",
        ]
        for phrase in required:
            self.assertIn(phrase, self.doc_lower)

    def test_runbook_explicitly_preserves_safety_boundaries(self) -> None:
        required = [
            "this runbook does not run git push",
            "there is no auto-merge",
            "there is no auto-approve",
            "there is no cleanup automation",
            "not automatic issue polling",
            "dispatcher-driven pr creation",
            "webhook or polling loop",
        ]
        for phrase in required:
            self.assertIn(phrase, self.doc_lower)

    def test_branch_push_foundation_is_not_implemented(self) -> None:
        self.assertIn("explicit branch push foundation is not implemented yet", self.doc_lower)
        self.assertIn("the system does not push branches", self.doc_lower)

    def test_real_draft_pr_creation_requires_dry_run_and_confirm(self) -> None:
        self.assertIn("dry-run first", self.doc_lower)
        self.assertIn("--confirm-create-pr", self.doc)
        self.assertIn("creates draft prs only", self.doc_lower)

    def test_runbook_references_existing_scripts_accurately(self) -> None:
        scripts = [
            "scripts/run_local_validation.py",
            "scripts/ingest_github_issue.py",
            "scripts/prepare_task_workspace.py",
            "scripts/run_dispatcher.py",
            "scripts/create_pr_handoff.py",
            "scripts/create_draft_pr.py",
            "scripts/run_draft_pr_fake_gh_golden_path_smoke.py",
        ]
        for script in scripts:
            self.assertTrue((REPO_ROOT / script).is_file(), f"missing referenced script {script}")
            self.assertIn(script, self.doc)

    def test_runbook_lists_expected_evidence(self) -> None:
        expected = [
            "issue_spec",
            "github_issue_ingested",
            "taskworktreerecord",
            "base_sha",
            "mission_contract",
            "validation result",
            "pr_handoff.json",
            "pr_handoff.md",
            "pr_handoff_created",
            "draft_pr.json",
            "draft_pr_created",
        ]
        normalized = self.doc_lower
        for phrase in expected:
            self.assertIn(phrase, normalized)

    def test_static_safety_no_forbidden_executable_commands(self) -> None:
        executable_text = "\n".join(_executable_code_blocks(self.doc)).lower()
        forbidden = [
            "git push",
            "gh pr merge",
            "gh pr review --approve",
            "gh issue edit",
            "git merge",
            "git rebase",
            "git branch -d",
            "git branch -D",
            "git worktree remove",
            "git reset --hard",
            "cleanup automation",
        ]
        for command in forbidden:
            self.assertNotIn(command, executable_text)

    def test_static_safety_negative_push_language_is_not_executable(self) -> None:
        self.assertIn("git push", self.doc_lower)
        executable_text = "\n".join(_executable_code_blocks(self.doc)).lower()
        self.assertNotIn("git push", executable_text)

    def test_workflow_mentions_operator_runbook(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8").lower()
        self.assertIn("operator issue-to-draft-pr dogfood runbook", workflow)
        self.assertIn("human-triggered semi-automatic procedure", workflow)


if __name__ == "__main__":
    unittest.main()
