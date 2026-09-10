"""Tests for agent_taskflow.integration_schema (V1 Step 2, spec §32.1)."""

from __future__ import annotations

import unittest

from agent_taskflow import integration_schema as schema
from agent_taskflow.models import TASK_STATUSES
from agent_taskflow.status_vocab import to_display_status, to_persisted_status


class TicketPrFieldListTests(unittest.TestCase):
    """§32.1 is the authoritative field list: names, types, enums, defaults."""

    def test_field_names_match_spec_exactly(self) -> None:
        self.assertEqual(
            schema.TICKET_PR_FIELD_NAMES,
            (
                "pr_number",
                "pr_url",
                "pr_state",
                "pr_merged",
                "pr_head_sha",
                "merge_commit_sha",
                "review_decision",
                "ci_status",
                "integrated_base_sha",
                "reintegration_count",
                "reintegration_required",
                "pr_last_polled_at",
            ),
        )

    def test_declared_types_and_nullability_match_spec(self) -> None:
        by_name = {spec.name: spec for spec in schema.TICKET_PR_FIELDS}
        self.assertEqual(by_name["pr_number"].python_type, "int")
        self.assertTrue(by_name["pr_number"].nullable)
        self.assertEqual(by_name["pr_merged"].python_type, "bool")
        self.assertFalse(by_name["pr_merged"].nullable)
        self.assertIs(by_name["pr_merged"].default, False)
        self.assertEqual(by_name["reintegration_count"].python_type, "int")
        self.assertEqual(by_name["reintegration_count"].default, 0)
        self.assertIs(by_name["reintegration_required"].default, False)
        self.assertEqual(by_name["pr_last_polled_at"].python_type, "datetime")

    def test_enum_values_match_spec(self) -> None:
        by_name = {spec.name: spec for spec in schema.TICKET_PR_FIELDS}
        self.assertEqual(by_name["pr_state"].enum, ("open", "closed"))
        self.assertEqual(
            by_name["review_decision"].enum,
            ("none", "approved", "changes_requested"),
        )
        self.assertEqual(
            by_name["ci_status"].enum,
            ("none", "pending", "success", "failure"),
        )

    def test_defaults_tolerate_null(self) -> None:
        defaults = schema.default_pr_state()
        self.assertIsNone(defaults["pr_number"])
        self.assertIsNone(defaults["pr_state"])
        self.assertIs(defaults["pr_merged"], False)
        self.assertEqual(defaults["reintegration_count"], 0)
        self.assertIs(defaults["reintegration_required"], False)

    def test_unknown_field_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            schema.validate_pr_state({"pr_squashed": True})

    def test_invalid_enum_value_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            schema.validate_pr_state({"pr_state": "merged"})
        with self.assertRaises(ValueError):
            schema.validate_pr_state({"review_decision": "CHANGES_REQUESTED"})

    def test_valid_values_round_trip(self) -> None:
        values = schema.validate_pr_state(
            {
                "pr_number": 42,
                "pr_state": "open",
                "pr_merged": False,
                "review_decision": "changes_requested",
                "ci_status": "failure",
            }
        )
        self.assertEqual(values["pr_number"], 42)
        self.assertEqual(values["ci_status"], "failure")


class LifecycleStatusTests(unittest.TestCase):
    def test_step2_statuses_are_registered_in_the_store_status_set(self) -> None:
        for status in (
            schema.READY_FOR_INTEGRATION,
            schema.INTEGRATING,
            schema.NEEDS_REVIEW,
            schema.NEEDS_DECISION,
            schema.CANCELLED,
            schema.COMPLETED,
        ):
            self.assertIn(status, TASK_STATUSES)

    def test_step2_constants_hold_the_persisted_spelling(self) -> None:
        """§12.2 — §12 names are display; TASK_STATUSES stays canonical."""
        self.assertEqual(schema.READY_FOR_INTEGRATION, "ready_for_integration")
        self.assertEqual(schema.INTEGRATING, "integrating")
        self.assertEqual(schema.NEEDS_DECISION, "needs_decision")
        # The four that are deliberately not identity.
        self.assertEqual(schema.NEEDS_REVIEW, "waiting_for_review")
        self.assertEqual(schema.CANCELLED, "canceled")
        self.assertEqual(schema.COMPLETED, "cleaned")

    def test_the_two_cancelled_spellings_never_coexist_in_the_enum(self) -> None:
        """§12.2 — one persisted spelling per idea."""
        self.assertIn("canceled", TASK_STATUSES)
        self.assertNotIn("cancelled", TASK_STATUSES)
        self.assertIn("waiting_for_review", TASK_STATUSES)
        self.assertNotIn("needs_review", TASK_STATUSES)

    def test_constants_are_resolved_through_status_vocab_not_duplicated(self) -> None:
        for display, constant in (
            ("ready_for_integration", schema.READY_FOR_INTEGRATION),
            ("integrating", schema.INTEGRATING),
            ("needs_review", schema.NEEDS_REVIEW),
            ("needs_decision", schema.NEEDS_DECISION),
            ("cancelled", schema.CANCELLED),
            ("completed", schema.COMPLETED),
        ):
            with self.subTest(display=display):
                self.assertEqual(to_persisted_status(display), constant)
                self.assertEqual(to_display_status(constant), display)

    def test_allowed_integration_transitions(self) -> None:
        allowed = [
            (schema.READY_FOR_INTEGRATION, schema.INTEGRATING),
            (schema.INTEGRATING, schema.READY_FOR_INTEGRATION),
            (schema.INTEGRATING, schema.NEEDS_REVIEW),
            (schema.INTEGRATING, schema.NEEDS_DECISION),
            (schema.NEEDS_REVIEW, schema.READY_FOR_INTEGRATION),
            (schema.NEEDS_REVIEW, schema.NEEDS_DECISION),
            (schema.NEEDS_REVIEW, schema.CANCELLED),
            (schema.NEEDS_REVIEW, schema.COMPLETED),
        ]
        for current, target in allowed:
            schema.validate_transition(current, target)

    def test_integration_cannot_jump_straight_to_completed(self) -> None:
        with self.assertRaises(ValueError):
            schema.validate_transition(schema.INTEGRATING, schema.COMPLETED)

    def test_needs_decision_cannot_be_self_approved_into_needs_review(self) -> None:
        with self.assertRaises(ValueError):
            schema.validate_transition(schema.NEEDS_DECISION, schema.NEEDS_REVIEW)


if __name__ == "__main__":
    unittest.main()
