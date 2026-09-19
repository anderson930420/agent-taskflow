"""Validation evidence must survive partial runs and never manufacture a pass."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

from agent_taskflow.validation_summary import MAX_EVIDENCE_BYTES, ValidationSummaryRecorder, artifact_root_for_claim
from agent_taskflow.validators.base import ValidatorResult


def recorder(root, names=("unit",), **kwargs):
    return ValidationSummaryRecorder(
        task_key="AT-1", artifact_dir=root, source="test", phase="implementation_validation",
        validators=names, **kwargs,
    )


def read(summary):
    return json.loads(summary.path.read_text())


class ValidationSummaryTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.tmp_path = Path(temporary_directory.name)

    def test_observed_verdicts_and_actual_evidence(self):
        for status, code, passed in [
            ("passed", 0, True), ("failed", 3, False),
            ("blocked", None, False), ("skipped", None, False),
        ]:
            with self.subTest(status=status, code=code, passed=passed):
                with tempfile.TemporaryDirectory(dir=self.tmp_path) as directory:
                    tmp_path = Path(directory)
                    log = tmp_path / "check.log"
                    log.write_text("actual validator output\n")
                    summary = recorder(tmp_path, attempt_id="attempt-exact", executor_run_id=42)
                    outcome = ValidatorResult("unit", status, exit_code=code, log_path=log)
                    self.assertIs(summary.observe(0, lambda: outcome), outcome)
                    summary.finish()
                    data = read(summary)
                    row = data["validators"][0]
                    self.assertEqual(data["attempt_id"], "attempt-exact")
                    self.assertEqual(data["attempt_binding"], "runtime_claim")
                    self.assertEqual(data["executor_run_id"], 42)
                    self.assertIs(data["passed"], passed)
                    self.assertEqual(row["result"], status)
                    self.assertEqual(row["exit_code"], code)
                    self.assertTrue(data["started_at"] <= row["started_at"] <= row["ended_at"] <= data["ended_at"])
                    self.assertEqual(Path(row["artifact_path"]).read_text(), "actual validator output\n")
                    self.assertEqual(row["config_reference"], "test.validators[0] (unit)")
                    self.assertNotIn("command", row)  # A callback is not evidence of an argv.

    def test_real_subprocess_and_running_record(self):
        tmp_path = self.tmp_path
        summary = recorder(tmp_path)
        log = tmp_path / "real.log"

        def run():
            during = read(summary)
            self.assertEqual(during["validators"][0]["result"], "running")
            self.assertIs(during["validators"][0]["ended_at"], None)
            self.assertFalse(during["passed"])
            result = subprocess.run([sys.executable, "-c", "print('real evidence')"],
                                    capture_output=True, text=True, check=False, cwd=tmp_path)
            log.write_text(result.stdout + result.stderr)
            return ValidatorResult("unit", "passed", exit_code=result.returncode, log_path=log)

        summary.observe(0, run)
        summary.finish()
        self.assertTrue(read(summary)["passed"])

    def test_missing_evidence_is_incomplete_without_changing_verdict(self):
        tmp_path = self.tmp_path
        summary = recorder(tmp_path)
        result = ValidatorResult("unit", "passed", exit_code=0, log_path=tmp_path / "absent")
        self.assertIs(summary.observe(0, lambda: result), result)
        summary.finish()
        data = read(summary)
        self.assertTrue(not data["complete"] and not data["passed"])
        self.assertIs(data["validators"][0]["artifact_path"], None)
        self.assertEqual(data["validators"][0]["result"], "passed")

    def test_empty_required_set_never_passes(self):
        tmp_path = self.tmp_path
        summary = recorder(tmp_path, names=())
        summary.finish()
        self.assertFalse(read(summary)["complete"])
        self.assertFalse(read(summary)["passed"])

    def test_unbound_identity_is_explicit_without_guessing(self):
        tmp_path = self.tmp_path
        summary = recorder(tmp_path, integration_run_id="integration-1")
        data = read(summary)
        self.assertIs(data["attempt_id"], None)
        self.assertEqual(data["attempt_binding"], "unbound")
        self.assertEqual(data["integration_run_id"], "integration-1")
        self.assertIs(data["executor_run_id"], None)

    def test_no_root_never_claims_persisted_success(self):
        summary = recorder(None)
        summary.observe(0, lambda: ValidatorResult("unit", "passed", exit_code=0))
        summary.finish()
        self.assertIs(summary.path, None)
        self.assertTrue(not summary.payload["complete"] and not summary.payload["passed"])

    def test_exception_preserves_prior_output_and_not_run_rows(self):
        for exception in [RuntimeError("runner exploded"), KeyboardInterrupt()]:
            with self.subTest(exception=repr(exception)):
                with tempfile.TemporaryDirectory(dir=self.tmp_path) as directory:
                    tmp_path = Path(directory)
                    summary = recorder(tmp_path, names=("first", "broken", "last"))
                    log = tmp_path / "first.log"
                    log.write_text("prior output")
                    summary.observe(0, lambda: ValidatorResult("first", "passed", exit_code=0, log_path=log))

                    def explode():
                        raise exception

                    with self.assertRaises(type(exception)) as raised:
                        summary.observe(1, explode)
                    self.assertIs(raised.exception, exception)
                    data = read(summary)
                    self.assertTrue(data["state"] == "error" and not data["passed"])
                    self.assertEqual(Path(data["validators"][0]["artifact_path"]).read_text(), "prior output")
                    self.assertEqual(data["validators"][1]["error"]["type"], type(exception).__name__)
                    last = data["validators"][2]
                    self.assertEqual((last["result"], last["exit_code"], last["started_at"], last["ended_at"], last["artifact_path"]), (
                        "not_run", None, None, None, None,
                    ))

    def test_partial_timeout_output_is_preserved(self):
        tmp_path = self.tmp_path
        summary = recorder(tmp_path)

        def timeout():
            raise subprocess.TimeoutExpired(["command"], 1, output=b"partial\xff", stderr=b"stderr")

        with self.assertRaises(subprocess.TimeoutExpired):
            summary.observe(0, timeout)
        row = read(summary)["validators"][0]
        error = json.loads(Path(row["artifact_path"]).read_text())
        self.assertEqual(error["stdout"], "partial\ufffd")
        self.assertEqual(error["stderr"], "stderr")
        self.assertIs(row["exit_code"], None)

    def test_retry_and_same_integration_id_never_overwrite(self):
        tmp_path = self.tmp_path
        source = tmp_path / "reused.log"
        summaries = []
        for attempt in ("attempt-1", "attempt-2", "attempt-2"):
            source.write_text(f"output {len(summaries)}")
            summary = recorder(tmp_path, attempt_id=attempt, integration_run_id="same-run")
            summary.observe(0, lambda: ValidatorResult("unit", "passed", exit_code=0, log_path=source))
            summary.finish()
            summaries.append(summary)
        self.assertEqual(len({s.path for s in summaries}), 3)
        self.assertEqual(len({read(s)["validation_run_id"] for s in summaries}), 3)
        for index, summary in enumerate(summaries):
            self.assertEqual(Path(read(summary)["validators"][0]["artifact_path"]).read_text(), f"output {index}")

    def test_artifact_snapshot_rejects_unsafe_paths(self):
        for kind in ["outside", "symlink", "parent_symlink", "fifo", "traversal"]:
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory(dir=self.tmp_path) as directory:
                    tmp_path = Path(directory)
                    root = tmp_path / "artifacts"
                    root.mkdir()
                    outside = tmp_path / "outside.log"
                    outside.write_text("must not be copied")
                    source = root / "check.log"
                    if kind == "outside":
                        source = outside
                    elif kind == "symlink":
                        source.symlink_to(outside)
                    elif kind == "parent_symlink":
                        link = root / "alias"
                        link.symlink_to(tmp_path, target_is_directory=True)
                        source = link / "outside.log"
                    elif kind == "fifo":
                        os.mkfifo(source)
                    else:
                        source = root / ".." / "outside.log"
                    summary = recorder(root)
                    summary.observe(0, lambda: ValidatorResult("unit", "passed", exit_code=0, log_path=source))
                    summary.finish()
                    self.assertFalse(read(summary)["complete"])
                    self.assertIs(read(summary)["validators"][0]["artifact_path"], None)
                    self.assertEqual(outside.read_text(), "must not be copied")

    def test_snapshot_is_bounded_and_reports_truncation(self):
        tmp_path = self.tmp_path
        source = tmp_path / "large.log"
        source.write_bytes(b"x" * (MAX_EVIDENCE_BYTES + 1))
        summary = recorder(tmp_path)
        summary.observe(0, lambda: ValidatorResult("unit", "passed", log_path=source))
        summary.finish()
        data = read(summary)
        self.assertFalse(data["passed"])
        row = data["validators"][0]
        self.assertEqual(row["evidence_error"], "evidence_truncated")
        self.assertEqual(Path(row["artifact_path"]).stat().st_size, MAX_EVIDENCE_BYTES)

    def test_atomic_finish_failure_retains_previous_incomplete_record(self):
        tmp_path = self.tmp_path
        summary = recorder(tmp_path)
        source = tmp_path / "unit.log"
        source.write_text("output")
        summary.observe(0, lambda: ValidatorResult("unit", "passed", exit_code=0, log_path=source))
        before = summary.path.read_bytes()
        with mock.patch("agent_taskflow.atomic_write.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                summary.finish()
        self.assertEqual(summary.path.read_bytes(), before)
        self.assertFalse(read(summary)["passed"])
        self.assertFalse(summary.payload["passed"])
        self.assertEqual(summary.payload["recording_error"]["message"], "disk failure")

    def test_adapter_failure_without_an_exit_does_not_guess_tool_or_verdict(self):
        tmp_path = self.tmp_path
        summary = recorder(tmp_path)
        log = tmp_path / "gate.log"
        log.write_text("failure evidence")
        summary.observe(0, lambda: ValidatorResult(
            "unit", "failed", log_path=log, summary="adapter failure without an exit",
        ))
        summary.finish()
        row = read(summary)["validators"][0]
        self.assertEqual(row["outcome_kind"], "unclassified_failure")
        self.assertEqual(row["summary"], "adapter failure without an exit")
        self.assertFalse(read(summary)["passed"])

    def test_contradictory_pass_and_nonzero_exit_never_claim_success(self):
        tmp_path = self.tmp_path
        summary = recorder(tmp_path)
        log = tmp_path / "gate.log"
        log.write_text("failure evidence")
        summary.observe(0, lambda: ValidatorResult("unit", "passed", exit_code=9, log_path=log))
        summary.finish()
        self.assertFalse(read(summary)["passed"])

    def test_initial_atomic_failure_prevents_any_invocation(self):
        tmp_path = self.tmp_path
        with mock.patch("agent_taskflow.atomic_write.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                recorder(tmp_path)
        self.assertEqual(list(tmp_path.rglob("validation-summary.json")), [])

    def test_exact_claim_resource_overrides_stale_task_root_without_mutating_it(self):
        tmp_path = self.tmp_path
        original = tmp_path / "old-root"
        recorded = tmp_path / "attempt-root"
        store = mock.Mock()
        store.attempt_resource.return_value = SimpleNamespace(
            task_key="AT-1", attempt_id="attempt-1", artifact_root=recorded,
        )
        self.assertEqual(artifact_root_for_claim(store, "AT-1", "attempt-1", original), recorded)
        store.attempt_resource.assert_called_once_with("AT-1")
        store.attempt_resource.return_value.attempt_id = "different-attempt"
        with self.assertRaisesRegex(ValueError, "captured runtime claim"):
            artifact_root_for_claim(store, "AT-1", "attempt-1", original)

    def test_unbound_legacy_root_never_queries_an_attempt(self):
        tmp_path = self.tmp_path
        store = mock.Mock()
        self.assertEqual(artifact_root_for_claim(store, "AT-1", None, tmp_path), tmp_path)
        store.attempt_resource.assert_not_called()

    def test_malformed_result_is_recorded_as_error_and_propagates(self):
        tmp_path = self.tmp_path
        summary = recorder(tmp_path)
        with self.assertRaises(AttributeError):
            summary.observe(0, lambda: None)
        data = read(summary)
        self.assertFalse(data["passed"])
        self.assertEqual(data["state"], "error")
        self.assertEqual(data["validators"][0]["recording_error"]["type"], "AttributeError")

    def test_preexisting_destination_symlink_cannot_redirect_runner_output(self):
        tmp_path = self.tmp_path
        root = tmp_path / "trusted"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_text("unchanged")
        (root / "validation-runs").symlink_to(outside, target_is_directory=True)
        errors = []
        summary = recorder(root, on_error=errors.append)
        self.assertIs(summary.path, None)
        self.assertTrue(summary.recording_failed)
        self.assertEqual(errors[0]["kind"], "validation_summary_error")
        self.assertTrue(not errors[0]["complete"] and not errors[0]["passed"])
        self.assertEqual(list(outside.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_text(), "unchanged")

    def test_substituted_destination_is_refused_before_observation(self):
        for substitute in ["parent_symlink", "run_symlink", "new_directory"]:
            with self.subTest(substitute=substitute):
                with tempfile.TemporaryDirectory(dir=self.tmp_path) as directory:
                    tmp_path = Path(directory)
                    outside = tmp_path / "outside"
                    outside.mkdir()
                    root = tmp_path / "trusted"
                    summary = recorder(root)
                    changed = summary.directory.parent if substitute == "parent_symlink" else summary.directory
                    changed.rename(root / "retained-original")
                    if substitute == "new_directory":
                        changed.mkdir()
                    else:
                        changed.symlink_to(outside, target_is_directory=True)
                    run = mock.Mock()
                    with self.assertRaises(OSError):
                        summary.observe(0, run)
                    run.assert_not_called()
                    self.assertEqual(list(outside.iterdir()), [])

    def test_rename_between_open_and_atomic_write_cannot_redirect_the_write(self):
        tmp_path = self.tmp_path
        from agent_taskflow.validation_summary import atomic_write_json
        root = tmp_path / "trusted"
        outside = tmp_path / "outside"
        outside.mkdir()
        summary = recorder(root, names=())
        original = summary.directory
        retained = root / "retained-original"
        raced = []
        def race(anchored, payload, **kwargs):
            if not raced:
                raced.append(True)
                original.rename(retained)
                original.symlink_to(outside, target_is_directory=True)
            return atomic_write_json(anchored, payload, **kwargs)
        with mock.patch("agent_taskflow.validation_summary.atomic_write_json", side_effect=race):
            with self.assertRaises(OSError):
                summary.finish()
        self.assertEqual(raced, [True])
        self.assertTrue((retained / "validation-summary.json").is_file())
        retained_data = json.loads((retained / "validation-summary.json").read_text())
        self.assertTrue(not retained_data["complete"] and not retained_data["passed"])
        self.assertTrue(retained_data["recording_error"])
        self.assertEqual(list(outside.iterdir()), [])

    def test_error_sink_keeps_recording_failed_even_if_validator_passes(self):
        tmp_path = self.tmp_path
        errors = []
        with mock.patch("agent_taskflow.validation_summary.atomic_write_json", side_effect=OSError("disk failure")):
            summary = recorder(tmp_path, on_error=errors.append)
        result = ValidatorResult("unit", "passed", exit_code=0)
        self.assertIs(summary.observe(0, lambda: result), result)
        summary.finish()
        self.assertIs(summary.path, None)
        self.assertTrue(not summary.payload["complete"] and not summary.payload["passed"])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error"]["message"], "disk failure")

    def test_source_root_replaced_during_validator_is_never_read(self):
        for substitute in ["symlink", "new_directory"]:
            with self.subTest(substitute=substitute):
                with tempfile.TemporaryDirectory(dir=self.tmp_path) as directory:
                    tmp_path = Path(directory)
                    root = tmp_path / "trusted"
                    outside = tmp_path / "outside"
                    outside.mkdir()
                    (outside / "check.log").write_text("outside sentinel")
                    errors = []
                    summary = recorder(root, on_error=errors.append)
                    result = ValidatorResult("unit", "passed", exit_code=0, log_path=root / "check.log")

                    def run():
                        root.rename(tmp_path / "retained-root")
                        if substitute == "symlink":
                            root.symlink_to(outside, target_is_directory=True)
                        else:
                            root.mkdir()
                            (root / "check.log").write_text("replacement sentinel")
                        return result

                    with mock.patch("agent_taskflow.validation_summary.os.open", wraps=os.open) as opened:
                        self.assertIs(summary.observe(0, run), result)
                    summary.finish()
                    self.assertTrue(all(call.args[0] != "check.log" for call in opened.call_args_list))
                    self.assertTrue(summary.recording_failed)
                    self.assertTrue(not summary.payload["complete"] and not summary.payload["passed"])
                    self.assertIs(summary.payload["validators"][0]["artifact_path"], None)
                    self.assertTrue(errors and not errors[0]["passed"])
                    self.assertEqual((outside / "check.log").read_text(), "outside sentinel")


if __name__ == "__main__":
    unittest.main()
