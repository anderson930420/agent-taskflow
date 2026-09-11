"""Step 3 runtime progress vocabulary and no-estimate guards (SPEC §14).

These tests are the acceptance gate for the §14.1 first-level step vocabulary,
the §14.1 status vocabulary, and the §14.2 "no fake percentage" rule. They are
written against the extension of the existing ``ExecutionObservedStep`` shape;
Step 3 must not build a parallel progress engine.
"""

from __future__ import annotations

import unittest

from agent_taskflow.execution_observability import ExecutionObservedStep
from agent_taskflow.runtime_progress import (
    RUNTIME_PROGRESS_SCHEMA_VERSION,
    RUNTIME_STEPS,
    RUNTIME_STEP_LABELS,
    RUNTIME_STEP_STATUSES,
    RUNTIME_STEP_STATUS_BLOCKED,
    RUNTIME_STEP_STATUS_FAILED,
    RUNTIME_STEP_STATUS_PASSED,
    RUNTIME_STEP_STATUS_PENDING,
    RUNTIME_STEP_STATUS_RUNNING,
    AttemptProgressSnapshot,
    RuntimeProgressError,
    assert_no_progress_estimate,
    find_progress_estimates,
    observed_step,
    step_glyph,
    validate_runtime_step,
    validate_runtime_step_status,
)


class RuntimeStepVocabularyTests(unittest.TestCase):
    """§14.1 first-level steps and their five statuses."""

    def test_first_level_steps_match_spec_section_14_1(self) -> None:
        self.assertEqual(
            RUNTIME_STEPS,
            (
                "Prepare",
                "Scout",
                "Planner",
                "Implementer",
                "Reviewer",
                "Validator",
                "Integration",
            ),
        )

    def test_step_statuses_match_spec_section_14_1(self) -> None:
        self.assertEqual(
            RUNTIME_STEP_STATUSES,
            ("pending", "running", "passed", "failed", "blocked"),
        )
        self.assertEqual(RUNTIME_STEP_STATUS_PENDING, "pending")
        self.assertEqual(RUNTIME_STEP_STATUS_RUNNING, "running")
        self.assertEqual(RUNTIME_STEP_STATUS_PASSED, "passed")
        self.assertEqual(RUNTIME_STEP_STATUS_FAILED, "failed")
        self.assertEqual(RUNTIME_STEP_STATUS_BLOCKED, "blocked")

    def test_every_step_has_a_display_label(self) -> None:
        for step in RUNTIME_STEPS:
            with self.subTest(step=step):
                self.assertIn(step, RUNTIME_STEP_LABELS)
                self.assertTrue(RUNTIME_STEP_LABELS[step])

    def test_validate_runtime_step_is_case_insensitive_and_canonicalizes(self) -> None:
        self.assertEqual(validate_runtime_step("prepare"), "Prepare")
        self.assertEqual(validate_runtime_step("  IMPLEMENTER "), "Implementer")

    def test_validate_runtime_step_rejects_unknown_step(self) -> None:
        with self.assertRaises(RuntimeProgressError):
            validate_runtime_step("Deploy")

    def test_validate_runtime_step_status_rejects_unknown_status(self) -> None:
        self.assertEqual(validate_runtime_step_status(" PASSED "), "passed")
        with self.assertRaises(RuntimeProgressError):
            validate_runtime_step_status("skipped")

    def test_step_glyph_covers_every_status_and_never_uses_a_number(self) -> None:
        for status in RUNTIME_STEP_STATUSES:
            with self.subTest(status=status):
                glyph = step_glyph(status)
                self.assertTrue(glyph)
                self.assertFalse(any(char.isdigit() for char in glyph))
                self.assertNotIn("%", glyph)

    def test_schema_version_is_declared(self) -> None:
        self.assertEqual(RUNTIME_PROGRESS_SCHEMA_VERSION, "runtime_progress.v1")


class ObservedStepExtensionTests(unittest.TestCase):
    """Step 3 extends the existing ExecutionObservedStep (§14)."""

    def test_observed_step_returns_the_existing_dataclass(self) -> None:
        step = observed_step("Prepare", "running", summary="Creating worktree")
        self.assertIsInstance(step, ExecutionObservedStep)
        self.assertEqual(step.name, "Prepare")
        self.assertEqual(step.status, "running")
        self.assertEqual(step.summary, "Creating worktree")

    def test_observed_step_validates_name_and_status(self) -> None:
        with self.assertRaises(RuntimeProgressError):
            observed_step("Deploy", "running")
        with self.assertRaises(RuntimeProgressError):
            observed_step("Prepare", "skipped")

    def test_observed_step_rejects_a_progress_estimate_in_the_summary(self) -> None:
        with self.assertRaises(RuntimeProgressError):
            observed_step("Implementer", "running", summary="73% complete")

    def test_observed_step_rejects_a_progress_estimate_in_metadata(self) -> None:
        with self.assertRaises(RuntimeProgressError):
            observed_step(
                "Implementer",
                "running",
                metadata={"percent_complete": 73},
            )


