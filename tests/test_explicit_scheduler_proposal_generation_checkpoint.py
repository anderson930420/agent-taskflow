"""Tests for docs/explicit-scheduler-proposal-generation-checkpoint.md."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT_DOC = (
    REPO_ROOT / "docs" / "explicit-scheduler-proposal-generation-checkpoint.md"
)


class ExplicitSchedulerProposalGenerationCheckpointTests(unittest.TestCase):
    """Verify the Phase J0 checkpoint contains required protected strings."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = CHECKPOINT_DOC.read_text(encoding="utf-8")

    def test_doc_exists(self) -> None:
        self.assertTrue(
            CHECKPOINT_DOC.is_file(),
            f"Phase J0 checkpoint doc must exist at {CHECKPOINT_DOC}",
        )

    def test_required_protected_strings_are_present(self) -> None:
        required_phrases = (
            "explicit proposal generation",
            "## 2. Current Level 1 foundation",
            "## 3. Level 2 definition",
            "## 4. Required input",
            "## 5. Output",
            "Phase G CLI/module candidate discovery",
            "Phase H API scheduler candidate readback",
            "Phase I Mission Control read-only scheduler candidate visibility",
            "proposal is not execution permission",
            "candidate visibility is not execution permission",
            "requires explicit operator command",
            "proposal is not confirmation",
            "scheduler_proposal artifact",
            "scheduler_proposal event",
            "proposal_hash",
            "proposal_item_id",
            "item_hash",
            "no runtime side effects",
            "no scheduler loop",
            "no background worker",
            "no automatic task picking",
            "no confirmation",
            "no verifier report",
            "no handoff",
            "no runtime execution",
            "no approved_task_runner",
            "no GitHub mutation",
            "no approval / merge / cleanup",
            "J1 CLI explicit proposal generation",
            "dry-run writes nothing",
            "confirmed mode writes proposal artifact/event only",
        )

        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.doc)


if __name__ == "__main__":
    unittest.main()
