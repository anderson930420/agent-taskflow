"""Tests for the read-only scheduler candidate discovery CLI script."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "discover_scheduler_candidates.py"


class DiscoverSchedulerCandidatesScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def seed_task(
        self,
        task_key: str,
        *,
        status: str,
        project: str = "agent-taskflow",
    ) -> Path:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project=project,
                board=project,
                title=f"Candidate {task_key}",
                status=status,
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )
        return artifact_dir

    def run_script(
        self,
        *args: str,
        db_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        cli_args = [
            sys.executable,
            str(SCRIPT),
        ]
        if db_path is not None or "--db-path" not in args:
            cli_args.extend(["--db-path", str(db_path or self.db_path)])
        cli_args.extend(args)
        return subprocess.run(
            cli_args,
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def db_counts(self) -> dict[str, int]:
        with sqlite3.connect(self.db_path) as conn:
            return {
                "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                "events": conn.execute(
                    "SELECT COUNT(*) FROM task_events"
                ).fetchone()[0],
                "artifacts": conn.execute(
                    "SELECT COUNT(*) FROM task_artifacts"
                ).fetchone()[0],
                "worktrees": conn.execute(
                    "SELECT COUNT(*) FROM task_worktrees"
                ).fetchone()[0],
            }

    def test_pretty_json_returns_ok_and_safety(self) -> None:
        self.seed_task("AT-CLI-G-001", status="queued")

        result = self.run_script("--pretty")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "read_only")
        self.assertTrue(payload["safety"]["read_only"])
        self.assertFalse(payload["safety"]["db_written"])
        self.assertFalse(payload["safety"]["artifact_written"])
        self.assertFalse(payload["safety"]["proposal_created"])
        self.assertFalse(payload["safety"]["confirmation_created"])
        self.assertFalse(payload["safety"]["handoff_created"])
        self.assertFalse(payload["safety"]["runtime_started"])
        self.assertFalse(payload["safety"]["approved_task_runner_called"])
        self.assertFalse(payload["safety"]["github_mutated"])
        candidate = payload["candidates"][0]
        self.assertEqual(candidate["task_key"], "AT-CLI-G-001")
        self.assertEqual(
            candidate["recommended_command_kind"], "create_task_execution_package"
        )
        self.assertTrue(candidate["safety"]["read_only"])
        self.assertIn("not execution permission", payload["discovery_note"].lower())

    def test_default_json_emits_compact_json(self) -> None:
        self.seed_task("AT-CLI-G-002", status="queued")

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["candidate_count"], 1)
        # Compact output has no leading whitespace per line.
        self.assertNotIn("\n  ", result.stdout)

    def test_no_candidates_when_db_empty(self) -> None:
        result = self.run_script("--pretty")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["candidate_count"], 0)
        self.assertEqual(payload["candidates"], [])
        self.assertTrue(payload["safety"]["read_only"])

    def test_task_key_filter(self) -> None:
        self.seed_task("AT-CLI-G-FILT-A", status="queued")
        self.seed_task("AT-CLI-G-FILT-B", status="queued")

        result = self.run_script(
            "--pretty", "--task-key", "AT-CLI-G-FILT-A"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(
            payload["candidates"][0]["task_key"], "AT-CLI-G-FILT-A"
        )

    def test_project_filter(self) -> None:
        self.seed_task(
            "AT-CLI-G-PROJ-A", status="queued", project="agent-taskflow"
        )
        self.seed_task(
            "AT-CLI-G-PROJ-B", status="queued", project="another-project"
        )

        result = self.run_script("--pretty", "--project", "another-project")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        keys = [candidate["task_key"] for candidate in payload["candidates"]]
        self.assertIn("AT-CLI-G-PROJ-B", keys)
        self.assertNotIn("AT-CLI-G-PROJ-A", keys)

    def test_status_filter(self) -> None:
        self.seed_task("AT-CLI-G-S-Q", status="queued")
        self.seed_task("AT-CLI-G-S-B", status="blocked")

        result = self.run_script("--pretty", "--status", "blocked")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        keys = [candidate["task_key"] for candidate in payload["candidates"]]
        self.assertIn("AT-CLI-G-S-B", keys)
        self.assertNotIn("AT-CLI-G-S-Q", keys)

    def test_invalid_args_returns_structured_error_and_no_mutation(self) -> None:
        self.seed_task("AT-CLI-G-INVAL", status="queued")
        before = self.db_counts()

        result = self.run_script("--pretty", "--status", "definitely-not-a-status")

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["candidate_count"], 0)
        self.assertEqual(payload["candidates"], [])
        safety = payload["safety"]
        self.assertTrue(safety["read_only"])
        for flag in (
            "db_written",
            "artifact_written",
            "proposal_created",
            "confirmation_created",
            "handoff_created",
            "runtime_started",
            "approved_task_runner_called",
            "github_mutated",
        ):
            self.assertFalse(safety[flag], flag)
        self.assertEqual(self.db_counts(), before)

    def test_missing_db_returns_error_without_creating_db(self) -> None:
        missing = self.root / "missing" / "state.db"

        result = self.run_script("--pretty", db_path=missing)

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertFalse(missing.exists())
        self.assertTrue(payload["safety"]["read_only"])

    def test_script_does_not_mutate_db(self) -> None:
        self.seed_task("AT-CLI-G-NOMUT", status="queued")
        before = self.db_counts()
        before_status = self.store.get_task("AT-CLI-G-NOMUT").status

        result = self.run_script("--pretty")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.db_counts(), before)
        self.assertEqual(
            self.store.get_task("AT-CLI-G-NOMUT").status, before_status
        )

    def test_script_does_not_create_scheduler_artifacts(self) -> None:
        artifact_dir = self.seed_task("AT-CLI-G-NOARTIFACT", status="queued")
        package_path = artifact_dir / "task_execution_package.json"
        package_path.write_text("{}\n", encoding="utf-8")
        self.store.record_task_artifact(
            "AT-CLI-G-NOARTIFACT", "task_execution_package", package_path
        )

        result = self.run_script("--pretty")

        self.assertEqual(result.returncode, 0, result.stderr)
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute(
                """
                SELECT COUNT(*) FROM task_artifacts
                WHERE artifact_type IN (
                    'scheduler_proposal',
                    'scheduler_confirmation',
                    'scheduler_confirmation_verifier_report',
                    'intake_runner_handoff',
                    'runtime_handoff_execution'
                )
                """
            ).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertFalse((self.artifact_root / "scheduler_proposals").exists())

    def test_include_no_action_flag(self) -> None:
        artifact_dir = self.seed_task("AT-CLI-G-DONE", status="completed")
        for artifact_type, filename, event_type in (
            ("local_cleanup", "local_cleanup.json", "local_cleanup_completed"),
            (
                "remote_branch_cleanup",
                "remote_branch_cleanup.json",
                "remote_branch_cleanup_completed",
            ),
            ("task_closeout", "task_closeout.json", "task_closeout_completed"),
        ):
            payload = {
                "kind": event_type,
                "artifact_type": artifact_type,
                "task_key": "AT-CLI-G-DONE",
            }
            path = artifact_dir / filename
            path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            self.store.record_task_artifact(
                "AT-CLI-G-DONE", artifact_type, path
            )
            self.store.record_task_event(
                "AT-CLI-G-DONE",
                event_type,
                f"{artifact_type}_confirm",
                payload=payload,
            )

        default_result = self.run_script("--pretty")
        include_result = self.run_script("--pretty", "--include-no-action")

        self.assertEqual(default_result.returncode, 0, default_result.stderr)
        self.assertEqual(include_result.returncode, 0, include_result.stderr)
        default_payload = json.loads(default_result.stdout)
        include_payload = json.loads(include_result.stdout)
        default_kinds = [
            candidate["recommended_command_kind"]
            for candidate in default_payload["candidates"]
        ]
        include_kinds = [
            candidate["recommended_command_kind"]
            for candidate in include_payload["candidates"]
        ]
        self.assertNotIn("no_action", default_kinds)
        self.assertIn("no_action", include_kinds)


if __name__ == "__main__":
    unittest.main()
