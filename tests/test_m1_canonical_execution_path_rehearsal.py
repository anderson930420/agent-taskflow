"""Tests for the repository-owned M1-C evidence writer."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.m1_canonical_execution_path_rehearsal import (
    M1CanonicalExecutionPathRehearsalRequest,
    run_m1_canonical_execution_path_rehearsal,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class M1CanonicalExecutionPathRehearsalTests(unittest.TestCase):
    def test_rehearsal_writes_authoritative_gate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "evidence" / "canonical-execution-path.json"
            payload = run_m1_canonical_execution_path_rehearsal(
                M1CanonicalExecutionPathRehearsalRequest(
                    repo_root=REPO_ROOT,
                    output_path=output,
                )
            )

            self.assertTrue(output.is_file())
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                payload,
            )
            self.assertEqual(
                payload["schema_version"],
                "m1_canonical_execution_path.v1",
            )
            self.assertEqual(payload["canonical_path"], "ExecutionEngine")
            self.assertTrue(payload["parity_test_passed"])
            self.assertTrue(payload["legacy_level2_rejected"])
            self.assertTrue(payload["merger_requires_canonical_attempt"])
            self.assertFalse(payload["production_db_mutated"])
            self.assertFalse(payload["real_executor_invoked"])
            self.assertTrue(all(payload["checks"].values()))

    def test_output_path_must_be_absolute_after_resolution(self) -> None:
        request = M1CanonicalExecutionPathRehearsalRequest(
            repo_root=REPO_ROOT,
            output_path=Path("relative-evidence.json"),
        )
        self.assertTrue(request.output_path.is_absolute())


if __name__ == "__main__":
    unittest.main()
