"""Tests for agent_taskflow.integration_validators (spec §29, §44)."""

from __future__ import annotations

import json
import tempfile
import subprocess
import sys
import unittest
from unittest import mock
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
    def test_summary_records_real_commands_unbound_identity_and_unique_snapshots(self) -> None:
        spec = IntegrationValidatorSpec("unit", (sys.executable, "-c", "print('real integration evidence')"))
        self._run((spec,), integration_run_id="same-id")
        first = next(self.artifact_dir.rglob("validation-summary.json"))
        before = first.read_bytes()
        self._run((spec,), integration_run_id="same-id")
        paths = list(self.artifact_dir.rglob("validation-summary.json"))
        self.assertEqual(len(paths), 2)
        self.assertEqual(first.read_bytes(), before)
        data = json.loads(before)
        self.assertTrue(data["passed"])
        self.assertEqual(data["phase"], "integration_validation")
        self.assertIsNone(data["attempt_id"])
        self.assertEqual(data["attempt_binding"], "unbound")
        self.assertEqual(data["integration_run_id"], "same-id")
        row = data["validators"][0]
        self.assertEqual(row["command"], list(spec.command))
        evidence = json.loads(Path(row["artifact_path"]).read_text())
        self.assertIn("real integration evidence", evidence["output"])
        self.assertLessEqual(row["started_at"], row["ended_at"])

    def test_missing_binary_is_tool_error_with_no_observed_exit(self) -> None:
        report = self._run((IntegrationValidatorSpec("missing", ("not-a-real-validator-binary",)),))
        self.assertEqual(report.outcomes[0].exit_code, 127)  # Existing compatibility behavior.
        data = json.loads(next(self.artifact_dir.rglob("validation-summary.json")).read_text())
        row = data["validators"][0]
        self.assertEqual(row["result"], "failed")
        self.assertEqual(row["outcome_kind"], "tool_error")
        self.assertIsNone(row["exit_code"])
        self.assertEqual(row["reported_exit_code"], 127)
        self.assertFalse(data["passed"])

    def test_real_timeout_preserves_captured_output(self) -> None:
        report = self._run((IntegrationValidatorSpec(
            "timeout", (sys.executable, "-c", "import time; print('before timeout', flush=True); time.sleep(5)"),
            timeout_seconds=1,
        ),))
        self.assertEqual(report.outcomes[0].exit_code, 124)
        self.assertIn("before timeout", report.outcomes[0].output)
        data = json.loads(next(self.artifact_dir.rglob("validation-summary.json")).read_text())
        row = data["validators"][0]
        self.assertIsNone(row["exit_code"])
        self.assertEqual(row["error"]["type"], "TimeoutExpired")
        self.assertIn("before timeout", Path(row["artifact_path"]).read_text())

    def test_unexpected_exception_keeps_earlier_output_and_propagates(self) -> None:
        runner = mock.Mock(side_effect=[subprocess.CompletedProcess(["unit"], 0, "earlier output", ""), RuntimeError("crash")])
        specs = tuple(IntegrationValidatorSpec(name, (name,)) for name in ("first", "broken", "last"))
        with self.assertRaisesRegex(RuntimeError, "crash"):
            self._run(specs, runner=runner)
        data = json.loads(next(self.artifact_dir.rglob("validation-summary.json")).read_text())
        self.assertEqual([row["result"] for row in data["validators"]], ["passed", "tool_error", "not_run"])
        self.assertIn("earlier output", Path(data["validators"][0]["artifact_path"]).read_text())
        self.assertFalse(data["complete"])

    def test_empty_set_produces_an_incomplete_summary(self) -> None:
        report = self._run(())
        self.assertFalse(report.passed)
        data = json.loads(next(self.artifact_dir.rglob("validation-summary.json")).read_text())
        self.assertEqual(data["validators"], [])
        self.assertFalse(data["complete"])
        self.assertFalse(data["passed"])

    def test_summary_write_failure_is_explicit_without_replacing_the_integration_verdict(self) -> None:
        with mock.patch("agent_taskflow.validation_summary.atomic_write_json", side_effect=OSError("disk failure")):
            report = self._run((IntegrationValidatorSpec("unit", ("true",)),))
        self.assertTrue(report.passed)
        self.assertEqual(list(self.artifact_dir.rglob("validation-summary.json")), [])
        errors = [json.loads(event.payload_json) for event in self.store.list_task_events("AT-701")
                  if event.payload_json and json.loads(event.payload_json).get("kind") == "validation_summary_error"]
        self.assertEqual(len(errors), 1)
        self.assertFalse(errors[0]["complete"])
        self.assertFalse(errors[0]["passed"])
        self.assertEqual(errors[0]["error"]["message"], "disk failure")

    def test_summary_finish_failure_preserves_red_integration_verdict_and_partial_summary(self) -> None:
        from agent_taskflow.validation_summary import atomic_write_json
        def write(path, payload, **kwargs):
            if payload.get("state") == "finished":
                raise OSError("finish disk failure")
            return atomic_write_json(path, payload, **kwargs)
        with mock.patch("agent_taskflow.validation_summary.atomic_write_json", side_effect=write):
            report = self._run((IntegrationValidatorSpec("unit", ("false",)),))
        self.assertFalse(report.passed)
        data = json.loads(next(self.artifact_dir.rglob("validation-summary.json")).read_text())
        self.assertEqual(data["validators"][0]["result"], "failed")
        self.assertFalse(data["passed"])
        self.assertFalse(data["complete"])

    def test_summary_registration_failure_does_not_replace_existing_red_gate(self) -> None:
        record = TaskMirrorStore.record_task_artifact
        def register(store, task_key, artifact_type, path):
            if Path(path).name == "validation-summary.json":
                raise OSError("summary index unavailable")
            return record(store, task_key, artifact_type, path)
        with mock.patch.object(TaskMirrorStore, "record_task_artifact", new=register):
            report = self._run((IntegrationValidatorSpec("unit", ("false",)),))
        self.assertFalse(report.passed)
        self.assertEqual(report.outcomes[0].exit_code, 1)
        data = json.loads(next(self.artifact_dir.rglob("validation-summary.json")).read_text())
        self.assertFalse(data["passed"])
        self.assertTrue(any(event.payload_json and "validation_summary_error" in event.payload_json
                            for event in self.store.list_task_events("AT-701")))

    def test_summary_error_io_cannot_mask_original_integration_runner_exception(self) -> None:
        from agent_taskflow.validation_summary import atomic_write_json
        def write(path, payload, **kwargs):
            if Path(path).name.endswith("-error.json"):
                raise OSError("summary error output unavailable")
            return atomic_write_json(path, payload, **kwargs)
        with mock.patch("agent_taskflow.validation_summary.atomic_write_json", side_effect=write):
            with self.assertRaisesRegex(RuntimeError, "original runner error"):
                self._run((IntegrationValidatorSpec("unit", ("unit",)),),
                          runner=mock.Mock(side_effect=RuntimeError("original runner error")))
        data = json.loads(next(self.artifact_dir.rglob("validation-summary.json")).read_text())
        self.assertFalse(data["passed"])
        self.assertFalse(data["complete"])

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



class ValidatorOutputDecodingTests(IntegrationValidatorTestCase):
    """Ruling 31b — a validator's non-UTF-8 output never raises."""

    def test_non_utf8_validator_output_is_decoded_with_replacement(self) -> None:
        spec = IntegrationValidatorSpec(name="bytes", command=("sh", "-c", "printf 'caf\\351\\n'"))
        report = self._run((spec,))
        self.assertTrue(report.passed, report.summary)
        self.assertIn("caf\ufffd", report.outcomes[0].output)


if __name__ == "__main__":
    unittest.main()
