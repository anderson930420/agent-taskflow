"""Documentation tests for P5-c: the scheduler ExecutionEngine shadow / compare
layer.

P5-c is a shadow / compare summary only. The doc must state that the compare
layer is pure, compares a legacy scheduler tick payload with an engine-shaped
request, executes nothing, wires no scheduler runtime, changes no active cron,
and that its mismatches are diagnostic only — never approval authority.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "scheduler-execution-engine-shadow-compare.md"


class SchedulerExecutionEngineShadowCompareDocTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = DOC.read_text(encoding="utf-8")
        cls.doc_lower = cls.doc.lower()
        # Strip markdown emphasis/backticks and collapse whitespace so phrase
        # assertions are robust to formatting.
        cls.normalized = re.sub(
            r"\s+",
            " ",
            cls.doc_lower.replace("*", "").replace("`", ""),
        )

    def test_doc_exists(self) -> None:
        self.assertTrue(DOC.is_file())

    def test_mentions_p5c(self) -> None:
        self.assertIn("p5-c", self.normalized)

    def test_mentions_shadow_compare_summary_only(self) -> None:
        self.assertIn("shadow / compare summary only", self.normalized)

    def test_mentions_legacy_scheduler_tick_payload(self) -> None:
        self.assertIn("legacy scheduler tick payload", self.normalized)

    def test_mentions_engine_shaped_request(self) -> None:
        self.assertIn("engine-shaped request", self.normalized)

    def test_mentions_p5a_boundary(self) -> None:
        self.assertIn("p5-a boundary", self.normalized)

    def test_mentions_p5b_request_builder(self) -> None:
        self.assertIn("p5-b request builder", self.normalized)

    def test_mentions_compare_function(self) -> None:
        self.assertIn(
            "compare_scheduler_tick_to_engine_request", self.doc
        )

    def test_states_no_engine_execution(self) -> None:
        self.assertIn("no engine execution", self.normalized)

    def test_states_no_scheduler_runtime_wiring(self) -> None:
        self.assertIn("no scheduler runtime wiring", self.normalized)

    def test_states_no_active_cron_change(self) -> None:
        self.assertIn("no active cron change", self.normalized)

    def test_states_no_approved_task_runner_call(self) -> None:
        self.assertIn("no approved_task_runner call", self.normalized)

    def test_states_no_executor_behavior(self) -> None:
        self.assertIn("no executor behavior", self.normalized)

    def test_states_no_validator_behavior(self) -> None:
        self.assertIn("no validator behavior", self.normalized)

    def test_states_no_db_behavior(self) -> None:
        self.assertIn("no db behavior", self.normalized)

    def test_states_no_github_mutation(self) -> None:
        self.assertIn("no github mutation", self.normalized)

    def test_mentions_publish_after_execution_false(self) -> None:
        self.assertIn("publish_after_execution=False", self.doc)

    def test_mentions_mode_execution_only(self) -> None:
        self.assertIn("mode=execution_only", self.doc)

    def test_mentions_execution_only_true(self) -> None:
        self.assertIn("execution_only=True", self.doc)

    def test_mentions_one_task_only(self) -> None:
        self.assertIn("one_task_only", self.doc)

    def test_mentions_scheduler_tick(self) -> None:
        self.assertIn("scheduler_tick", self.doc)

    def test_mentions_no_loop_background_worker_multi_task(self) -> None:
        for phrase in (
            "scheduler loop",
            "background worker",
            "multi-task",
        ):
            self.assertIn(phrase, self.normalized, msg=phrase)

    def test_mentions_approval_and_merge(self) -> None:
        self.assertIn("approval", self.normalized)
        self.assertIn("merge", self.normalized)

    def test_mentions_missing_legacy_fields_become_warnings(self) -> None:
        self.assertIn("missing legacy fields become warnings", self.normalized)

    def test_mentions_mismatches_diagnostic_only_not_approval_authority(
        self,
    ) -> None:
        self.assertIn("diagnostic only", self.normalized)
        self.assertIn("not approval authority", self.normalized)

    def test_mentions_validators_and_human_review_authority(self) -> None:
        self.assertIn(
            "deterministic validators and human review gates remain",
            self.normalized,
        )

    def test_mentions_p5d_future_opt_in_execution_path(self) -> None:
        self.assertIn("p5-d", self.normalized)
        self.assertIn("opt-in execution path", self.normalized)


if __name__ == "__main__":
    unittest.main()
