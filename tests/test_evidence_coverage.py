"""L2-M2.2: the shared runner evidence index states what exists and what does not.

The coverage index is published by real runs — the real Dispatcher and the real
integration validator runner — and every assertion here reads the file those
runs wrote. Absence is asserted as absence: no test accepts a coverage entry
that claims evidence which is not readable on disk.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5_support import RecordingExecutor, make_fixture  # noqa: E402

from agent_taskflow.evidence_coverage import (  # noqa: E402
    COVERAGE_ARTIFACT_NAME,
    COVERAGE_KIND,
    EVIDENCE_CHANGED_FILES_AUDIT,
    EVIDENCE_COMPILEALL_LOG,
    EVIDENCE_DUAL_WRITE_CONSISTENCY,
    EVIDENCE_EXECUTOR_LAUNCH_SPEC,
    EVIDENCE_KINDS,
    EVIDENCE_POLICY_VALIDATE_LOG,
    EVIDENCE_PREFLIGHT_PR_CHECK,
    EVIDENCE_VALIDATION_SUMMARY,
    EVIDENCE_VALIDATOR_LOGS,
    RunnerEvidenceCollector,
    validator_config_identity,
)
from agent_taskflow.integration_store import IntegrationStore  # noqa: E402
from agent_taskflow.integration_validators import (  # noqa: E402
    IntegrationValidatorSpec,
    run_integration_validators,
)
from agent_taskflow.models import TaskRecord  # noqa: E402
from agent_taskflow.store import TaskMirrorStore  # noqa: E402
from agent_taskflow.validation_summary import ValidationSummaryRecorder  # noqa: E402
from agent_taskflow.validators.base import (  # noqa: E402
    Validator,
    ValidatorContext,
    ValidatorResult,
)
from agent_taskflow.validators.policy import PolicyCheckValidator  # noqa: E402
from agent_taskflow.validators.pytest import PytestValidator  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_module(name: str):
    """Load a sibling test module for its fixtures, as the boundary suite does."""
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ArtifactValidator(Validator):
    """A validator that writes real files and reports them, like the real ones."""

    def __init__(
        self,
        name: str,
        *,
        artifacts: dict[str, str],
        status: str = "passed",
        exit_code: int | None = 0,
        missing: dict[str, str] | None = None,
        command: list[str] | None = None,
    ) -> None:
        self.name = name
        self._artifacts = artifacts
        self._missing = missing or {}
        self._status = status
        self._exit_code = exit_code
        if command is not None:
            self.command = command

    def run(self, context: ValidatorContext) -> ValidatorResult:
        produced: dict[str, Path] = {}
        for key, filename in self._artifacts.items():
            path = context.artifact_dir / filename
            path.write_text(f"{self.name} wrote {filename}\n", encoding="utf-8")
            produced[key] = path
        for key, filename in self._missing.items():
            produced[key] = context.artifact_dir / filename
        return ValidatorResult(
            validator=self.name,
            status=self._status,
            exit_code=self._exit_code,
            log_path=produced.get("log"),
            artifacts=produced,
            summary=f"{self.name} {self._status}",
        )


class DispatcherCoverageTests(unittest.TestCase):
    """The dispatcher publishes coverage for the run it actually performed."""

    def setUp(self) -> None:
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        self.fx.repository = replace(self.fx.repository, github_repo="owner/alpha")
        self.store = TaskMirrorStore(self.fx.db_path)

    # -- helpers -----------------------------------------------------------
    def run_dir(self, task_key: str) -> Path:
        roots = [
            path
            for path in (self.fx.artifacts / task_key).rglob("validation-runs")
            if path.is_dir()
        ]
        self.assertEqual(len(roots), 1, roots)
        runs = sorted(roots[0].iterdir())
        self.assertEqual(len(runs), 1)
        return runs[0]

    def coverage(self, task_key: str) -> dict:
        return json.loads((self.run_dir(task_key) / COVERAGE_ARTIFACT_NAME).read_text())

    def summary(self, task_key: str) -> dict:
        return json.loads((self.run_dir(task_key) / "validation-summary.json").read_text())

    def item(self, coverage: dict, evidence: str) -> dict:
        matches = [entry for entry in coverage["items"] if entry["evidence"] == evidence]
        self.assertEqual(len(matches), 1, evidence)
        return matches[0]

    # -- tests -------------------------------------------------------------
    def test_every_required_evidence_kind_is_indexed_once(self) -> None:
        key = self.fx.create_ticket("Index the evidence").task_key
        self.fx.dispatch(key)

        coverage = self.coverage(key)
        self.assertEqual(coverage["kind"], COVERAGE_KIND)
        self.assertEqual(coverage["task_key"], key)
        self.assertEqual(
            [entry["evidence"] for entry in coverage["items"]], list(EVIDENCE_KINDS)
        )
        self.assertEqual(coverage["attempt_id"], self.fx.attempts(key)[-1]["attempt_id"])
        self.assertEqual(coverage["attempt_binding"], "runtime_claim")
        self.assertEqual(
            coverage["validation_run_id"], self.summary(key)["validation_run_id"]
        )
        # The summary points back at the index it published.
        self.assertEqual(
            self.summary(key)["evidence_coverage"]["path"],
            str(self.run_dir(key) / COVERAGE_ARTIFACT_NAME),
        )
        self.assertEqual(self.summary(key)["evidence_coverage"]["status"], "published")

    def test_a_present_reference_is_a_readable_file_with_its_own_digest(self) -> None:
        key = self.fx.create_ticket("Readable evidence").task_key
        validator = ArtifactValidator("audit", artifacts={"log": "audit.log"})
        self.fx.dispatch(key, validators=(validator,))

        logs = self.item(self.coverage(key), EVIDENCE_VALIDATOR_LOGS)
        self.assertEqual(logs["status"], "present")
        self.assertEqual(len(logs["references"]), 1)
        reference = logs["references"][0]
        path = Path(reference["path"])
        self.assertTrue(path.is_file())
        self.assertTrue(reference["readable"])
        self.assertTrue(reference["inside_artifact_root"])
        self.assertEqual(reference["sha256"], sha256(path))
        self.assertEqual(reference["size_bytes"], path.stat().st_size)
        self.assertIsNone(reference["error"])

    def test_a_validator_artifact_supplies_its_own_evidence_kind(self) -> None:
        key = self.fx.create_ticket("Changed files and policy").task_key
        validators = (
            ArtifactValidator(
                "changed-files",
                artifacts={"log": "changed-files-validate.log", "audit": EVIDENCE_CHANGED_FILES_AUDIT},
            ),
            ArtifactValidator("policy", artifacts={"log": EVIDENCE_POLICY_VALIDATE_LOG}),
        )
        self.fx.dispatch(key, validators=validators)

        coverage = self.coverage(key)
        audit = self.item(coverage, EVIDENCE_CHANGED_FILES_AUDIT)
        self.assertEqual(audit["status"], "present")
        self.assertEqual(audit["producers"], ["changed-files"])
        self.assertEqual(
            Path(audit["references"][0]["path"]).name, EVIDENCE_CHANGED_FILES_AUDIT
        )
        policy = self.item(coverage, EVIDENCE_POLICY_VALIDATE_LOG)
        self.assertEqual(policy["status"], "present")
        self.assertEqual(policy["producers"], ["policy"])

    def test_an_evidence_kind_no_validator_produces_is_not_applicable(self) -> None:
        key = self.fx.create_ticket("Nothing produces compileall").task_key
        self.fx.dispatch(key, validators=(ArtifactValidator("unit", artifacts={"log": "unit.log"}),))

        item = self.item(self.coverage(key), EVIDENCE_COMPILEALL_LOG)
        self.assertEqual(item["applicability"], "not_applicable")
        self.assertEqual(item["status"], "not_applicable")
        self.assertEqual(item["references"], [])
        # The reason names this run's own configuration, not a fixed framework.
        self.assertIn("unit", item["reason"])
        self.assertIn("No validator configured for this run", item["reason"])

    def test_a_configured_compileall_command_makes_its_log_applicable(self) -> None:
        key = self.fx.create_ticket("Compileall configured").task_key
        validator = ArtifactValidator(
            "compile",
            artifacts={"log": EVIDENCE_COMPILEALL_LOG},
            command=[sys.executable, "-m", "compileall", "-q", "agent_taskflow"],
        )
        self.fx.dispatch(key, validators=(validator,))

        item = self.item(self.coverage(key), EVIDENCE_COMPILEALL_LOG)
        self.assertEqual(item["applicability"], "applicable")
        self.assertEqual(item["status"], "present")
        self.assertEqual(
            Path(item["references"][0]["path"]).name, EVIDENCE_COMPILEALL_LOG
        )

    def test_a_reported_artifact_that_is_missing_is_not_reported_present(self) -> None:
        key = self.fx.create_ticket("Missing audit").task_key
        validator = ArtifactValidator(
            "changed-files",
            artifacts={"log": "changed-files-validate.log"},
            missing={"audit": EVIDENCE_CHANGED_FILES_AUDIT},
        )
        self.fx.dispatch(key, validators=(validator,))

        coverage = self.coverage(key)
        audit = self.item(coverage, EVIDENCE_CHANGED_FILES_AUDIT)
        self.assertEqual(audit["applicability"], "applicable")
        self.assertEqual(audit["status"], "missing")
        reference = audit["references"][0]
        self.assertFalse(reference["readable"])
        self.assertIsNone(reference["sha256"])
        self.assertTrue(reference["error"].startswith("FileNotFoundError:"))
        self.assertIn(EVIDENCE_CHANGED_FILES_AUDIT, coverage["unresolved"])
        self.assertFalse(coverage["complete"])

    def test_unobserved_evidence_is_unknown_and_never_invented(self) -> None:
        key = self.fx.create_ticket("Nothing observed").task_key
        self.fx.dispatch(key)

        coverage = self.coverage(key)
        for evidence in (EVIDENCE_PREFLIGHT_PR_CHECK, EVIDENCE_DUAL_WRITE_CONSISTENCY):
            item = self.item(coverage, evidence)
            self.assertEqual(item["applicability"], "unknown", evidence)
            self.assertEqual(item["status"], "unknown", evidence)
            self.assertEqual(item["references"], [], evidence)
            self.assertTrue(item["reason"])
            self.assertNotIn(evidence, coverage["unresolved"])

    def test_the_executor_launch_spec_is_linked_when_the_executor_reports_it(self) -> None:
        key = self.fx.create_ticket("Launch spec").task_key
        written: list[Path] = []

        class LaunchingExecutor(RecordingExecutor):
            """Writes its launch spec where the managed launch path writes it.

            `executor_launch.run_managed_process` publishes the spec under the
            Attempt's own artifact root, which is the root the recorder is bound
            to, so the reference is inside the publication boundary.
            """

            def run(self, context):
                result = super().run(context)
                spec_path = Path(context.artifact_dir) / "executor-launch-spec.json"
                spec_path.parent.mkdir(parents=True, exist_ok=True)
                spec_path.write_text('{"argv": ["fake"]}\n', encoding="utf-8")
                written.append(spec_path)
                return replace(result, artifacts={"executor_launch_spec": spec_path})

        self.fx.dispatch(key, LaunchingExecutor())

        spec_path = written[0]
        item = self.item(self.coverage(key), EVIDENCE_EXECUTOR_LAUNCH_SPEC)
        self.assertEqual(item["status"], "present")
        reference = item["references"][0]
        self.assertEqual(reference["path"], str(spec_path))
        self.assertEqual(reference["origin"], "reported_by_executor")
        self.assertEqual(reference["sha256"], sha256(spec_path))
        self.assertTrue(reference["admissible"])
        self.assertTrue(reference["inside_recorder_root"])

    def test_an_executor_without_a_managed_launch_says_so(self) -> None:
        key = self.fx.create_ticket("No launch spec").task_key
        self.fx.dispatch(key)

        item = self.item(self.coverage(key), EVIDENCE_EXECUTOR_LAUNCH_SPEC)
        self.assertEqual(item["applicability"], "applicable")
        self.assertEqual(item["status"], "not_run")
        self.assertEqual(item["references"], [])
        self.assertIn("reported no managed", item["reason"])

    def test_a_stopped_run_still_publishes_what_it_did_and_did_not_produce(self) -> None:
        key = self.fx.create_ticket("Red validator").task_key
        red = ArtifactValidator("unit", artifacts={"log": "unit.log"}, status="failed", exit_code=2)
        result = self.fx.dispatch(key, validators=(red,))

        # §29.1 for a Ticket: a red validator stops for a decision. Unchanged.
        self.assertEqual(result.status, "needs_decision")
        coverage = self.coverage(key)
        self.assertEqual(self.summary(key)["state"], "stopped")
        logs = self.item(coverage, EVIDENCE_VALIDATOR_LOGS)
        self.assertEqual(logs["status"], "present")
        row = logs["detail"]["validators"][0]
        self.assertEqual(row["result"], "failed")
        self.assertEqual(row["exit_code"], 2)
        self.assertEqual(row["outcome_kind"], "validator_verdict")

    def test_a_validator_that_raises_keeps_its_identity_and_tool_error(self) -> None:
        key = self.fx.create_ticket("Raising validator").task_key

        class Raising(Validator):
            name = "unit"

            def run(self, context: ValidatorContext) -> ValidatorResult:
                raise RuntimeError("validator tooling broke")

        result = self.fx.dispatch(key, validators=(Raising(),))

        self.assertEqual(result.status, "failed")
        row = self.summary(key)["validators"][0]
        self.assertEqual(row["result"], "tool_error")
        self.assertEqual(row["outcome_kind"], "tool_error")
        self.assertEqual(row["error"]["type"], "RuntimeError")
        # The identity was captured before the invocation, so the failed row
        # still names what was resolved and run — including the runtime path's
        # own proxies, with the real validator at the end of the chain.
        self.assertEqual(
            row["config_identity"]["implementation_chain"][-1],
            f"{Raising.__module__}.{Raising.__qualname__}",
        )
        self.assertEqual(
            row["config_identity"]["implementation"],
            row["config_identity"]["implementation_chain"][0],
        )
        self.assertEqual(
            row["config_identity"]["command_availability"],
            "in_process_validator_exposes_no_command",
        )
        # The error evidence is the row's own artifact, and coverage links it.
        self.assertTrue(Path(row["artifact_path"]).is_file())
        logs = self.item(self.coverage(key), EVIDENCE_VALIDATOR_LOGS)
        self.assertEqual(logs["references"][0]["path"], row["artifact_path"])
        self.assertTrue(logs["references"][0]["readable"])

    def test_a_command_validator_records_the_argv_it_was_about_to_run(self) -> None:
        key = self.fx.create_ticket("Command identity").task_key
        validator = ArtifactValidator(
            "unit", artifacts={"log": "unit.log"}, command=["/usr/bin/true", "--now"]
        )
        self.fx.dispatch(key, validators=(validator,))

        row = self.summary(key)["validators"][0]
        self.assertEqual(row["command"], ["/usr/bin/true", "--now"])
        self.assertEqual(row["config_identity"]["command_reference"], "ArtifactValidator.command")
        self.assertEqual(
            row["config_identity"]["resolution"], "dispatcher_validator_registry"
        )
        self.assertEqual(row["config_identity"]["config_source"], "Dispatcher.validators")


class ApprovedRunnerCoverageTests(unittest.TestCase):
    """The other runner seam publishes the same shared index."""

    def setUp(self) -> None:
        self.approved = _load_module("test_approved_task_runner")
        self.fx = self.approved.ApprovedTaskRunnerTests()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)

    def test_the_approved_runner_indexes_its_own_evidence(self) -> None:
        key = "AT-GH-401"
        self.fx._add_task(key)
        self.fx._write_codex_advisory_evidence(key)
        validator = self.approved.FakeValidator(name="unit")
        result = self.approved.run_approved_task(
            self.fx._request(validators=("unit",), preflight=False),
            store=self.fx.store,
            executor_registry={"noop": self.approved.FakeExecutor(name="noop")},
            validator_registry={"unit": validator},
        )
        self.assertTrue(result.ok, result.error)

        runs = [
            path
            for path in (self.fx.artifact_root / key).rglob("validation-runs")
            if path.is_dir()
        ]
        self.assertEqual(len(runs), 1, runs)
        run_dir = sorted(runs[0].iterdir())[0]
        summary = json.loads((run_dir / "validation-summary.json").read_text())
        coverage = json.loads((run_dir / COVERAGE_ARTIFACT_NAME).read_text())

        self.assertEqual(summary["source"], "approved_task_runner")
        self.assertEqual(coverage["source"], "approved_task_runner")
        self.assertEqual(
            [entry["evidence"] for entry in coverage["items"]], list(EVIDENCE_KINDS)
        )
        self.assertEqual(
            summary["validators"][0]["config_identity"]["config_source"],
            "ApprovedTaskRunRequest.validators",
        )
        logs = [
            entry for entry in coverage["items"] if entry["evidence"] == EVIDENCE_VALIDATOR_LOGS
        ][0]
        self.assertEqual(logs["status"], "present")
        self.assertTrue(Path(logs["references"][0]["path"]).is_file())


class ValidatorIdentityTests(unittest.TestCase):
    """The identity describes the object that ran, not a guess about it."""

    def test_a_subprocess_validator_exposes_its_resolved_argv(self) -> None:
        validator = PytestValidator(python_bin="/usr/bin/python3", extra_args=["-q"])
        identity = validator_config_identity(
            validator,
            name="pytest",
            index=0,
            config_source="Dispatcher.validators",
            resolution="agent_taskflow.validators.registry.get_validator",
        )
        self.assertEqual(identity["command"], ["/usr/bin/python3", "-m", "pytest", "-q"])
        self.assertEqual(identity["command_availability"], "resolved_before_invocation")
        self.assertEqual(
            identity["implementation"], "agent_taskflow.validators.pytest.PytestValidator"
        )
        self.assertEqual(identity["validator_name"], "pytest")

    def test_an_in_process_validator_states_that_it_has_no_command(self) -> None:
        identity = validator_config_identity(
            PolicyCheckValidator(),
            name="policy",
            index=1,
            config_source="ApprovedTaskRunRequest.validators",
            resolution="agent_taskflow.validators.registry.get_validator",
        )
        self.assertNotIn("command", identity)
        self.assertEqual(
            identity["command_availability"], "in_process_validator_exposes_no_command"
        )
        self.assertIsNone(identity["command_reference"])

    def test_an_unreadable_command_is_recorded_as_unreadable(self) -> None:
        class Broken:
            name = "broken"

            @property
            def command(self):
                raise RuntimeError("no argv available")

        identity = validator_config_identity(
            Broken(), name="broken", index=0, config_source="test", resolution="test"
        )
        self.assertEqual(identity["command_availability"], "unreadable")
        self.assertEqual(identity["command_error"], "RuntimeError: no argv available")
        self.assertNotIn("command", identity)


class IntegrationCoverageTests(unittest.TestCase):
    """The integration validator runner indexes its own evidence too."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.integration = IntegrationStore(self.db_path)
        self.integration.init_db()
        self.artifacts = self.root / "artifacts" / "AT-900"
        self.artifacts.mkdir(parents=True)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-900",
                project="demo",
                status="integrating",
                repo_path=self.root,
                artifact_dir=self.artifacts,
            )
        )

    def run_validators(self, specs, run_id="run-1"):
        return run_integration_validators(
            task_key="AT-900",
            worktree_path=self.worktree,
            artifact_dir=self.artifacts,
            specs=specs,
            branch_sha="a" * 40,
            target_sha="b" * 40,
            diff_context="",
            integration_run_id=run_id,
            integration_store=self.integration,
        )

    def coverage(self) -> dict:
        runs = sorted((self.artifacts / "validation-runs").iterdir())
        self.assertEqual(len(runs), 1)
        return json.loads((runs[0] / COVERAGE_ARTIFACT_NAME).read_text())

    def summary(self) -> dict:
        runs = sorted((self.artifacts / "validation-runs").iterdir())
        return json.loads((runs[0] / "validation-summary.json").read_text())

    def test_the_real_command_exit_and_evidence_are_the_observed_ones(self) -> None:
        script = "import sys; sys.stdout.write('integration validator ran'); sys.exit(3)"
        report = self.run_validators(
            (IntegrationValidatorSpec(name="unit", command=(sys.executable, "-c", script)),)
        )

        self.assertFalse(report.passed)
        row = self.summary()["validators"][0]
        self.assertEqual(row["command"], [sys.executable, "-c", script])
        self.assertEqual(row["exit_code"], 3)
        self.assertEqual(row["result"], "failed")
        self.assertEqual(row["outcome_kind"], "validator_verdict")
        evidence = json.loads(Path(row["artifact_path"]).read_text())
        self.assertEqual(evidence["exit_code"], 3)
        self.assertIn("integration validator ran", evidence["output"])
        self.assertTrue(row["started_at"] <= row["ended_at"])

        logs = [
            item
            for item in self.coverage()["items"]
            if item["evidence"] == EVIDENCE_VALIDATOR_LOGS
        ][0]
        self.assertEqual(logs["status"], "present")
        self.assertEqual(logs["references"][0]["path"], row["artifact_path"])
        self.assertEqual(
            logs["detail"]["validators"][0]["command"], [sys.executable, "-c", script]
        )

    def test_a_missing_binary_is_a_tool_error_with_its_command_recorded(self) -> None:
        report = self.run_validators(
            (IntegrationValidatorSpec(name="unit", command=("no-such-validator-binary",)),)
        )

        self.assertFalse(report.passed)
        row = self.summary()["validators"][0]
        self.assertEqual(row["command"], ["no-such-validator-binary"])
        self.assertEqual(row["outcome_kind"], "tool_error")
        # The adapter's synthetic 127 is preserved but never read as an
        # observed process exit.
        self.assertIsNone(row["exit_code"])
        self.assertEqual(row["reported_exit_code"], 127)
        self.assertEqual(row["error"]["type"], "FileNotFoundError")
        self.assertTrue(Path(row["artifact_path"]).is_file())
        # The existing recorder contract is unchanged: the tool error has its
        # evidence, so the run is complete, and it cannot be read as a pass.
        self.assertTrue(self.summary()["complete"])
        self.assertFalse(self.summary()["passed"])

    def test_the_index_is_published_beside_the_summary(self) -> None:
        self.run_validators((IntegrationValidatorSpec(name="unit", command=("true",)),))

        coverage = self.coverage()
        self.assertEqual(coverage["source"], "integration_validators")
        self.assertEqual(coverage["phase"], "integration_validation")
        summary_item = [
            item for item in coverage["items"] if item["evidence"] == EVIDENCE_VALIDATION_SUMMARY
        ][0]
        self.assertEqual(summary_item["status"], "present")
        self.assertTrue(Path(summary_item["references"][0]["path"]).is_file())
        launch = [
            item
            for item in coverage["items"]
            if item["evidence"] == EVIDENCE_EXECUTOR_LAUNCH_SPEC
        ][0]
        self.assertEqual(launch["applicability"], "not_applicable")
        self.assertIn("launches no executor", launch["reason"])


