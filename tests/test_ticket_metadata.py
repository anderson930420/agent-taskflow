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
    TASK_KEY_PREFIX,
    derive_worktree_path,
    fallback_title_from_prompt,
    format_task_key,
    normalize_prompt,
    normalize_prompt_text,
    parse_task_key_sequence,
    shorten_title,
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


class NormalizePromptTextTests(unittest.TestCase):
    def test_keeps_lines_and_inner_spacing(self) -> None:
        self.assertEqual(
            normalize_prompt_text("Steps:\n1. Read  it\n   - indented item"),
            "Steps:\n1. Read  it\n   - indented item",
        )

    def test_converts_line_endings(self) -> None:
        self.assertEqual(normalize_prompt_text("a\r\nb\rc"), "a\nb\nc")

    def test_tabs_become_single_spaces(self) -> None:
        self.assertEqual(normalize_prompt_text("Fix\tthe\t\tbug"), "Fix the  bug")

    def test_drops_non_displayable_characters(self) -> None:
        self.assertEqual(
            normalize_prompt_text("Fix\x00 the\x07 bug\nnow\x1b"),
            "Fix the bug\nnow",
        )

    def test_strips_trailing_spaces_per_line(self) -> None:
        self.assertEqual(normalize_prompt_text("one   \ntwo \t"), "one\ntwo")

    def test_collapses_excess_blank_lines(self) -> None:
        self.assertEqual(
            normalize_prompt_text("first\n\n\n\n\nsecond\n\nthird"),
            "first\n\nsecond\n\nthird",
        )

    def test_whitespace_only_blank_lines_also_collapse(self) -> None:
        self.assertEqual(
            normalize_prompt_text("first\n  \n\t\n \nsecond"),
            "first\n\nsecond",
        )

    def test_strips_surrounding_whitespace(self) -> None:
        self.assertEqual(
            normalize_prompt_text("\n\n  Fix\tthe\n\nlogin flow  \n\n"),
            "Fix the\n\nlogin flow",
        )

    def test_empty_prompt_normalizes_to_empty(self) -> None:
        self.assertEqual(normalize_prompt_text("   \r\n\t  "), "")


class FallbackTitleTests(unittest.TestCase):
    """SPEC §10.1: normalized whitespace, shortened at a word boundary."""

    def test_short_prompt_is_used_verbatim(self) -> None:
        self.assertEqual(
            fallback_title_from_prompt("Separate ending-page image"),
            "Separate ending-page image",
        )

    def test_long_prompt_is_cut_at_a_word_boundary(self) -> None:
        prompt = "Separate the ending page image from the shared hero component please"
        title = fallback_title_from_prompt(prompt)
        self.assertEqual(title, "Separate the ending page image from the shared hero…")
        self.assertTrue(title.endswith("…"))
        self.assertLessEqual(len(title), TITLE_FALLBACK_MAX_CHARS)

    def test_truncation_counts_after_whitespace_normalization(self) -> None:
        prompt = "word   " * 40
        title = fallback_title_from_prompt(prompt)
        self.assertEqual(title, ("word " * 12).strip() + "…")
        self.assertLessEqual(len(title), TITLE_FALLBACK_MAX_CHARS)

    def test_trailing_punctuation_before_the_cut_is_removed(self) -> None:
        for mark in (",", ":"):
            with self.subTest(mark=mark):
                prompt = "x" * 50 + mark + " " + "y" * 20
                self.assertEqual(
                    fallback_title_from_prompt(prompt),
                    "x" * 50 + "…",
                )

    def test_single_overlong_word_is_cut_mid_word(self) -> None:
        title = fallback_title_from_prompt("x" * 80)
        self.assertEqual(title, "x" * 59 + "…")
        self.assertEqual(len(title), TITLE_FALLBACK_MAX_CHARS)

    def test_create_ticket_prompt_keeps_whole_words(self) -> None:
        prompt = (
            "Add a small command-line tool `scripts/create_ticket.py` that "
            "creates a Ticket through the running API, so an operator no "
            "longer needs curl."
        )
        self.assertEqual(
            fallback_title_from_prompt(prompt),
            "Add a small command-line tool `scripts/create_ticket.py`…",
        )

    def test_blank_prompt_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            fallback_title_from_prompt("   \n  ")


