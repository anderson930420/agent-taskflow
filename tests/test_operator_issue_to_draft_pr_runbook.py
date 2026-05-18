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
            "does not push branches automatically",
            "there is no auto-merge",
            "there is no auto-approve",
            "there is no cleanup automation",
            "not automatic issue polling",
            "dispatcher-driven pr creation",
            "webhook or polling loop",
        ]
        for phrase in required:
            self.assertIn(phrase, self.doc_lower)

    def test_branch_push_foundation_is_documented_as_explicit(self) -> None:
        self.assertIn("explicit branch push foundation is implemented", self.doc_lower)
        self.assertIn("does not push branches automatically", self.doc_lower)
        self.assertIn("scripts/push_task_branch.py", self.doc)
        self.assertIn("--confirm-push", self.doc)

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
            "scripts/push_task_branch.py",
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

    def test_static_safety_no_raw_git_push_command_is_executable(self) -> None:
        self.assertIn("scripts/push_task_branch.py", self.doc)
        executable_text = "\n".join(_executable_code_blocks(self.doc)).lower()
        self.assertNotIn("git push", executable_text)

    def test_workflow_mentions_operator_runbook(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8").lower()
        self.assertIn("operator issue-to-draft-pr dogfood runbook", workflow)
        self.assertIn("human-triggered semi-automatic procedure", workflow)

    def test_runbook_includes_first_real_executor_checklist(self) -> None:
        self.assertIn("first real executor dogfood checklist", self.doc_lower)
        self.assertIn("before execution", self.doc_lower)
        self.assertIn("during execution", self.doc_lower)
        self.assertIn("after execution", self.doc_lower)

    def test_checklist_mentions_use_only_one_executor(self) -> None:
        self.assertIn("use only one executor", self.doc_lower)
        self.assertIn("preferably pi agent first", self.doc_lower)


    def test_checklist_mentions_keep_task_small_and_low_risk(self) -> None:
        self.assertIn("keep the task small and low-risk", self.doc_lower)

    def test_checklist_mentions_modify_docs_tests_rather_than_core(self) -> None:
        self.assertIn("modify `docs/` or `tests/`", self.doc_lower)
        self.assertIn("rather than core push/pr/cleanup logic", self.doc_lower)

    def test_checklist_mentions_start_from_clean_main(self) -> None:
        self.assertIn("start from a clean main branch", self.doc_lower)

    def test_checklist_mentions_run_baseline_validation_before_execution(self) -> None:
        self.assertIn("run baseline validation before execution", self.doc_lower)
        self.assertIn("python3 -m unittest discover", self.doc_lower)
        self.assertIn("python3 -m compileall", self.doc_lower)

    def test_checklist_mentions_produce_review_evidence_before_pr_handoff(self) -> None:
        self.assertIn("produce review evidence", self.doc_lower)
        self.assertIn("before pr handoff", self.doc_lower)
        self.assertIn("executor log", self.doc_lower)
        self.assertIn("validator logs", self.doc_lower)
        self.assertIn("git status", self.doc_lower)
        self.assertIn("git diff", self.doc_lower)

    def test_checklist_mentions_run_branch_push_dry_run_before_confirmed_push(self) -> None:
        self.assertIn("run branch push dry-run before confirmed push", self.doc_lower)
        self.assertIn("--dry-run", self.doc_lower)

    def test_checklist_mentions_run_draft_pr_dry_run_before_confirmed_draft_pr(self) -> None:
        self.assertIn("run draft pr dry-run before confirmed draft pr creation", self.doc_lower)
        self.assertIn("--dry-run", self.doc_lower)

    def test_checklist_mentions_keep_merge_and_cleanup_human_controlled(self) -> None:
        self.assertIn("keep merge and cleanup human-controlled", self.doc_lower)

    def test_checklist_safety_constraints_are_documented(self) -> None:
        safety_constraints = [
            "do not touch branch push implementation",
            "do not touch draft pr creation implementation",
            "do not touch dispatcher implementation",
            "do not touch workspace manager implementation",
            "do not touch cleanup/merge policy",
            "do not touch mission control frontend",
            "do not approve tasks in the executor",
            "do not push branches from the executor",
        ]
        for constraint in safety_constraints:
            self.assertIn(constraint, self.doc_lower)

    def test_checklist_includes_why_this_matters_section(self) -> None:
        self.assertIn("why this checklist matters", self.doc_lower)
        self.assertIn("bounded implementation workers", self.doc_lower)
        self.assertIn("cannot self-approve", self.doc_lower)
        self.assertIn("cannot self-merge", self.doc_lower)
        self.assertIn("human review remains the final gate", self.doc_lower)


if __name__ == "__main__":
    unittest.main()
