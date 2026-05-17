"""Tests for scripts/run_pi_executor_golden_path_smoke.py.

These tests use a fake pi binary. They do not call the real Pi agent.
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

from agent_taskflow.executors.registry import list_executor_names
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_pi_executor_golden_path_smoke.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location("run_pi_executor_golden_path_smoke", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RunPiExecutorGoldenPathSmokeTests(unittest.TestCase):
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
        self.assertIn("--real-pi", result.stdout)
        self.assertIn("--confirm-real-pi", result.stdout)
        self.assertIn("--workspace-root", result.stdout)

    def test_refuses_real_pi_without_confirm_flag(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--real-pi",
                "--workspace-root",
                str(self.workspace_root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires --confirm-real-pi", result.stderr)

    def test_fake_pi_smoke_exercises_full_control_plane_path(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--task-key",
                "AT-PI-GOLDEN-PATH-TEST",
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
        self.assertEqual(payload["mode"], "fake-pi")
        self.assertEqual(payload["task_key"], "AT-PI-GOLDEN-PATH-TEST")
        self.assertEqual(payload["final_status"], "waiting_approval")
        self.assertEqual(payload["executor"]["name"], "pi")
        self.assertEqual(payload["executor"]["status"], "completed")
        self.assertEqual(payload["validator"]["name"], "pi-golden-path")
        self.assertEqual(payload["validator"]["status"], "passed")
        self.assertIn("pi_golden_path_result.txt", payload["readbacks"]["artifacts"])
        self.assertIn("mission_contract.json", payload["readbacks"]["artifacts"])
        self.assertIn("pi-executor.log", payload["readbacks"]["artifacts"])
        self.assertIn("pi_mission_prompt.md", payload["readbacks"]["artifacts"])

        store = TaskMirrorStore(Path(payload["db_path"]))
        task = store.get_task("AT-PI-GOLDEN-PATH-TEST")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "waiting_approval")
        runs = store.list_executor_runs("AT-PI-GOLDEN-PATH-TEST")
        validations = store.list_validation_results("AT-PI-GOLDEN-PATH-TEST")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["executor"], "pi")
        self.assertEqual(runs[0]["status"], "completed")
        self.assertEqual(len(validations), 1)
        self.assertEqual(validations[0]["validator"], "pi-golden-path")
        self.assertEqual(validations[0]["status"], "passed")

        artifact = Path(payload["artifact_dir"]) / "pi_golden_path_result.txt"
        self.assertEqual(artifact.read_text(encoding="utf-8"), "pi-golden-path-ok")

    def test_expected_artifact_content_contract_has_no_trailing_newline(self) -> None:
        smoke = _load_smoke_module()

        self.assertEqual(smoke.EXPECTED_ARTIFACT_CONTENT, "pi-golden-path-ok")
        self.assertFalse(smoke.EXPECTED_ARTIFACT_CONTENT.endswith("\n"))
        fake_script = smoke._fake_pi_script(write_expected_artifact=True)
        self.assertIn("write_text('pi-golden-path-ok', encoding='utf-8')", fake_script)
        self.assertNotIn("pi-golden-path-ok\\n", fake_script)

    def test_validator_failure_blocks_task(self) -> None:
        smoke = _load_smoke_module()

        summary = smoke.run_smoke(
            workspace_root=self.workspace_root,
            task_key="AT-PI-GOLDEN-PATH-FAIL",
            fake_pi_writes_expected_artifact=False,
        )

        self.assertFalse(summary["ok"])
        self.assertEqual(summary["final_status"], "blocked")
        self.assertEqual(summary["executor"]["name"], "pi")
        self.assertEqual(summary["executor"]["status"], "completed")
        self.assertEqual(summary["validator"]["status"], "failed")

        store = TaskMirrorStore(Path(summary["db_path"]))
        task = store.get_task("AT-PI-GOLDEN-PATH-FAIL")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "blocked")
        self.assertIn("content mismatch", task.blocked_reason or "")

    def test_no_product_executor_registry_entry_is_added(self) -> None:
        self.assertEqual(
            list_executor_names(),
            ["manual", "noop", "shell", "opencode", "pi"],
        )

    def test_script_does_not_introduce_forbidden_surface_area(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8").lower()

        self.assertNotIn("mission-control/", text)
        self.assertNotIn("github", text)
        self.assertNotIn("multi-agent", text)
        self.assertNotIn("list_executor_names", text)
        self.assertNotIn("get_executor(", text)


if __name__ == "__main__":
    unittest.main()
