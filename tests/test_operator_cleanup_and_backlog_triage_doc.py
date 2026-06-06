"""Tests for the operator cleanup and backlog triage runbook.

The runbook is documentation-only. These tests assert that the required review,
triage, and cleanup guidance is present and that no dangerous unconditional
command leaks into the document.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "operator-cleanup-and-backlog-triage.md"


class OperatorCleanupAndBacklogTriageDocTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = DOC.read_text(encoding="utf-8")
        cls.doc_lower = cls.doc.lower()

    def test_doc_exists(self) -> None:
        self.assertTrue(DOC.is_file())

    def test_mentions_backlog_states(self) -> None:
        for needle in ("waiting_approval", "blocked", "queued"):
            self.assertIn(needle, self.doc_lower, f"doc missing {needle!r}")

    def test_mentions_pre_fix_blocked_smoke_tasks(self) -> None:
        self.assertIn("AT-GH-67", self.doc)
        self.assertIn("AT-GH-69", self.doc)
        self.assertIn("implementation_prompt.md", self.doc)
        self.assertIn("SMOKE_TASK_KEY", self.doc)

    def test_mentions_policy_blocked_dogfood_examples(self) -> None:
        self.assertIn("GH-9603", self.doc)
        self.assertIn("GH-9601", self.doc)

    def test_waiting_approval_is_not_auto_publish(self) -> None:
        self.assertIn("publish_after_execution=false", self.doc_lower)
        self.assertIn(
            "never triggers a branch push or draft pr by itself", self.doc_lower
        )
        self.assertIn("start of human review", self.doc_lower)
        self.assertIn("auto-publish", self.doc_lower)

    def test_decision_categories_documented(self) -> None:
        self.assertIn("publish-worthy change", self.doc_lower)
        self.assertIn("smoke-only evidence", self.doc_lower)
        self.assertIn("bad / irrelevant output", self.doc_lower)

    def test_executor_artifacts_documented(self) -> None:
        for artifact in (
            "implementation_prompt.md",
            "opencode-events.jsonl",
            "git-status-after-opencode.txt",
            "diff-after-opencode.patch",
            "untracked-files-after-opencode.txt",
            "policy-validate.log",
        ):
            self.assertIn(artifact, self.doc, f"doc missing artifact {artifact!r}")

    def test_references_summarize_script(self) -> None:
        script = "scripts/summarize_real_scheduled_execution.py"
        self.assertTrue((REPO_ROOT / script).is_file())
        self.assertIn(script, self.doc)
        self.assertIn("TaskMirrorStore", self.doc)
        self.assertIn("artifact_dir", self.doc)

    def test_smoke_issue_handling_documented(self) -> None:
        self.assertIn("smoke", self.doc_lower)
        self.assertIn("supersed", self.doc_lower)
        self.assertIn("gh issue comment", self.doc)
        self.assertIn("gh issue close", self.doc)
        # The close/comment commands must be clearly labelled manual examples.
        self.assertIn("manual example", self.doc_lower)

    def test_queued_handling_documented(self) -> None:
        self.assertIn("AT-GH-14", self.doc)
        self.assertIn("relevance review", self.doc_lower)
        self.assertIn("do not auto-run queued", self.doc_lower)

    def test_worktree_cleanup_commands_documented(self) -> None:
        self.assertIn("git worktree list", self.doc)
        self.assertIn("git worktree remove <path>", self.doc)
        self.assertIn("git worktree prune", self.doc)
        self.assertIn("git -C <path> status --short", self.doc)

    def test_protected_paths_documented(self) -> None:
        self.assertIn("/home/ubuntu/agent-taskflow-cron", self.doc)
        self.assertIn("/home/ubuntu/agent-taskflow", self.doc)
        self.assertIn("protected", self.doc_lower)
        # Both protected paths must be named under a "never remove" framing.
        self.assertIn("never remove", self.doc_lower)

    def test_dirty_backup_policy_documented(self) -> None:
        self.assertIn("/home/ubuntu/agent-taskflow-dirty-backup", self.doc)
        self.assertIn("ls -lh /home/ubuntu/agent-taskflow-dirty-backup", self.doc)
        self.assertIn(
            "tar -tzf /home/ubuntu/agent-taskflow-dirty-backup/"
            "untracked-runtime-backup.tar.gz",
            self.doc,
        )
        # The only deletion command in the doc targets the dirty backup only.
        self.assertIn("rm -rf /home/ubuntu/agent-taskflow-dirty-backup", self.doc)

    def test_includes_required_safety_statements(self) -> None:
        for phrase in (
            "do not auto-close issues",
            "do not modify crontab",
            "do not add automation",
        ):
            self.assertIn(phrase, self.doc_lower, f"doc missing {phrase!r}")

    def test_warns_against_rm_rf_before_worktree_remove(self) -> None:
        self.assertIn("never `rm -rf` a worktree", self.doc_lower)

    def test_no_dangerous_unconditional_commands(self) -> None:
        # Simple forbidden substrings that must never appear anywhere.
        for forbidden in (
            "rm -rf /home/ubuntu/agent-taskflow-cron",
            "git clean -fdx",
            "gh pr merge",
            "--publish-after-execution",
        ):
            self.assertNotIn(
                forbidden, self.doc, f"doc must not contain {forbidden!r}"
            )

        # A bare `rm -rf` against the protected main checkout (or any subpath of
        # it) is forbidden. The only allowed deletion is the dirty-backup
        # directory, so we exclude that exact suffix via negative lookahead.
        forbidden_protected_rm = re.compile(
            r"rm\s+-rf\s+/home/ubuntu/agent-taskflow(?!-dirty-backup)"
        )
        self.assertIsNone(
            forbidden_protected_rm.search(self.doc),
            "doc must not delete a protected agent-taskflow path with rm -rf",
        )


if __name__ == "__main__":
    unittest.main()
