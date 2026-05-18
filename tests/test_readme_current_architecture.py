"""Source-level tests for README.md current architecture claims."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"


class ReadmeCurrentArchitectureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.readme = README.read_text(encoding="utf-8")
        cls.normalized = cls.readme.lower()

    def test_readme_exists(self) -> None:
        self.assertTrue(README.is_file())

    def test_current_positioning(self) -> None:
        for phrase in (
            "Python-native",
            "GitHub-oriented",
            "Symphony-style",
            "Manage work, not agents",
        ):
            self.assertIn(phrase, self.readme)

    def test_task_input_and_state_storage(self) -> None:
        for phrase in (
            "GitHub Issues or specs as task input",
            "local SQLite store",
            "issue/spec",
        ):
            self.assertIn(phrase, self.readme)

    def test_workspace_executor_validator_chain(self) -> None:
        for phrase in (
            "isolated git worktrees",
            "bounded executor",
            "pi and opencode",
            "deterministic validators",
            "proof-of-work",
        ):
            self.assertIn(phrase, self.normalized)

    def test_review_evidence_and_handoff(self) -> None:
        for phrase in (
            "reviewable artifacts",
            "PR handoff evidence",
            "Branch push is explicit",
            "Draft PR creation is explicit",
        ):
            self.assertIn(phrase, self.readme)

    def test_mission_control_boundary(self) -> None:
        self.assertIn("Mission Control is a review and evidence dashboard", self.readme)
        self.assertIn("read-only", self.normalized)
        self.assertIn("not the execution core", self.readme)

    def test_human_gate_and_forbidden_automation(self) -> None:
        for phrase in (
            "human review remains the final gate",
            "does not auto-merge",
            "self-approve",
            "does not perform automatic cleanup",
        ):
            self.assertIn(phrase, self.normalized)

    def test_current_loop_is_semi_automatic(self) -> None:
        self.assertIn("Current Semi-Automatic Dogfood Loop", self.readme)
        self.assertIn("operator-driven and semi-automatic", self.readme)
        self.assertIn("waiting_approval", self.readme)

    def test_deferred_automation_is_explicit(self) -> None:
        for phrase in (
            "Queue or polling automation",
            "Webhook/background GitHub issue sync",
            "Dispatcher-driven branch push or PR creation",
            "Automatic merge after approval",
            "Automatic cleanup",
        ):
            self.assertIn(phrase, self.readme)

    def test_outdated_hermes_kanban_quick_start_is_demoted(self) -> None:
        self.assertNotIn("scripts/kanban_create.py", self.readme)
        self.assertNotIn("scripts/kanban_accept_cleanup.py", self.readme)
        self.assertNotIn("scripts/kanban_workflow_regression.py", self.readme)
        self.assertIn("Historical Note", self.readme)
        self.assertIn("Hermes/Kanban", self.readme)


if __name__ == "__main__":
    unittest.main()
