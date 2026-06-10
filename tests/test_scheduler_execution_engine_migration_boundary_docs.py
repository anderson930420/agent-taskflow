"""Documentation tests for P5-a: the scheduler-to-ExecutionEngine migration
boundary inventory.

The boundary document is documentation only. It defines how the existing
scheduler tick path could later migrate to the ExecutionEngine contract. It
does not modify the active crontab, change scheduler execution behavior, or
migrate the scheduler tick to ExecutionEngine.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "scheduler-execution-engine-migration-boundary.md"


class SchedulerExecutionEngineMigrationBoundaryDocTests(unittest.TestCase):
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

    def test_mentions_scheduler_to_execution_engine_migration(self) -> None:
        self.assertIn(
            "scheduler-to-executionengine migration", self.normalized
        )

    def test_mentions_migration_boundary_name(self) -> None:
        self.assertIn(
            "scheduler-execution-engine migration boundary", self.normalized
        )

    def test_mentions_active_cron_remains_stable(self) -> None:
        self.assertIn("active cron remains stable", self.normalized)

    def test_mentions_approved_task_runner_remains_live_authority(self) -> None:
        self.assertIn(
            "existing approved task runner path remains the live execution"
            " authority",
            self.normalized,
        )

    def test_mentions_real_cron_remains_execution_only(self) -> None:
        self.assertIn("real cron remains execution-only", self.normalized)

    def test_mentions_observability_summary_field(self) -> None:
        self.assertIn("observability_summary", self.doc)

    def test_mentions_dashboard_reads_unified_summary(self) -> None:
        self.assertIn("dashboard reads the unified summary", self.normalized)

    def test_mentions_no_engine_backed_scheduler_execution_yet(self) -> None:
        self.assertIn(
            "no executionengine-backed scheduler execution is active yet",
            self.normalized,
        )

    def test_mentions_contract_dataclasses_protocol(self) -> None:
        self.assertIn(
            "executionengine contract dataclasses / protocol", self.normalized
        )
        self.assertIn("execution_engine_contract.py", self.doc)

    def test_mentions_approved_task_runner_adapter(self) -> None:
        self.assertIn("ApprovedTaskRunnerExecutionEngineAdapter", self.doc)

    def test_mentions_manual_opt_in_engine_facade(self) -> None:
        self.assertIn("manual opt-in engine facade", self.normalized)

    def test_mentions_unified_execution_observability_summary(self) -> None:
        self.assertIn(
            "unified execution observability summary", self.normalized
        )

    def test_mentions_contract_mapping_concepts(self) -> None:
        for phrase in (
            "executor profile",
            "validator profile",
            "workspace profile",
            "artifact refs",
            "safety flags",
            "execution request",
            "execution result",
        ):
            self.assertIn(phrase, self.normalized, msg=phrase)

    def test_mentions_contract_dataclass_names(self) -> None:
        for name in (
            "ExecutionEngineExecutorProfile",
            "ExecutionEngineValidatorProfile",
            "ExecutionEngineWorkspaceProfile",
            "ExecutionEngineArtifactRef",
            "ExecutionEngineSafety",
            "ExecutionEngineRequest",
            "ExecutionEngineResult",
        ):
            self.assertIn(name, self.doc, msg=name)

    def test_mentions_legacy_fallback_remains_default_and_readable(self) -> None:
        self.assertIn(
            "legacy scheduler path remains the default", self.normalized
        )
        self.assertIn(
            "legacy fallback remains default and readable", self.normalized
        )

    def test_mentions_engine_path_must_be_opt_in(self) -> None:
        self.assertIn(
            "future engine path must be opt-in", self.normalized
        )

    def test_mentions_publish_after_execution_false(self) -> None:
        self.assertIn("publish_after_execution=False", self.doc)

    def test_mentions_engine_result_not_approval_authority(self) -> None:
        self.assertIn(
            "no executionengine result can become an approval authority",
            self.normalized,
        )

    def test_mentions_deterministic_validators_human_review_gates(self) -> None:
        self.assertIn(
            "deterministic validators / human review gates", self.normalized
        )

    def test_mentions_staged_plan_p5b_through_p5g(self) -> None:
        for stage in ("p5-b", "p5-c", "p5-d", "p5-e", "p5-f", "p5-g"):
            self.assertIn(stage, self.normalized, msg=stage)

    def test_states_active_cron_not_changed_by_p5a(self) -> None:
        self.assertIn("active cron is not changed by p5-a", self.normalized)
        self.assertIn(
            "active crontab is not modified by this phase", self.normalized
        )

    def test_states_scheduler_tick_not_migrated_to_execution_engine(self) -> None:
        self.assertIn(
            "scheduler tick is not migrated to executionengine",
            self.normalized,
        )

    def test_states_no_runtime_behavior_added(self) -> None:
        for phrase in (
            "no scheduler execution behavior",
            "no automation behavior",
            "no cron behavior",
            "no approved_task_runner behavior",
            "no executor behavior",
            "no validator behavior",
            "no db behavior",
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


if __name__ == "__main__":
    unittest.main()
