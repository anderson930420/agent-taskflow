"""Documentation tests for P4-h: the real scheduled dashboard reads the
normalized ``UnifiedExecutionSummary`` (``observability_summary``) when present,
with a preserved legacy fallback and no cron / execution-semantics changes.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "real-scheduled-execution-observability.md"


class RealScheduledDashboardUnifiedSummaryDocTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = DOC.read_text(encoding="utf-8")
        cls.doc_lower = cls.doc.lower()
        cls.doc_normalized = re.sub(r"\s+", " ", cls.doc_lower)

    def test_doc_exists(self) -> None:
        self.assertTrue(DOC.is_file())

    def test_mentions_unified_summary_and_field(self) -> None:
        self.assertIn("UnifiedExecutionSummary", self.doc)
        self.assertIn("observability_summary", self.doc)

    def test_mentions_legacy_fallback_and_scheduler_tick_logs(self) -> None:
        self.assertIn("legacy fallback", self.doc_normalized)
        self.assertIn("scheduler tick logs", self.doc_normalized)

    def test_states_no_cron_change(self) -> None:
        self.assertIn("no cron change", self.doc_normalized)

    def test_states_scheduler_tick_not_migrated_to_execution_engine(self) -> None:
        self.assertIn(
            "not migrated to executionengine", self.doc_normalized
        )

    def test_states_no_change_to_execution_semantics(self) -> None:
        self.assertIn("does not change execution semantics", self.doc_normalized)

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
            self.assertIn(phrase, self.doc_normalized, msg=phrase)


if __name__ == "__main__":
    unittest.main()
