"""Tests for agent_taskflow.integration_metrics (spec §39)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_taskflow import integration_schema as schema
from agent_taskflow.integration_metrics import compute_integration_metrics
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


class MetricsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.integration = IntegrationStore(self.db_path)
        self.integration.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _task(self, key: str, *, status: str = schema.NEEDS_REVIEW) -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key=key, project="demo", status=status, repo_path=self.root / "repo"
            )
        )

    def _metrics(self):
        return compute_integration_metrics(self.integration, store=self.store)


class EmptyStateTests(MetricsTestCase):
    def test_rates_are_zero_when_nothing_has_integrated(self) -> None:
        metrics = self._metrics()
        self.assertEqual(metrics.integration_conflict_rate, 0.0)
        self.assertEqual(metrics.reintegration_rate, 0.0)
        self.assertEqual(metrics.ai_conflict_resolution_success_rate, 0.0)
        self.assertEqual(metrics.reintegration_count_total, 0)


class ReIntegrationMetricsTests(MetricsTestCase):
    def test_reintegration_count_total_sums_every_ticket(self) -> None:
        for key, count in (("AT-1", 0), ("AT-2", 2), ("AT-3", 1)):
            self._task(key)
            self.integration.update_pr_state(key, pr_number=1, reintegration_count=count)
        self.assertEqual(self._metrics().reintegration_count_total, 3)

    def test_reintegration_rate_is_the_share_of_reviewed_tickets_that_reintegrated(self) -> None:
        for key, count in (("AT-1", 0), ("AT-2", 2), ("AT-3", 1), ("AT-4", 0)):
            self._task(key)
            self.integration.update_pr_state(key, pr_number=1, reintegration_count=count)
        self.assertAlmostEqual(self._metrics().reintegration_rate, 0.5)

    def test_tickets_that_never_reached_a_pr_are_excluded(self) -> None:
        self._task("AT-1", status=schema.READY_FOR_INTEGRATION)
        self._task("AT-2")
        self.integration.update_pr_state("AT-2", pr_number=1, reintegration_count=1)
        self.assertAlmostEqual(self._metrics().reintegration_rate, 1.0)


class ConflictMetricsTests(MetricsTestCase):
    def _conflict(self, key: str, *, run: str, resolved: bool) -> None:
        self.integration.record_conflict_evidence(
            key,
            integration_run_id=run,
            resolver="test",
            resolved=resolved,
            conflict_hunks=[{"path": "a.txt", "hunk": "<<<<<<<"}],
            explanation="e",
        )

    def _validators(self, key: str, *, run: str, status: str) -> None:
        self.integration.record_validator_evidence(
            key,
            integration_run_id=run,
            validator="unit",
            command=("true",),
            status=status,
            exit_code=0 if status == "passed" else 1,
            output="",
            branch_sha="b",
            target_sha="t",
            diff_context="d",
        )

    def test_conflict_rate_counts_runs_that_hit_a_conflict(self) -> None:
        for key in ("AT-1", "AT-2", "AT-3", "AT-4"):
            self._task(key)
            self._validators(key, run=f"{key}-run", status="passed")
        self._conflict("AT-1", run="AT-1-run", resolved=True)
        self.assertAlmostEqual(self._metrics().integration_conflict_rate, 0.25)

    def test_ai_resolution_success_rate(self) -> None:
        for key, resolved in (("AT-1", True), ("AT-2", True), ("AT-3", False), ("AT-4", False)):
            self._task(key)
            self._conflict(key, run=f"{key}-run", resolved=resolved)
        self.assertAlmostEqual(self._metrics().ai_conflict_resolution_success_rate, 0.5)

    def test_a_resolution_that_failed_verification_is_not_a_success(self) -> None:
        """§39.3 success means a verified conflict-free tree, not a claim."""
        self._task("AT-1")
        self._conflict("AT-1", run="AT-1-run", resolved=True)
        self.integration.record_conflict_verification(
            "AT-1", integration_run_id="AT-1-run",
            checks=[{"name": "worktree_clean", "passed": False, "detail": ""}],
        )
        self.assertEqual(self._metrics().ai_conflict_resolution_success_rate, 0.0)

    def test_post_resolution_validator_failure_rate(self) -> None:
        for key, resolved, status in (
            ("AT-1", True, "failed"),
            ("AT-2", True, "passed"),
            ("AT-3", False, "failed"),
        ):
            self._task(key)
            self._conflict(key, run=f"{key}-run", resolved=resolved)
            self._validators(key, run=f"{key}-run", status=status)
        self.assertAlmostEqual(self._metrics().post_resolution_validator_failure_rate, 0.5)

    def test_conflict_rework_cost_counts_extra_integration_runs(self) -> None:
        self._task("AT-1")
        self._validators("AT-1", run="run-1", status="failed")
        self._conflict("AT-1", run="run-1", resolved=False)
        self._validators("AT-1", run="run-2", status="passed")
        self.assertEqual(self._metrics().conflict_rework_cost, 1)


class SerializationTests(MetricsTestCase):
    def test_metrics_serialize_to_a_flat_dict(self) -> None:
        payload = self._metrics().to_summary_dict()
        for field in (
            "integration_conflict_rate",
            "late_dependency_rate",
            "upstream_rework_rate",
            "conflict_rework_cost",
            "reintegration_count_total",
            "reintegration_rate",
            "ai_conflict_resolution_success_rate",
            "post_resolution_validator_failure_rate",
        ):
            self.assertIn(field, payload)


if __name__ == "__main__":
    unittest.main()
