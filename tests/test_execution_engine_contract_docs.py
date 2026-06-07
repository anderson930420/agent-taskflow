"""Documentation tests for the ExecutionEngine contract (P4-b)."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "execution-engine-contract.md"


class ExecutionEngineContractDocTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = DOC.read_text(encoding="utf-8")
        cls.doc_lower = cls.doc.lower()

    def test_doc_exists(self) -> None:
        self.assertTrue(DOC.is_file())

    def test_states_contract_only_scope_and_no_runtime_migration(self) -> None:
        self.assertIn("contract-only", self.doc_lower)
        self.assertIn("no runtime migration", self.doc_lower)

    def test_mentions_contract_types(self) -> None:
        for contract_type in (
            "ExecutionEngineRequest",
            "ExecutionEngineResult",
            "ExecutionEngineSafety",
            "ExecutionEngineExecutorProfile",
            "ExecutionEngineValidatorProfile",
            "ExecutionEngineWorkspaceProfile",
        ):
            self.assertIn(contract_type, self.doc)

    def test_mentions_execution_engine_protocol(self) -> None:
        self.assertIn("`ExecutionEngine` protocol", self.doc)

    def test_states_post_execution_actions_are_outside_contract(self) -> None:
        self.assertIn(
            "merge, cleanup, archive, closeout, and pr publication are outside the contract",
            self.doc_lower,
        )

    def test_mentions_future_phases(self) -> None:
        for phase in ("p4-c", "p4-d", "p4-e", "p4-f"):
            self.assertIn(phase, self.doc_lower)


if __name__ == "__main__":
    unittest.main()
