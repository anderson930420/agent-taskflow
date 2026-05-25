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
from agent_taskflow.scheduler_proposals import (
    PROPOSAL_ARTIFACT_TYPE,
    PROPOSAL_EVENT_TYPE,
)
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "create_scheduler_proposal_from_candidate.py"

FORBIDDEN_ARTIFACT_TYPES = (
    "scheduler_confirmation",
    "scheduler_confirmation_verifier_report",
    "verifier_report",
    "intake_runner_handoff",
    "runtime_handoff_execution",
    "validation_result",
    "approval_decision",
    "merge_recorded",
    "cleanup",
)

FORBIDDEN_EVENT_TYPES = (
    "scheduler_confirmation_created",
    "scheduler_confirmation_verifier_report",
    "verifier_report",
    "intake_runner_handoff_created",
    "runtime_preflight_finished",
    "runtime_execution_started",
    "runtime_execution_finished",
    "executor_run_started",
    "executor_run_finished",
    "validation_result",
    "approval_decision",
    "merge_recorded",
    "cleanup",
)

FORBIDDEN_PAYLOAD_MARKERS = (
    "executor_run_started",
    "executor_run_finished",
    "validation_result",
    "approved_task_runner",
)


class CreateSchedulerProposalFromCandidateScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_task(self, task_key: str, *, status: str) -> Path:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title=f"CLI candidate proposal {task_key}",
                status=status,
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )
        return artifact_dir

    def _seed_completed_no_action(self, task_key: str) -> None:
        artifact_dir = self._seed_task(task_key, status="completed")
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
                "task_key": task_key,
            }
            path = artifact_dir / filename
            path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            self.store.record_task_artifact(task_key, artifact_type, path)
            self.store.record_task_event(
                task_key,
                event_type,
                f"{artifact_type}_confirm",
                payload=payload,
            )

    def _run_script(
        self,
        *args: str,
        task_key: str = "AT-J1-CLI-001",
        db_path: Path | None = None,
        artifact_root: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--db-path",
                str(db_path or self.db_path),
                "--task-key",
                task_key,
                "--artifact-root",
                str(artifact_root or self.artifact_root),
                *args,
            ],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def _db_counts(self) -> dict[str, int]:
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

    def _forbidden_side_effect_counts(self) -> dict[str, int]:
        with sqlite3.connect(self.db_path) as conn:
            artifact_placeholders = ",".join("?" for _ in FORBIDDEN_ARTIFACT_TYPES)
            event_placeholders = ",".join("?" for _ in FORBIDDEN_EVENT_TYPES)
            marker_clause = " OR ".join(
                "payload_json LIKE ?" for _ in FORBIDDEN_PAYLOAD_MARKERS
            )
            artifact_count = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM task_artifacts
                WHERE artifact_type IN ({artifact_placeholders})
                """,
                FORBIDDEN_ARTIFACT_TYPES,
            ).fetchone()[0]
            event_count = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM task_events
                WHERE event_type IN ({event_placeholders})
                """,
                FORBIDDEN_EVENT_TYPES,
            ).fetchone()[0]
            payload_count = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM task_events
                WHERE payload_json IS NOT NULL
                  AND ({marker_clause})
                """,
                tuple(f"%{marker}%" for marker in FORBIDDEN_PAYLOAD_MARKERS),
            ).fetchone()[0]
        return {
            "artifacts": artifact_count,
            "events": event_count,
            "payload_markers": payload_count,
        }

    def test_cli_dry_run_default_does_not_write(self) -> None:
        self._seed_task("AT-J1-CLI-001", status="queued")
        before = self._db_counts()

        result = self._run_script("--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "preview")
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(self._db_counts(), before)
        self.assertFalse((self.artifact_root / "scheduler_proposals").exists())

    def test_cli_dry_run_default_returns_preview(self) -> None:
        self._seed_task("AT-J1-CLI-002", status="queued")

        result = self._run_script("--json", task_key="AT-J1-CLI-002")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "preview")
        self.assertFalse(payload["proposal"]["created"])
        self.assertEqual(
            payload["proposal"]["recommended_command_kind"],
            "create_task_execution_package",
        )

    def test_cli_confirmed_mode_writes_only_proposal(self) -> None:
        self._seed_task("AT-J1-CLI-003", status="queued")
        before = self._db_counts()

        result = self._run_script(
            "--json",
            "--confirm-create-proposal",
            task_key="AT-J1-CLI-003",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "created")
        self.assertEqual(payload["mode"], "confirmed")
        artifact_path = Path(payload["proposal"]["proposal_artifact_path"])
        self.assertTrue(artifact_path.exists())
        artifacts = self.store.list_task_artifacts("AT-J1-CLI-003")
        events = self.store.list_task_events("AT-J1-CLI-003")
        self.assertEqual([a.artifact_type for a in artifacts], [PROPOSAL_ARTIFACT_TYPE])
        self.assertEqual([e.event_type for e in events], [PROPOSAL_EVENT_TYPE])
        after = self._db_counts()
        self.assertEqual(after["artifacts"], before["artifacts"] + 1)
        self.assertEqual(after["events"], before["events"] + 1)
        self.assertEqual(after["tasks"], before["tasks"])
        self.assertEqual(
            self._forbidden_side_effect_counts(),
            {"artifacts": 0, "events": 0, "payload_markers": 0},
        )

    def test_cli_candidate_not_ready_returns_blocked_exit_code_2(self) -> None:
        self._seed_completed_no_action("AT-J1-CLI-004")
        before = self._db_counts()

        result = self._run_script(
            "--json",
            "--include-no-action",
            task_key="AT-J1-CLI-004",
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["block_reason"], "candidate_not_ready")
        self.assertEqual(self._db_counts(), before)

    def test_cli_stale_expected_status_returns_blocked_exit_code_2(self) -> None:
        self._seed_task("AT-J1-CLI-005", status="queued")

        result = self._run_script(
            "--json",
            "--expected-status",
            "blocked",
            task_key="AT-J1-CLI-005",
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["block_reason"], "stale_expected_status")

    def test_cli_stale_expected_command_kind_returns_blocked_exit_code_2(self) -> None:
        self._seed_task("AT-J1-CLI-006", status="queued")

        result = self._run_script(
            "--json",
            "--expected-recommended-command-kind",
            "queued_task_handoff",
            task_key="AT-J1-CLI-006",
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(
            payload["block_reason"],
            "stale_expected_recommended_command_kind",
        )

    def test_cli_invalid_args_returns_argparse_failure(self) -> None:
        self._seed_task("AT-J1-CLI-007", status="queued")

        result = self._run_script(
            "--dry-run",
            "--confirm-create-proposal",
            "--json",
            task_key="AT-J1-CLI-007",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with argument", result.stderr)

    def test_cli_json_returns_parseable_compact_json(self) -> None:
        self._seed_task("AT-J1-CLI-008", status="queued")

        result = self._run_script("--json", task_key="AT-J1-CLI-008")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "preview")
        self.assertNotIn("\n  ", result.stdout)

    def test_cli_pretty_returns_parseable_indented_json(self) -> None:
        self._seed_task("AT-J1-CLI-009", status="queued")

        result = self._run_script("--pretty", task_key="AT-J1-CLI-009")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "preview")
        self.assertIn("\n  ", result.stdout)

    def test_cli_confirmed_mode_safety_says_no_downstream_behavior(self) -> None:
        self._seed_task("AT-J1-CLI-010", status="queued")

        result = self._run_script(
            "--json",
            "--confirm-create-proposal",
            task_key="AT-J1-CLI-010",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        safety = payload["safety"]
        self.assertTrue(safety["proposal_created"])
        self.assertFalse(safety["dry_run"])
        self.assertFalse(safety["confirmation_created"])
        self.assertFalse(safety["verifier_report_created"])
        self.assertFalse(safety["handoff_created"])
        self.assertFalse(safety["runtime_started"])
        self.assertFalse(safety["approved_task_runner_called"])
        self.assertFalse(safety["executor_started"])
        self.assertFalse(safety["validators_started"])
        self.assertFalse(safety["github_mutated"])
        self.assertFalse(safety["approved"])
        self.assertFalse(safety["merged"])
        self.assertFalse(safety["cleanup_performed"])
        self.assertFalse(safety["background_worker_started"])
        self.assertTrue(safety["not_execution_permission"])


if __name__ == "__main__":
    unittest.main()
