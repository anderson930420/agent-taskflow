"""Documentation tests for P5-b: the scheduler ExecutionEngine request
builder.

P5-b is a request-builder contract only. The doc must state that the builder
is pure and behavior-free, and that it adds no scheduler runtime wiring, no
engine execution, no active cron change, and no behavior change anywhere
else.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "scheduler-execution-engine-request-builder.md"


class SchedulerExecutionEngineRequestBuilderDocTests(unittest.TestCase):
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

    def test_mentions_p5b(self) -> None:
        self.assertIn("p5-b", self.normalized)

    def test_mentions_request_builder_contract_only(self) -> None:
        self.assertIn("request-builder contract only", self.normalized)

    def test_mentions_build_input_dataclass(self) -> None:
        self.assertIn("SchedulerExecutionEngineRequestBuildInput", self.doc)

    def test_mentions_builder_function(self) -> None:
        self.assertIn("build_scheduler_execution_engine_request", self.doc)

    def test_mentions_execution_engine_request(self) -> None:
        self.assertIn("ExecutionEngineRequest", self.doc)

    def test_mentions_scheduled_tick_source(self) -> None:
        self.assertIn("REQUEST_SOURCE_SCHEDULED_TICK", self.doc)

    def test_mentions_publish_after_execution_false(self) -> None:
        self.assertIn("publish_after_execution=False", self.doc)

    def test_mentions_mode_execution_only(self) -> None:
        self.assertIn("mode=execution_only", self.doc)

    def test_mentions_pure_and_behavior_free(self) -> None:
        self.assertIn("pure and behavior-free", self.normalized)

    def test_states_no_scheduler_runtime_wiring(self) -> None:
        self.assertIn("no scheduler runtime wiring", self.normalized)

    def test_states_no_engine_execution(self) -> None:
        self.assertIn("no engine execution", self.normalized)

    def test_states_no_active_cron_change(self) -> None:
        self.assertIn("no active cron change", self.normalized)

    def test_states_no_runtime_behavior_change(self) -> None:
        for phrase in (
            "no approved_task_runner behavior change",
            "no executor behavior change",
            "no validator behavior change",
        ):
            self.assertIn(phrase, self.normalized, msg=phrase)

    def test_states_no_governance_side_effects(self) -> None:
        for phrase in (
            "no github mutation",
            "no approval",
            "no merge",
            "no cleanup",
            "no archive",
            "no closeout",
            "no pr publication",
            "no issue close",
            "no branch deletion",
            "no worktree deletion",
            "no daemon",
            "no webhook",
            "no background worker",
            "no scheduler loop",
            "no multi-task behavior",
        ):
            self.assertIn(phrase, self.normalized, msg=phrase)

    def test_mentions_p5c_shadow_compare_future_stage(self) -> None:
        self.assertIn("p5-c", self.normalized)
        self.assertIn("shadow / compare summary", self.normalized)
        self.assertIn("future work", self.normalized)

    def test_points_to_shadow_compare_doc(self) -> None:
        self.assertIn(
            "docs/scheduler-execution-engine-shadow-compare.md", self.doc
        )


if __name__ == "__main__":
    unittest.main()
