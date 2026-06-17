"""Doc and metadata tests for the v0.2.1 release."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_NOTES = REPO_ROOT / "docs" / "release-notes-v0.2.1-github-release-body.md"


class TestV021ReleaseNotes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = RELEASE_NOTES.read_text(encoding="utf-8")
        cls.lower = cls.content.lower()

    def test_version_string(self) -> None:
        self.assertIn("v0.2.1", self.content)

    def test_codex_advisory_reviewer_scope(self) -> None:
        self.assertIn("Codex Advisory Reviewer", self.content)
        self.assertIn("dry-run contract", self.lower)

    def test_generated_artifacts(self) -> None:
        self.assertIn("codex-advisory-review-prompt.md", self.content)
        self.assertIn("codex-advisory-review.json", self.content)
        self.assertIn("codex-advisory-review.md", self.content)

    def test_packaged_entrypoint_and_script(self) -> None:
        self.assertIn("agent-taskflow-codex-advisory-review", self.content)
        self.assertIn("scripts/run_codex_advisory_review.py", self.content)

    def test_advisory_only_authority_boundary(self) -> None:
        self.assertIn("advisory-only", self.lower)
        self.assertIn("non-authoritative", self.lower)
        self.assertIn("validation_authority = false", self.content)
        self.assertIn("human_review_required = true", self.content)

    def test_no_runtime_authority_changes(self) -> None:
        for phrase in (
            "invoke Codex CLI",
            "invoke subprocesses",
            "change scheduler behavior",
            "change lifecycle transitions",
            "change approval authority",
            "change ExecutionEngine authority",
            "add Claude Code executor",
            "implement P5-f",
        ):
            self.assertIn(phrase, self.content)


if __name__ == "__main__":
    unittest.main()
