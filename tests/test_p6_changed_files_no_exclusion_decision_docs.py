"""Regression tests for the P6-E changed-files no-exclusion decision."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DECISION_RECORD = REPO_ROOT / "docs" / "changed-files-no-exclusion-decision.md"


class ChangedFilesNoExclusionDecisionDocsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = DECISION_RECORD.read_text(encoding="utf-8")
        cls.text_lower = cls.text.lower()

    def test_decision_record_exists(self) -> None:
        self.assertTrue(DECISION_RECORD.is_file())

    def test_preserves_required_decision_language(self) -> None:
        for phrase in (
            "Changed-files No-Exclusion Decision",
            "closes the remaining #7/#8",
            "Do not add changed-files validator exclusions for atomic temp files",
            "Do not add atomic temp files to `.gitignore`",
            "Do not hide orphan atomic temp files from evidence",
            "Do not automatically delete orphan atomic temp files",
            ".{target.name}.{16 lowercase hex}.tmp",
            "summarize_atomic_temp_orphans.py",
            "docs/atomic-artifact-safety-runbook.md",
            "not cleanup",
            "not approval",
            "not validation authority",
            "does not run executors or validators",
            "separate, explicit, human-confirmed cleanup workflow",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.text)

    def test_rejects_affirmative_unsafe_guidance(self) -> None:
        for phrase in (
            "add changed-files validator exclusions",
            "hide orphan atomic temp files from evidence",
            "automatically delete orphan atomic temp files",
            "treat atomic temp matches as validator-ignored noise",
            "implement broad pattern-based filtering",
        ):
            with self.subTest(phrase=phrase):
                self.assertIsNone(
                    re.search(rf"(?<!do not ){re.escape(phrase)}", self.text_lower)
                )


if __name__ == "__main__":
    unittest.main()