class CoverageFailureTests(unittest.TestCase):
    """A coverage failure is recorded; it never manufactures a pass."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def recorder(self, builder):
        return ValidationSummaryRecorder(
            task_key="AT-1",
            artifact_dir=self.root,
            source="test",
            phase="implementation_validation",
            validators=("unit",),
            coverage_builder=builder,
        )

    def test_a_builder_failure_is_recorded_as_unavailable(self) -> None:
        def broken(recorder):
            raise RuntimeError("coverage is broken")

        summary = self.recorder(broken)
        log = self.root / "unit.log"
        log.write_text("real output\n", encoding="utf-8")
        summary.observe(0, lambda: ValidatorResult("unit", "passed", exit_code=0, log_path=log))
        summary.finish()

        published = json.loads(summary.path.read_text())
        self.assertEqual(published["evidence_coverage"]["status"], "unavailable")
        self.assertEqual(published["evidence_coverage"]["error"]["type"], "RuntimeError")
        self.assertIsNone(published["evidence_coverage"]["path"])
        # The validator verdict and the summary's own contract are untouched.
        self.assertTrue(published["complete"])
        self.assertTrue(published["passed"])
        self.assertFalse((summary.directory / COVERAGE_ARTIFACT_NAME).exists())

    def test_coverage_is_published_inside_the_recorders_own_run_directory(self) -> None:
        collector = RunnerEvidenceCollector(source="test", artifact_roots=(self.root,))
        summary = self.recorder(collector.coverage)
        log = self.root / "unit.log"
        log.write_text("real output\n", encoding="utf-8")
        summary.observe(0, lambda: ValidatorResult("unit", "passed", exit_code=0, log_path=log))
        summary.finish()

        published = json.loads(summary.path.read_text())
        coverage_path = Path(published["evidence_coverage"]["path"])
        self.assertEqual(coverage_path.parent, summary.directory)
        self.assertTrue(coverage_path.is_file())
        coverage = json.loads(coverage_path.read_text())
        self.assertEqual(coverage["validation_run_id"], published["validation_run_id"])

    def test_a_run_without_an_artifact_root_publishes_no_index(self) -> None:
        collector = RunnerEvidenceCollector(source="test", artifact_roots=())
        summary = ValidationSummaryRecorder(
            task_key="AT-1",
            artifact_dir=None,
            source="test",
            phase="implementation_validation",
            validators=("unit",),
            coverage_builder=collector.coverage,
        )
        summary.observe(0, lambda: ValidatorResult("unit", "passed", exit_code=0))
        summary.finish()

        self.assertIsNone(summary.path)
        self.assertNotIn("evidence_coverage", summary.payload)

    def test_a_symlinked_reference_is_reported_not_followed(self) -> None:
        real = self.root / "real.log"
        real.write_text("real output\n", encoding="utf-8")
        link = self.root / "link.log"
        link.symlink_to(real)

        collector = RunnerEvidenceCollector(source="test", artifact_roots=(self.root,))
        summary = self.recorder(collector.coverage)
        summary.observe(
            0,
            lambda: ValidatorResult(
                "unit", "passed", exit_code=0, artifacts={"audit": self.root / "changed-files-audit.json"}
            ),
        )
        summary.finish()
        coverage = json.loads((summary.directory / COVERAGE_ARTIFACT_NAME).read_text())
        audit = [
            item for item in coverage["items"] if item["evidence"] == EVIDENCE_CHANGED_FILES_AUDIT
        ][0]
        self.assertEqual(audit["status"], "missing")

        # And a reference that is a symlink is refused rather than resolved.
        collector = RunnerEvidenceCollector(source="test", artifact_roots=(self.root,))
        second = self.recorder(collector.coverage)
        second.observe(
            0,
            lambda: ValidatorResult("unit", "passed", exit_code=0, artifacts={"log": link}),
        )
        second.finish()
        coverage = json.loads((second.directory / COVERAGE_ARTIFACT_NAME).read_text())
        logs = [
            item for item in coverage["items"] if item["evidence"] == EVIDENCE_VALIDATOR_LOGS
        ][0]
        reported = {
            reference["path"]: reference
            for reference in logs["references"]
        }
        self.assertIn(str(link), reported)
        self.assertFalse(reported[str(link)]["readable"])
        self.assertEqual(reported[str(link)]["error"], "OSError:40")


class RejectedReferenceTests(unittest.TestCase):
    """Review finding M22-R1-N1: readable is not the same as admissible.

    The recorder refuses to snapshot evidence outside its artifact root and
    marks that run incomplete. The index must agree: the path stays visible with
    its digest, but it cannot make the evidence kind present.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "artifacts"
        self.root.mkdir()
        self.outside = Path(self.tmp.name) / "elsewhere"
        self.outside.mkdir()

    def run_with(self, artifacts, *, collector_roots=()):
        collector = RunnerEvidenceCollector(
            source="test", artifact_roots=(self.root, *collector_roots)
        )
        summary = ValidationSummaryRecorder(
            task_key="AT-1",
            artifact_dir=self.root,
            source="test",
            phase="implementation_validation",
            validators=("changed-files",),
            coverage_builder=collector.coverage,
        )
        summary.observe(
            0,
            lambda: ValidatorResult(
                "changed-files", "passed", exit_code=0, artifacts=artifacts
            ),
        )
        summary.finish()
        return (
            json.loads(summary.path.read_text()),
            json.loads((summary.directory / COVERAGE_ARTIFACT_NAME).read_text()),
        )

    def item(self, coverage, evidence):
        return [entry for entry in coverage["items"] if entry["evidence"] == evidence][0]

    def test_a_readable_reference_the_recorder_refused_is_not_present(self) -> None:
        audit = self.outside / EVIDENCE_CHANGED_FILES_AUDIT
        audit.write_text('{"outside": true}\n', encoding="utf-8")

        summary, coverage = self.run_with({"audit": audit})

        # The recorder's own decision, unchanged.
        self.assertFalse(summary["complete"])
        self.assertEqual(
            summary["validators"][0]["evidence_error"], "evidence_outside_artifact_root"
        )
        self.assertIsNone(summary["validators"][0]["artifact_path"])

        # The index no longer disagrees with it.
        item = self.item(coverage, EVIDENCE_CHANGED_FILES_AUDIT)
        self.assertEqual(item["status"], "missing")
        self.assertEqual(item["admissible_references"], 0)
        self.assertEqual(item["rejected_references"], 1)
        reference = item["references"][0]
        self.assertTrue(reference["readable"])
        self.assertFalse(reference["admissible"])
        self.assertFalse(reference["inside_recorder_root"])
        self.assertEqual(
            reference["recorder_rejection"], "evidence_outside_artifact_root"
        )
        self.assertEqual(
            reference["rejection_reason"],
            "recorder_rejected:evidence_outside_artifact_root",
        )
        # The digest and path stay, so the rejection can be diagnosed.
        self.assertEqual(reference["path"], str(audit))
        self.assertEqual(reference["sha256"], sha256(audit))

        self.assertIn(EVIDENCE_CHANGED_FILES_AUDIT, coverage["unresolved"])
        self.assertFalse(coverage["complete"])
        # Both entries that point at the refused path list it, with the reason.
        self.assertEqual(
            coverage["rejected_references"],
            [
                {
                    "evidence": EVIDENCE_CHANGED_FILES_AUDIT,
                    "path": str(audit),
                    "reason": "recorder_rejected:evidence_outside_artifact_root",
                },
                {
                    "evidence": EVIDENCE_VALIDATOR_LOGS,
                    "path": str(audit),
                    "reason": "recorder_rejected:evidence_outside_artifact_root",
                },
            ],
        )
        self.assertEqual(coverage["recorder_artifact_root"], str(self.root))
        # The same rejection is on the validator-logs entry, which has no
        # recorded evidence for this row either.
        logs = self.item(coverage, EVIDENCE_VALIDATOR_LOGS)
        self.assertEqual(logs["status"], "missing")
        self.assertEqual(logs["admissible_references"], 0)

    def test_a_reference_in_a_collector_root_outside_the_recorder_root_is_rejected(
        self,
    ) -> None:
        # A seam may declare more than one artifact root; only the recorder's
        # own root is the publication boundary.
        audit = self.outside / EVIDENCE_CHANGED_FILES_AUDIT
        audit.write_text('{"second root": true}\n', encoding="utf-8")

        _, coverage = self.run_with({"audit": audit}, collector_roots=(self.outside,))

        item = self.item(coverage, EVIDENCE_CHANGED_FILES_AUDIT)
        reference = item["references"][0]
        self.assertTrue(reference["inside_artifact_root"])
        self.assertFalse(reference["inside_recorder_root"])
        self.assertFalse(reference["admissible"])
        self.assertEqual(item["status"], "missing")
        self.assertFalse(coverage["complete"])

    def test_a_legitimate_in_root_reference_is_still_present_and_complete(self) -> None:
        audit = self.root / EVIDENCE_CHANGED_FILES_AUDIT
        audit.write_text('{"inside": true}\n', encoding="utf-8")
        log = self.root / "changed-files-validate.log"
        log.write_text("real output\n", encoding="utf-8")

        summary, coverage = self.run_with({"log": log, "audit": audit})

        self.assertTrue(summary["complete"])
        item = self.item(coverage, EVIDENCE_CHANGED_FILES_AUDIT)
        self.assertEqual(item["status"], "present")
        self.assertEqual(item["admissible_references"], 1)
        self.assertEqual(item["rejected_references"], 0)
        reference = item["references"][0]
        self.assertTrue(reference["admissible"])
        self.assertTrue(reference["inside_recorder_root"])
        self.assertIsNone(reference["rejection_reason"])
        self.assertEqual(reference["sha256"], sha256(audit))
        self.assertEqual(coverage["rejected_references"], [])
        self.assertNotIn(EVIDENCE_CHANGED_FILES_AUDIT, coverage["unresolved"])


