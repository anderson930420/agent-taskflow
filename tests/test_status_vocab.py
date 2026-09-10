"""Status vocabulary bridge tests (SPEC §12.2 ruling).

Every value in `TASK_STATUSES` and every §12 display name must round-trip
through `agent_taskflow.status_vocab`. Display names round-trip exactly.
Legacy values round-trip to their canonical sibling, which for a canonical
value is itself; the alias table is asserted explicitly so drift is caught.
"""

from __future__ import annotations

import unittest
from collections import Counter

from agent_taskflow.models import TASK_STATUSES, validate_task_status
from agent_taskflow.status_vocab import (
    CANONICAL_PERSISTED_STATUSES,
    DISPLAY_STATUSES,
    DISPLAY_STATUS_SEQUENCE,
    DISPLAY_TO_PERSISTED,
    PERSISTED_ALIASES,
    PERSISTED_TO_DISPLAY,
    StatusVocabularyError,
    canonical_persisted_status,
    is_alias_status,
    persisted_statuses_for_display,
    to_display_status,
    to_persisted_status,
    unmapped_persisted_statuses,
)
from agent_taskflow.ticket_models import TICKET_STATUS_SEQUENCE


# The persisted vocabulary as it stood before the §12.2 ruling. Nothing here
# may disappear: the ruling is additive only.
PRE_RULING_TASK_STATUSES = frozenset(
    {
        "unknown",
        "created",
        "queued",
        "preparing",
        "implementing",
        "validating",
        "waiting_approval",
        "waiting_for_review",
        "blocked",
        "accepted",
        "rejected",
        "cleaned",
        "completed",
        "canceled",
        "archived",
        "backlog",
        "todo",
        "in_progress",
        "review",
        "done",
    }
)

# §12 names added to TASK_STATUSES additively. `blocked` is in the ruling's
# list but already existed, so it is asserted separately as pre-existing.
ADDED_PERSISTED_STATUSES = frozenset(
    {
        "paused",
        "needs_decision",
        "ready_for_integration",
        "integrating",
        "failed",
    }
)


class VocabularyShapeTests(unittest.TestCase):
    def test_display_vocabulary_is_exactly_spec_section_12(self) -> None:
        self.assertEqual(DISPLAY_STATUS_SEQUENCE, TICKET_STATUS_SEQUENCE)
        self.assertEqual(set(DISPLAY_TO_PERSISTED), DISPLAY_STATUSES)

    def test_ruling_is_additive_only(self) -> None:
        self.assertTrue(PRE_RULING_TASK_STATUSES.issubset(TASK_STATUSES))
        self.assertEqual(
            frozenset(TASK_STATUSES) - PRE_RULING_TASK_STATUSES,
            ADDED_PERSISTED_STATUSES,
        )

    def test_blocked_was_already_persisted_and_is_not_repurposed(self) -> None:
        self.assertIn("blocked", PRE_RULING_TASK_STATUSES)
        self.assertEqual(DISPLAY_TO_PERSISTED["blocked"], "blocked")
        self.assertEqual(PERSISTED_TO_DISPLAY["blocked"], "blocked")

    def test_added_statuses_validate(self) -> None:
        for status in ADDED_PERSISTED_STATUSES:
            with self.subTest(status=status):
                self.assertEqual(validate_task_status(status), status)


class FixedRulingTests(unittest.TestCase):
    """The five display -> persisted pairs fixed by the human ruling."""

    def test_fixed_mappings(self) -> None:
        self.assertEqual(to_persisted_status("ready"), "created")
        self.assertEqual(to_persisted_status("running"), "implementing")
        self.assertEqual(to_persisted_status("needs_review"), "waiting_for_review")
        self.assertEqual(to_persisted_status("completed"), "cleaned")
        self.assertEqual(to_persisted_status("cancelled"), "canceled")

    def test_persisted_spelling_of_cancelled_stays_legacy(self) -> None:
        self.assertIn("canceled", TASK_STATUSES)
        self.assertNotIn("cancelled", TASK_STATUSES)


