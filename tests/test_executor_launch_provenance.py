from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from agent_taskflow.executors.base import ExecutorContext
from agent_taskflow.executors.pi import PiExecutor
from agent_taskflow.executors.opencode import OpenCodeExecutor
from agent_taskflow.executors.claude_code import ClaudeCodeExecutor
from agent_taskflow.launch_provenance import ExecutorLaunchProvenance, LaunchContentReference
from agent_taskflow.mission_contract import build_from_task_fields, write_mission_contract
from agent_taskflow.store import connect
import tests.test_executor_launch_evidence as fixtures
import tests.test_approved_task_runner as runner_fixtures


class ExecutorLaunchProvenanceTests(unittest.TestCase):
    setUp = fixtures.ExecutorLaunchEvidenceTests.setUp
    spec = fixtures.ExecutorLaunchEvidenceTests.spec
    run_spec = fixtures.ExecutorLaunchEvidenceTests.run_spec
    evidence = fixtures.ExecutorLaunchEvidenceTests.evidence

    def context(self):
        prompt = self.binding.artifact_root / "input.md"
        prompt.write_text("selected input — private task\n", encoding="utf-8")
        return self.store.bind_executor_context(ExecutorContext(
            task_key=self.binding.task_key, project="agent-taskflow",
            worktree_path=self.binding.worktree_path, artifact_dir=self.binding.artifact_root,
            repo_root=self.repo, prompt_path=prompt, model="context-model", timeout_seconds=5,
        ))

    def fake_cli(self, *, stdin=False):
        path = self.root / "fixture-cli"
        path.write_text(
            f"#!{sys.executable}\nimport sys,hashlib,json\n"
            + ("text = sys.stdin.read()\n" if stdin else "text = sys.argv[-1]\n")
            + "print(json.dumps({'selected_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest()}))\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def adapter_evidence(self, result):
        self.assertEqual(result.exit_code, 0, result.summary)
        launch = json.loads(result.artifacts["executor_launch_spec"].read_text())
        ref = launch["resolved_launch_evidence"]
        self.assertEqual(ref["status"], "written")
        return json.loads(Path(ref["path"]).read_text())

    def assert_selected_prompt(self, result, evidence, reference, text):
        prompt = evidence["prompt_reference"]
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.assertEqual(prompt["reference"], str(reference))
        self.assertEqual(prompt["sha256"], digest)
        self.assertEqual(prompt["length_bytes"], len(text.encode("utf-8")))
        self.assertIn(digest, result.log_path.read_text())  # Actual child observed those bytes.
        self.assertNotIn(text, json.dumps(evidence))
        self.assertNotIn("prompt_reference", evidence["missing_fields"])
        self.assertIsNone(evidence["observed_model"])

    def test_pi_hashes_rendered_mission_and_constructor_model_not_input_or_context(self):
        context = self.context()
        contract = build_from_task_fields(
            task_key=context.task_key, goal="fixture", repo_path=self.repo,
            worktree_path=context.worktree_path, artifact_dir=context.artifact_dir,
            executor="pi", model="contract-model",
        )
        write_mission_contract(contract, artifact_dir=context.artifact_dir)
        result = PiExecutor(pi_bin=str(self.fake_cli()), model="selected-pi-model", tools=["read"]).run(context)
        evidence = self.adapter_evidence(result)
        rendered = result.artifacts["pi_mission_prompt"]
        self.assertNotEqual(rendered.read_text(), context.prompt_path.read_text())
        self.assert_selected_prompt(result, evidence, rendered, rendered.read_text())
        self.assertEqual(evidence["prompt_reference"]["source"], "pi_mission_rendered")
        self.assertEqual(evidence["requested_launch"]["model"], "selected-pi-model")
        self.assertEqual(evidence["requested_launch"]["tools"], ["read"])
        self.assertIsNone(evidence["allowed_tools"])
        self.assertEqual(evidence["configured_attempt"]["model"], "configured-model")

    def test_pi_does_not_claim_unused_context_model_was_requested(self):
        context = self.context()
        result = PiExecutor(pi_bin=str(self.fake_cli())).run(context)
        evidence = self.adapter_evidence(result)
        self.assert_selected_prompt(result, evidence, context.prompt_path, context.prompt_path.read_text())
        self.assertIsNone(evidence["requested_launch"]["model"])

    def test_opencode_captures_selected_text_without_rereading_changed_path(self):
        from agent_taskflow.executor_launch import run_managed_process
        context = self.context()
        selected = context.prompt_path.read_text()

        def replace_input_then_launch(*args, **kwargs):
            context.prompt_path.write_text("changed after selection")
            return run_managed_process(*args, **kwargs)

        with patch("agent_taskflow.executors.opencode.run_managed_process", side_effect=replace_input_then_launch):
            result = OpenCodeExecutor(opencode_bin=str(self.fake_cli()), model="constructor-model").run(context)
        evidence = self.adapter_evidence(result)
        self.assert_selected_prompt(result, evidence, context.prompt_path, selected)
        self.assertEqual(evidence["requested_launch"]["model"], "constructor-model")
        self.assertNotEqual(evidence["prompt_reference"]["sha256"],
                            hashlib.sha256(context.prompt_path.read_bytes()).hexdigest())

    def test_claude_hashes_rendered_stdin_and_does_not_infer_opaque_command_model(self):
        context = self.context()
        result = ClaudeCodeExecutor(command=[str(self.fake_cli(stdin=True))], enable_invocation=True,
                                    worktree_root=self.repo / ".worktrees").run(context)
        evidence = self.adapter_evidence(result)
        rendered = result.artifacts["claude_code_prompt"]
        self.assert_selected_prompt(result, evidence, rendered, rendered.read_text())
        self.assertEqual(evidence["prompt_reference"]["source"], "claude_code_implementer_rendered")
        self.assertIsNone(evidence["requested_launch"]["model"])

    def test_runtime_requested_values_stay_distinct_from_exact_attempt_snapshot(self):
        with closing(connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempts SET policy_version='stored-policy', permission_profile='stored-permission' WHERE attempt_id=?",
                         (self.binding.attempt_id,))
            conn.execute("UPDATE tasks SET model='later-default' WHERE task_key=?", (self.binding.task_key,))
        provenance = ExecutorLaunchProvenance(
            base_commit="runtime-base", base_source="fixture_runtime_selection",
            policy_version="runtime-policy", permission_profile="runtime-permission",
            requested_model="runtime-model", model_source="fixture_adapter_selection",
        )
        _, evidence = self.evidence(self.run_spec(self.spec(provenance=provenance)))
        self.assertEqual(evidence["requested_launch"]["executor"], "same-command")
        self.assertEqual(evidence["requested_launch"]["timeout_seconds"], 5)
        self.assertEqual(evidence["requested_launch"]["base_commit"], "runtime-base")
        self.assertEqual(evidence["requested_launch"]["policy_version"], "runtime-policy")
        self.assertEqual(evidence["requested_launch"]["permission_profile"], "runtime-permission")
        self.assertEqual(evidence["configured_attempt"]["policy_version"], "stored-policy")
        self.assertEqual(evidence["configured_attempt"]["permission_profile"], "stored-permission")
        self.assertIsNone(evidence["observed_model"])
        self.assertIsNone(evidence["canonical_execution_path"])  # Binding is not engine attestation.

    def test_invalid_metadata_remains_unknown_and_preserves_subprocess_failure(self):
        invalid = ExecutorLaunchProvenance(
            requested_model={"fabricated": "model"}, canonical_execution_path=123,
            prompt_reference=LaunchContentReference("input", "invalid-hash", -1, "fixture"),
        )
        result = self.run_spec(self.spec(provenance=invalid, argv=(sys.executable, "-c", "raise SystemExit(7)")))
        self.assertEqual(result.exit_code, 7)
        _, evidence = self.evidence(result)
        self.assertIsNone(evidence["requested_launch"]["model"])
        self.assertIsNone(evidence["canonical_execution_path"])
        self.assertIsNone(evidence["prompt_reference"])
        self.assertEqual(evidence["unknown_field_reasons"]["prompt_reference"], "invalid_runner_metadata")
        self.assertNotIn("fabricated", json.dumps(evidence))
        for field in ("credential_policy", "network_policy", "environment_allowlist"):
            self.assertIsNone(evidence[field])
            self.assertIn(field, evidence["missing_fields"])

    def test_malformed_optional_context_does_not_prevent_real_adapter_launch(self):
        context = replace(self.context(), launch_provenance={"canonical_execution_path": "invented"})
        result = PiExecutor(pi_bin=str(self.fake_cli()), model="selected-model").run(context)
        evidence = self.adapter_evidence(result)
        self.assertIsNone(evidence["canonical_execution_path"])
        self.assertEqual(evidence["requested_launch"]["model"], "selected-model")
        self.assert_selected_prompt(result, evidence, context.prompt_path, context.prompt_path.read_text())

    def test_bound_base_is_actual_claimed_resource_and_path_stays_unobserved(self):
        context = self.context()
        self.assertEqual(context.launch_provenance.base_commit,
                         self.store.attempt_resource(self.binding.task_key).base_sha)
        self.assertIsNone(context.launch_provenance.canonical_execution_path)


class EnginePathObservationTests(unittest.TestCase):
    def test_engine_scope_is_exact_nested_and_resets(self):
        from agent_taskflow.level2_execution_authority import (
            execution_engine_primitive_active, execution_engine_primitive_authority,
        )
        identity = {"task_key": "AT-SCOPE", "db_path": Path("/tmp/fixture-a.db")}
        self.assertFalse(execution_engine_primitive_active(**identity))
        with execution_engine_primitive_authority(**identity):
            self.assertTrue(execution_engine_primitive_active(**identity))
            self.assertFalse(execution_engine_primitive_active(**{**identity, "task_key": "AT-OTHER"}))
            self.assertFalse(execution_engine_primitive_active(**{**identity, "db_path": Path("/tmp/fixture-b.db")}))
            with execution_engine_primitive_authority(task_key="AT-OTHER", db_path=identity["db_path"]):
                self.assertFalse(execution_engine_primitive_active(**identity))
            self.assertTrue(execution_engine_primitive_active(**identity))
        self.assertFalse(execution_engine_primitive_active(**identity))


class RunnerLaunchProvenanceTests(unittest.TestCase):
    setUp = runner_fixtures.ApprovedTaskRunnerTests.setUp
    tearDown = runner_fixtures.ApprovedTaskRunnerTests.tearDown
    _git = runner_fixtures.ApprovedTaskRunnerTests._git
    _init_repo = runner_fixtures.ApprovedTaskRunnerTests._init_repo
    _add_task = runner_fixtures.ApprovedTaskRunnerTests._add_task

    def run_runner(self, *, engine=False, existing_prompt=False):
        from agent_taskflow.approved_task_runner import ApprovedTaskRunRequest, run_approved_task
        from agent_taskflow.execution_engine_approved_task_adapter import ApprovedTaskRunnerExecutionEngineAdapter
        from agent_taskflow.execution_engine_contract import (
            ExecutionEngineRequest, ExecutionEngineExecutorProfile,
            ExecutionEngineValidatorProfile, ExecutionEngineWorkspaceProfile,
        )
        self.worktree_root = self.repo / ".worktrees"
        artifact = self._add_task("AT-PROVENANCE")
        artifact.mkdir(parents=True)
        spec_text = "Issue spec with Unicode: 測試\n"
        (artifact / "issue_spec.md").write_text(spec_text, encoding="utf-8")
        if existing_prompt:
            (artifact / "implementation_prompt.md").write_text("Previously rendered prompt\n")
        cli = self.root / "fake-pi"
        cli.write_text(f"#!{sys.executable}\nprint('fixture executor completed')\n")
        cli.chmod(0o755)
        validator = runner_fixtures.FakeValidator(name="fixture")

        def invoke(request, **kwargs):
            return run_approved_task(replace(request, require_codex_advisory_evidence=False),
                                     validator_registry={"fixture": validator},
                                     executor_registry={"opencode": OpenCodeExecutor(opencode_bin=str(cli))}, **kwargs)

        if engine:
            request = ExecutionEngineRequest(
                task_key="AT-PROVENANCE", dry_run=False, preflight=False,
                lifecycle_db_path=self.db_path, metadata={"confirmed": True},
                executor_profile=ExecutionEngineExecutorProfile(executor="opencode", model="override-model", pi_bin=str(cli)),
                validator_profile=ExecutionEngineValidatorProfile(validators=("fixture",)),
                workspace=ExecutionEngineWorkspaceProfile(repo_path=self.repo, artifact_dir=self.artifact_root,
                                                          worktree_root=self.worktree_root),
            )
            result = ApprovedTaskRunnerExecutionEngineAdapter(approved_task_runner=invoke).execute(request)
        else:
            request = ApprovedTaskRunRequest(
                task_key="AT-PROVENANCE", executor="opencode", repo_path=self.repo,
                db_path=self.db_path, worktree_root=self.worktree_root, artifact_root=self.artifact_root,
                validators=("fixture",), preflight=False, confirm_approved_task=True,
                model="override-model", pi_bin=str(cli),
            )
            result = invoke(request)
        self.assertTrue(result.ok, result)
        artifacts = list(self.root.rglob("resolved-launch-process-*.json"))
        self.assertEqual(len(artifacts), 1)
        return json.loads(artifacts[0].read_text()), spec_text

    def test_approved_runner_captures_config_spec_and_base_from_actual_selection(self):
        evidence, spec_text = self.run_runner()
        self.assertEqual(evidence["canonical_execution_path"], "approved_task_runner")
        self.assertEqual(evidence["requested_launch"]["model"], "override-model")
        self.assertEqual(evidence["spec_reference"]["sha256"], hashlib.sha256(spec_text.encode()).hexdigest())
        self.assertEqual(evidence["spec_reference"]["length_bytes"], len(spec_text.encode()))
        self.assertTrue(evidence["spec_reference"]["reference"].endswith("/issue_spec.md"))
        self.assertEqual(evidence["config_snapshot_reference"]["reference"], "approved_task_runner.resolved_configuration")
        expected_config = dict(executor="opencode", model="override-model", provider=None, tools=None,
                               base_branch="main", validators=["fixture"], timeout_seconds=None)
        expected = json.dumps(expected_config, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        self.assertEqual(evidence["config_snapshot_reference"]["sha256"], hashlib.sha256(expected).hexdigest())
        self.assertEqual(evidence["requested_launch"]["base_commit"], evidence["configured_attempt"]["base_commit"])
        self.assertIsNotNone(evidence["requested_launch"]["base_commit"])
        for key in ("policy_version", "permission_profile"):
            self.assertIsNone(evidence["requested_launch"][key])
        self.assertIsNone(evidence["observed_model"])
        self.assertNotIn("requested_launch.timeout_seconds", evidence["missing_fields"])

    def test_real_engine_adapter_scope_reaches_launch_and_existing_spec_is_not_reopened(self):
        evidence, _ = self.run_runner(engine=True, existing_prompt=True)
        self.assertEqual(evidence["canonical_execution_path"], "execution_engine")
        self.assertEqual(evidence["runner_provenance"]["path_source"],
                         "approved_task_runner.direct_engine_authority_scope")
        self.assertIsNone(evidence["spec_reference"])
        self.assertEqual(evidence["unknown_field_reasons"]["spec_reference"], "not_observed_by_runner")
