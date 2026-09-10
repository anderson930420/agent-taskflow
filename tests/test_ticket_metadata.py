"""Deterministic Ticket metadata derivation tests (SPEC §10.1, §9)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_taskflow.ticket_metadata import (
    BRANCH_SLUG_MAX_CHARS,
    TITLE_FALLBACK_MAX_CHARS,
    derive_artifact_dir,
    derive_branch_name,
    derive_ticket_metadata,
    derive_worktree_path,
    fallback_title_from_prompt,
    format_ticket_id,
    normalize_prompt,
    parse_ticket_sequence,
    slugify_branch_component,
)
from agent_taskflow.ticket_repositories import TicketRepository


def repository(root: Path) -> TicketRepository:
    return TicketRepository(
        repository="forms",
        repo_path=root / "forms",
        worktrees_dir=root / "forms" / ".worktrees",
        artifacts_root=root / "artifacts",
        base_branch="main",
        branch_prefix="task/",
        ticket_prefix="AT",
        github_repo="example/forms",
    )


class NormalizePromptTests(unittest.TestCase):
    def test_collapses_every_kind_of_whitespace(self) -> None:
        self.assertEqual(
            normalize_prompt("  Separate\tthe\nending   page\r\nimage  "),
            "Separate the ending page image",
        )

    def test_drops_non_displayable_characters(self) -> None:
        self.assertEqual(normalize_prompt("Fix\x00 the\x07 bug"), "Fix the bug")

    def test_empty_prompt_normalizes_to_empty(self) -> None:
        self.assertEqual(normalize_prompt("   \n\t  "), "")


class FallbackTitleTests(unittest.TestCase):
    """SPEC §10.1: normalized whitespace, first 60 displayable characters."""

    def test_short_prompt_is_used_verbatim(self) -> None:
        self.assertEqual(
            fallback_title_from_prompt("Separate ending-page image"),
            "Separate ending-page image",
        )

    def test_long_prompt_is_truncated_to_sixty_characters(self) -> None:
        prompt = "x" * 200
        title = fallback_title_from_prompt(prompt)
        self.assertEqual(len(title), TITLE_FALLBACK_MAX_CHARS)
        self.assertEqual(title, "x" * TITLE_FALLBACK_MAX_CHARS)

    def test_truncation_counts_after_whitespace_normalization(self) -> None:
        prompt = "word   " * 40
        self.assertEqual(
            fallback_title_from_prompt(prompt),
            ("word " * 40).strip()[:TITLE_FALLBACK_MAX_CHARS],
        )

    def test_blank_prompt_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            fallback_title_from_prompt("   \n  ")


class BranchSlugTests(unittest.TestCase):
    def test_slug_is_lowercase_and_hyphenated(self) -> None:
        self.assertEqual(
            slugify_branch_component("Separate Ending-Page Image!"),
            "separate-ending-page-image",
        )

    def test_slug_is_bounded(self) -> None:
        slug = slugify_branch_component("alpha " * 40)
        self.assertLessEqual(len(slug), BRANCH_SLUG_MAX_CHARS)
        self.assertFalse(slug.endswith("-"))

    def test_unusable_slug_falls_back(self) -> None:
        self.assertEqual(slugify_branch_component("!!! ???"), "ticket")


class TicketIdTests(unittest.TestCase):
    def test_ticket_id_is_zero_padded(self) -> None:
        self.assertEqual(format_ticket_id("AT", 1), "AT-001")
        self.assertEqual(format_ticket_id("AT", 98), "AT-098")
        self.assertEqual(format_ticket_id("AT", 101), "AT-101")

    def test_ticket_id_grows_past_the_padding_width(self) -> None:
        self.assertEqual(format_ticket_id("AT", 1000), "AT-1000")

    def test_rejects_bad_prefix_and_sequence(self) -> None:
        with self.assertRaises(ValueError):
            format_ticket_id("AT/", 1)
        with self.assertRaises(ValueError):
            format_ticket_id("AT", 0)

    def test_parse_round_trips(self) -> None:
        self.assertEqual(parse_ticket_sequence("AT-098", "AT"), 98)
        self.assertIsNone(parse_ticket_sequence("BJ-098", "AT"))
        self.assertIsNone(parse_ticket_sequence("AT-GH-188", "AT"))
        self.assertIsNone(parse_ticket_sequence("AT-abc", "AT"))


class BranchNameTests(unittest.TestCase):
    def test_branch_matches_the_spec_example_shape(self) -> None:
        self.assertEqual(
            derive_branch_name("task/", "AT-101", "separate ending image"),
            "task/AT-101-separate-ending-image",
        )

    def test_ticket_id_is_embedded_so_identical_slugs_cannot_collide(self) -> None:
        first = derive_branch_name("task/", "AT-001", "same slug")
        second = derive_branch_name("task/", "AT-002", "same slug")
        self.assertNotEqual(first, second)

    def test_unsafe_branch_prefix_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            derive_branch_name("../evil/", "AT-001", "slug")


class DerivedPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_worktree_path_is_worktrees_dir_plus_ticket_id(self) -> None:
        self.assertEqual(
            derive_worktree_path(self.root / "forms" / ".worktrees", "AT-101"),
            self.root / "forms" / ".worktrees" / "AT-101",
        )

    def test_artifact_dir_is_artifacts_root_plus_ticket_id(self) -> None:
        self.assertEqual(
            derive_artifact_dir(self.root / "artifacts", "AT-101"),
            self.root / "artifacts" / "AT-101",
        )

    def test_derivation_is_pure_and_repeatable(self) -> None:
        repo = repository(self.root)
        first = derive_ticket_metadata(repo, "AT-101", "separate ending image")
        second = derive_ticket_metadata(repo, "AT-101", "separate ending image")
        self.assertEqual(first, second)
        self.assertEqual(first.base_branch, "main")
        self.assertEqual(first.github_repo, "example/forms")
        self.assertEqual(first.branch, "task/AT-101-separate-ending-image")

    def test_derivation_creates_nothing_on_disk(self) -> None:
        repo = repository(self.root)
        derived = derive_ticket_metadata(repo, "AT-101", "slug")
        self.assertFalse(derived.worktree_path.exists())
        self.assertFalse(derived.artifact_dir.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
