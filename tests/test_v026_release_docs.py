"""Doc and metadata tests for the v0.2.6 release."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
RELEASE_NOTES = REPO_ROOT / "docs" / "release-notes-v0.2.6-github-release-body.md"

CHECKLIST_AREAS = (
    "architecture_boundary",
    "design_risk",
    "test_quality",
    "silent_failure",
    "fallback_correctness",
    "race_concurrency",
    "path_cwd_repo_root",
    "human_review_priority",
)


class TestV026ReleaseMetadata(unittest.TestCase):
    def test_pyproject_version_matches_v026_release(self) -> None:
        with PYPROJECT.open("rb") as handle:
            pyproject = tomllib.load(handle)

        self.assertEqual(pyproject["project"]["version"], "0.2.6")


class TestV026ReleaseNotes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = RELEASE_NOTES.read_text(encoding="utf-8")
        cls.lower = cls.content.lower()

    def test_release_notes_exist(self) -> None:
        self.assertTrue(RELEASE_NOTES.exists())

    def test_version_string(self) -> None:
        self.assertIn("v0.2.6", self.content)

    def test_release_scope(self) -> None:
        self.assertIn("Codex Advisory Review Checklist Hardening", self.content)

    def test_structured_checklist_required(self) -> None:
        self.assertIn("review_checklist", self.content)
        self.assertIn("structured", self.lower)

    def test_human_review_priorities_required(self) -> None:
        self.assertIn("human_review_priorities", self.content)
        self.assertIn("non-empty", self.lower)

    def test_all_checklist_areas_present(self) -> None:
        for area in CHECKLIST_AREAS:
            self.assertIn(area, self.content)

    def test_all_checklist_statuses_present(self) -> None:
        for status in ("pass", "concern", "not_applicable", "unknown"):
            self.assertIn(status, self.content)

    def test_statuses_are_advisory_not_automatic_blockers(self) -> None:
        self.assertIn("advisory", self.lower)
        self.assertIn(
            "Checklist statuses are advisory evidence and do not automatically block",
            self.content,
        )
        self.assertIn(
            "`concern`, `unknown`, and `not_applicable` are advisory evidence and "
            "do not block",
            self.content,
        )

    def test_contract_invalid_cases_are_documented(self) -> None:
        for phrase in ("missing", "malformed", "incomplete", "empty"):
            self.assertIn(phrase, self.lower)

    def test_evidence_gate_inheritance_is_documented(self) -> None:
        self.assertIn("v0.2.5", self.content)
        self.assertIn("delegates", self.lower)
        self.assertIn("codex advisory artifact contract", self.lower)
        self.assertIn("validator", self.lower)

    def test_safety_boundary_is_documented(self) -> None:
        self.assertIn("Codex still has no approval authority", self.content)
        self.assertIn("Codex still has no validator authority", self.content)
        self.assertIn("Human final review is still required", self.content)


if __name__ == "__main__":
    unittest.main()
