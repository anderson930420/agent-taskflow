from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class ValidatorProcessCliTests(unittest.TestCase):
    def test_migration_cli_runs_without_site_packages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-S",
                    "scripts/migrate_validator_process_lifecycle.py",
                    "--db-path",
                    str(db_path),
                ],
                cwd=REPO_ROOT,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        payload = json.loads(completed.stdout)
        self.assertTrue(payload["migration_recorded"])
        self.assertTrue(payload["process_role_column_installed"])
        self.assertEqual(payload["active_validator_processes"], 0)
        self.assertEqual(payload["termination"]["shared_registry"], "executor_processes")
        self.assertTrue(payload["termination"]["verified_exit_required"])
        self.assertIn("changed-files:git-status", payload["managed_validator_commands"])


if __name__ == "__main__":
    unittest.main()
