from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from agent_taskflow import Dispatcher
from agent_taskflow.approved_task_runner import (
    ApprovedTaskRunRequest,
    run_approved_task,
)
from agent_taskflow.lifecycle_control import RuntimeControlStore


class LifecycleEntrypointControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        RuntimeControlStore(self.db_path).pause(actor="test-operator")

    def test_approved_task_runner_returns_structured_blocked_when_paused(self) -> None:
        result = run_approved_task(
            ApprovedTaskRunRequest(
                task_key="AT-PR6-PAUSED",
                executor="noop",
                repo_path=self.repo,
                db_path=self.db_path,
                confirm_approved_task=True,
                preflight=False,
                require_codex_advisory_evidence=False,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.phase, "runtime_control")
        self.assertIn("paused", result.error or "")
        self.assertFalse(result.safety["executor_started"])
        self.assertTrue(result.safety["read_only"])

    def test_dispatcher_returns_structured_blocked_when_paused(self) -> None:
        result = Dispatcher(db_path=self.db_path).dispatch_task("AT-PR6-PAUSED")

        self.assertEqual(result.status, "blocked")
        self.assertIn("paused", result.summary)
        self.assertEqual(result.blocked_reason, result.summary)


if __name__ == "__main__":
    unittest.main()
