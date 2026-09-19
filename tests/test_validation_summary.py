"""Validation evidence must survive partial runs and never manufacture a pass."""

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock
from types import SimpleNamespace

import pytest

from agent_taskflow.validation_summary import MAX_EVIDENCE_BYTES, ValidationSummaryRecorder, artifact_root_for_claim
from agent_taskflow.validators.base import ValidatorResult


def recorder(root, names=("unit",), **kwargs):
    return ValidationSummaryRecorder(
        task_key="AT-1", artifact_dir=root, source="test", phase="implementation_validation",
        validators=names, **kwargs,
    )


def read(summary):
    return json.loads(summary.path.read_text())


@pytest.mark.parametrize("status,code,passed", [
    ("passed", 0, True), ("failed", 3, False),
    ("blocked", None, False), ("skipped", None, False),
])
def test_observed_verdicts_and_actual_evidence(tmp_path, status, code, passed):
    log = tmp_path / "check.log"
    log.write_text("actual validator output\n")
    summary = recorder(tmp_path, attempt_id="attempt-exact", executor_run_id=42)
    outcome = ValidatorResult("unit", status, exit_code=code, log_path=log)
    assert summary.observe(0, lambda: outcome) is outcome
    summary.finish()
    data = read(summary)
    row = data["validators"][0]
    assert data["attempt_id"] == "attempt-exact"
    assert data["attempt_binding"] == "runtime_claim"
    assert data["executor_run_id"] == 42
    assert data["passed"] is passed
    assert row["result"] == status
    assert row["exit_code"] == code
    assert data["started_at"] <= row["started_at"] <= row["ended_at"] <= data["ended_at"]
    assert Path(row["artifact_path"]).read_text() == "actual validator output\n"
    assert row["config_reference"] == "test.validators[0] (unit)"
    assert "command" not in row  # A callback is not evidence of an argv.


def test_real_subprocess_and_running_record(tmp_path):
    summary = recorder(tmp_path)
    log = tmp_path / "real.log"

    def run():
        during = read(summary)
        assert during["validators"][0]["result"] == "running"
        assert during["validators"][0]["ended_at"] is None
        assert not during["passed"]
        result = subprocess.run([sys.executable, "-c", "print('real evidence')"],
                                capture_output=True, text=True, check=False, cwd=tmp_path)
        log.write_text(result.stdout + result.stderr)
        return ValidatorResult("unit", "passed", exit_code=result.returncode, log_path=log)

    summary.observe(0, run)
    summary.finish()
    assert read(summary)["passed"]


def test_missing_evidence_is_incomplete_without_changing_verdict(tmp_path):
    summary = recorder(tmp_path)
    result = ValidatorResult("unit", "passed", exit_code=0, log_path=tmp_path / "absent")
    assert summary.observe(0, lambda: result) is result
    summary.finish()
    data = read(summary)
    assert not data["complete"] and not data["passed"]
    assert data["validators"][0]["artifact_path"] is None
    assert data["validators"][0]["result"] == "passed"


def test_empty_required_set_never_passes(tmp_path):
    summary = recorder(tmp_path, names=())
    summary.finish()
    assert not read(summary)["complete"]
    assert not read(summary)["passed"]


def test_unbound_identity_is_explicit_without_guessing(tmp_path):
    summary = recorder(tmp_path, integration_run_id="integration-1")
    data = read(summary)
    assert data["attempt_id"] is None
    assert data["attempt_binding"] == "unbound"
    assert data["integration_run_id"] == "integration-1"
    assert data["executor_run_id"] is None


def test_no_root_never_claims_persisted_success():
    summary = recorder(None)
    summary.observe(0, lambda: ValidatorResult("unit", "passed", exit_code=0))
    summary.finish()
    assert summary.path is None
    assert not summary.payload["complete"] and not summary.payload["passed"]


