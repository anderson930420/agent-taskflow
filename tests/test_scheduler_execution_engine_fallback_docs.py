"""Documentation tests for P5-e: legacy-vs-engine fallback hardening.

P5-e classifies the P5-d ``execution_engine`` opt-in evidence. The doc must
state that the legacy scheduler remains the effective authority, the engine
path remains opt-in and off by default, the active cron and the cron / deploy /
systemd examples are unchanged, the fallback assessment fields and failure
classifications are machine-readable, a clean candidate is usable for future
migration but is never approval authority, deterministic validators and human
review gates remain the validation authority, rollback is removing the opt-in
flag, and the future P5-f stage is the operator rollout runbook.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "scheduler-execution-engine-fallback-hardening.md"


class SchedulerExecutionEngineFallbackHardeningDocTests(unittest.TestCase):
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

    def test_mentions_p5e(self) -> None:
        self.assertIn("p5-e", self.normalized)

    def test_mentions_legacy_vs_engine_fallback_hardening(self) -> None:
        self.assertIn("legacy-vs-engine fallback hardening", self.normalized)

    def test_mentions_effective_authority_legacy_scheduler(self) -> None:
        self.assertIn("effective_authority", self.doc)
        self.assertIn("legacy_scheduler", self.doc)

    def test_mentions_engine_authority_false(self) -> None:
        self.assertIn("engine_authority=False", self.doc)

    def test_mentions_engine_result_accepted_as_authority_false(self) -> None:
        self.assertIn("engine_result_accepted_as_authority=False", self.doc)

    def test_mentions_fallback_required_and_reason(self) -> None:
        self.assertIn("fallback_required", self.doc)
        self.assertIn("fallback_reason", self.doc)

    def test_mentions_engine_candidate_usable_for_future_migration(
        self,
    ) -> None:
        self.assertIn("engine_candidate_usable_for_future_migration", self.doc)

    def test_mentions_shadow_compare_mismatch(self) -> None:
        self.assertIn("shadow compare mismatch", self.normalized)

    def test_mentions_unsafe_safety_marker(self) -> None:
        self.assertIn("unsafe safety marker", self.normalized)

    def test_mentions_publication_boundary(self) -> None:
        self.assertIn("publication boundary", self.normalized)

    def test_mentions_publish_after_execution_false(self) -> None:
        self.assertIn("publish_after_execution=False", self.doc)

    def test_mentions_mode_execution_only(self) -> None:
        self.assertIn("mode=execution_only", self.doc)

    def test_mentions_active_cron_unchanged(self) -> None:
        self.assertIn("active cron is unchanged", self.normalized)

    def test_mentions_opt_in_off_by_default(self) -> None:
        self.assertIn("opt-in and off by default", self.normalized)

    def test_mentions_no_cron_deploy_systemd_example_change(self) -> None:
        self.assertIn(
            "no cron / deploy / systemd example change", self.normalized
        )

    def test_mentions_no_publish_pr_branch_push_draft_pr(self) -> None:
        self.assertIn(
            "no publish / pr publication / branch push / draft pr",
            self.normalized,
        )

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

    def test_mentions_not_approval_authority(self) -> None:
        self.assertIn("not approval authority", self.normalized)

    def test_mentions_validators_and_human_review_authority(self) -> None:
        self.assertIn(
            "deterministic validators and human review gates remain the "
            "validation and approval authority",
            self.normalized,
        )

    def test_mentions_rollback_by_removing_opt_in_flag(self) -> None:
        self.assertIn("rollback", self.normalized)
        self.assertIn("removing the opt-in flag", self.normalized)

    def test_mentions_p5f_operator_rollout_runbook_future_stage(self) -> None:
        self.assertIn("p5-f", self.normalized)
        self.assertIn("operator rollout runbook", self.normalized)
        self.assertIn("future", self.normalized)

    def test_mentions_module_and_schema(self) -> None:
        self.assertIn(
            "agent_taskflow/scheduler_execution_engine_fallback.py", self.doc
        )
        self.assertIn("scheduler_execution_engine_fallback.v1", self.doc)

    def test_references_p5a_p5b_p5c_p5d(self) -> None:
        for phrase in ("p5-a", "p5-b", "p5-c", "p5-d"):
            self.assertIn(phrase, self.normalized, msg=phrase)


if __name__ == "__main__":
    unittest.main()
