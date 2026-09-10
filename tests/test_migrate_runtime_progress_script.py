"""Tests for scripts/migrate_runtime_progress.py.

The migration is the only schema Step 3 owns. These tests pin that it is
additive and idempotent, that it creates none of the §32.1 PR columns — the
Step 2 watcher is their sole writer — and that it **fails closed** instead of
installing the lifecycle schema it depends on.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.attempt_schema import (
    TASK_ATTEMPT_LIFECYCLE_MIGRATION,
    migrate_task_attempt_lifecycle,
)
from agent_taskflow.store import init_db


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "migrate_runtime_progress.py"
LIFECYCLE_SCRIPT = "scripts/migrate_task_attempt_lifecycle.py"

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


def _tables(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def _task_columns(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        return [row[1] for row in conn.execute("PRAGMA table_info(tasks)")]


class ScriptTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "state.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_script(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--db-path", str(self.db_path)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            check=False,
        )

    def run_ok(self) -> dict[str, object]:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def prepare_lifecycle_schema(self) -> None:
        """What an operator does on purpose, before Step 3's migration."""
        init_db(self.db_path)
        migrate_task_attempt_lifecycle(self.db_path)


class FailClosedTests(ScriptTestCase):
    """3a — never install lifecycle schema; refuse and name the migration."""

    def assert_refusal_names_the_migration(
        self, result: subprocess.CompletedProcess[str]
    ) -> dict[str, object]:
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(TASK_ATTEMPT_LIFECYCLE_MIGRATION, result.stderr)
        self.assertIn(LIFECYCLE_SCRIPT, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["refused"])
        self.assertEqual(payload["required_migration"], TASK_ATTEMPT_LIFECYCLE_MIGRATION)
        self.assertEqual(payload["required_script"], LIFECYCLE_SCRIPT)
        return payload

    def test_missing_database_is_refused_and_not_created(self) -> None:
        result = self.run_script()
        self.assert_refusal_names_the_migration(result)
        self.assertFalse(self.db_path.exists(), "the refusal must not create the DB")

    def test_database_without_lifecycle_schema_is_refused_untouched(self) -> None:
        # Base tables only — what API startup (store.init_db) produces.
        init_db(self.db_path)
        tables_before = _tables(self.db_path)
        columns_before = _task_columns(self.db_path)

        result = self.run_script()

        self.assert_refusal_names_the_migration(result)
        self.assertEqual(_tables(self.db_path), tables_before)
        self.assertEqual(_task_columns(self.db_path), columns_before)
        self.assertNotIn("attempts", _tables(self.db_path))
        self.assertNotIn("attempt_progress", _tables(self.db_path))

    def test_refusal_lists_what_is_missing(self) -> None:
        init_db(self.db_path)
        result = self.run_script()
        self.assertIn("tasks.task_id", result.stderr)
        self.assertIn("table attempts", result.stderr)


class InstallTests(ScriptTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.prepare_lifecycle_schema()

    def test_script_installs_the_progress_tables(self) -> None:
        payload = self.run_ok()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["migration_recorded"])
        self.assertTrue(payload["attempt_progress_installed"])
        self.assertTrue(payload["attempt_observed_steps_installed"])
        self.assertFalse(payload["lifecycle_migration_run_by_this_script"])

    def test_script_adds_no_column_to_tasks(self) -> None:
        columns_before = _task_columns(self.db_path)
        payload = self.run_ok()
        self.assertEqual(payload["task_columns_added_by_this_migration"], [])
        self.assertEqual(_task_columns(self.db_path), columns_before)

    def test_script_is_idempotent(self) -> None:
        first = self.run_ok()
        second = self.run_ok()
        self.assertTrue(second["migration_recorded"])
        self.assertEqual(second["attempt_progress_rows"], 0)
        self.assertEqual(second["task_columns_added_by_this_migration"], [])
        self.assertEqual(first["migration"], second["migration"])

    def test_script_creates_no_pr_columns(self) -> None:
        payload = self.run_ok()
        self.assertEqual(payload["pr_columns_created_by_this_migration"], [])
        self.assertEqual(payload["pr_columns_present"], [])
        columns = set(_task_columns(self.db_path))
        for column in PR_COLUMNS:
            with self.subTest(column=column):
                self.assertNotIn(column, columns)

    def test_script_leaves_existing_pr_columns_untouched(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("ALTER TABLE tasks ADD COLUMN pr_number INTEGER")
            conn.execute("ALTER TABLE tasks ADD COLUMN ci_status TEXT")

        payload = self.run_ok()
        self.assertEqual(payload["pr_columns_created_by_this_migration"], [])
        self.assertEqual(payload["pr_columns_present"], ["ci_status", "pr_number"])
        self.assertEqual(payload["task_columns_added_by_this_migration"], [])

    def test_script_reports_no_github_or_pr_write(self) -> None:
        payload = self.run_ok()
        self.assertFalse(payload["pr_fields_written"])
        self.assertFalse(payload["github_contacted"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
