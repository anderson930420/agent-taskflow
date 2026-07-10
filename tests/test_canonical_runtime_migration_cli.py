from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "migrate_canonical_runtime_admission.py"


class CanonicalRuntimeMigrationCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "state.db"

    def _run_without_site_packages(self) -> dict[str, object]:
        completed = subprocess.run(
            [
                sys.executable,
                "-S",
                str(SCRIPT),
                "--db-path",
                str(self.db_path),
            ],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        return json.loads(completed.stdout)

    def test_source_checkout_cli_requires_no_site_packages(self) -> None:
        payload = self._run_without_site_packages()

        self.assertTrue(payload["migration_recorded"])
        self.assertTrue(payload["implicit_pickup_disabled"])
        self.assertTrue(payload["executor_start_requires_claim_metadata"])
        self.assertTrue(payload["token_terminal_requires_owned_release"])
        self.assertEqual(payload["active_leases_by_auth_mode"], {})

    def test_cli_is_idempotent_and_verifies_inert_compatibility_trigger(self) -> None:
        first = self._run_without_site_packages()
        second = self._run_without_site_packages()

        self.assertEqual(first["migration"], second["migration"])
        self.assertTrue(second["implicit_pickup_disabled"])

        with sqlite3.connect(self.db_path) as conn:
            trigger_sql = conn.execute(
                """
                SELECT sql
                FROM sqlite_master
                WHERE type = 'trigger'
                  AND name = 'runtime_pickup_claim_after_preparing'
                """
            ).fetchone()[0]
        normalized = " ".join(trigger_sql.lower().split())
        self.assertRegex(normalized, re.compile(r"\bwhen\s+0\b"))


if __name__ == "__main__":
    unittest.main()
