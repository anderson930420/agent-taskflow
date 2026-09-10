"""Tests for scripts/migrate_runtime_progress.py.

The migration is the only schema Step 3 owns. These tests pin that it is
additive, idempotent, and that it creates none of the §32.1 PR columns — the
Step 2 watcher is their sole writer.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "migrate_runtime_progress.py"

PR_COLUMNS = (
    "pr_number",
    "pr_url",
    "pr_state",
    "pr_merged",
    "pr_head_sha",
    "merge_commit_sha",
    "review_decision",
    "ci_status",
    "integrated_base_sha",
    "reintegration_count",
    "reintegration_required",
    "pr_last_polled_at",
)


class MigrateRuntimeProgressScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "state.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_script(self) -> dict[str, object]:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--db-path", str(self.db_path)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_script_installs_the_progress_tables(self) -> None:
        payload = self.run_script()
        self.assertTrue(payload["migration_recorded"])
        self.assertTrue(payload["attempt_progress_installed"])
        self.assertTrue(payload["attempt_observed_steps_installed"])

    def test_script_is_idempotent(self) -> None:
        first = self.run_script()
        second = self.run_script()
        self.assertTrue(second["migration_recorded"])
        self.assertEqual(second["attempt_progress_rows"], 0)
        self.assertEqual(
            second["task_columns_added_by_this_migration"],
            [],
            "a rerun must not alter the tasks table",
        )
        self.assertEqual(first["migration"], second["migration"])

    def test_script_creates_no_pr_columns(self) -> None:
        payload = self.run_script()
        self.assertEqual(payload["pr_columns_created_by_this_migration"], [])
        self.assertEqual(payload["pr_columns_present"], [])
        with sqlite3.connect(self.db_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        for column in PR_COLUMNS:
            with self.subTest(column=column):
                self.assertNotIn(column, columns)

    def test_script_leaves_existing_pr_columns_untouched(self) -> None:
        self.run_script()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("ALTER TABLE tasks ADD COLUMN pr_number INTEGER")
            conn.execute("ALTER TABLE tasks ADD COLUMN ci_status TEXT")

        payload = self.run_script()
        self.assertEqual(payload["pr_columns_created_by_this_migration"], [])
        self.assertEqual(payload["pr_columns_present"], ["ci_status", "pr_number"])
        self.assertEqual(payload["task_columns_added_by_this_migration"], [])

    def test_script_reports_no_github_or_pr_write(self) -> None:
        payload = self.run_script()
        self.assertFalse(payload["pr_fields_written"])
        self.assertFalse(payload["github_contacted"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
