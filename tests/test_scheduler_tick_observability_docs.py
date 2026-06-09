"""Documentation tests for scheduler tick observability summary output."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "github-issue-one-task-scheduler-tick.md"


class SchedulerTickObservabilityDocTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = DOC.read_text(encoding="utf-8")
        cls.doc_lower = cls.doc.lower()
        cls.doc_normalized = re.sub(r"\s+", " ", cls.doc_lower)

    def test_doc_exists(self) -> None:
        self.assertTrue(DOC.is_file())

    def test_mentions_observability_flags_and_summary_shape(self) -> None:
        self.assertIn("--include-observability-summary", self.doc)
        self.assertIn("--observability-summary-only", self.doc)
        self.assertIn("UnifiedExecutionSummary", self.doc)

    def test_says_default_output_unchanged(self) -> None:
        self.assertIn("default output is unchanged", self.doc_normalized)

    def test_says_scheduler_tick_not_migrated_to_execution_engine(self) -> None:
        self.assertIn(
            "scheduler tick is not migrated to executionengine",
            self.doc_normalized,
        )

    def test_says_cron_and_execution_semantics_unchanged(self) -> None:
        self.assertIn("does not change cron behavior", self.doc_normalized)
        self.assertIn("does not change execution semantics", self.doc_normalized)

    def test_states_read_only_observability(self) -> None:
        self.assertIn("read-only observability", self.doc_normalized)

    def test_states_no_governance_or_github_side_effects(self) -> None:
        for phrase in (
            "no approval",
            "no merge",
            "no cleanup",
            "no archive",
            "no closeout",
            "no pr publication",
            "no issue close",
            "no branch deletion",
            "no worktree deletion",
            "no github mutation",
        ):
            self.assertIn(phrase, self.doc_normalized)


if __name__ == "__main__":
    unittest.main()
