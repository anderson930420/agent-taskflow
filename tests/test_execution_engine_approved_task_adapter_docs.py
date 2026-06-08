"""Documentation tests for the P4-c approved task adapter."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "execution-engine-approved-task-adapter.md"


class ApprovedTaskAdapterDocTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = DOC.read_text(encoding="utf-8")
        cls.doc_lower = cls.doc.lower()

    def test_doc_exists(self) -> None:
        self.assertTrue(DOC.is_file())

    def test_states_adapter_only_scope(self) -> None:
        self.assertIn("adapter-only", self.doc_lower)

    def test_states_no_runtime_migration(self) -> None:
        self.assertIn("no runtime migration", self.doc_lower)

    def test_mentions_adapter_class(self) -> None:
        self.assertIn("ApprovedTaskRunnerExecutionEngineAdapter", self.doc)

    def test_mentions_contract_request_and_result(self) -> None:
        self.assertIn("ExecutionEngineRequest", self.doc)
        self.assertIn("ExecutionEngineResult", self.doc)

    def test_mentions_approved_runner_types(self) -> None:
        self.assertIn("ApprovedTaskRunRequest", self.doc)
        self.assertIn("run_approved_task", self.doc)

    def test_states_scheduler_and_automation_do_not_use_adapter(self) -> None:
        self.assertIn("scheduler", self.doc_lower)
        self.assertIn("automation", self.doc_lower)
        self.assertIn("do not use the adapter yet", self.doc_lower)

    def test_states_destructive_operations_outside_engine(self) -> None:
        self.assertIn(
            "destructive operations remain outside executionengine",
            self.doc_lower,
        )

    def test_mentions_p4d_future_phase(self) -> None:
        self.assertIn("p4-d", self.doc_lower)


if __name__ == "__main__":
    unittest.main()
