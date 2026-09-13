from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import agent_taskflow.launch_evidence as launch_evidence
from agent_taskflow.executor_launch import (
    ExecutorLaunchSpec, ExecutorProcessStore, run_managed_process,
)
from agent_taskflow.executor_process_runtime_path import ExecutorProcessRuntimeTaskStore
from agent_taskflow.executors.base import ExecutorContext
from agent_taskflow.launch_evidence import (
    _publish_once, launch_evidence_reference, read_bound_attempt_snapshot,
)
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore, connect


class ExecutorLaunchEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (
            ("init", "-b", "main"), ("config", "user.email", "test@example.com"),
            ("config", "user.name", "Test User"), ("commit", "--allow-empty", "-m", "fixture"),
        ):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)
        self.db = self.root / "state.db"
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        mirror = TaskMirrorStore(self.db)
        mirror.init_db()
        mirror.upsert_task(TaskRecord(
            task_key="AT-LAUNCH-1", project="agent-taskflow", board="agent-taskflow",
            title="Launch evidence", status="queued", repo_path=self.repo,
            artifact_dir=self.artifacts, executor="shell", model="configured-model",
        ))
        self.store = ExecutorProcessRuntimeTaskStore(self.db, heartbeat_interval_seconds=60)
        self.store.preclaim_runtime(
            "AT-LAUNCH-1", source="test", artifact_base_root=self.artifacts,
            worktree_root=self.repo / ".worktrees", base_branch="main",
        )
        prepared = self.store.prepare_attempt_workspace("AT-LAUNCH-1")
        self.assertTrue(prepared.ok, prepared.summary)
        resource = self.store.attempt_resource("AT-LAUNCH-1")
        context = self.store.bind_executor_context(ExecutorContext(
            task_key="AT-LAUNCH-1", project="agent-taskflow",
            worktree_path=resource.worktree_path, artifact_dir=resource.artifact_root,
        ))
        self.binding = context.launch_binding
        self.addCleanup(self.store.shutdown_runtime_supervisors)
        self.addCleanup(self.store.update_task_status, "AT-LAUNCH-1", "blocked",
                        source="test", blocked_reason="fixture cleanup")

    def spec(self, **changes) -> ExecutorLaunchSpec:
        return replace(ExecutorLaunchSpec(
            executor_name="same-command", argv=(sys.executable, "-c", "print('ok')"),
            cwd=self.binding.worktree_path, artifact_dir=self.binding.artifact_root,
            timeout_seconds=5, stdin_mode="devnull", combined_output=True,
        ), **changes)

    def run_spec(self, spec=None, **kwargs):
        return run_managed_process(
            self.binding, spec or self.spec(),
            stdout_path=self.binding.artifact_root / "output.log", **kwargs,
        )

    def evidence(self, result):
        reference = json.loads(result.launch_spec_path.read_text())["resolved_launch_evidence"]
        self.assertEqual(reference["status"], "written", reference)
        path = Path(reference["path"])
        self.assertEqual(path.parent, self.binding.artifact_root)
        return path, json.loads(path.read_text())

    def test_exact_persisted_attempt_configuration_and_unknown_runtime_model(self):
        with closing(connect(self.db)) as conn, conn:
            conn.execute("UPDATE tasks SET model = 'changed-task-default' WHERE task_key = ?",
                         (self.binding.task_key,))
            conn.execute("""UPDATE attempts SET policy_version = 'policy-1',
                config_snapshot_hash = 'config-hash', prompt_template_version = 'prompt-1',
                permission_profile = 'profile-1' WHERE attempt_id = ?""",
                         (self.binding.attempt_id,))
        result = self.run_spec()
        self.assertEqual(result.exit_code, 0)
        _, evidence = self.evidence(result)
        self.assertEqual(evidence["task_id"], self.binding.task_id)
        self.assertEqual(evidence["attempt_id"], self.binding.attempt_id)
        self.assertEqual(evidence["process_id"], result.process_id)
        self.assertEqual(evidence["configured_attempt"]["model"], "configured-model")
        self.assertEqual(evidence["configured_attempt"]["policy_version"], "policy-1")
        self.assertEqual(evidence["configured_attempt"]["permission_profile"], "profile-1")
        self.assertEqual(evidence["configured_attempt"]["config_snapshot_hash"], "config-hash")
        self.assertEqual(evidence["configured_attempt"]["prompt_template_version"], "prompt-1")
        self.assertIsNotNone(evidence["configured_attempt"]["base_commit"])
        for field in ("observed_model", "canonical_execution_path", "prompt_reference",
                      "spec_reference", "allowed_tools", "environment_allowlist", "network_policy"):
            self.assertIsNone(evidence[field])
            self.assertIn(field, evidence["missing_fields"])
        self.assertTrue(evidence["metadata_provenance"]["binding_verified"])
        self.assertEqual(evidence["preflight"]["resolved_executable"], str(Path(sys.executable).resolve()))
        self.assertTrue(evidence["preflight"]["observed"])
        self.assertIs(evidence["preflight"]["ok"], True)
        self.assertLessEqual(evidence["preflight"]["started_at"], evidence["preflight"]["ended_at"])
        self.assertTrue(evidence["environment_inheritance_retained"])
        self.assertTrue(evidence["parent_environment_inherited_by_popen"])
        self.assertFalse(evidence["environment_keys_are_allowlist"])
        self.assertFalse(evidence["network_isolation"])
        self.assertFalse(evidence["security_eligibility_established"])
        self.assertFalse(evidence["lifecycle_authority"])
        self.assertEqual(evidence["launch_outcome"], "started")
        self.assertEqual(evidence["process_identity"]["pid"], evidence["process_identity"]["pgid"])

    def test_nullable_configuration_stays_null(self):
        with closing(connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempts SET model = NULL WHERE attempt_id = ?",
                         (self.binding.attempt_id,))
        _, evidence = self.evidence(self.run_spec())
        for field in ("model", "policy_version", "config_snapshot_hash", "prompt_template_version",
                      "permission_profile"):
            self.assertIsNone(evidence["configured_attempt"][field])
            self.assertIn("configured_attempt." + field, evidence["missing_fields"])

    def test_running_process_executable_is_observed_separately_from_preflight(self):
        result = self.run_spec(self.spec(argv=(sys.executable, "-c", "import time; time.sleep(0.2)")))
        _, evidence = self.evidence(result)
        self.assertEqual(evidence["observed_process_executable"], str(Path(sys.executable).resolve()))
        self.assertEqual(evidence["observed_process_executable_provenance"], "linux_proc_exe")

    def test_unavailable_metadata_after_binding_is_explicit_and_does_not_block_launch(self):
        original_connect = sqlite3.connect

        class MissingMetadataConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql.startswith("SELECT executor, model"):
                    raise sqlite3.OperationalError("unavailable column; private error text")
                return super().execute(sql, *args, **kwargs)

        def missing_metadata(*args, **kwargs):
            if kwargs.get("uri"):
                kwargs["factory"] = MissingMetadataConnection
            return original_connect(*args, **kwargs)

        with patch("agent_taskflow.launch_evidence.sqlite3.connect", side_effect=missing_metadata):
            result = self.run_spec()
        self.assertEqual(result.exit_code, 0)
        path, evidence = self.evidence(result)
        self.assertTrue(evidence["metadata_provenance"]["binding_verified"])
        self.assertEqual(evidence["metadata_provenance"]["status"], "unavailable")
        self.assertEqual(evidence["metadata_provenance"]["error_type"], "OperationalError")
        self.assertTrue(all(value is None for value in evidence["configured_attempt"].values()))
        self.assertNotIn("private error text", path.read_text())

    def test_managed_invalid_binding_cannot_publish_configuration(self):
        foreign = replace(self.binding, task_key="AT-FOREIGN")
        result = run_managed_process(foreign, self.spec(),
                                     stdout_path=self.binding.artifact_root / "refused.log")
        self.assertTrue(result.preflight_errors)
        reference = json.loads(result.launch_spec_path.read_text())["resolved_launch_evidence"]
        self.assertEqual(reference["status"], "not_written")
        self.assertEqual(reference["metadata_provenance"]["status"], "binding_mismatch")
        self.assertFalse(Path(reference["path"]).exists())
        self.assertNotIn("configured-model", result.launch_spec_path.read_text())

    def test_mismatched_identities_withhold_configuration_before_config_query(self):
        original_connect = sqlite3.connect
        queries = []

        def traced_connect(*args, **kwargs):
            conn = original_connect(*args, **kwargs)
            conn.set_trace_callback(queries.append)
            return conn

        for key, value in (("task_id", "foreign-task"), ("task_key", "AT-FOREIGN"),
                           ("attempt_id", "foreign-attempt"), ("lease_id", "foreign-lease"),
                           ("owner_id", "foreign-owner"), ("artifact_root", self.root),
                           ("worktree_path", self.repo)):
            with self.subTest(key=key), patch("agent_taskflow.launch_evidence.sqlite3.connect",
                                             side_effect=traced_connect):
                queries.clear()
                snapshot = read_bound_attempt_snapshot(replace(self.binding, **{key: value}))
                self.assertEqual(snapshot["provenance"]["status"], "binding_mismatch")
                self.assertFalse(snapshot["provenance"]["binding_verified"])
                self.assertTrue(all(value is None for value in snapshot["configured"].values()))
                self.assertFalse(any("SELECT executor, model" in query for query in queries))

    def test_exact_binding_never_falls_back_to_higher_attempt_number(self):
        with closing(connect(self.db)) as conn, conn:
            conn.execute("""INSERT INTO attempts (
                attempt_id, task_id, attempt_number, status, is_active, is_legacy,
                executor, model, ended_at, created_at, updated_at)
                SELECT 'other-attempt', task_id, attempt_number + 1, 'execution_failed',
                0, 0, 'foreign-executor', 'foreign-model', created_at, created_at, updated_at
                FROM attempts WHERE attempt_id = ?""", (self.binding.attempt_id,))
        snapshot = read_bound_attempt_snapshot(self.binding)
        self.assertEqual(snapshot["configured"]["model"], "configured-model")
        self.assertEqual(snapshot["attempt_number"], 1)

    def test_resource_owner_mismatch_withholds_configuration(self):
        with closing(connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempt_resources SET owner_id = 'foreign-owner' WHERE attempt_id = ?",
                         (self.binding.attempt_id,))
        snapshot = read_bound_attempt_snapshot(self.binding)
        self.assertFalse(snapshot["provenance"]["binding_verified"])
        self.assertTrue(all(value is None for value in snapshot["configured"].values()))

    def test_real_repeated_executor_and_validator_launches_keep_each_observation(self):
        first = self.run_spec()
        first_path, _ = self.evidence(first)
        original = first_path.read_bytes()
        second = self.run_spec()
        second_path, _ = self.evidence(second)
        third = self.run_spec(self.spec(process_role="validator"))
        third_path, third_evidence = self.evidence(third)
        self.assertEqual(len({first_path, second_path, third_path}), 3)
        self.assertEqual(first_path.read_bytes(), original)
        self.assertEqual(third_evidence["process_role"], "validator")
        pid = json.loads(third.pid_manifest_path.read_text())
        self.assertEqual(pid["resolved_launch_evidence"]["path"], str(third_path))
        with closing(connect(self.db)) as conn:
            event = conn.execute("SELECT metadata_json FROM executor_process_events WHERE process_id = ? ORDER BY event_id LIMIT 1",
                                 (first.process_id,)).fetchone()
        self.assertEqual(json.loads(event[0])["resolved_launch_evidence"]["path"], str(first_path))

    def test_real_preflight_refusal_is_distinct_from_real_exec_start_failure(self):
        refused = self.run_spec(self.spec(argv=(str(self.root / "missing-executable"),)))
        _, refusal = self.evidence(refused)
        self.assertEqual(refusal["launch_outcome"], "preflight_failed")
        self.assertIs(refusal["preflight"]["ok"], False)
        self.assertIsNone(refusal["preflight"]["resolved_executable"])
        self.assertIsNone(refusal["process_identity"])
        self.assertTrue(refusal["preflight"]["blocking_errors"])
        broken = self.root / "broken-interpreter"
        broken.write_text("#!/nonexistent-interpreter-for-launch-evidence-test\n")
        broken.chmod(0o755)
        failed = self.run_spec(self.spec(argv=(str(broken),)))
        _, failure = self.evidence(failed)
        self.assertEqual(failure["launch_outcome"], "start_failed")
        self.assertIs(failure["preflight"]["ok"], True)
        self.assertEqual(failure["preflight"]["resolved_executable"], str(broken))
        self.assertEqual(failure["start_error"]["type"], "FileNotFoundError")
        self.assertIsNone(failure["process_identity"])
        self.assertEqual(ExecutorProcessStore(self.db).get(failed.process_id).state, "start_failed")

    def test_redacted_argv_prompt_stdin_and_environment_values_are_not_copied(self):
        spec = self.spec(argv=(sys.executable, "-c", "print('ok')", "argv-secret-prompt"),
                         redacted_arg_indexes=(3,), stdin_mode="text",
                         environment_keys=("TOKEN",))
        result = self.run_spec(spec, stdin_text="stdin-secret-prompt",
                               run_env={"TOKEN": "environment-secret-value"})
        path, evidence = self.evidence(result)
        raw = path.read_text()
        for secret in ("argv-secret-prompt", "stdin-secret-prompt", "environment-secret-value"):
            self.assertNotIn(secret, raw)
        self.assertEqual(evidence["resolved_launch_spec"]["argv"][-1], "<redacted>")
        self.assertEqual(evidence["resolved_launch_spec"]["environment_keys"], ["TOKEN"])
        self.assertFalse(evidence["parent_environment_inherited_by_popen"])
        self.assertEqual(evidence["environment_source"], "caller_supplied_mapping")

    def test_redacted_executable_is_not_leaked_through_preflight_error(self):
        secret = str(self.root / "private-executable-name")
        path, _ = self.evidence(self.run_spec(self.spec(argv=(secret,), redacted_arg_indexes=(0,))))
        self.assertNotIn(secret, path.read_text())

    def test_redacted_executable_alias_suppresses_derived_paths(self):
        private = self.root / "private-executable-target"
        shutil.copy2(sys.executable, private)
        alias = self.root / "executable-alias"
        alias.symlink_to(private)
        result = self.run_spec(self.spec(
            argv=(str(alias), "-c", "print('ok')"), redacted_arg_indexes=(0,),
        ))
        self.assertEqual(result.exit_code, 0)
        path, evidence = self.evidence(result)
        self.assertNotIn(str(private), path.read_text())
        self.assertIsNone(evidence["observed_process_executable"])
        self.assertIsNone(evidence["preflight"]["resolved_executable"])
        self.assertEqual(evidence["observed_process_executable_provenance"], "redacted_argv0")
        self.assertEqual(evidence["preflight"]["resolved_executable_provenance"], "redacted_argv0")
        self.assertIn("preflight.resolved_executable", evidence["missing_fields"])

    def test_staged_symlink_and_forged_payload_are_not_completed_evidence(self):
        outside = self.root / "unrelated.json"
        outside.write_text('{"unrelated": true}')
        original_write = launch_evidence.atomic_write_json
        for replacement in ("symlink", "forged_json", "fifo"):
            with self.subTest(replacement=replacement):
                def substitute(path, *args, **kwargs):
                    original_write(path, *args, **kwargs)
                    path.unlink()
                    if replacement == "symlink":
                        path.symlink_to(outside)
                    elif replacement == "fifo":
                        os.mkfifo(path)
                    else:
                        path.write_text('{"forged": true}')

                with patch.object(launch_evidence, "atomic_write_json", side_effect=substitute):
                    result = self.run_spec()
                self.assertEqual(result.exit_code, 0)
                self.assertTrue(result.verified_exit)
                reference = json.loads(result.launch_spec_path.read_text())["resolved_launch_evidence"]
                self.assertEqual(reference["status"], "write_failed")
                self.assertFalse(Path(reference["path"]).exists())
                self.assertFalse(Path(reference["path"]).is_symlink())
                self.assertEqual(outside.read_text(), '{"unrelated": true}')

    def test_staging_replacement_and_content_mutation_at_link_are_rejected(self):
        outside = self.root / "unrelated.json"
        outside.write_text('{"unrelated": true}')
        original_link = os.link
        for replacement in ("staged_symlink", "published_content"):
            with self.subTest(replacement=replacement):
                def substitute(source, target, **kwargs):
                    if replacement == "staged_symlink":
                        staged = next(self.binding.artifact_root.glob(".*.staged"))
                        staged.rename(self.binding.artifact_root / "saved-staged-payload")
                        staged.symlink_to(outside)
                    original_link(source, target, **kwargs)
                    if replacement == "published_content":
                        (self.binding.artifact_root / target).write_text('{"forged": true}')

                with patch.object(launch_evidence.os, "link", side_effect=substitute):
                    result = self.run_spec()
                self.assertEqual(result.exit_code, 0)
                self.assertTrue(result.verified_exit)
                reference = json.loads(result.launch_spec_path.read_text())["resolved_launch_evidence"]
                self.assertEqual(reference["status"], "write_failed")
                self.assertFalse(Path(reference["path"]).exists())
                self.assertFalse(Path(reference["path"]).is_symlink())
                self.assertEqual(outside.read_text(), '{"unrelated": true}')

    def test_directory_replacement_during_staging_or_link_has_no_completed_reference(self):
        original_write = launch_evidence.atomic_write_json
        original_link = os.link
        for timing in ("staging", "link"):
            with self.subTest(timing=timing):
                moved = self.root / ("moved-" + timing)

                def move_directory():
                    self.binding.artifact_root.rename(moved)
                    self.binding.artifact_root.mkdir()

                def write_and_move(path, *args, **kwargs):
                    original_write(path, *args, **kwargs)
                    move_directory()

                def link_and_move(source, target, **kwargs):
                    original_link(source, target, **kwargs)
                    move_directory()

                target = "atomic_write_json" if timing == "staging" else "os.link"
                action = write_and_move if timing == "staging" else link_and_move
                with patch("agent_taskflow.launch_evidence." + target, side_effect=action):
                    result = self.run_spec()
                self.assertEqual(result.exit_code, 0)
                self.assertTrue(result.verified_exit)
                reference = json.loads(result.launch_spec_path.read_text())["resolved_launch_evidence"]
                self.assertEqual(reference["status"], "write_failed")
                self.assertFalse(Path(reference["path"]).exists())
                self.assertFalse((moved / Path(reference["path"]).name).exists())

    def test_failed_publication_preserves_subprocess_result_and_has_no_completed_sidecar(self):
        with patch("agent_taskflow.launch_evidence.os.link", side_effect=OSError("fixture failure")):
            result = self.run_spec()
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.verified_exit)
        manifest = json.loads(result.pid_manifest_path.read_text())
        reference = manifest["resolved_launch_evidence"]
        self.assertEqual(reference["status"], "write_failed")
        self.assertFalse(Path(reference["path"]).exists())
        self.assertEqual(list(self.binding.artifact_root.glob("*.staged")), [])

    def test_missing_database_is_read_only_and_does_not_create_or_migrate(self):
        missing = self.root / "missing.db"
        snapshot = read_bound_attempt_snapshot(replace(self.binding, db_path=missing))
        self.assertFalse(missing.exists())
        self.assertEqual(snapshot["provenance"]["status"], "unavailable")
        self.assertEqual(snapshot["provenance"]["error_type"], "OperationalError")
        self.assertFalse(snapshot["provenance"]["binding_verified"])


class ImmutableLaunchPublicationTests(unittest.TestCase):
    def test_collision_and_symlink_destination_never_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish_once(root, "observation.json", {"first": True})
            original = (root / "observation.json").read_bytes()
            with self.assertRaises(FileExistsError):
                _publish_once(root, "observation.json", {"second": True})
            self.assertEqual((root / "observation.json").read_bytes(), original)
            (root / "link.json").symlink_to(root / "observation.json")
            with self.assertRaises(FileExistsError):
                _publish_once(root, "link.json", {"second": True})
            self.assertTrue((root / "link.json").is_symlink())
            self.assertEqual((root / "observation.json").read_bytes(), original)

    def test_symlink_directory_and_path_traversal_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            with self.assertRaises(OSError):
                _publish_once(alias, "observation.json", {"unsafe": True})
            self.assertEqual(list(real.iterdir()), [])
            with self.assertRaises(ValueError):
                launch_evidence_reference(root, "../../outside")
            with self.assertRaises(ValueError):
                _publish_once(root / "..", "observation.json", {"unsafe": True})
            with self.assertRaises(ValueError):
                _publish_once(root, "../outside.json", {"unsafe": True})


if __name__ == "__main__":
    unittest.main()
