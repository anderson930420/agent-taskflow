"""Tests for agent_taskflow.integration_validators (spec §29, §44)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_taskflow import integration_schema as schema
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.integration_validators import (
    IntegrationValidatorSpec,
    run_integration_validators,
)
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


class IntegrationValidatorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.artifact_dir = self.root / "artifacts"
        self.artifact_dir.mkdir()
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.integration = IntegrationStore(self.db_path)
        self.integration.init_db()
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-701",
                project="demo",
                status=schema.INTEGRATING,
                repo_path=self.root / "repo",
                artifact_dir=self.artifact_dir,
            )
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, specs, **overrides):
        kwargs = dict(
            task_key="AT-701",
            worktree_path=self.worktree,
            artifact_dir=self.artifact_dir,
            specs=specs,
            branch_sha="branchsha",
            target_sha="targetsha",
            diff_context="M shared.txt",
            integration_run_id="run-1",
            integration_store=self.integration,
        )
        kwargs.update(overrides)
        return run_integration_validators(**kwargs)


class ValidatorGateTests(IntegrationValidatorTestCase):
    def test_all_green_reports_passed(self) -> None:
        report = self._run(
            (
                IntegrationValidatorSpec(name="unit", command=("true",)),
                IntegrationValidatorSpec(name="lint", command=("true",)),
            )
        )
        self.assertTrue(report.passed)
        self.assertEqual([o.status for o in report.outcomes], ["passed", "passed"])

    def test_a_single_red_validator_fails_the_gate(self) -> None:
        report = self._run(
            (
                IntegrationValidatorSpec(name="unit", command=("true",)),
                IntegrationValidatorSpec(name="lint", command=("false",)),
            )
        )
        self.assertFalse(report.passed)
        self.assertEqual(report.failed_names, ["lint"])

    def test_an_empty_validator_set_does_not_count_as_a_pass(self) -> None:
        """A gate that gates nothing is not a gate."""
        report = self._run(())
        self.assertFalse(report.passed)
        self.assertIn("no validators", report.summary.lower())

    def test_a_missing_validator_binary_is_a_failure_not_a_skip(self) -> None:
        report = self._run(
            (IntegrationValidatorSpec(name="unit", command=("definitely-not-a-real-binary-xyz",)),)
        )
        self.assertFalse(report.passed)
        self.assertEqual(report.outcomes[0].status, "failed")


class ValidatorEvidenceTests(IntegrationValidatorTestCase):
    def test_evidence_persists_every_spec_29_field(self) -> None:
        self._run((IntegrationValidatorSpec(name="unit", command=("false",)),))
        rows = self.integration.list_validator_evidence("AT-701")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["validator"], "unit")
        self.assertEqual(row["command"], ["false"])
        self.assertEqual(row["branch_sha"], "branchsha")
        self.assertEqual(row["target_sha"], "targetsha")
        self.assertEqual(row["diff_context"], "M shared.txt")
        self.assertIsNotNone(row["output"])

    def test_evidence_artifact_is_written_and_recorded(self) -> None:
        report = self._run((IntegrationValidatorSpec(name="unit", command=("true",)),))
        self.assertTrue(report.evidence_path.is_file())
        payload = json.loads(report.evidence_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["task_key"], "AT-701")
        self.assertEqual(payload["integration_run_id"], "run-1")
        self.assertIs(payload["passed"], True)
        types = {a.artifact_type for a in self.store.list_task_artifacts("AT-701")}
        self.assertIn("integration_validator_evidence", types)

    def test_each_run_appends_rather_than_overwriting_evidence(self) -> None:
        self._run((IntegrationValidatorSpec(name="unit", command=("true",)),), integration_run_id="run-1")
        self._run((IntegrationValidatorSpec(name="unit", command=("false",)),), integration_run_id="run-2")
        rows = self.integration.list_validator_evidence("AT-701")
        self.assertEqual([row["integration_run_id"] for row in rows], ["run-1", "run-2"])
        self.assertEqual([row["status"] for row in rows], ["passed", "failed"])

    def test_output_is_captured_for_the_reviewer(self) -> None:
        report = self._run(
            (
                IntegrationValidatorSpec(
                    name="unit", command=("sh", "-c", "echo hello-from-validator; exit 1")
                ),
            )
        )
        self.assertIn("hello-from-validator", report.outcomes[0].output)


if __name__ == "__main__":
    unittest.main()