@pytest.mark.parametrize("exception", [RuntimeError("runner exploded"), KeyboardInterrupt()])
def test_exception_preserves_prior_output_and_not_run_rows(tmp_path, exception):
    summary = recorder(tmp_path, names=("first", "broken", "last"))
    log = tmp_path / "first.log"
    log.write_text("prior output")
    summary.observe(0, lambda: ValidatorResult("first", "passed", exit_code=0, log_path=log))

    def explode():
        raise exception

    with pytest.raises(type(exception)) as raised:
        summary.observe(1, explode)
    assert raised.value is exception
    data = read(summary)
    assert data["state"] == "error" and not data["passed"]
    assert Path(data["validators"][0]["artifact_path"]).read_text() == "prior output"
    assert data["validators"][1]["error"]["type"] == type(exception).__name__
    last = data["validators"][2]
    assert (last["result"], last["exit_code"], last["started_at"], last["ended_at"], last["artifact_path"]) == (
        "not_run", None, None, None, None,
    )


def test_partial_timeout_output_is_preserved(tmp_path):
    summary = recorder(tmp_path)

    def timeout():
        raise subprocess.TimeoutExpired(["command"], 1, output=b"partial\xff", stderr=b"stderr")

    with pytest.raises(subprocess.TimeoutExpired):
        summary.observe(0, timeout)
    row = read(summary)["validators"][0]
    error = json.loads(Path(row["artifact_path"]).read_text())
    assert error["stdout"] == "partial\ufffd"
    assert error["stderr"] == "stderr"
    assert row["exit_code"] is None


def test_retry_and_same_integration_id_never_overwrite(tmp_path):
    source = tmp_path / "reused.log"
    summaries = []
    for attempt in ("attempt-1", "attempt-2", "attempt-2"):
        source.write_text(f"output {len(summaries)}")
        summary = recorder(tmp_path, attempt_id=attempt, integration_run_id="same-run")
        summary.observe(0, lambda: ValidatorResult("unit", "passed", exit_code=0, log_path=source))
        summary.finish()
        summaries.append(summary)
    assert len({s.path for s in summaries}) == 3
    assert len({read(s)["validation_run_id"] for s in summaries}) == 3
    for index, summary in enumerate(summaries):
        assert Path(read(summary)["validators"][0]["artifact_path"]).read_text() == f"output {index}"


@pytest.mark.parametrize("kind", ["outside", "symlink", "parent_symlink", "fifo", "traversal"])
def test_artifact_snapshot_rejects_unsafe_paths(tmp_path, kind):
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
    assert not read(summary)["complete"]
    assert read(summary)["validators"][0]["artifact_path"] is None
    assert outside.read_text() == "must not be copied"


def test_snapshot_is_bounded_and_reports_truncation(tmp_path):
    source = tmp_path / "large.log"
    source.write_bytes(b"x" * (MAX_EVIDENCE_BYTES + 1))
    summary = recorder(tmp_path)
    summary.observe(0, lambda: ValidatorResult("unit", "passed", log_path=source))
    summary.finish()
    data = read(summary)
    assert not data["passed"]
    row = data["validators"][0]
    assert row["evidence_error"] == "evidence_truncated"
    assert Path(row["artifact_path"]).stat().st_size == MAX_EVIDENCE_BYTES


def test_atomic_finish_failure_retains_previous_incomplete_record(tmp_path):
    summary = recorder(tmp_path)
    source = tmp_path / "unit.log"
    source.write_text("output")
    summary.observe(0, lambda: ValidatorResult("unit", "passed", exit_code=0, log_path=source))
    before = summary.path.read_bytes()
    with mock.patch("agent_taskflow.atomic_write.os.replace", side_effect=OSError("disk failure")):
        with pytest.raises(OSError, match="disk failure"):
            summary.finish()
    assert summary.path.read_bytes() == before
    assert not read(summary)["passed"]
    assert not summary.payload["passed"]
    assert summary.payload["recording_error"]["message"] == "disk failure"


def test_adapter_failure_without_an_exit_does_not_guess_tool_or_verdict(tmp_path):
    summary = recorder(tmp_path)
    log = tmp_path / "gate.log"
    log.write_text("failure evidence")
    summary.observe(0, lambda: ValidatorResult(
        "unit", "failed", log_path=log, summary="adapter failure without an exit",
    ))
    summary.finish()
    row = read(summary)["validators"][0]
    assert row["outcome_kind"] == "unclassified_failure"
    assert row["summary"] == "adapter failure without an exit"
    assert not read(summary)["passed"]


def test_contradictory_pass_and_nonzero_exit_never_claim_success(tmp_path):
    summary = recorder(tmp_path)
    log = tmp_path / "gate.log"
    log.write_text("failure evidence")
    summary.observe(0, lambda: ValidatorResult("unit", "passed", exit_code=9, log_path=log))
    summary.finish()
    assert not read(summary)["passed"]


