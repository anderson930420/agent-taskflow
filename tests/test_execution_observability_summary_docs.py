"""Documentation tests for the P4-e unified execution observability summary."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "execution-observability-summary.md"


class ExecutionObservabilitySummaryDocTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = DOC.read_text(encoding="utf-8")
        cls.doc_lower = cls.doc.lower()
        # Collapse markdown line-wrapping so phrase checks survive wrapping.
        cls.doc_normalized = re.sub(r"\s+", " ", cls.doc_lower)

    def test_doc_exists(self) -> None:
        self.assertTrue(DOC.is_file())

    def test_mentions_unified_execution_summary(self) -> None:
        self.assertIn("unified execution summary", self.doc_lower)

    def test_mentions_read_only_normalization(self) -> None:
        self.assertIn("read-only normalization", self.doc_lower)

    def test_mentions_execution_engine_result(self) -> None:
        self.assertIn("ExecutionEngineResult", self.doc)

    def test_mentions_approved_task_runner_payload(self) -> None:
        self.assertIn("approved_task_runner", self.doc)
        self.assertIn("payload", self.doc_lower)

    def test_mentions_scheduler_tick_payload(self) -> None:
        self.assertIn("scheduler tick payload", self.doc_normalized)

    def test_says_no_live_scheduler_migration(self) -> None:
        self.assertIn("no live scheduler migration", self.doc_normalized)

    def test_says_no_cron_change(self) -> None:
        self.assertIn("no cron change", self.doc_normalized)

    def test_mentions_mission_control_future_consumer(self) -> None:
        self.assertIn("mission control", self.doc_normalized)
        self.assertIn("future use", self.doc_normalized)

    def test_mentions_safety_defaults(self) -> None:
        self.assertIn("safety defaults", self.doc_normalized)
        self.assertIn("conservative", self.doc_normalized)

    def test_says_no_behavior_change(self) -> None:
        self.assertIn("no behavior change", self.doc_normalized)

    def test_mentions_schema_version(self) -> None:
        self.assertIn("execution_observability_summary.v1", self.doc)


if __name__ == "__main__":
    unittest.main()
