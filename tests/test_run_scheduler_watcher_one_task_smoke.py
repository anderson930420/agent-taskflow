"""Tests for the Level 8B watcher one-task smoke."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "run_scheduler_watcher_one_task_smoke.py"
DOC = REPO_ROOT / "docs" / "scheduler-watcher-one-task.md"


class RunSchedulerWatcherOneTaskSmokeTests(unittest.TestCase):
    def run_smoke(self) -> dict[str, object]:
        result = subprocess.run(
            [sys.executable, str(SMOKE_SCRIPT)],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def test_smoke_returns_ok(self) -> None:
        payload = self.run_smoke()

        self.assertTrue(payload["ok"], payload)

    def test_smoke_dry_run_no_calls(self) -> None:
        payload = self.run_smoke()
        dry_run = payload["dry_run"]

        self.assertEqual(dry_run["candidate_count"], 1)
        self.assertEqual(dry_run["runner_call_count"], 0)
        self.assertEqual(dry_run["branch_push_call_count"], 0)
        self.assertEqual(dry_run["draft_pr_call_count"], 0)

    def test_smoke_confirmed_processes_one_task(self) -> None:
        payload = self.run_smoke()
        confirmed = payload["confirmed"]

        self.assertEqual(confirmed["status"], "completed_one_task")
        self.assertEqual(confirmed["selected_task_key"], "AT-L8B-WATCHER-ELIGIBLE")
        self.assertEqual(confirmed["processed_task_count"], 1)
        self.assertEqual(confirmed["runner_call_count"], 1)
        self.assertEqual(confirmed["branch_push_call_count"], 1)
        self.assertEqual(confirmed["draft_pr_call_count"], 1)

    def test_smoke_resume_no_duplicate_calls(self) -> None:
        payload = self.run_smoke()
        resume = payload["resume"]

        self.assertEqual(resume["status"], "completed_one_task")
        self.assertEqual(resume["task_to_draft_pr_status"], "draft_pr_already_created")
        self.assertEqual(resume["runner_call_count_after_resume"], 1)
        self.assertEqual(resume["branch_push_call_count_after_resume"], 1)
        self.assertEqual(resume["draft_pr_call_count_after_resume"], 1)

    def test_smoke_forbidden_side_effect_counts_zero(self) -> None:
        payload = self.run_smoke()
        forbidden = payload["forbidden_side_effect_counts"]

        self.assertEqual(forbidden["artifacts"], 0)
        self.assertEqual(forbidden["events"], 0)
        self.assertEqual(forbidden["payload_markers"], 0)

    def test_smoke_safety_flags(self) -> None:
        payload = self.run_smoke()
        safety = payload["safety"]

        self.assertTrue(safety["one_task_only"])
        self.assertTrue(safety["human_review_required"])
        for key in (
            "scheduler_loop_started",
            "background_worker_started",
            "automatic_task_picking_started",
            "multi_task_batch_started",
            "approved",
            "merged",
            "cleanup_performed",
        ):
            self.assertFalse(safety[key], key)

    def test_doc_contains_safety_language(self) -> None:
        text = DOC.read_text(encoding="utf-8").lower()
        required = (
            "one-task-at-a-time confirmed watcher",
            "one task per invocation",
            "preview is read-only",
            "first-candidate",
            "--confirm-select-first-candidate",
            "--confirm-run-watcher-one-task",
            "--confirm-run-one-shot-pipeline",
            "--confirm-prepare-pr",
            "--confirm-github-mutations",
            "--confirm-branch-push",
            "--confirm-draft-pr",
            "no background loop",
            "no scheduler daemon",
            "no cron",
            "no webhook",
            "no polling",
            "no multi-task batch execution",
            "no approval",
            "no merge",
            "no cleanup",
            "no task closeout",
            "no branch deletion",
            "no worktree deletion",
            "no mission control action ui",
            "no api endpoint",
            "human final review remains required",
            "no silent automatic picking",
        )
        for phrase in required:
            self.assertIn(phrase, text, phrase)


if __name__ == "__main__":
    unittest.main()
