"""Documentation tests for P4-j: the active cron observability rollout runbook.

The runbook is documentation only. It describes how an operator can safely roll
out ``--include-observability-summary`` to the *active* real ``opencode`` cron
line, but it does not modify the active crontab, change scheduler execution
behavior, or migrate the scheduler tick to ExecutionEngine.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "active-cron-observability-rollout.md"


class ActiveCronObservabilityRolloutDocTests(unittest.TestCase):
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

    def test_mentions_observability_flag(self) -> None:
        self.assertIn("--include-observability-summary", self.doc)

    def test_mentions_active_cron_observability_rollout(self) -> None:
        self.assertIn("active cron observability rollout", self.normalized)

    def test_states_documentation_or_runbook_only(self) -> None:
        self.assertIn("documentation only", self.normalized)
        self.assertIn("runbook only", self.normalized)

    def test_states_active_crontab_not_modified_by_this_phase(self) -> None:
        self.assertIn(
            "active crontab is not modified by this phase", self.normalized
        )

    def test_mentions_crontab_l_read_only_inspection(self) -> None:
        self.assertIn("crontab -l", self.normalized)
        self.assertIn("read-only inspection", self.normalized)

    def test_mentions_backup_crontab(self) -> None:
        self.assertIn("backup crontab", self.normalized)

    def test_mentions_candidate_crontab(self) -> None:
        self.assertIn("candidate crontab", self.normalized)

    def test_mentions_manual_candidate_apply_step(self) -> None:
        self.assertIn("crontab /path/to/candidate", self.normalized)
        # The apply step must be explicitly human-gated / manual.
        self.assertIn("manual operator action only", self.normalized)

    def test_mentions_rollback_using_backup(self) -> None:
        self.assertIn("crontab /path/to/backup", self.normalized)
        self.assertIn("rollback", self.normalized)

    def test_mentions_observability_summary_field(self) -> None:
        self.assertIn("observability_summary", self.doc)

    def test_mentions_last_tick_uses_observability_summary(self) -> None:
        self.assertIn("last_tick_uses_observability_summary", self.doc)

    def test_mentions_legacy_fallback(self) -> None:
        self.assertIn("legacy fallback", self.normalized)

    def test_mentions_runtime_log_path(self) -> None:
        self.assertIn(
            "/home/ubuntu/agent-taskflow-cron/logs/"
            "github-issue-one-task-real-opencode.jsonl",
            self.doc,
        )

    def test_states_scheduler_tick_not_migrated_to_execution_engine(self) -> None:
        self.assertIn(
            "scheduler tick is not migrated to executionengine", self.normalized
        )

    def test_states_no_governance_or_runtime_side_effects(self) -> None:
        for phrase in (
            "no approval",
            "no merge",
            "no cleanup",
            "no archive",
            "no closeout",
            "no pr publication",
            "no issue close",
            "no branch deletion",
            "no worktree deletion",
            "no github mutation",
            "no daemon",
            "no webhook",
            "no background worker",
            "no scheduler loop",
            "no multi-task behavior",
        ):
            self.assertIn(phrase, self.normalized, msg=phrase)


if __name__ == "__main__":
    unittest.main()
