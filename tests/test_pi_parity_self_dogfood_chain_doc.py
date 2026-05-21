"""Tests for the Pi Agent parity section in p2-architecture-checkpoint.md."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT = REPO_ROOT / "docs" / "p2-architecture-checkpoint.md"


class PiParitySelfDogfoodChainDocTests(unittest.TestCase):
    """Regression tests for the Pi Agent parity self-dogfood chain documentation."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = CHECKPOINT.read_text(encoding="utf-8")
        cls.doc_lower = cls.doc.lower()

    def test_checkpoint_file_exists(self) -> None:
        self.assertTrue(CHECKPOINT.is_file())

    def test_pi_agent_parity_section_exists(self) -> None:
        self.assertIn("## 13. Pi Agent parity", self.doc)

    def test_chain_diagram_present(self) -> None:
        required_chain_lines = [
            "offline issue/spec",
            "deterministic intake",
            "Task Execution Package",
            "explicit queued-task handoff",
            "approved_task_runner",
            "Pi executor",
            "deterministic validators",
            "waiting_approval",
        ]
        for line in required_chain_lines:
            self.assertIn(line, self.doc, f"Missing chain element: {line}")

    def test_required_strings_present(self) -> None:
        """Assert all ten required strings are present for regression testing."""
        required_strings = [
            "Pi Agent",
            "Task Execution Package",
            "queued-task handoff",
            "approved_task_runner",
            "deterministic validators",
            "waiting_approval",
            "no auto-push",
            "no auto-PR",
            "no auto-merge",
            "no auto-cleanup",
        ]
        for s in required_strings:
            self.assertIn(s, self.doc, f"Missing required string: {s}")

    def test_pi_is_bounded_coder_only(self) -> None:
        self.assertIn("Pi is used only as the bounded coder executor", self.doc)

    def test_validators_remain_deterministic(self) -> None:
        self.assertIn("Validators remain deterministic", self.doc)

    def test_pi_does_not_approve_merge_push_create_pr_or_cleanup(self) -> None:
        self.assertIn("Pi does not approve, merge, push, create PRs, or cleanup", self.doc)

    def test_human_review_remains_final_gate(self) -> None:
        self.assertIn("Human review remains the final gate", self.doc)


if __name__ == "__main__":
    unittest.main()