def test_initial_atomic_failure_prevents_any_invocation(tmp_path):
    with mock.patch("agent_taskflow.atomic_write.os.replace", side_effect=OSError("disk failure")):
        with pytest.raises(OSError, match="disk failure"):
            recorder(tmp_path)
    assert list(tmp_path.rglob("validation-summary.json")) == []


def test_exact_claim_resource_overrides_stale_task_root_without_mutating_it(tmp_path):
    original = tmp_path / "old-root"
    recorded = tmp_path / "attempt-root"
    store = mock.Mock()
    store.attempt_resource.return_value = SimpleNamespace(
        task_key="AT-1", attempt_id="attempt-1", artifact_root=recorded,
    )
    assert artifact_root_for_claim(store, "AT-1", "attempt-1", original) == recorded
    store.attempt_resource.assert_called_once_with("AT-1")
    store.attempt_resource.return_value.attempt_id = "different-attempt"
    with pytest.raises(ValueError, match="captured runtime claim"):
        artifact_root_for_claim(store, "AT-1", "attempt-1", original)


def test_unbound_legacy_root_never_queries_an_attempt(tmp_path):
    store = mock.Mock()
    assert artifact_root_for_claim(store, "AT-1", None, tmp_path) == tmp_path
    store.attempt_resource.assert_not_called()


def test_malformed_result_is_recorded_as_error_and_propagates(tmp_path):
    summary = recorder(tmp_path)
    with pytest.raises(AttributeError):
        summary.observe(0, lambda: None)
    data = read(summary)
    assert not data["passed"]
    assert data["state"] == "error"
    assert data["validators"][0]["recording_error"]["type"] == "AttributeError"


def test_preexisting_destination_symlink_cannot_redirect_runner_output(tmp_path):
    root = tmp_path / "trusted"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("unchanged")
    (root / "validation-runs").symlink_to(outside, target_is_directory=True)
    errors = []
    summary = recorder(root, on_error=errors.append)
    assert summary.path is None
    assert summary.recording_failed
    assert errors[0]["kind"] == "validation_summary_error"
    assert not errors[0]["complete"] and not errors[0]["passed"]
    assert list(outside.iterdir()) == [sentinel]
    assert sentinel.read_text() == "unchanged"


@pytest.mark.parametrize("substitute", ["parent_symlink", "run_symlink", "new_directory"])
def test_substituted_destination_is_refused_before_observation(tmp_path, substitute):
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
    with pytest.raises(OSError):
        summary.observe(0, run)
    run.assert_not_called()
    assert list(outside.iterdir()) == []


def test_rename_between_open_and_atomic_write_cannot_redirect_the_write(tmp_path):
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
        with pytest.raises(OSError):
            summary.finish()
    assert raced == [True]
    assert (retained / "validation-summary.json").is_file()
    retained_data = json.loads((retained / "validation-summary.json").read_text())
    assert not retained_data["complete"] and not retained_data["passed"]
    assert retained_data["recording_error"]
    assert list(outside.iterdir()) == []


def test_error_sink_keeps_recording_failed_even_if_validator_passes(tmp_path):
    errors = []
    with mock.patch("agent_taskflow.validation_summary.atomic_write_json", side_effect=OSError("disk failure")):
        summary = recorder(tmp_path, on_error=errors.append)
    result = ValidatorResult("unit", "passed", exit_code=0)
    assert summary.observe(0, lambda: result) is result
    summary.finish()
    assert summary.path is None
    assert not summary.payload["complete"] and not summary.payload["passed"]
    assert len(errors) == 1
    assert errors[0]["error"]["message"] == "disk failure"


@pytest.mark.parametrize("substitute", ["symlink", "new_directory"])
def test_source_root_replaced_during_validator_is_never_read(tmp_path, substitute):
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
        assert summary.observe(0, run) is result
    summary.finish()
    assert all(call.args[0] != "check.log" for call in opened.call_args_list)
    assert summary.recording_failed
    assert not summary.payload["complete"] and not summary.payload["passed"]
    assert summary.payload["validators"][0]["artifact_path"] is None
    assert errors and not errors[0]["passed"]
    assert (outside / "check.log").read_text() == "outside sentinel"
