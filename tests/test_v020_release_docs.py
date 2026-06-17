"""Doc and metadata tests for the v0.2.0 release."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_NOTES = REPO_ROOT / "docs" / "release-notes-v0.2.0.md"


class TestV020ReleaseNotes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = RELEASE_NOTES.read_text(encoding="utf-8")
        cls.lower = cls.content.lower()

    def test_version_string(self) -> None:
        self.assertIn("v0.2.0", self.content)

    def test_scheduled_one_task_automation(self) -> None:
        self.assertIn("Scheduled One-Task Automation", self.content)
        self.assertIn("one-task scheduler tick", self.lower)

    def test_observability_summary(self) -> None:
        self.assertIn("Observability", self.content)
        self.assertIn("structured summaries", self.lower)

    def test_execution_engine_scope(self) -> None:
        self.assertIn("ExecutionEngine", self.content)
        self.assertIn("legacy scheduler path remains authoritative", self.lower)

    def test_packaging_and_cli_namespace(self) -> None:
        self.assertIn("Python Packaging and CLI Namespace Stabilization", self.content)
        self.assertIn("agent_taskflow.cli", self.content)
        self.assertIn("agent-taskflow-local-validation", self.content)

    def test_local_validation_guard(self) -> None:
        self.assertIn("Local validation guard", self.content)
        self.assertIn("repository checkout", self.lower)

    def test_validation_status_lists_latest_suite(self) -> None:
        self.assertIn("3723 tests passed", self.content)
        self.assertIn("compileall", self.lower)


if __name__ == "__main__":
    unittest.main()
