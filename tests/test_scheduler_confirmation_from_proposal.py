"""Tests for the explicit scheduler confirmation creation helper (K2)."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    REPO_ROOT / "agent_taskflow" / "scheduler_confirmation_from_proposal.py"
)


class SourceContractTests(unittest.TestCase):
    def test_source_does_not_import_or_call_forbidden_runtime_paths(
        self,
    ) -> None:
        text = MODULE_PATH.read_text(encoding="utf-8")

        strict_forbidden = (
            "executor_run_started",
            "validation_result",
            "runtime_execution_started",
            "create_verifier_report",
            "intake_runner_handoff",
            "subprocess",
            "requests.post",
            "gh pr",
        )
        for needle in strict_forbidden:
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)

        # `approved_task_runner` is permitted only as the safety-flag key
        # `approved_task_runner_called` asserting the runner was NOT
        # invoked. It must never appear as an import target or as a call.
        self.assertNotIn("from agent_taskflow.approved_task_runner", text)
        self.assertNotIn("import agent_taskflow.approved_task_runner", text)
        self.assertNotIn("approved_task_runner(", text)
        self.assertNotIn("approved_task_runner.", text)

        forbidden_imports = (
            "from scripts",
            "import scripts",
            "from agent_taskflow.api",
            "import agent_taskflow.api",
            "from agent_taskflow.executors",
            "import agent_taskflow.executors",
            "from agent_taskflow.validators",
            "import agent_taskflow.validators",
            "mission_control",
            "mission-control",
        )
        for needle in forbidden_imports:
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
