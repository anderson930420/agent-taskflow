"""Tests for scripts/run_workflow_policy_review_evidence_smoke.py."""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_workflow_policy_review_evidence_smoke.py"
EXAMPLE_POLICY = REPO_ROOT / "examples" / "workflow-policy.example.json"
SUMMARY_FILENAME = "workflow_policy_summary.json"
INDEX_FILENAME = "artifact_index.json"
SMOKE_TASK_KEY = "AT-REVIEW-EVIDENCE-SMOKE"


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "run_workflow_policy_review_evidence_smoke",
        SCRIPT,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_main(argv: list[str]) -> tuple[int, str]:
    module = _load_script_module()
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = module.main(argv)
    return exit_code, stdout.getvalue()


def _example_policy_data() -> dict:
    return json.loads(EXAMPLE_POLICY.read_text(encoding="utf-8"))


class WorkflowPolicyReviewEvidenceSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dirs: list[Path] = []

    def tearDown(self) -> None:
        for d in self._temp_dirs:
            shutil.rmtree(d, ignore_errors=True)

    def _make_temp_dir(self) -> Path:
        d = Path(tempfile.mkdtemp(prefix="agent-taskflow-test-"))
        self._temp_dirs.append(d)
        return d

    def test_smoke_succeeds_with_default_policy(self) -> None:
        exit_code, output = _run_main([])

        self.assertEqual(exit_code, 0)
        self.assertIn("Workflow policy review evidence smoke", output)
        self.assertIn("status: passed", output)

    def test_custom_artifact_dir_works(self) -> None:
        tmp = self._make_temp_dir()
        artifact_dir = tmp / "artifacts"

        exit_code, output = _run_main(["--artifact-dir", str(artifact_dir)])

        self.assertEqual(exit_code, 0)
        self.assertIn(f"artifact dir: {artifact_dir}", output)
        self.assertTrue((artifact_dir / SUMMARY_FILENAME).exists())
        self.assertTrue((artifact_dir / INDEX_FILENAME).exists())

    def test_custom_db_path_works(self) -> None:
        tmp = self._make_temp_dir()
        db_path = tmp / "state.db"
        artifact_dir = tmp / "artifacts"

        exit_code, output = _run_main(
            ["--db-path", str(db_path), "--artifact-dir", str(artifact_dir)]
        )

        self.assertEqual(exit_code, 0)
        self.assertTrue(db_path.exists())
        self.assertIn(f"db path: {db_path}", output)

    def test_review_evidence_includes_workflow_policy_summary(self) -> None:
        tmp = self._make_temp_dir()
        artifact_dir = tmp / "artifacts"

        exit_code, output = _run_main(["--artifact-dir", str(artifact_dir)])

        self.assertEqual(exit_code, 0)
        # Verify the artifact appears in review evidence output.
        self.assertIn("workflow_policy_summary.json", output)

    def test_review_evidence_includes_artifact_index(self) -> None:
        tmp = self._make_temp_dir()
        artifact_dir = tmp / "artifacts"

        exit_code, output = _run_main(["--artifact-dir", str(artifact_dir)])

        self.assertEqual(exit_code, 0)
        self.assertIn("artifact_index.json", output)

    def test_referenced_artifact_files_exist(self) -> None:
        tmp = self._make_temp_dir()
        artifact_dir = tmp / "artifacts"

        exit_code, output = _run_main(["--artifact-dir", str(artifact_dir)])

        self.assertEqual(exit_code, 0)
        # Check summary exists and is non-empty.
        summary_path = artifact_dir / SUMMARY_FILENAME
        self.assertTrue(summary_path.exists())
        summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertGreater(summary_path.stat().st_size, 0)

        # Check index exists and is non-empty.
        index_path = artifact_dir / INDEX_FILENAME
        self.assertTrue(index_path.exists())
        index_data = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertGreater(index_path.stat().st_size, 0)

        # Verify paths referenced in index point to existing files.
        for artifact in index_data.get("artifacts", []):
            artifact_path = artifact_dir / artifact["path"]
            self.assertTrue(artifact_path.exists())

    def test_workflow_policy_summary_has_validation_status_passed(self) -> None:
        tmp = self._make_temp_dir()
        artifact_dir = tmp / "artifacts"

        exit_code, output = _run_main(["--artifact-dir", str(artifact_dir)])

        summary_path = artifact_dir / SUMMARY_FILENAME
        summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(summary_data.get("validation_status"), "passed")

    def test_invalid_policy_causes_nonzero_exit(self) -> None:
        data = copy.deepcopy(_example_policy_data())
        # Break the policy: ai_workers_may_cleanup must be false.
        data["orchestration_boundary"]["ai_workers_may_cleanup"] = True

        tmp = self._make_temp_dir()
        policy_path = tmp / "workflow-policy.json"
        artifact_dir = tmp / "artifacts"
        policy_path.write_text(json.dumps(data), encoding="utf-8")

        exit_code, output = _run_main(
            ["--policy", str(policy_path), "--artifact-dir", str(artifact_dir)]
        )

        self.assertNotEqual(exit_code, 0)
        self.assertIn("status: failed", output)

    def test_missing_policy_causes_nonzero_exit(self) -> None:
        tmp = self._make_temp_dir()
        policy_path = tmp / "missing-policy.json"
        artifact_dir = tmp / "artifacts"

        exit_code, output = _run_main(
            ["--policy", str(policy_path), "--artifact-dir", str(artifact_dir)]
        )

        self.assertNotEqual(exit_code, 0)
        self.assertIn("status: failed", output)

    def test_script_does_not_execute_external_shell_commands(self) -> None:
        tmp = self._make_temp_dir()
        artifact_dir = tmp / "artifacts"

        with mock.patch.object(subprocess, "run") as run:
            exit_code, output = _run_main(["--artifact-dir", str(artifact_dir)])

        run.assert_not_called()
        self.assertEqual(exit_code, 0)
        self.assertIn("status: passed", output)

    def test_script_does_not_call_dispatcher_or_executors(self) -> None:
        tmp = self._make_temp_dir()
        artifact_dir = tmp / "artifacts"

        # Verify the script does not import or call dispatcher/executor modules.
        # We check this by patching the import mechanism to raise on those modules.
        original_import = __builtins__["__import__"]

        def selective_import(name, *args, **kwargs):
            if name in (
                "agent_taskflow.dispatcher",
                "agent_taskflow.executor",
                "agent_taskflow.executors",
            ) or name.startswith("agent_taskflow.dispatcher.") or name.startswith(
                "agent_taskflow.executor."
            ):
                self.fail(f"Script imported forbidden module: {name}")
            return original_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=selective_import):
            exit_code, output = _run_main(["--artifact-dir", str(artifact_dir)])

        self.assertEqual(exit_code, 0)
        self.assertIn("status: passed", output)

    def test_keep_artifacts_preserves_temp_output(self) -> None:
        exit_code, output = _run_main(["--keep-artifacts"])

        self.assertEqual(exit_code, 0)
        # Find the artifact dir from output.
        artifact_dir_line = next(
            line for line in output.splitlines() if line.startswith("artifact dir: ")
        )
        artifact_dir = Path(artifact_dir_line.removeprefix("artifact dir: "))
        self.assertTrue((artifact_dir / SUMMARY_FILENAME).exists())
        self.assertTrue((artifact_dir / INDEX_FILENAME).exists())
        self.assertIn("artifacts kept: yes", output)
        # Remember to clean up later.
        self._temp_dirs.append(artifact_dir)

    def test_artifact_index_references_workflow_policy_summary(self) -> None:
        tmp = self._make_temp_dir()
        artifact_dir = tmp / "artifacts"

        exit_code, output = _run_main(["--artifact-dir", str(artifact_dir)])

        index_path = artifact_dir / INDEX_FILENAME
        index_data = json.loads(index_path.read_text(encoding="utf-8"))

        summary_entries = [
            a for a in index_data.get("artifacts", [])
            if isinstance(a, dict) and a.get("name") == "workflow_policy_summary"
        ]
        self.assertEqual(len(summary_entries), 1)
        entry = summary_entries[0]
        self.assertEqual(entry["artifact_type"], "workflow_policy_summary")
        self.assertEqual(entry["path"], SUMMARY_FILENAME)
        self.assertIs(entry["required"], True)

    def test_store_api_records_workflow_policy_artifacts(self) -> None:
        tmp = self._make_temp_dir()
        db_path = tmp / "state.db"
        artifact_dir = tmp / "artifacts"

        exit_code, output = _run_main(
            ["--db-path", str(db_path), "--artifact-dir", str(artifact_dir)]
        )

        self.assertEqual(exit_code, 0)
        # Verify artifacts are recorded in DB via store API.
        module = _load_script_module()
        store = module.TaskMirrorStore(db_path)
        artifacts = store.list_task_artifacts(SMOKE_TASK_KEY)
        artifact_types = {a.artifact_type for a in artifacts}
        # workflow_policy_summary and artifact_index are explicit proof-of-work
        # metadata artifact types registered in TASK_ARTIFACT_TYPES.
        self.assertIn("workflow_policy_summary", artifact_types)
        self.assertIn("artifact_index", artifact_types)
        # Verify both artifacts were recorded.
        self.assertGreaterEqual(len(artifacts), 2)

    def test_review_evidence_shows_no_forbidden_action_in_output(self) -> None:
        """Smoke should not perform merge/push/cleanup/delete operations."""
        tmp = self._make_temp_dir()
        artifact_dir = tmp / "artifacts"

        exit_code, output = _run_main(["--artifact-dir", str(artifact_dir)])

        self.assertEqual(exit_code, 0)
        # Verify no forbidden action calls in output.
        forbidden = ["merge(", "push(", "cleanup(", "delete_branch", "delete_worktree"]
        for word in forbidden:
            self.assertNotIn(word, output.lower())

    def test_review_evidence_verifies_artifact_paths_exist(self) -> None:
        tmp = self._make_temp_dir()
        artifact_dir = tmp / "artifacts"

        exit_code, output = _run_main(["--artifact-dir", str(artifact_dir)])

        self.assertEqual(exit_code, 0)
        # Verify referenced paths in artifact index point to real files.
        index_path = artifact_dir / INDEX_FILENAME
        index_data = json.loads(index_path.read_text(encoding="utf-8"))
        for artifact in index_data.get("artifacts", []):
            artifact_path = artifact_dir / artifact["path"]
            self.assertTrue(
                artifact_path.exists(),
                f"referenced artifact path does not exist: {artifact_path}",
            )

    def test_summary_artifact_has_all_required_fields(self) -> None:
        tmp = self._make_temp_dir()
        artifact_dir = tmp / "artifacts"

        exit_code, output = _run_main(["--artifact-dir", str(artifact_dir)])

        summary_path = artifact_dir / SUMMARY_FILENAME
        summary_data = json.loads(summary_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        for field in (
            "artifact_type",
            "schema_version",
            "source_path",
            "validation_status",
            "allowed_executors",
            "required_validators",
            "path_policy",
            "workspace_policy",
            "proof_of_work",
            "human_review",
            "forbidden_actions",
            "deferred_integrations",
            "governance_invariants",
            "generated_at",
        ):
            self.assertIn(field, summary_data)

    def test_review_evidence_includes_size_bytes(self) -> None:
        tmp = self._make_temp_dir()
        artifact_dir = tmp / "artifacts"

        exit_code, output = _run_main(["--artifact-dir", str(artifact_dir)])

        self.assertEqual(exit_code, 0)
        # Verify review evidence lines include size info.
        self.assertIn("size:", output)


if __name__ == "__main__":
    unittest.main()