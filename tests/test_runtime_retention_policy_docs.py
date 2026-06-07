"""Tests for the runtime logs / artifacts retention policy documentation (P2-c).

The doc is documentation-only. These tests assert that every retained runtime
path, the required retention phrases, and the forbidden-action language for
P2-c are present. P2-c implements no cleanup automation, so the doc must read
as policy, not as an executable cleanup procedure.
"""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "runtime-retention-policy.md"


class RuntimeRetentionPolicyDocTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = DOC.read_text(encoding="utf-8")
        cls.doc_lower = cls.doc.lower()

    def test_doc_exists(self) -> None:
        self.assertTrue(DOC.is_file())

    def test_mentions_every_retained_path(self) -> None:
        for path in (
            "/home/ubuntu/agent-taskflow-cron/logs/github-issue-one-task-real-opencode.jsonl",
            "/home/ubuntu/agent-taskflow-cron/artifacts/scheduler-tick/",
            "/home/ubuntu/agent-taskflow-cron/artifacts/evidence-archive/",
            "/home/ubuntu/agent-taskflow-cron/artifacts/task-closeout/",
            "/home/ubuntu/agent-taskflow/.agent-taskflow/artifacts/",
            "/home/ubuntu/agent-taskflow-backups/",
        ):
            self.assertIn(path, self.doc, f"doc missing retained path {path!r}")

    def test_contains_required_phrases(self) -> None:
        for phrase in (
            "must not be auto-deleted",
            "explicit human-confirmed",
            "proof-of-work",
            "disposition evidence",
            "closeout evidence",
        ):
            self.assertIn(phrase, self.doc, f"doc missing required phrase {phrase!r}")

    def test_contains_forbidden_action_language(self) -> None:
        for phrase in (
            "git clean",
            "git reset",
            "git worktree prune",
            "cron cleanup",
            "DB mutation",
            "GitHub mutation",
            "executor",
            "validator",
        ):
            self.assertIn(
                phrase, self.doc, f"doc missing forbidden-action language {phrase!r}"
            )

    def test_lists_retention_categories(self) -> None:
        for category in (
            "active operational logs",
            "runtime execution evidence",
            "disposition evidence",
            "closeout evidence",
            "manual backup evidence",
        ):
            self.assertIn(
                category, self.doc_lower, f"doc missing retention category {category!r}"
            )

    def test_lists_allowed_actions(self) -> None:
        for action in ("inspect", "copy", "compress", "rotate"):
            self.assertIn(
                action, self.doc_lower, f"doc missing allowed action {action!r}"
            )

    def test_lists_recommended_future_phases(self) -> None:
        for phase in ("p2-d", "p2-e", "p2-f"):
            self.assertIn(
                phase, self.doc_lower, f"doc missing future phase {phase!r}"
            )

    def test_states_documentation_only_scope(self) -> None:
        self.assertIn("documentation and tests only", self.doc_lower)


if __name__ == "__main__":
    unittest.main()
