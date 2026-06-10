"""Documentation tests for P5-d: the scheduler ExecutionEngine opt-in path.

P5-d adds an off-by-default opt-in execution path. The doc must state that the
path is opt-in via ``--use-execution-engine``, off by default, confirmed-mode
only, execution-only, leaves the active cron and default legacy path unchanged,
adds no publish / PR / branch push / merge / approval / cleanup / branch- or
worktree-deletion / daemon / webhook / background-worker / scheduler-loop /
multi-task behavior, records evidence that is not approval authority and a shadow
compare that is diagnostic only, keeps deterministic validators and human review
gates as the validation authority, rolls back by removing the flag, and names
the future P5-e fallback hardening stage.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "scheduler-execution-engine-opt-in-path.md"


class SchedulerExecutionEngineOptInPathDocTests(unittest.TestCase):
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

    def test_mentions_p5d(self) -> None:
        self.assertIn("p5-d", self.normalized)

    def test_mentions_opt_in_execution_path(self) -> None:
        self.assertIn("opt-in execution path", self.normalized)

    def test_mentions_use_execution_engine_flag(self) -> None:
        self.assertIn("--use-execution-engine", self.doc)

    def test_mentions_off_by_default(self) -> None:
        self.assertIn("off by default", self.normalized)

    def test_mentions_active_cron_unchanged(self) -> None:
        self.assertIn("active cron is unchanged", self.normalized)

    def test_mentions_default_scheduler_path_remains_legacy(self) -> None:
        self.assertIn("default scheduler path remains legacy", self.normalized)

    def test_mentions_confirmed_mode_only(self) -> None:
        self.assertIn("confirmed-mode only", self.normalized)

    def test_mentions_execution_only(self) -> None:
        self.assertIn("execution-only", self.normalized)

    def test_mentions_publish_after_execution_false(self) -> None:
        self.assertIn("publish_after_execution=False", self.doc)

    def test_mentions_no_publish_pr_draft_pr_branch_push(self) -> None:
        for phrase in (
            "no publish / pr publication / draft pr / branch push",
            "draft pr",
            "branch push",
        ):
            self.assertIn(phrase, self.normalized, msg=phrase)

    def test_mentions_no_approval_merge_cleanup_archive_closeout(self) -> None:
        self.assertIn(
            "no approval / merge / cleanup / archive / closeout",
            self.normalized,
        )

    def test_mentions_no_branch_or_worktree_deletion(self) -> None:
        self.assertIn("no branch deletion / worktree deletion", self.normalized)

    def test_mentions_no_daemon_webhook_worker_loop_multi_task(self) -> None:
        self.assertIn(
            "no daemon / webhook / background worker / scheduler loop / "
            "multi-task behavior",
            self.normalized,
        )

    def test_mentions_shadow_compare_diagnostic_only(self) -> None:
        self.assertIn("shadow compare", self.normalized)
        self.assertIn("diagnostic only", self.normalized)

    def test_mentions_not_approval_authority(self) -> None:
        self.assertIn("not approval authority", self.normalized)

    def test_mentions_validators_and_human_review_authority(self) -> None:
        self.assertIn(
            "deterministic validators and human review gates remain the "
            "validation and approval authority",
            self.normalized,
        )

    def test_mentions_evidence_only(self) -> None:
        self.assertIn("runtime evidence only", self.normalized)

    def test_mentions_failure_behavior(self) -> None:
        self.assertIn("failure behavior", self.normalized)
        self.assertIn("engine_error", self.doc)

    def test_mentions_rollback_by_removing_flag(self) -> None:
        self.assertIn("rollback", self.normalized)
        self.assertIn("remove the opt-in flag", self.normalized)

    def test_mentions_p5e_fallback_hardening_future_stage(self) -> None:
        self.assertIn("p5-e", self.normalized)
        self.assertIn("fallback", self.normalized)

    def test_mentions_module_and_request_source(self) -> None:
        self.assertIn(
            "agent_taskflow/scheduler_execution_engine_opt_in.py", self.doc
        )
        self.assertIn("scheduled_tick", self.doc)

    def test_references_p5a_p5b_p5c(self) -> None:
        for phrase in ("p5-a", "p5-b", "p5-c"):
            self.assertIn(phrase, self.normalized, msg=phrase)


if __name__ == "__main__":
    unittest.main()
