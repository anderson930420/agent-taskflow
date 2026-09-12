"""Tests for agent_taskflow.reviewer_hints (spec §26.1, §38)."""

from __future__ import annotations

import unittest

from agent_taskflow.reviewer_hints import (
    HINT_AI_CONFLICT_RESOLUTION,
    HINT_LARGE_DIFF,
    HINT_MIGRATION_CHANGED,
    HINT_OVERLAPPING_FILES,
    HINT_REINTEGRATED,
    HINT_REINTEGRATED_REPEATEDLY,
    HINT_SIGNIFICANT_BEHIND_COUNT,
    HINT_VALIDATOR_RESULT_CHANGED,
    build_reviewer_hints,
    render_hints_markdown,
    render_reintegration_hint_block,
)


class HintGenerationTests(unittest.TestCase):
    def test_no_hints_on_a_clean_first_integration(self) -> None:
        self.assertEqual(build_reviewer_hints(), [])

    def test_ai_conflict_resolution_hint(self) -> None:
        codes = [hint.code for hint in build_reviewer_hints(ai_resolved_conflict=True)]
        self.assertIn(HINT_AI_CONFLICT_RESOLUTION, codes)

    def test_reintegration_hints_escalate_with_count(self) -> None:
        once = [hint.code for hint in build_reviewer_hints(reintegration_count=1)]
        self.assertIn(HINT_REINTEGRATED, once)
        self.assertNotIn(HINT_REINTEGRATED_REPEATEDLY, once)

        thrice = [hint.code for hint in build_reviewer_hints(reintegration_count=3)]
        self.assertIn(HINT_REINTEGRATED_REPEATEDLY, thrice)
        self.assertIn("3", next(h.message for h in build_reviewer_hints(reintegration_count=3) if h.code == HINT_REINTEGRATED_REPEATEDLY))

    def test_overlapping_files_hint(self) -> None:
        hints = build_reviewer_hints(overlapping_files=("app/models.py",))
        self.assertIn(HINT_OVERLAPPING_FILES, [h.code for h in hints])
        self.assertIn("app/models.py", next(h.message for h in hints))

    def test_behind_count_and_large_diff_hints(self) -> None:
        codes = [h.code for h in build_reviewer_hints(behind_count=42, changed_file_count=80)]
        self.assertIn(HINT_SIGNIFICANT_BEHIND_COUNT, codes)
        self.assertIn(HINT_LARGE_DIFF, codes)

    def test_small_diff_and_small_behind_count_produce_no_hint(self) -> None:
        codes = [h.code for h in build_reviewer_hints(behind_count=1, changed_file_count=2)]
        self.assertNotIn(HINT_SIGNIFICANT_BEHIND_COUNT, codes)
        self.assertNotIn(HINT_LARGE_DIFF, codes)

    def test_migration_and_validator_change_hints(self) -> None:
        codes = [
            h.code
            for h in build_reviewer_hints(
                changed_files=("migrations/0007_add_column.py",), validator_result_changed=True
            )
        ]
        self.assertIn(HINT_MIGRATION_CHANGED, codes)
        self.assertIn(HINT_VALIDATOR_RESULT_CHANGED, codes)

    def test_hints_are_attention_routing_not_decisions(self) -> None:
        """§38 — hints must never carry a verdict field."""
        for hint in build_reviewer_hints(ai_resolved_conflict=True, reintegration_count=3):
            self.assertFalse(hasattr(hint, "approved"))
            self.assertFalse(hasattr(hint, "blocking"))
            self.assertFalse(hasattr(hint, "verdict"))


class RenderingTests(unittest.TestCase):
    def test_reintegration_block_matches_the_spec_shape(self) -> None:
        block = render_reintegration_hint_block(
            previous_base_sha="aaa111",
            current_base_sha="bbb222",
            trigger="AT-123 merged before this PR was reviewed.",
        )
        self.assertIn("Re-integrated after target branch advanced.", block)
        self.assertIn("Previous base:", block)
        self.assertIn("aaa111", block)
        self.assertIn("Current base:", block)
        self.assertIn("bbb222", block)
        self.assertIn("Trigger:", block)
        self.assertIn("AT-123", block)

    def test_markdown_rendering_lists_every_hint(self) -> None:
        hints = build_reviewer_hints(ai_resolved_conflict=True, reintegration_count=2)
        markdown = render_hints_markdown(hints)
        for hint in hints:
            self.assertIn(hint.message, markdown)

    def test_empty_hints_render_to_empty_string(self) -> None:
        self.assertEqual(render_hints_markdown([]), "")


if __name__ == "__main__":
    unittest.main()
