from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_issue_to_waiting_approval_smoke import (  # noqa: E402
    APPROVED_TASK_STATUS,
    DEFAULT_TASK_KEY,
    FAKE_MARKER_RELATIVE,
    PACKAGE_EVENT_TYPE,
    PACKAGE_ARTIFACT_TYPE,
    PROMPT_ARTIFACT_TYPE,
    run_smoke,
)
from agent_taskflow.store import TaskMirrorStore  # noqa: E402


SCRIPT = REPO_ROOT / "scripts" / "run_issue_to_waiting_approval_smoke.py"


class IssueToWaitingApprovalSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_smoke(self) -> dict[str, object]:
        return run_smoke(workspace_root=self.workspace_root)

    def test_smoke_reaches_waiting_approval(self) -> None:
        summary = self._run_smoke()
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["final_status"], APPROVED_TASK_STATUS)
        self.assertEqual(summary["task_key"], DEFAULT_TASK_KEY)
        self.assertEqual(summary["handoff"]["status"], APPROVED_TASK_STATUS)
        self.assertEqual(summary["runner_summary"]["status"], APPROVED_TASK_STATUS)

    def test_smoke_records_package_artifacts(self) -> None:
        summary = self._run_smoke()
        artifact_dir = Path(str(summary["artifact_dir"]))
        prompt_path = Path(str(summary["package"]["implementation_prompt_path"]))
        package_path = Path(str(summary["package"]["package_path"]))

        self.assertTrue(prompt_path.is_file())
        self.assertTrue(package_path.is_file())
        self.assertEqual(prompt_path.parent, artifact_dir)
        self.assertEqual(package_path.parent, artifact_dir)
        self.assertEqual(summary["package"]["package_event_count"], 1)

        store = TaskMirrorStore(Path(str(summary["db_path"])))
        artifact_records = store.list_task_artifacts(str(summary["task_key"]))
        artifact_index = {(record.artifact_type, str(record.path)) for record in artifact_records}
        self.assertIn((PROMPT_ARTIFACT_TYPE, str(prompt_path)), artifact_index)
        self.assertIn((PACKAGE_ARTIFACT_TYPE, str(package_path)), artifact_index)

        events = store.list_task_events(str(summary["task_key"]))
        package_events = [event for event in events if event.event_type == PACKAGE_EVENT_TYPE]
        self.assertEqual(len(package_events), 1)

    def test_smoke_records_executor_run_and_validator_result(self) -> None:
        summary = self._run_smoke()
        self.assertGreaterEqual(int(summary["executor_run_count"]), 1)
        self.assertGreaterEqual(int(summary["validation_result_count"]), 1)

        store = TaskMirrorStore(Path(str(summary["db_path"])))
        runs = store.list_executor_runs(str(summary["task_key"]))
        self.assertGreaterEqual(len(runs), 1)
        self.assertTrue(any(run.get("status") == "completed" for run in runs))

        validations = store.list_validation_results(str(summary["task_key"]))
        self.assertGreaterEqual(len(validations), 1)
        self.assertTrue(any(item.get("status") == "passed" for item in validations))

    def test_smoke_safety_block_enforces_non_goals(self) -> None:
        summary = self._run_smoke()
        safety = summary["safety"]
        self.assertTrue(safety["local_only"])
        self.assertFalse(safety["used_real_executor"])
        self.assertFalse(safety["network_used"])
        self.assertFalse(safety["github_mutated"])
        self.assertFalse(safety["branch_pushed"])
        self.assertFalse(safety["pr_created"])
        self.assertFalse(safety["merged"])
        self.assertFalse(safety["approved"])
        self.assertFalse(safety["cleanup_performed"])
        self.assertFalse(safety["background_worker_started"])

    def test_smoke_writes_fake_marker_in_worktree(self) -> None:
        summary = self._run_smoke()
        worktree_root = Path(str(summary["worktree_root"]))
        task_key = str(summary["task_key"])
        # worktree path follows worktree_path_from_base(worktree_root, task_key)
        marker = worktree_root / task_key / FAKE_MARKER_RELATIVE
        self.assertTrue(
            marker.is_file(),
            f"fake executor marker missing in worktree: {marker}",
        )


class IssueToWaitingApprovalSmokeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.tmp.name) / "ws"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _invoke(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_cli_keep_workspace_preserves_workspace(self) -> None:
        completed = self._invoke(
            [
                "--workspace-root", str(self.workspace_root),
                "--keep-workspace",
            ]
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["final_status"], APPROVED_TASK_STATUS)
        self.assertTrue(payload["workspace_kept"])
        # Workspace and key artifacts are preserved for inspection.
        self.assertTrue(self.workspace_root.is_dir())
        artifact_dir = Path(payload["artifact_dir"])
        self.assertTrue(artifact_dir.is_dir())
        self.assertTrue((artifact_dir / "implementation_prompt.md").is_file())
        self.assertTrue((artifact_dir / "task_execution_package.json").is_file())

    def test_cli_default_temp_workspace_cleans_up_after_success(self) -> None:
        completed = self._invoke(["--pretty"])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["final_status"], APPROVED_TASK_STATUS)
        self.assertFalse(payload["workspace_kept"])
        # Temp workspace should be removed after default-mode success.
        self.assertFalse(
            Path(payload["workspace_root"]).exists(),
            "default mode should clean up the temp workspace",
        )

    def test_cli_emits_compact_json_when_json_flag_set(self) -> None:
        completed = self._invoke(
            [
                "--workspace-root", str(self.workspace_root),
                "--keep-workspace",
                "--json",
            ]
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip().count("\n"), 0)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main()
