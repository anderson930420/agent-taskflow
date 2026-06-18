"""Doc and metadata tests for the v0.2.7 release."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
RELEASE_NOTES = REPO_ROOT / "docs" / "release-notes-v0.2.7-github-release-body.md"


class TestV027ReleaseMetadata(unittest.TestCase):
    def test_pyproject_version_matches_v027_release(self) -> None:
        with PYPROJECT.open("rb") as handle:
            pyproject = tomllib.load(handle)

        self.assertEqual(pyproject["project"]["version"], "0.2.7")


class TestV027ReleaseNotes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = RELEASE_NOTES.read_text(encoding="utf-8")
        cls.lower = cls.content.lower()

    def test_release_notes_exist(self) -> None:
        self.assertTrue(RELEASE_NOTES.exists())

    def test_version_string(self) -> None:
        self.assertIn("v0.2.7", self.content)

    def test_release_scope(self) -> None:
        self.assertIn("Claude Code Bounded Implementer Executor", self.content)
        self.assertIn("claude-code", self.content)

    def test_artifact_contract(self) -> None:
        for phrase in (
            "claude-code-implementer-prompt.md",
            "claude-code-execution.json",
            "claude_code_executor.v1",
        ):
            self.assertIn(phrase, self.content)

    def test_default_and_opt_in_invocation_semantics(self) -> None:
        self.assertIn("Default behavior is prompt-only / dry-run", self.content)
        self.assertIn("Real invocation is opt-in only", self.content)
        self.assertIn(
            "Real invocation requires explicit command configuration",
            self.content,
        )

    def test_real_invocation_execution_context_and_capture(self) -> None:
        self.assertIn(
            "configured command runs with `cwd` set to the prepared worktree",
            self.content,
        )
        for phrase in ("stdout", "stderr", "exit code", "timeout"):
            self.assertIn(phrase, self.lower)

    def test_authority_boundaries(self) -> None:
        self.assertIn("Claude Code has no validation authority", self.content)
        self.assertIn("Claude Code has no approval authority", self.content)
        self.assertIn("Claude Code has no merge authority", self.content)
        self.assertIn("Claude Code has no cleanup authority", self.content)
        self.assertIn("Human final review remains required", self.content)

    def test_validator_and_codex_advisory_flow(self) -> None:
        self.assertIn(
            "The runner still executes deterministic validators after the executor",
            self.content,
        )
        self.assertIn(
            "Codex advisory artifact contract validator still runs after "
            "deterministic validators",
            self.content,
        )
        self.assertIn(
            "Codex advisory evidence gate remains authoritative before "
            "`waiting_approval`",
            self.content,
        )


if __name__ == "__main__":
    unittest.main()
