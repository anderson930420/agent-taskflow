"""Tests for docs/explicit-scheduler-confirmation-preparation-checkpoint.md."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT_DOC = (
    REPO_ROOT
    / "docs"
    / "explicit-scheduler-confirmation-preparation-checkpoint.md"
)


class ExplicitSchedulerConfirmationPreparationCheckpointTests(unittest.TestCase):
    """Verify the Phase K0 checkpoint contains required protected strings."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = CHECKPOINT_DOC.read_text(encoding="utf-8")

    def test_doc_exists(self) -> None:
        self.assertTrue(
            CHECKPOINT_DOC.is_file(),
            f"Phase K0 checkpoint doc must exist at {CHECKPOINT_DOC}",
        )

    def test_required_headings_are_present(self) -> None:
        required_headings = (
            "## 1. Purpose",
            "## 2. Completed Level 1 and Level 2 foundation",
            "## 3. Level 3 definition",
            "## 4. Required input",
            "## 5. Output",
            "## 6. Safety boundary",
            "## 7. Required invariants before confirmation preparation",
            "## 8. Proposed phases",
            "## 9. Acceptance criteria for K2",
            "## 10. Non-goals",
        )

        for heading in required_headings:
            with self.subTest(heading=heading):
                self.assertIn(heading, self.doc)

    def test_required_protected_strings_are_present(self) -> None:
        required_phrases = (
            "Level 3 confirmation preparation",
            "scheduler_proposal → scheduler_confirmation",
            "scheduler_confirmation is not execution permission",
            "scheduler_confirmation is not verifier report",
            "scheduler_confirmation is not handoff",
            "scheduler_confirmation is not runtime execution",
            "scheduler_proposal is not confirmation",
            "scheduler_proposal is not execution permission",
            "explicit operator intent",
            "dry-run writes nothing",
            "--confirm-create-confirmation",
            "scheduler_confirmation artifact",
            "scheduler_confirmation_created event",
            "confirmation_id",
            "proposal_hash",
            "proposal_item_id",
            "item_hash",
            "no verifier report",
            "no handoff",
            "no runtime execution",
            "no approved_task_runner",
            "no executor",
            "no validators",
            "no GitHub mutation",
            "no approval / merge / cleanup",
            "no scheduler loop",
            "no background worker",
            "no automatic task picking",
            "Mission Control remains read-only",
        )

        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.doc)


if __name__ == "__main__":
    unittest.main()
