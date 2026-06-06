"""Test that p2-architecture-checkpoint.md documents the local self-dogfood chain."""

import pathlib
import unittest

DOCS_FILE = pathlib.Path(__file__).parent.parent / "docs" / "p2-architecture-checkpoint.md"

REQUIRED_STRINGS = [
    "Task Execution Package",
    "queued-task handoff",
    "waiting_approval",
    "no auto-push",
    "no auto-PR",
    "no auto-merge",
    "no auto-cleanup",
]


class TestLocalSelfDogfoodChainDoc(unittest.TestCase):
    def test_docs_contains_required_strings(self):
        self.assertTrue(
            DOCS_FILE.exists(),
            f"docs file not found: {DOCS_FILE}",
        )
        content = DOCS_FILE.read_text()
        for required in REQUIRED_STRINGS:
            self.assertIn(
                required,
                content,
                f"docs file missing required string: {required!r}",
            )

    def test_docs_contains_section_12(self):
        content = DOCS_FILE.read_text()
        self.assertIn("## 12. Local Self-Dogfood Chain", content)

    def test_docs_describes_package_before_handoff(self):
        content = DOCS_FILE.read_text()
        self.assertIn("implementation_prompt.md", content)
        self.assertIn("task_execution_package.json", content)
        self.assertIn("exists before the\n  queued-task handoff starts the runner", content)


if __name__ == "__main__":
    unittest.main()