"""Source-level tests for README.md current architecture claims.

These tests pin the portfolio README to current repo reality: positioning,
task input and state storage, the workspace/executor/validator chain, review
evidence and handoff semantics, Mission Control boundaries, human gates,
the scheduled-execution / ExecutionEngine migration status, and the deferred
automation list. They fail when the README drifts from what the system
actually does or claims authority it does not have.
"""

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
            "human-gated",
            "Manage work, not agents",
        ):
            self.assertIn(phrase, self.readme)

    def test_task_input_and_state_storage(self) -> None:
        for phrase in (
            "GitHub Issue or spec",
            "Local SQLite task mirror and orchestrator state storage",
            "issue/spec",
        ):
            self.assertIn(phrase, self.readme)

    def test_workspace_executor_validator_chain(self) -> None:
        for phrase in (
            "isolated git worktree",
            "bounded executor",
            "pi / opencode",
            "deterministic validators",
            "proof-of-work",
        ):
            self.assertIn(phrase, self.normalized)

    def test_review_evidence_and_handoff(self) -> None:
        for phrase in (
            "reviewable evidence",
            "PR handoff evidence",
            "Branch push is explicit",
            "Draft PR creation is explicit",
        ):
            self.assertIn(phrase, self.readme)

    def test_mission_control_boundary(self) -> None:
        self.assertIn(
            "Mission Control as read-only review and evidence dashboard",
            self.readme,
        )
        self.assertIn("read-only", self.normalized)
        self.assertIn("not the execution core", self.readme)

    def test_human_gate_and_forbidden_automation(self) -> None:
        for phrase in (
            "human review as final approval gate",
            "automatic merge",
            "self-approve",
            "automatic cleanup",
        ):
            self.assertIn(phrase, self.normalized)

    def test_current_loop_is_semi_automatic(self) -> None:
        self.assertIn("Semi-Automatic Dogfood Loop", self.readme)
        self.assertIn("operator-driven and semi-automatic", self.readme)
        self.assertIn("waiting_approval", self.readme)

    def test_scheduled_execution_status_is_explicit(self) -> None:
        for phrase in (
            "Scheduled one-task execution exists",
            "Active cron observability exists",
            "Live cron remains execution-only",
            "Publication, merge, and cleanup remain human-gated",
        ):
            self.assertIn(phrase, self.readme)

    def test_execution_engine_migration_is_evidence_only(self) -> None:
        self.assertIn(
            "ExecutionEngine migration is in progress and evidence-only",
            self.readme,
        )
        self.assertIn("--use-execution-engine", self.readme)
        self.assertIn("off by default", self.readme)
        self.assertIn("evidence only, not approval authority", self.readme)

    def test_deferred_automation_is_explicit(self) -> None:
        for phrase in (
            "Continuous queue or polling automation",
            "Webhook or background GitHub issue sync",
            "Dispatcher-driven branch push or PR creation",
            "Automatic merge after approval",
            "Automatic cleanup",
        ):
            self.assertIn(phrase, self.readme)

    def test_deferred_automation_acknowledges_scheduled_tick(self) -> None:
        # The deferred list must not imply that all scheduler automation is
        # absent: the locked one-task scheduled tick exists, while always-on
        # loop/daemon/background-worker automation remains deferred.
        self.assertIn(
            "scheduled execution path exists",
            self.normalized,
        )
        self.assertIn(
            "always-on scheduler loop, daemon, or",
            self.readme,
        )

    def test_outdated_hermes_kanban_quick_start_is_demoted(self) -> None:
        self.assertNotIn("scripts/kanban_create.py", self.readme)
        self.assertNotIn("scripts/kanban_accept_cleanup.py", self.readme)
        self.assertNotIn("scripts/kanban_workflow_regression.py", self.readme)
        self.assertIn("Historical Note", self.readme)
        self.assertIn("Hermes/Kanban", self.readme)


if __name__ == "__main__":
    unittest.main()
