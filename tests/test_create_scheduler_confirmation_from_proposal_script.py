"""CLI tests for scripts/create_scheduler_confirmation_from_proposal.py."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "create_scheduler_confirmation_from_proposal.py"


class _CliBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )


class ScriptHelpTests(_CliBase):
    def test_script_help(self) -> None:
        result = self._run("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scheduler_confirmation", result.stdout.lower())
        self.assertIn("--task-key", result.stdout)
        self.assertIn("--proposal-item-id", result.stdout)
        self.assertIn("--confirm-create-confirmation", result.stdout)


class ScriptSourceContractTests(unittest.TestCase):
    def test_script_source_has_no_forbidden_runtime_calls(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")

        strict_forbidden = (
            "executor_run_started",
            "validation_result",
            "runtime_execution_started",
            "create_verifier_report",
            "intake_runner_handoff",
            "subprocess",
            "requests.post",
            "gh pr",
        )
        for needle in strict_forbidden:
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)

        # `approved_task_runner` may only ever appear as a key reference;
        # it must never be imported or called from this CLI.
        self.assertNotIn("from agent_taskflow.approved_task_runner", text)
        self.assertNotIn("import agent_taskflow.approved_task_runner", text)
        self.assertNotIn("approved_task_runner(", text)
        self.assertNotIn("approved_task_runner.", text)

        forbidden_imports = (
            "from agent_taskflow.api",
            "import agent_taskflow.api",
            "from agent_taskflow.executors",
            "import agent_taskflow.executors",
            "from agent_taskflow.validators",
            "import agent_taskflow.validators",
            "mission_control",
            "mission-control",
        )
        for needle in forbidden_imports:
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