class DisplayRoundTripTests(unittest.TestCase):
    """Every §12 display name round-trips exactly."""

    def test_every_display_name_round_trips(self) -> None:
        for display in DISPLAY_STATUS_SEQUENCE:
            with self.subTest(display=display):
                persisted = to_persisted_status(display)
                self.assertIn(persisted, TASK_STATUSES)
                self.assertEqual(to_display_status(persisted), display)

    def test_display_mapping_is_injective(self) -> None:
        persisted_values = list(DISPLAY_TO_PERSISTED.values())
        self.assertEqual(len(persisted_values), len(set(persisted_values)))
        self.assertEqual(len(persisted_values), len(DISPLAY_STATUS_SEQUENCE))

    def test_every_canonical_persisted_value_is_a_real_status(self) -> None:
        self.assertTrue(CANONICAL_PERSISTED_STATUSES.issubset(TASK_STATUSES))


class PersistedRoundTripTests(unittest.TestCase):
    """Every persisted value maps to a §12 name and back to a real status."""

    def test_mapping_is_total_over_task_statuses(self) -> None:
        self.assertEqual(unmapped_persisted_statuses(), frozenset())
        self.assertEqual(set(PERSISTED_TO_DISPLAY), set(TASK_STATUSES))

    def test_every_persisted_value_round_trips(self) -> None:
        for persisted in sorted(TASK_STATUSES):
            with self.subTest(persisted=persisted):
                display = to_display_status(persisted)
                self.assertIn(display, DISPLAY_STATUSES)

                canonical = canonical_persisted_status(persisted)
                self.assertIn(canonical, TASK_STATUSES)
                self.assertIn(canonical, CANONICAL_PERSISTED_STATUSES)
                self.assertEqual(to_display_status(canonical), display)

    def test_canonical_values_round_trip_to_themselves(self) -> None:
        for persisted in sorted(CANONICAL_PERSISTED_STATUSES):
            with self.subTest(persisted=persisted):
                self.assertEqual(canonical_persisted_status(persisted), persisted)
                self.assertFalse(is_alias_status(persisted))

    def test_canonicalization_is_idempotent(self) -> None:
        for persisted in sorted(TASK_STATUSES):
            with self.subTest(persisted=persisted):
                once = canonical_persisted_status(persisted)
                self.assertEqual(canonical_persisted_status(once), once)


class AliasTableTests(unittest.TestCase):
    """Aliases are declared, not accidental."""

    def test_alias_table_is_exact(self) -> None:
        self.assertEqual(
            PERSISTED_ALIASES,
            {
                # The repo's human review gate, alongside waiting_for_review.
                "waiting_approval": "waiting_for_review",
                # Operator approved the proof-of-work; §33.1 keeps an
                # approved-but-unmerged item at needs_review.
                "accepted": "waiting_for_review",
                # Human said no and must choose a disposition (§33.2).
                "rejected": "needs_decision",
                # Local state is not trustworthy: route to a human.
                "unknown": "needs_decision",
                # Terminal success spellings.
                "completed": "cleaned",
                "done": "cleaned",
                # Terminal abandon spelling.
                "archived": "canceled",
                # External Kanban mirror spellings.
                "backlog": "queued",
                "todo": "created",
                "in_progress": "implementing",
                "review": "waiting_for_review",
            },
        )

    def test_every_alias_is_a_real_status_that_is_not_canonical(self) -> None:
        for alias, canonical in PERSISTED_ALIASES.items():
            with self.subTest(alias=alias):
                self.assertIn(alias, TASK_STATUSES)
                self.assertIn(canonical, TASK_STATUSES)
                self.assertTrue(is_alias_status(alias))
                self.assertNotIn(alias, CANONICAL_PERSISTED_STATUSES)
                self.assertNotEqual(alias, canonical)

    def test_aliases_and_canonicals_partition_the_persisted_vocabulary(self) -> None:
        self.assertEqual(
            CANONICAL_PERSISTED_STATUSES | frozenset(PERSISTED_ALIASES),
            frozenset(TASK_STATUSES),
        )
        self.assertEqual(
            CANONICAL_PERSISTED_STATUSES & frozenset(PERSISTED_ALIASES),
            frozenset(),
        )


