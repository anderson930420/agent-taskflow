from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "migrate_attempt_resources.py"


class AttemptResourcesCliTests(unittest.TestCase):
    def test_source_checkout_cli_runs_without_site_packages_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            command = [
                sys.executable,
                "-S",
                str(SCRIPT),
                "--db-path",
                str(db_path),
            ]
            first = subprocess.run(
                command,
                cwd=REPO_ROOT,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            second = subprocess.run(
                command,
                cwd=REPO_ROOT,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            first_payload = json.loads(first.stdout)
            second_payload = json.loads(second.stdout)
            for payload in (first_payload, second_payload):
                self.assertEqual(
                    payload["migration"], "level2_attempt_scoped_resources_v1"
                )
                self.assertTrue(payload["migration_recorded"])
                self.assertEqual(payload["active_attempt_resources"], 0)
                self.assertFalse(payload["historical_worktrees_deleted"])
                self.assertFalse(payload["historical_artifacts_deleted"])
                self.assertFalse(payload["historical_branches_deleted"])

    def test_reap_mode_is_safe_on_empty_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-S",
                    str(SCRIPT),
                    "--db-path",
                    str(db_path),
                    "--reap",
                ],
                cwd=REPO_ROOT,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["expired_attempt_ids_reaped"], [])
            self.assertEqual(payload["reaped_attempt_ids"], [])
            self.assertEqual(payload["blocked_live_pid_attempt_ids"], [])


if __name__ == "__main__":
    unittest.main()
