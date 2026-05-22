"""Tests for operator-facing documentation maps."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_MAP = REPO_ROOT / "docs" / "script-map.md"
SCHEDULER_MAP = REPO_ROOT / "docs" / "scheduler-module-map.md"


class DocumentationMapTests(unittest.TestCase):
    def test_script_map_exists_and_has_required_headings(self) -> None:
        self.assertTrue(SCRIPT_MAP.exists())
        text = SCRIPT_MAP.read_text(encoding="utf-8")

        for heading in (
            "## Intake / GitHub Issues",
            "## Scheduler Proposal / Confirmation",
            "## Task Execution Package / Queued Handoff",
            "## Executor Smoke / Golden Path",
            "## PR Handoff / Draft PR / Branch Push Helpers",
            "## Validation / Policy / Proof-of-Work",
            "## Cleanup / Closeout",
            "## Mission Control / API Smoke",
            "## Release / Documentation Checks",
        ):
            self.assertIn(heading, text)

    def test_scheduler_module_map_exists_and_has_required_headings(self) -> None:
        self.assertTrue(SCHEDULER_MAP.exists())
        text = SCHEDULER_MAP.read_text(encoding="utf-8")

        for heading in (
            "## Module Boundaries",
            "## Related Scripts",
            "## Flow",
            "## Safety Invariants",
        ):
            self.assertIn(heading, text)

        for module in (
            "task_recommendations.py",
            "scheduler_proposals.py",
            "scheduler_proposal_review.py",
            "scheduler_confirmations.py",
            "scheduler_confirmation_verifier.py",
        ):
            self.assertIn(module, text)

    def test_required_safety_phrases_are_present(self) -> None:
        combined = (
            SCRIPT_MAP.read_text(encoding="utf-8")
            + "\n"
            + SCHEDULER_MAP.read_text(encoding="utf-8")
        ).lower()

        for phrase in (
            "no background scheduler behavior",
            "no auto-merge",
            "no self-approval",
            "proposal is not action evidence",
            "confirmation is not runtime consumption",
        ):
            self.assertIn(phrase, combined)


if __name__ == "__main__":
    unittest.main()