class SemanticIntentTests(unittest.TestCase):
    """The judgement calls, pinned so a later change is deliberate."""

    def test_dispatcher_completion_state_displays_as_needs_review(self) -> None:
        # dispatcher.py writes waiting_approval after executor + validators
        # pass, and the PR/branch/closeout gates all require it.
        self.assertEqual(to_display_status("waiting_approval"), "needs_review")

    def test_operator_approval_displays_as_needs_review(self) -> None:
        # api approve route writes accepted; approval implies no push, merge
        # or cleanup, and §33.1 keeps it at needs_review until merged.
        self.assertEqual(to_display_status("accepted"), "needs_review")

    def test_rejection_and_unknown_route_to_a_human_decision(self) -> None:
        self.assertEqual(to_display_status("rejected"), "needs_decision")
        self.assertEqual(to_display_status("unknown"), "needs_decision")

    def test_terminal_success_spellings_agree(self) -> None:
        for persisted in ("cleaned", "completed", "done"):
            with self.subTest(persisted=persisted):
                self.assertEqual(to_display_status(persisted), "completed")

    def test_terminal_abandon_spellings_agree(self) -> None:
        for persisted in ("canceled", "archived"):
            with self.subTest(persisted=persisted):
                self.assertEqual(to_display_status(persisted), "cancelled")


class DisplayFilterTests(unittest.TestCase):
    """Filtering stored rows by a display name must match every alias."""

    def test_needs_review_filter_matches_every_review_spelling(self) -> None:
        self.assertEqual(
            persisted_statuses_for_display("needs_review"),
            frozenset({"waiting_for_review", "waiting_approval", "accepted", "review"}),
        )

    def test_every_persisted_value_is_in_exactly_one_filter_set(self) -> None:
        seen: Counter[str] = Counter()
        for display in DISPLAY_STATUS_SEQUENCE:
            seen.update(persisted_statuses_for_display(display))
        self.assertEqual(set(seen), set(TASK_STATUSES))
        self.assertEqual(set(seen.values()), {1})

    def test_filter_set_always_contains_the_canonical_value(self) -> None:
        for display in DISPLAY_STATUS_SEQUENCE:
            with self.subTest(display=display):
                self.assertIn(
                    to_persisted_status(display),
                    persisted_statuses_for_display(display),
                )

    def test_unknown_display_name_is_rejected(self) -> None:
        with self.assertRaises(StatusVocabularyError):
            persisted_statuses_for_display("created")


class ErrorTests(unittest.TestCase):
    def test_unknown_display_name_is_rejected(self) -> None:
        with self.assertRaises(StatusVocabularyError):
            to_persisted_status("not-a-status")
        # A persisted-only spelling is not a display name.
        with self.assertRaises(StatusVocabularyError):
            to_persisted_status("waiting_approval")

    def test_unknown_persisted_value_is_rejected(self) -> None:
        with self.assertRaises(StatusVocabularyError):
            to_display_status("not-a-status")
        # A display-only spelling is not a persisted value.
        with self.assertRaises(StatusVocabularyError):
            to_display_status("cancelled")

    def test_blank_input_is_rejected(self) -> None:
        for blank in ("", "   "):
            with self.subTest(blank=blank):
                with self.assertRaises(ValueError):
                    to_persisted_status(blank)
                with self.assertRaises(ValueError):
                    to_display_status(blank)

    def test_errors_are_value_errors(self) -> None:
        self.assertTrue(issubclass(StatusVocabularyError, ValueError))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
