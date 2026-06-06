from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.github_issue_ingestion_failures import GitHubIssueIngestionFailureRegistry


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "summarize_real_scheduled_execution.py"


def _tick(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "schema_version": "github_issue_one_task_scheduler_tick.v1",
        "source": "github_issue_one_task_scheduler_tick",
        "status": "no_eligible_issues",
        "mode": "confirmed",
        "repo": "anderson930420/agent-taskflow",
        "selected_task_key": None,
        "lock": {"acquired": True, "contended": False, "released": True},
    }
    payload.update(overrides)
    return payload


class SummarizeRealScheduledExecutionScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log_path = self.root / "tick.jsonl"
        self.db_path = self.root / "state.db"
        self.repo = self.root / "repo"
        self.repo.mkdir()

        self.log_path.write_text(
            "\n".join(
                json.dumps(t, sort_keys=True)
                for t in (
                    _tick(status="no_eligible_issues"),
                    _tick(status="execution_completed", selected_task_key="AT-GH-5"),
                )
            )
            + "\n",
            encoding="utf-8",
        )

        store = TaskMirrorStore(self.db_path)
        store.init_db()
        store.upsert_task(
            TaskRecord(
                task_key="AT-GH-5",
                project="agent-taskflow",
                title="Waiting task",
                status="waiting_approval",
                repo_path=self.repo,
            )
        )
        store.upsert_task(
            TaskRecord(
                task_key="AT-GH-6",
                project="agent-taskflow",
                title="Blocked task",
                status="blocked",
                repo_path=self.repo,
                blocked_reason="GitHub issue is closed",
            )
        )
        GitHubIssueIngestionFailureRegistry(self.db_path).record_failure(
            repo="anderson930420/agent-taskflow",
            issue_number=99,
            error_summary="boom",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--db-path",
                str(self.db_path),
                "--log-path",
                str(self.log_path),
                *args,
            ],
            cwd=str(REPO_ROOT),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def test_help_runs(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=str(REPO_ROOT),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("--recent-limit", result.stdout)

    def test_json_emits_valid_json(self) -> None:
        result = self._run("--json")

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        for key in (
            "ok",
            "schema_version",
            "source",
            "log_path",
            "db_path",
            "last_tick",
            "recent_ticks",
            "backlog",
            "ingestion_failure_registry",
            "safety",
        ):
            self.assertIn(key, payload)

        self.assertEqual(payload["last_tick"]["status"], "execution_completed")
        self.assertEqual(payload["recent_ticks"]["execution_completed_count"], 1)
        self.assertEqual(payload["recent_ticks"]["no_eligible_count"], 1)
        self.assertEqual(payload["backlog"]["waiting_approval_count"], 1)
        self.assertEqual(payload["backlog"]["blocked_count"], 1)
        self.assertEqual(
            payload["ingestion_failure_registry"]["ingestion_failure_count"], 1
        )
        self.assertTrue(payload["safety"]["read_only"])
        self.assertFalse(payload["safety"]["db_written"])
        self.assertFalse(payload["safety"]["cron_modified"])

    def test_human_readable_output(self) -> None:
        result = self._run()

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        out = result.stdout
        self.assertIn("Last tick status: execution_completed", out)
        self.assertIn("waiting_approval: 1", out)
        self.assertIn("blocked: 1", out)
        self.assertIn("ingestion failure count: 1", out)
        # Read-only assurance is part of human output.
        self.assertIn("read-only", out.lower())

    def test_missing_log_is_tolerated(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--db-path",
                str(self.db_path),
                "--log-path",
                str(self.root / "does-not-exist.jsonl"),
                "--json",
            ],
            cwd=str(REPO_ROOT),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["last_tick"])


if __name__ == "__main__":
    unittest.main()
