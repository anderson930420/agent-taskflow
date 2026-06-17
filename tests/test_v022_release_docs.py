"""Doc and metadata tests for the v0.2.2 release."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
RELEASE_NOTES = REPO_ROOT / "docs" / "release-notes-v0.2.2-github-release-body.md"


class TestV022ReleaseMetadata(unittest.TestCase):
    def test_pyproject_version_matches_v022_release(self) -> None:
        with PYPROJECT.open("rb") as handle:
            pyproject = tomllib.load(handle)

        self.assertEqual(pyproject["project"]["version"], "0.2.2")


class TestV022ReleaseNotes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = RELEASE_NOTES.read_text(encoding="utf-8")
        cls.lower = cls.content.lower()

    def test_version_string(self) -> None:
        self.assertIn("v0.2.2", self.content)

    def test_confirm_run_scope(self) -> None:
        self.assertIn("Codex Advisory Reviewer Confirm-Run Support", self.content)
        self.assertIn("--confirm-run", self.content)
        self.assertIn("--codex-command", self.content)
        self.assertIn("--timeout-seconds", self.content)

    def test_dry_run_default_is_preserved(self) -> None:
        self.assertIn("Dry-run remains the default", self.content)
        self.assertIn("invokes no subprocess", self.content)

    def test_confirm_run_artifacts(self) -> None:
        self.assertIn("codex-advisory-review-prompt.md", self.content)
        self.assertIn("codex-advisory-review.json", self.content)
        self.assertIn("codex-advisory-review.md", self.content)
        self.assertIn("codex-advisory-review-stdout.txt", self.content)
        self.assertIn("codex-advisory-review-stderr.txt", self.content)

    def test_tool_error_fallbacks(self) -> None:
        for phrase in (
            "command not found",
            "timeout",
            "non-zero exit",
            "unparseable stdout",
            "invalid review status",
            "invalid risk level",
            "authority invariant violations",
        ):
            self.assertIn(phrase, self.lower)

    def test_advisory_only_authority_boundary(self) -> None:
        self.assertIn("advisory-only", self.lower)
        self.assertIn("non-authoritative", self.lower)
        self.assertIn("validation_authority = false", self.content)
        self.assertIn("human_review_required = true", self.content)

    def test_no_runtime_authority_changes(self) -> None:
        for phrase in (
            "scheduler behavior",
            "lifecycle transitions",
            "approval authority",
            "ExecutionEngine authority",
            "waiting_approval summary integration",
            "branch push",
            "PR creation",
            "merge behavior",
            "cleanup behavior",
            "branch deletion",
            "worktree deletion",
            "Claude Code executor integration",
            "P5-f",
        ):
            self.assertIn(phrase, self.content)


if __name__ == "__main__":
    unittest.main()
