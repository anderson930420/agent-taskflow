from __future__ import annotations

import tomllib
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TestV031ReleaseMetadata(unittest.TestCase):
    def test_pyproject_version_matches_v031_release(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"]["version"], "0.3.1")

    def test_v031_release_notes_exist(self) -> None:
        path = ROOT / "docs" / "release-notes-v0.3.1.md"
        self.assertTrue(path.exists())

        text = path.read_text(encoding="utf-8")
        self.assertIn("v0.3.1", text)
        self.assertIn("post-v0.3.0 safety hotfix", text)
        self.assertIn("Claude Code", text)
        self.assertIn("stdin", text)
        self.assertIn("OSError", text)
        self.assertIn("human review remains required", text.lower())

    def test_v031_github_release_body_exists(self) -> None:
        path = ROOT / "docs" / "release-notes-v0.3.1-github-release-body.md"
        self.assertTrue(path.exists())

        text = path.read_text(encoding="utf-8")
        self.assertIn("Agent TaskFlow v0.3.1", text)
        self.assertIn("stdin", text)
        self.assertIn("OSError", text)
        self.assertIn("implementation evidence only", text)


if __name__ == "__main__":
    unittest.main()
