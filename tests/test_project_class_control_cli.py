from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL_SCRIPT = REPO_ROOT / "scripts" / "runtime_control.py"
MIGRATION_SCRIPT = REPO_ROOT / "scripts" / "migrate_project_class_controls.py"


class ProjectClassControlCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db_path = root / "state.db"
        repo = root / "repo"
        repo.mkdir()
        artifacts = root / "artifacts"
        artifacts.mkdir()
        mirror = TaskMirrorStore(self.db_path)
        mirror.init_db()
        mirror.upsert_task(
            TaskRecord(
                task_key="AT-M1D-CLI",
                project="project-a",
                status="queued",
                repo_path=repo,
                artifact_dir=artifacts,
            )
        )
        attempts = AttemptStore(self.db_path)
        attempts.init_db()
        attempts.register_task_identity(
            "AT-M1D-CLI", task_class="docs-only", is_legacy=False
        )

    def _run(self, script: Path, *args: str, check: bool = True):
        return subprocess.run(
            [sys.executable, "-S", str(script), *args],
            cwd=REPO_ROOT,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_migration_cli_reports_new_scope_contract(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            before = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name = 'level2_project_class_controls_v1'"
            ).fetchone()
        self.assertIsNone(before)
        completed = self._run(
            MIGRATION_SCRIPT, "--db-path", str(self.db_path)
        )
        payload = json.loads(completed.stdout)

        self.assertTrue(payload["migration_recorded"])
        self.assertTrue(payload["scope_schema_verified"])
        self.assertEqual(
            payload["supported_scopes"],
            ["global", "project", "task_class", "task", "attempt"],
        )
        self.assertFalse(payload["actual_auto_merge_enabled"])

    def test_project_pause_and_task_class_governance_commands_are_distinct(self) -> None:
        self._run(MIGRATION_SCRIPT, "--db-path", str(self.db_path))
        paused = json.loads(
            self._run(
                CONTROL_SCRIPT,
                "pause",
                "--db-path",
                str(self.db_path),
                "--scope-kind",
                "project",
                "--scope-id",
                "project-a",
                "--actor",
                "alice",
            ).stdout
        )
        self.assertEqual(paused["control"]["mode"], "paused")
        self.assertEqual(paused["project_pause_semantics"], "deny_new_admission_only")

        disabled = json.loads(
            self._run(
                CONTROL_SCRIPT,
                "disable-governance",
                "--db-path",
                str(self.db_path),
                "--scope-kind",
                "task_class",
                "--scope-id",
                "docs-only",
                "--actor",
                "bob",
            ).stdout
        )
        self.assertFalse(disabled["task_class_governance_permitted"])
        self.assertEqual(disabled["task_class_semantics"], "governance_eligibility_only")
        self.assertFalse(disabled["actual_auto_merge_enabled"])
        self.assertFalse(disabled["os_signals_sent"])

        cleared = json.loads(
            self._run(
                CONTROL_SCRIPT,
                "clear",
                "--db-path",
                str(self.db_path),
                "--scope-kind",
                "task_class",
                "--scope-id",
                "docs-only",
                "--actor",
                "bob",
            ).stdout
        )
        self.assertTrue(cleared["task_class_governance_permitted"])
        self.assertFalse(cleared["actual_auto_merge_enabled"])

    def test_mutation_requires_explicit_actor_and_help_explains_boundaries(self) -> None:
        missing_actor = self._run(
            CONTROL_SCRIPT,
            "pause",
            "--db-path",
            str(self.db_path),
            "--scope-kind",
            "project",
            "--scope-id",
            "project-a",
            check=False,
        )
        self.assertNotEqual(missing_actor.returncode, 0)
        self.assertIn("--actor is required", missing_actor.stderr)

        help_result = self._run(CONTROL_SCRIPT, "--help")
        self.assertIn("Project pause denies new execution admission", help_result.stdout)
        self.assertIn("sends no OS signal", help_result.stdout)


if __name__ == "__main__":
    unittest.main()