class NoFakePercentageTests(unittest.TestCase):
    """§14.2 — no percentage, ETA, or completion estimate anywhere."""

    def test_detects_percentage_in_a_string_value(self) -> None:
        found = find_progress_estimates({"current_activity": "73% complete"})
        self.assertTrue(found)

    def test_detects_percentage_key(self) -> None:
        self.assertTrue(find_progress_estimates({"progress_percent": 12}))
        self.assertTrue(find_progress_estimates({"pct": 12}))
        self.assertTrue(find_progress_estimates({"completionPercentage": 12}))

    def test_detects_eta_key_and_eta_text(self) -> None:
        self.assertTrue(find_progress_estimates({"eta": "2m"}))
        self.assertTrue(find_progress_estimates({"note": "ETA 3 minutes"}))

    def test_detects_completion_estimate_keys(self) -> None:
        for key in (
            "estimated_completion",
            "completion_estimate",
            "time_remaining",
            "seconds_remaining",
            "fraction_complete",
        ):
            with self.subTest(key=key):
                self.assertTrue(find_progress_estimates({key: 1}))

    def test_metadata_key_is_not_a_false_positive_for_eta(self) -> None:
        self.assertEqual(find_progress_estimates({"metadata": {"beta": "ok"}}), ())

    def test_ordinary_progress_payload_is_clean(self) -> None:
        payload = {
            "current_phase": "Implementer",
            "current_activity": "Adding frontend regression tests",
            "steps": [
                {"name": "Prepare", "status": "passed"},
                {"name": "Implementer", "status": "running"},
            ],
            "counts": {"running": 1, "ready": 2},
        }
        self.assertEqual(find_progress_estimates(payload), ())

    def test_find_progress_estimates_reports_the_path(self) -> None:
        found = find_progress_estimates({"steps": [{"summary": "50% done"}]})
        self.assertEqual(found, ("steps[0].summary",))

    def test_assert_no_progress_estimate_raises_with_context(self) -> None:
        with self.assertRaises(RuntimeProgressError) as ctx:
            assert_no_progress_estimate({"eta": "5m"}, context="board")
        self.assertIn("board", str(ctx.exception))

    def test_assert_no_progress_estimate_accepts_clean_payload(self) -> None:
        assert_no_progress_estimate({"status": "running"}, context="board")


class AttemptProgressSnapshotTests(unittest.TestCase):
    """ObservedStep records attach to an Attempt, not the Ticket (§14.0)."""

    def _snapshot(self, **overrides: object) -> AttemptProgressSnapshot:
        payload: dict[str, object] = {
            "attempt_id": "attempt-1",
            "task_key": "AT-101",
            "task_id": "task:AT-101",
            "attempt_number": 1,
            "is_active": True,
            "current_phase": "Implementer",
            "current_activity": "Adding frontend regression tests",
            "updated_at": "2026-01-01T00:00:00Z",
            "steps": (
                observed_step("Prepare", "passed"),
                observed_step("Implementer", "running"),
            ),
        }
        payload.update(overrides)
        return AttemptProgressSnapshot(**payload)  # type: ignore[arg-type]

    def test_snapshot_is_attempt_scoped(self) -> None:
        snapshot = self._snapshot()
        self.assertEqual(snapshot.attempt_id, "attempt-1")
        self.assertEqual(snapshot.attempt_number, 1)
        self.assertEqual(snapshot.task_key, "AT-101")

    def test_step_status_defaults_unrecorded_steps_to_pending(self) -> None:
        snapshot = self._snapshot()
        self.assertEqual(snapshot.step_status("Prepare"), "passed")
        self.assertEqual(snapshot.step_status("Implementer"), "running")
        self.assertEqual(snapshot.step_status("Integration"), "pending")

    def test_ordered_steps_cover_every_first_level_step_in_spec_order(self) -> None:
        snapshot = self._snapshot()
        ordered = snapshot.ordered_steps()
        self.assertEqual(tuple(step.name for step in ordered), RUNTIME_STEPS)

    def test_snapshot_rejects_a_progress_estimate_in_current_activity(self) -> None:
        with self.assertRaises(RuntimeProgressError):
            self._snapshot(current_activity="73% complete")

    def test_snapshot_rejects_an_unknown_current_phase(self) -> None:
        with self.assertRaises(RuntimeProgressError):
            self._snapshot(current_phase="Deploy")

    def test_snapshot_allows_absent_phase_and_activity(self) -> None:
        snapshot = self._snapshot(current_phase=None, current_activity=None)
        self.assertIsNone(snapshot.current_phase)
        self.assertIsNone(snapshot.current_activity)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
