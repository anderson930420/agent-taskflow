"""Tests for scripts/migrate_ticket_fields.py, run as an operator would.

The script is exercised as a real subprocess, matching the Step 3
`test_migrate_runtime_progress_script.py` pattern. Fixtures elsewhere call
`migrate_ticket_fields()`, the function this script wraps.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.store import init_db as init_task_db
from agent_taskflow.ticket_fields_schema import (
    TASK_TICKET_COLUMNS,
    TASK_TICKET_INDEXES,
    TICKET_FIELDS_MIGRATION,
    TICKET_FIELDS_MIGRATION_SCRIPT,
    missing_ticket_fields,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / TICKET_FIELDS_MIGRATION_SCRIPT


class MigrateTicketFieldsScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "state.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_script_path_matches_the_named_constant(self) -> None:
        self.assertTrue(SCRIPT.is_file())
        self.assertEqual(TICKET_FIELDS_MIGRATION_SCRIPT, "scripts/migrate_ticket_fields.py")

    def test_script_installs_and_reports_exactly_what_it_added(self) -> None:
        init_task_db(self.db_path)
        completed = self.run_script("--db-path", str(self.db_path))
        self.assertEqual(completed.returncode, 0, completed.stderr)

        report = json.loads(completed.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["migration"], TICKET_FIELDS_MIGRATION)
        self.assertEqual(
            report["task_columns_added"],
            [name for name, _sql in TASK_TICKET_COLUMNS],
        )
        self.assertEqual(
            sorted(report["indexes_added"]),
            sorted(name for name, _sql in TASK_TICKET_INDEXES),
        )
        self.assertFalse(report["already_installed"])
        self.assertTrue(report["migration_newly_recorded"])
        self.assertEqual(report["still_missing"], [])
        self.assertEqual(missing_ticket_fields(self.db_path), ())

    def test_second_run_is_an_idempotent_reported_no_op(self) -> None:
        init_task_db(self.db_path)
        first = self.run_script("--db-path", str(self.db_path))
        self.assertEqual(first.returncode, 0, first.stderr)

        second = self.run_script("--db-path", str(self.db_path))
        self.assertEqual(second.returncode, 0, second.stderr)
        report = json.loads(second.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["task_columns_added"], [])
        self.assertEqual(report["indexes_added"], [])
        self.assertTrue(report["already_installed"])
        self.assertFalse(report["migration_newly_recorded"])
        self.assertEqual(report["still_missing"], [])

    def test_script_refuses_without_the_legacy_schema_and_creates_nothing(self) -> None:
        completed = self.run_script("--db-path", str(self.db_path))
        self.assertEqual(completed.returncode, 2)
        report = json.loads(completed.stdout)
        self.assertFalse(report["ok"])
        self.assertTrue(report["refused"])
        self.assertIn("agent_taskflow.store.init_db", completed.stderr)
        self.assertFalse(self.db_path.exists())

    def test_script_requires_an_explicit_db_path(self) -> None:
        completed = self.run_script()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--db-path", completed.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
