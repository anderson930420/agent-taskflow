"""Tests for the read-only scheduler confirmation eligibility helper."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    REPO_ROOT / "agent_taskflow" / "scheduler_confirmation_eligibility.py"
)


class SchedulerConfirmationEligibilityTests(unittest.TestCase):
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

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # tests
    # ------------------------------------------------------------------

    def test_source_does_not_import_or_call_forbidden_runtime_paths(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")

        strict_forbidden = (
            "create_event",
            "create_draft",
            "send_email",
            "executor_run_started",
            "validation_result",
            "create_scheduler_confirmation",
            "runtime_execution_started",
            "subprocess",
            "requests.post",
            "gh pr",
        )
        for token in strict_forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

        # `approved_task_runner` is permitted only as the safety-flag KEY
        # asserting the runner was NOT invoked. It must never appear as an
        # import target or as a function call.
        self.assertNotIn("from agent_taskflow.approved_task_runner", source)
        self.assertNotIn("import agent_taskflow.approved_task_runner", source)
        self.assertNotIn("approved_task_runner(", source)
        self.assertNotIn("approved_task_runner.", source)

        forbidden_imports = (
            "from agent_taskflow.api",
            "import agent_taskflow.api",
            "from agent_taskflow.executors",
            "import agent_taskflow.executors",
            "from agent_taskflow.validators",
            "import agent_taskflow.validators",
            "from scripts",
            "import scripts",
            "mission_control",
            "mission-control",
        )
        for token in forbidden_imports:
            with self.subTest(token=token):
                self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
