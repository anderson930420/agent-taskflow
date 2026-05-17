"""Tests for scripts/run_mission_control_smoke.py.

These tests do not call external workers, GitHub, or the Mission Control
frontend. The smoke executor and validator are script-local fixtures.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_mission_control_smoke.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location("run_mission_control_smoke", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RunMissionControlSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_help_flag_succeeds(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("--task-key", result.stdout)
        self.assertIn("--workspace-root", result.stdout)
        self.assertIn("--keep-workspace", result.stdout)

    def test_cli_smoke_runs_full_api_dispatcher_readback_path(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--task-key",
                "AT-MC-SMOKE-TEST",
                "--workspace-root",
                str(self.workspace_root),
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["task_key"], "AT-MC-SMOKE-TEST")
        self.assertEqual(payload["final_status"], "waiting_approval")
        self.assertEqual(payload["executor"]["name"], "smoke")
        self.assertEqual(payload["executor"]["status"], "completed")
        self.assertEqual(payload["validator"]["name"], "smoke")
        self.assertEqual(payload["validator"]["status"], "passed")
        self.assertIn("mission_control_smoke_result.txt", payload["readbacks"]["artifacts"])
        self.assertIn("mission_contract.json", payload["readbacks"]["artifacts"])

        store = TaskMirrorStore(Path(payload["db_path"]))
        task = store.get_task("AT-MC-SMOKE-TEST")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")
        self.assertEqual(len(store.list_executor_runs("AT-MC-SMOKE-TEST")), 1)
        self.assertEqual(len(store.list_validation_results("AT-MC-SMOKE-TEST")), 1)
        artifact = Path(payload["executor"]["artifact"])
        self.assertEqual(artifact.read_text(encoding="utf-8"), "mission-control-smoke-ok\n")

    def test_validator_failure_blocks_task_through_dispatcher(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-MC-SMOKE-FAIL",
            artifact_content="wrong-content\n",
            expected_content="mission-control-smoke-ok\n",
        )

        self.assertFalse(summary["ok"])
        self.assertEqual(summary["final_status"], "blocked")
        self.assertEqual(summary["executor"]["status"], "completed")
        self.assertEqual(summary["validator"]["status"], "failed")

        store = TaskMirrorStore(Path(summary["db_path"]))
        task = store.get_task("AT-MC-SMOKE-FAIL")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "blocked")
        self.assertIn("content did not match", task.blocked_reason or "")

    def test_script_does_not_register_product_executor_or_github_or_ui(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertNotIn("get_executor(", text)
        self.assertNotIn("list_executor_names", text)
        self.assertNotIn("github", text.lower())
        self.assertNotIn("mission-control/", text)
        self.assertNotIn("subprocess.run", text)


if __name__ == "__main__":
    unittest.main()
