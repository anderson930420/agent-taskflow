"""Historical doc tests for the v0.2.8 release.

These tests are historical: they verify that the v0.2.8 release notes still
exist and carry the important v0.2.8 content. They no longer assert that
``pyproject.toml`` is currently ``0.2.8`` -- the current version is tracked by
the latest release-doc test (see ``tests/test_v029_release_docs.py``).
"""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_NOTES = REPO_ROOT / "docs" / "release-notes-v0.2.8-github-release-body.md"


class TestV028ReleaseNotes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = RELEASE_NOTES.read_text(encoding="utf-8")
        cls.lower = cls.content.lower()

    def test_release_notes_exist(self) -> None:
        self.assertTrue(RELEASE_NOTES.exists())

    def test_version_string(self) -> None:
        self.assertIn("v0.2.8", self.content)

    def test_release_scope(self) -> None:
        self.assertIn("Claude Code Opt-in Real Invocation Profile", self.content)
        self.assertIn("claude-code", self.content)

    def test_cli_flags_documented(self) -> None:
        self.assertIn("--claude-code-enable-invocation", self.content)
        self.assertIn("--claude-code-command-json", self.content)
        self.assertIn("--claude-code-timeout-seconds", self.content)

    def test_default_and_opt_in_semantics(self) -> None:
        self.assertIn("Dry-run / prompt-only remains the default", self.content)
        self.assertIn("Real invocation requires explicit enable flag", self.content)
        self.assertIn(
            "Real invocation requires\nexplicit command argv JSON",
            self.content,
        )

    def test_command_parsing_documented(self) -> None:
        self.assertIn("parsed as a JSON array of strings", self.content)
        self.assertIn("The command is\npassed as argv", self.content)
        self.assertIn("no shell parsing and no `shell=True`", self.content)

    def test_execution_context_and_capture(self) -> None:
        self.assertIn(
            "configured command runs with `cwd` set to\nthe prepared worktree",
            self.content,
        )
        self.assertIn("the timeout is enforced by the subprocess timeout", self.content)
        for phrase in ("stdout", "stderr", "exit code", "timeout"):
            self.assertIn(phrase, self.lower)

    def test_artifact_contract(self) -> None:
        for phrase in (
            "claude-code-implementer-prompt.md",
            "claude-code-execution.json",
            "claude-code-stdout.log",
            "claude-code-stderr.log",
            "claude_code_executor.v1",
        ):
            self.assertIn(phrase, self.content)

    def test_deterministic_safety_gates(self) -> None:
        self.assertIn(
            "A missing command with invocation enabled is blocked",
            self.content,
        )
        self.assertIn(
            "Claude Code options on non-`claude-code` executors are\nrejected",
            self.content,
        )
        self.assertIn(
            "still executes deterministic validators after the executor",
            self.content,
        )
        self.assertIn(
            "Codex advisory\nevidence gate remains authoritative before `waiting_approval`",
            self.content,
        )
        self.assertIn("Human final\nreview remains required", self.content)

    def test_authority_boundaries(self) -> None:
        self.assertIn("Claude Code has no validation authority", self.content)
        self.assertIn("Claude Code has no approval authority", self.content)
        self.assertIn("Claude Code has no merge authority", self.content)
        self.assertIn("Claude Code has no cleanup authority", self.content)


if __name__ == "__main__":
    unittest.main()
