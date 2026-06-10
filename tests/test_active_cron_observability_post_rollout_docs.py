"""Documentation tests for P4-k: the active cron observability post-rollout
validation record.

The record is documentation / evidence only. It captures the observed smoke
evidence after the operator manually applied the P4-j rollout runbook to the
active real ``opencode`` cron line. It does not modify the active crontab,
change scheduler execution behavior, or migrate the scheduler tick to
ExecutionEngine.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "active-cron-observability-post-rollout-validation.md"


class ActiveCronObservabilityPostRolloutDocTests(unittest.TestCase):
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

    def test_mentions_active_cron_observability_rollout(self) -> None:
        self.assertIn("active cron observability rollout", self.normalized)

    def test_mentions_post_rollout_validation(self) -> None:
        self.assertIn("post-rollout validation", self.normalized)

    def test_mentions_observability_flag(self) -> None:
        self.assertIn("--include-observability-summary", self.doc)

    def test_mentions_observability_summary_field(self) -> None:
        self.assertIn("observability_summary", self.doc)

    def test_mentions_schema_version(self) -> None:
        self.assertIn("execution_observability_summary.v1", self.doc)

    def test_mentions_scheduler_tick_source(self) -> None:
        self.assertIn("scheduler_tick", self.doc)

    def test_mentions_active_runner_config(self) -> None:
        self.assertIn("opencode", self.doc)
        self.assertIn("minimax-coding-plan/MiniMax-M2.7", self.doc)
        self.assertIn("policy", self.doc)

    def test_mentions_execution_only_publication(self) -> None:
        self.assertIn("publish_after_execution=False", self.doc)
        self.assertIn("mode=execution_only", self.doc)

    def test_mentions_latest_jsonl_tick_evidence(self) -> None:
        self.assertIn("json_line 342", self.normalized)

    def test_mentions_dashboard_evidence_counters(self) -> None:
        for phrase in (
            "observability summaries read 20",
            "failures 0",
            "lock_contention 0",
            "waiting_approval 1",
            "blocked 0",
            "queued 0",
            "ingestion_failure_count 0",
            "quarantined 0",
        ):
            self.assertIn(phrase, self.normalized, msg=phrase)

    def test_mentions_malformed_lines_skipped(self) -> None:
        self.assertIn("malformed lines skipped: 26", self.normalized)

    def test_explains_malformed_lines_are_historical_residue(self) -> None:
        self.assertIn("historical residue", self.normalized)
        self.assertIn("not a current runtime failure", self.normalized)
        self.assertIn("safely skips malformed lines", self.normalized)

    def test_mentions_runtime_worktree_synced_to_origin_main(self) -> None:
        self.assertIn("/home/ubuntu/agent-taskflow-cron", self.doc)
        self.assertIn("synced to origin/main", self.normalized)

    def test_mentions_runtime_log_path(self) -> None:
        self.assertIn(
            "/home/ubuntu/agent-taskflow-cron/logs/"
            "github-issue-one-task-real-opencode.jsonl",
            self.doc,
        )

    def test_mentions_backup_crontab_location(self) -> None:
        self.assertIn("/home/ubuntu/agent-taskflow-cron-backups/", self.doc)

    def test_mentions_rollback_procedure_reference(self) -> None:
        self.assertIn("rollback", self.normalized)
        self.assertIn("docs/active-cron-observability-rollout.md", self.doc)

    def test_states_active_crontab_not_modified_by_this_phase(self) -> None:
        self.assertIn(
            "active crontab is not modified by this phase", self.normalized
        )

    def test_states_scheduler_tick_not_migrated_to_execution_engine(self) -> None:
        self.assertIn(
            "scheduler tick is not migrated to executionengine", self.normalized
        )

    def test_states_no_governance_or_runtime_side_effects(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
