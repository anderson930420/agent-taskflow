"""Tests for the evidence-only / superseded task archive documentation.

The doc is documentation-only. These tests assert the required separation from
``confirm_task_closeout.py``, the reason-code/example coverage, and the safety
language are present, and that no dangerous unconditional command leaks in.
"""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "evidence-only-task-archive.md"
SCRIPT = REPO_ROOT / "scripts" / "archive_task_evidence_only.py"


class EvidenceOnlyTaskArchiveDocTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = DOC.read_text(encoding="utf-8")
        cls.doc_lower = cls.doc.lower()

    def test_doc_exists(self) -> None:
        self.assertTrue(DOC.is_file())

    def test_references_the_script(self) -> None:
        self.assertTrue(SCRIPT.is_file())
        self.assertIn("scripts/archive_task_evidence_only.py", self.doc)

    def test_states_not_merged_pr_closeout(self) -> None:
        self.assertIn("not", self.doc_lower)
        self.assertIn("merged-pr closeout", self.doc_lower)

    def test_states_does_not_replace_closeout(self) -> None:
        self.assertIn("confirm_task_closeout.py", self.doc)
        self.assertIn("does not replace", self.doc_lower)

    def test_states_closeout_is_separate_and_stricter(self) -> None:
        self.assertIn("stricter", self.doc_lower)
        # The closeout command is described as the full draft PR pipeline path.
        self.assertIn("draft pr", self.doc_lower)
        self.assertIn("merged", self.doc_lower)

    def test_directs_full_closeout_to_closeout_script(self) -> None:
        self.assertIn(
            "use `confirm_task_closeout.py` for full draft pr pipeline closeout",
            self.doc_lower,
        )

    def test_directs_evidence_only_to_archive_script(self) -> None:
        self.assertIn(
            "use `archive_task_evidence_only.py` for evidence-only", self.doc_lower
        )

    def test_lists_all_reason_codes(self) -> None:
        for reason_code in (
            "salvaged_by_pr",
            "smoke_evidence_only",
            "superseded_by_later_smoke",
            "no_op_evidence",
            "stale_policy_blocked",
            "stale_branch_push",
            "obsolete_queued",
        ):
            self.assertIn(reason_code, self.doc, f"doc missing reason code {reason_code!r}")

    def test_includes_required_examples(self) -> None:
        self.assertIn("GH-9604", self.doc)
        self.assertIn("--superseded-by-pr 78", self.doc)
        self.assertIn("AT-GH-74", self.doc)
        self.assertIn("smoke_evidence_only", self.doc)
        self.assertIn("AT-GH-69", self.doc)
        self.assertIn("--superseded-by-task AT-GH-74", self.doc)
        self.assertIn("GH-9601", self.doc)
        self.assertIn("stale_policy_blocked", self.doc)

    def test_dry_run_default_documented(self) -> None:
        self.assertIn("dry-run by default", self.doc_lower)
        self.assertIn("--confirm-evidence-archive", self.doc)
        self.assertIn("no db write", self.doc_lower)

    def test_safety_boundaries_documented(self) -> None:
        for phrase in (
            "no github mutation",
            "no deletion",
            "no cleanup automation",
        ):
            self.assertIn(phrase, self.doc_lower, f"doc missing safety phrase {phrase!r}")

    def test_safety_no_github_no_delete_no_executor_validator_cron(self) -> None:
        for phrase in (
            "close a github issue",
            "delete a local or remote branch",
            "inspect or remove filesystem worktrees",
            "start an executor or a validator",
            "modify cron",
        ):
            self.assertIn(phrase, self.doc_lower, f"doc missing safety phrase {phrase!r}")

    def test_human_review_final_gate(self) -> None:
        self.assertIn("human review remains the final gate", self.doc_lower)

    def test_no_dangerous_unconditional_commands(self) -> None:
        for forbidden in (
            "gh pr merge",
            "gh issue close",
            "git push",
            "git branch -d",
            "git branch -D",
            "git worktree remove",
            "rm -rf",
            "crontab",
        ):
            self.assertNotIn(
                forbidden, self.doc, f"doc must not contain {forbidden!r}"
            )


if __name__ == "__main__":
    unittest.main()
