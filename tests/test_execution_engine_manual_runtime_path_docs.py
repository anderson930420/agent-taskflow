"""Documentation tests for the P4-d manual ExecutionEngine runtime path."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "execution-engine-manual-runtime-path.md"


class ManualRuntimePathDocTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = DOC.read_text(encoding="utf-8")
        cls.doc_lower = cls.doc.lower()
        # Collapse markdown line-wrapping so phrase checks are not broken by
        # newlines inserted in the middle of a sentence.
        cls.doc_normalized = re.sub(r"\s+", " ", cls.doc_lower)

    def test_doc_exists(self) -> None:
        self.assertTrue(DOC.is_file())

    def test_mentions_cli_script(self) -> None:
        self.assertIn(
            "scripts/run_execution_engine_approved_task.py", self.doc
        )

    def test_says_manual_runtime_path(self) -> None:
        self.assertIn("manual runtime path", self.doc_lower)

    def test_says_opt_in(self) -> None:
        self.assertIn("opt-in", self.doc_lower)

    def test_says_dry_run_default(self) -> None:
        self.assertIn("dry-run", self.doc_lower)
        self.assertIn("default", self.doc_lower)

    def test_says_confirm_flag(self) -> None:
        self.assertIn("--confirm-execution-engine-run", self.doc)

    def test_says_scheduler_automation_cron_unchanged(self) -> None:
        self.assertIn("scheduler", self.doc_lower)
        self.assertIn("automation", self.doc_lower)
        self.assertIn("cron", self.doc_lower)
        self.assertIn("unchanged", self.doc_lower)

    def test_mentions_adapter(self) -> None:
        self.assertIn("ApprovedTaskRunnerExecutionEngineAdapter", self.doc)

    def test_mentions_approved_task_runner_function(self) -> None:
        self.assertIn("approved_task_runner.run_approved_task", self.doc)

    def test_mentions_execution_engine_result(self) -> None:
        self.assertIn("ExecutionEngineResult", self.doc)

    def test_states_no_governance_side_effects(self) -> None:
        for phrase in (
            "no approval",
            "no merge",
            "no cleanup",
            "no archive",
            "no closeout",
            "no issue close",
            "no branch deletion",
            "no worktree deletion",
            "no github mutation",
        ):
            self.assertIn(phrase, self.doc_normalized)


if __name__ == "__main__":
    unittest.main()