class RealSubprocessCoverageTests(unittest.TestCase):
    """One end-to-end check against files a real process wrote."""

    def test_a_real_compileall_log_is_indexed_with_its_real_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "sample.py"
            module.write_text("VALUE = 1\n", encoding="utf-8")
            log = root / EVIDENCE_COMPILEALL_LOG
            completed = subprocess.run(
                [sys.executable, "-m", "compileall", "-q", str(module)],
                capture_output=True,
                text=True,
                check=False,
                cwd=root,
            )
            log.write_text(completed.stdout + completed.stderr, encoding="utf-8")

            collector = RunnerEvidenceCollector(source="test", artifact_roots=(root,))
            summary = ValidationSummaryRecorder(
                task_key="AT-1",
                artifact_dir=root,
                source="test",
                phase="implementation_validation",
                validators=("compileall",),
                coverage_builder=collector.coverage,
            )
            summary.observe(
                0,
                lambda: ValidatorResult(
                    "compileall",
                    "passed" if completed.returncode == 0 else "failed",
                    exit_code=completed.returncode,
                    log_path=log,
                ),
            )
            summary.finish()

            coverage = json.loads((summary.directory / COVERAGE_ARTIFACT_NAME).read_text())
            item = [
                entry for entry in coverage["items"]
                if entry["evidence"] == EVIDENCE_COMPILEALL_LOG
            ][0]
            self.assertEqual(item["status"], "present")
            reference = item["references"][0]
            self.assertEqual(reference["sha256"], sha256(Path(reference["path"])))
            self.assertEqual(completed.returncode, 0)


if __name__ == "__main__":  # pragma: no cover - direct unittest execution
    unittest.main()
