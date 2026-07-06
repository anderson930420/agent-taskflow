"""Regression tests for the P6 atomic artifact operator runbook."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs" / "atomic-artifact-safety-runbook.md"


class AtomicArtifactRunbookDocsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = RUNBOOK.read_text(encoding="utf-8")
        cls.text_lower = cls.text.lower()

    def test_runbook_exists(self) -> None:
        self.assertTrue(RUNBOOK.is_file())

    def test_preserves_required_commands_and_safety_language(self) -> None:
        for phrase in (
            "summarize_atomic_temp_orphans.py",
            "reset_task_status.py",
            "blocked -> queued",
            "--confirm-reset",
            "--dry-run",
            ".{target.name}.{16 lowercase hex}.tmp",
            "Do not add changed-files validator exclusions",
            "Do not automatically delete orphan temp files",
            "not approval",
            "not merge",
            "not cleanup",
            "not validation authority",
            "approved_task_runner",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.text)

    def test_rejects_unsafe_or_stale_guidance(self) -> None:
        # The required prohibition contains "automatically delete"; reject
        # that guidance only when it is not immediately qualified by "do not".
        self.assertIsNone(
            re.search(
                r"(?<!do not )automatically delete orphan temp",
                self.text_lower,
            )
        )
        for phrase in (
            "ignore atomic temp files in changed-files",
            "exclude atomic temp files from validators",
        ):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, self.text_lower)


if __name__ == "__main__":
    unittest.main()
