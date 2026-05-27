from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "run_scheduler_watcher_preview_smoke.py"
DOC = REPO_ROOT / "docs" / "scheduler-watcher-preview.md"


class RunSchedulerWatcherPreviewSmokeTests(unittest.TestCase):
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

        self.assertTrue(payload["ok"])

    def test_smoke_candidates_and_skips_expected(self) -> None:
        payload = self.run_smoke()

        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["skipped_count"], 3)
        self.assertTrue(payload["eligible_task_seen"])
        self.assertTrue(payload["blocked_task_skipped"])
        self.assertTrue(payload["waiting_task_skipped"])
        self.assertTrue(payload["completed_task_skipped"])

    def test_smoke_db_counts_unchanged(self) -> None:
        payload = self.run_smoke()

        self.assertTrue(payload["db_counts_unchanged"])
        self.assertTrue(payload["task_statuses_unchanged"])

    def test_smoke_safety_flags(self) -> None:
        payload = self.run_smoke()
        safety = payload["safety"]

        self.assertTrue(safety["dry_run_preview"])
        self.assertTrue(safety["read_only"])
        for key, value in safety.items():
            if key not in {"dry_run_preview", "read_only"}:
                self.assertFalse(value, key)

    def test_smoke_cli_outputs_json(self) -> None:
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

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])

    def test_doc_contains_safety_language(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        required = (
            "preview only",
            "read-only",
            "Suggested commands are inert text",
            "No execution happens",
            "No GitHub mutation happens",
            "No branch push happens",
            "No draft PR is created",
            "No approval, merge, or cleanup happens",
            "Human review remains required",
            "No API endpoint",
            "No Mission Control action UI",
        )
        for phrase in required:
            self.assertIn(phrase, text, phrase)


if __name__ == "__main__":
    unittest.main()
