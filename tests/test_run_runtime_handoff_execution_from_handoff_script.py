"""Tests for the run_runtime_handoff_execution_from_handoff.py CLI."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_runtime_handoff_execution_from_handoff import _seed_to_handoff

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_runtime_handoff_execution_from_handoff.py"


class RunRuntimeHandoffExecutionFromHandoffScriptTests(unittest.TestCase):
    def test_help_runs(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--handoff-id", result.stdout)
        self.assertIn("--confirm-run-approved-task-runner", result.stdout)

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            seeded = _seed_to_handoff(workspace, "AT-L6A-CLI-TEST")
            handoff = seeded["handoff"]
            before_events = list(
                seeded["store"].list_task_events(seeded["task_key"])
            )
            before_artifacts = list(
                seeded["store"].list_task_artifacts(seeded["task_key"])
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--task-key",
                    seeded["task_key"],
                    "--handoff-id",
                    handoff["handoff_id"],
                    "--db-path",
                    str(seeded["db_path"]),
                    "--artifact-root",
                    str(seeded["artifact_root"]),
                    "--handoff-artifact-path",
                    str(seeded["handoff_path"]),
                ],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                env={"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "dry_run")
            self.assertTrue(payload["preflight_passed"])

            self.assertEqual(
                len(before_events),
                len(seeded["store"].list_task_events(seeded["task_key"])),
            )
            self.assertEqual(
                len(before_artifacts),
                len(seeded["store"].list_task_artifacts(seeded["task_key"])),
            )

    def test_source_does_not_call_arbitrary_executor_or_github(self) -> None:
        source = SCRIPT.read_text()
        for forbidden in (
            "from agent_taskflow.executors",
            "from agent_taskflow.validators",
            "from agent_taskflow.branch_push",
            "from agent_taskflow.draft_pr",
            "import subprocess",
        ):
            self.assertNotIn(forbidden, source, f"unexpected import: {forbidden}")


if __name__ == "__main__":
    unittest.main()
