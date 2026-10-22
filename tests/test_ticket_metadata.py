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


class FallbackTitleTests(unittest.TestCase):
    """First non-empty line, normalized, at most 60 characters at a word boundary."""

    def test_short_prompt_is_used_verbatim(self) -> None:
        self.assertEqual(
            fallback_title_from_prompt("Separate ending-page image"),
            "Separate ending-page image",
        )

    def test_long_single_word_is_cut_mid_word_with_ellipsis(self) -> None:
        prompt = "x" * 80
        title = fallback_title_from_prompt(prompt)
        self.assertEqual(len(title), TITLE_FALLBACK_MAX_CHARS)
        self.assertEqual(title, "x" * (TITLE_FALLBACK_MAX_CHARS - 1) + "…")

    def test_truncation_counts_after_whitespace_normalization(self) -> None:
        prompt = "word   " * 40
        self.assertEqual(
            fallback_title_from_prompt(prompt),
            ("word " * 12).strip() + "…",
        )

    def test_long_prompt_is_cut_at_a_word_boundary(self) -> None:
        prompt = "Rework the scheduler so that every worker reports its heartbeat"
        title = fallback_title_from_prompt(prompt)
        self.assertLessEqual(len(title), TITLE_FALLBACK_MAX_CHARS)
        self.assertEqual(
            title,
            "Rework the scheduler so that every worker reports its…",
        )

    def test_trailing_punctuation_is_removed_before_ellipsis(self) -> None:
        self.assertEqual(
            fallback_title_from_prompt(
                "Rework the scheduler so that every worker reports, heartbeats"
            ),
            "Rework the scheduler so that every worker reports…",
        )
        self.assertEqual(
            fallback_title_from_prompt(
                "Rework the scheduler so that every worker reports these:"
                " heartbeat"
            ),
            "Rework the scheduler so that every worker reports these…",
        )

    def test_only_the_first_non_empty_line_is_used(self) -> None:
        prompt = "\n   \nFix the login redirect\n\nDetails follow on later lines."
        self.assertEqual(
            fallback_title_from_prompt(prompt),
            "Fix the login redirect",
        )

    def test_create_ticket_prompt_title(self) -> None:
        prompt = (
            "Add a small command-line tool `scripts/create_ticket.py` that"
            " creates a Ticket through the running API, so an operator no"
            " longer needs curl."
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
        self.assertEqual(shorten_title("Fix  the\tbug"), "Fix the bug")

    def test_result_never_exceeds_max_chars(self) -> None:
        title = shorten_title("alpha beta gamma delta", max_chars=12)
        self.assertEqual(title, "alpha beta…")
        self.assertLessEqual(len(title), 12)

    def test_blank_text_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            shorten_title(" \x00 ")


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
