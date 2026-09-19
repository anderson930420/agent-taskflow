"""Real-caller regressions for summary setup and publication boundaries."""

import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

from agent_taskflow import validation_summary as summary_module
from agent_taskflow.integration_validators import IntegrationValidatorSpec


def _fixtures(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dispatch = _fixtures("test_dispatcher")
approved = _fixtures("test_approved_task_runner")
integration = _fixtures("test_integration_validators")


class SummaryBoundaryTests(unittest.TestCase):
    def fixture(self, cls):
        fixture = cls()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def errors(self, store, key):
        return [json.loads(event.payload_json) for event in store.list_task_events(key)
                if event.payload_json and json.loads(event.payload_json).get("kind") == "validation_summary_error"]

    def test_setup_io_preserves_both_callers_worker_and_terminal_outcomes(self):
        for caller in ("dispatcher", "approved"):
            for operation in ("absolute", "open_root"):
                for executor_error in (False, True):
                    with self.subTest(caller=caller, operation=operation, executor_error=executor_error):
                        if caller == "dispatcher":
                            f = self.fixture(dispatch.DispatcherTests)
                            key = "AT-0007"
                            f.add_task()
                            worker = dispatch.FakeExecutor()
                            validator = dispatch.FakeValidator("unit")
                            invoke = lambda: f.make_dispatcher(
                                executor=worker, validators={"unit": validator}, validator_names=("unit",),
                            ).dispatch_task(key)
                        else:
                            f = self.fixture(approved.ApprovedTaskRunnerTests)
                            key = "AT-GH-401"
                            f._add_task(key)
                            f._write_codex_advisory_evidence(key)
                            worker = approved.FakeExecutor(name="noop")
                            validator = approved.FakeValidator(name="unit")
                            invoke = lambda: approved.run_approved_task(
                                f._request(validators=("unit",), preflight=False), store=f.store,
                                executor_registry={"noop": worker}, validator_registry={"unit": validator},
                            )
                        worker.run = mock.Mock(wraps=worker.run)
                        validator.run = mock.Mock(wraps=validator.run)
                        if executor_error:
                            worker.run.side_effect = RuntimeError("original executor exception")
                        fired = []
                        original = Path.absolute

                        def fail_absolute(path):
                            frame = sys._getframe(1)
                            if frame.f_globals.get("__name__") == summary_module.__name__:
                                fired.append(True)
                                raise OSError("summary path setup unavailable")
                            return original(path)

                        def fail_open(recorder, **kwargs):
                            fired.append(True)
                            raise OSError("summary root open unavailable")

                        target, name, replacement = (
                            (Path, "absolute", fail_absolute) if operation == "absolute" else
                            (summary_module.ValidationSummaryRecorder, "_open_root", fail_open)
                        )
                        with mock.patch.object(target, name, new=replacement):
                            result = invoke()
                        self.assertEqual(fired, [True], "the actual setup operation must fail exactly once")
                        worker.run.assert_called_once()
                        self.assertEqual(validator.run.call_count, 0 if executor_error else 1)
                        run = f.store.list_executor_runs(key)[0]
                        self.assertIsNotNone(run["status"])
                        self.assertIsNotNone(run["finished_at"])
                        errors = self.errors(f.store, key)
                        self.assertEqual(len(errors), 1)
                        self.assertFalse(errors[0]["complete"])
                        self.assertFalse(errors[0]["passed"])
                        self.assertEqual(errors[0]["error"]["type"], "OSError")
                        if executor_error:
                            message = result.blocked_reason if caller == "dispatcher" else result.error
                            self.assertIn("original executor exception", message)
                            self.assertNotIn("unavailable", message)
                            self.assertIn("original executor exception", run["summary"])
                        elif caller == "dispatcher":
                            self.assertEqual(result.status, "waiting_approval")
                        else:
                            self.assertTrue(result.ok, result.error)

    def test_final_publication_detects_run_replacement_and_broken_evidence(self):
        for replacement in ("symlink", "directory", "missing_evidence", "replaced_evidence"):
            with self.subTest(replacement=replacement):
                f = self.fixture(integration.IntegrationValidatorTestCase)
                outside = f.root / "outside"
                outside.mkdir()
                sentinel = outside / "sentinel"
                sentinel.write_text("unchanged")
                original = summary_module.atomic_write_json
                raced = []
                retained = []

                def race(path, payload, **kwargs):
                    if payload.get("kind") == "validation_summary" and payload.get("state") == "finished" and not raced:
                        raced.append(True)
                        run = f.artifact_dir / "validation-runs" / payload["validation_run_id"]
                        if replacement in {"symlink", "directory"}:
                            old = f.root / "retained-run"
                            run.rename(old)
                            retained.append(old)
                            if replacement == "symlink":
                                run.symlink_to(outside, target_is_directory=True)
                            else:
                                run.mkdir()
                                (run / "sentinel").write_text("replacement directory")
                        else:
                            retained.append(run)
                            evidence = Path(payload["validators"][0]["artifact_path"])
                            evidence.rename(run / "retained-evidence.json")
                            if replacement == "replaced_evidence":
                                evidence.write_text('{"output":"replacement"}')
                    return original(path, payload, **kwargs)

                with mock.patch.object(summary_module, "atomic_write_json", side_effect=race):
                    report = f._run((IntegrationValidatorSpec(
                        "real-unit", (sys.executable, "-c", "print('actual boundary validator')"),
                    ),))
                self.assertEqual(raced, [True])
                self.assertTrue(report.passed)
                self.assertEqual(report.outcomes[0].exit_code, 0)
                self.assertIn("actual boundary validator", report.outcomes[0].output)
                data = json.loads((retained[0] / "validation-summary.json").read_text())
                self.assertFalse(data["complete"])
                self.assertFalse(data["passed"])
                self.assertIn("recording_error", data)
                errors = self.errors(f.store, "AT-701")
                self.assertEqual(len(errors), 1)
                self.assertFalse(errors[0]["complete"])
                self.assertFalse(errors[0]["passed"])
                self.assertEqual(list(outside.iterdir()), [sentinel])
                self.assertEqual(sentinel.read_text(), "unchanged")
                if replacement == "directory":
                    run = f.artifact_dir / "validation-runs" / data["validation_run_id"]
                    self.assertEqual([p.name for p in run.iterdir()], ["sentinel"])

    def test_initial_root_alias_and_parent_chain_never_publish_summary_outputs(self):
        for alias in ("root", "chain", "parent"):
            with self.subTest(alias=alias):
                f = self.fixture(integration.IntegrationValidatorTestCase)
                requested = f.artifact_dir
                outside = f.root / "outside"
                outside.mkdir()
                sentinel = outside / "sentinel"
                sentinel.write_text("unchanged")
                if alias == "parent":
                    link = f.root / "parent-alias"
                    link.symlink_to(outside, target_is_directory=True)
                    requested = link / "nested" / "artifacts"
                else:
                    requested.rmdir()
                    target = outside
                    if alias == "chain":
                        target = f.root / "second-alias"
                        target.symlink_to(outside, target_is_directory=True)
                    requested.symlink_to(target, target_is_directory=True)
                report = f._run((IntegrationValidatorSpec(
                    "real-unit", (sys.executable, "-c", "print('root boundary validator')"),
                ),), artifact_dir=requested)
                self.assertTrue(report.passed)
                self.assertIn("root boundary validator", report.outcomes[0].output)
                self.assertEqual(list(outside.rglob("validation-runs")), [])
                self.assertFalse(any(a.path.name == "validation-summary.json"
                                     for a in f.store.list_task_artifacts("AT-701")))
                self.assertEqual(sentinel.read_text(), "unchanged")
                errors = self.errors(f.store, "AT-701")
                self.assertEqual(len(errors), 1)
                self.assertFalse(errors[0]["complete"])
                self.assertFalse(errors[0]["passed"])
                self.assertIsNone(errors[0]["artifact_path"])
                # Existing integration report policy is deliberately preserved.
                self.assertTrue(report.evidence_path.is_file())

    def test_failed_recovery_invalidates_only_its_summary_and_audits_failure(self):
        f = self.fixture(integration.IntegrationValidatorTestCase)
        original = summary_module.atomic_write_json
        retained = f.root / "retained-run"
        raced = []
        recovery_failures = []

        def write(path, payload, **kwargs):
            if payload.get("kind") == "validation_summary":
                if payload.get("state") == "error":
                    recovery_failures.append(True)
                    raise OSError("recovery write unavailable")
                if payload.get("state") == "finished":
                    result = original(path, payload, **kwargs)
                    run = f.artifact_dir / "validation-runs" / payload["validation_run_id"]
                    run.rename(retained)
                    run.mkdir()
                    (run / "sentinel").write_text("replacement directory")
                    raced.append(run)
                    return result
            return original(path, payload, **kwargs)

        with mock.patch.object(summary_module, "atomic_write_json", side_effect=write):
            report = f._run((IntegrationValidatorSpec(
                "real-unit", (sys.executable, "-c", "print('compound failure validator')"),
            ),))
        self.assertTrue(report.passed)
        self.assertEqual(len(raced), 1)
        self.assertEqual(recovery_failures, [True])
        self.assertFalse((retained / "validation-summary.json").exists())
        evidence = json.loads((retained / "validator-000-evidence.json").read_text())
        self.assertIn("compound failure validator", evidence["output"])
        self.assertEqual([p.name for p in raced[0].iterdir()], ["sentinel"])
        errors = self.errors(f.store, "AT-701")
        self.assertEqual(len(errors), 1)
        self.assertFalse(errors[0]["complete"])
        self.assertFalse(errors[0]["passed"])
        self.assertIsNone(errors[0]["artifact_path"])
        self.assertEqual(errors[0]["recovery"]["error"]["message"], "recovery write unavailable")
        self.assertEqual(errors[0]["recovery"]["invalidated_summary_path"], str(raced[0] / "validation-summary.json"))


if __name__ == "__main__":
    unittest.main()