class ShortenTitleTests(unittest.TestCase):
    def test_short_text_is_unchanged(self) -> None:
        self.assertEqual(shorten_title("Fix the login flow"), "Fix the login flow")

    def test_text_of_exactly_max_chars_is_unchanged(self) -> None:
        text = "x" * TITLE_FALLBACK_MAX_CHARS
        self.assertEqual(shorten_title(text), text)

    def test_long_text_never_exceeds_max_chars(self) -> None:
        for length in range(55, 75):
            with self.subTest(length=length):
                text = ("ab " * 40)[:length]
                self.assertLessEqual(len(shorten_title(text)), TITLE_FALLBACK_MAX_CHARS)

    def test_whitespace_only_text_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            shorten_title(" \t\n ")


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


class TaskKeyTests(unittest.TestCase):
    """One global counter, zero-padded to 4 digits (PR #195 ruling)."""

    def test_task_key_is_zero_padded_to_four_digits(self) -> None:
        self.assertEqual(format_task_key(1), "AT-0001")
        self.assertEqual(format_task_key(98), "AT-0098")
        self.assertEqual(format_task_key(101), "AT-0101")

    def test_task_key_grows_past_the_padding_width(self) -> None:
        self.assertEqual(format_task_key(10000), "AT-10000")

    def test_prefix_is_fixed_not_per_repository(self) -> None:
        self.assertEqual(TASK_KEY_PREFIX, "AT")

    def test_rejects_non_positive_sequence(self) -> None:
        with self.assertRaises(ValueError):
            format_task_key(0)

    def test_parse_round_trips(self) -> None:
        self.assertEqual(parse_task_key_sequence("AT-0098"), 98)
        self.assertEqual(parse_task_key_sequence(format_task_key(42)), 42)
        for key in ("AT-GH-188", "AT-MC-SMOKE", "BJ-0001", "AT-abc"):
            with self.subTest(key=key):
                self.assertIsNone(parse_task_key_sequence(key))


class BranchNameTests(unittest.TestCase):
    def test_branch_matches_the_spec_example_shape(self) -> None:
        self.assertEqual(
            derive_branch_name("task/", "AT-0101", "separate ending image"),
            "task/AT-0101-separate-ending-image",
        )

    def test_ticket_id_is_embedded_so_identical_slugs_cannot_collide(self) -> None:
        first = derive_branch_name("task/", "AT-0001", "same slug")
        second = derive_branch_name("task/", "AT-0002", "same slug")
        self.assertNotEqual(first, second)

    def test_unsafe_branch_prefix_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            derive_branch_name("../evil/", "AT-0001", "slug")


class DerivedPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_worktree_path_is_worktrees_dir_plus_ticket_id(self) -> None:
        self.assertEqual(
            derive_worktree_path(self.root / "forms" / ".worktrees", "AT-0101"),
            self.root / "forms" / ".worktrees" / "AT-0101",
        )

    def test_artifact_dir_is_artifacts_root_plus_ticket_id(self) -> None:
        self.assertEqual(
            derive_artifact_dir(self.root / "artifacts", "AT-0101"),
            self.root / "artifacts" / "AT-0101",
        )

    def test_derivation_is_pure_and_repeatable(self) -> None:
        repo = repository(self.root)
        first = derive_ticket_metadata(repo, "AT-0101", "separate ending image")
        second = derive_ticket_metadata(repo, "AT-0101", "separate ending image")
        self.assertEqual(first, second)
        self.assertEqual(first.base_branch, "main")
        self.assertEqual(first.github_repo, "example/forms")
        self.assertEqual(first.branch, "task/AT-0101-separate-ending-image")

    def test_derivation_creates_nothing_on_disk(self) -> None:
        repo = repository(self.root)
        derived = derive_ticket_metadata(repo, "AT-0101", "slug")
        self.assertFalse(derived.worktree_path.exists())
        self.assertFalse(derived.artifact_dir.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
