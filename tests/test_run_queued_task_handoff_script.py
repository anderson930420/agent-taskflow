from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agent_taskflow.intake_runner_handoff import (
    SCHEMA_VERSION as INTAKE_RUNNER_HANDOFF_SCHEMA_VERSION,
    STATUS_CREATED as INTAKE_RUNNER_HANDOFF_STATUS_CREATED,
    VERIFIER_REPORT_ARTIFACT_SCHEMA_VERSION,
)
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.task_execution_package import (
    IMPLEMENTATION_PROMPT_FILENAME,
    PACKAGE_FILENAME,
    TaskExecutionPackageRequest,
    create_task_execution_package,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_queued_task_handoff.py"


def _utc_now_iso() -> str:
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    return now.isoformat().replace("+00:00", "Z")


def _write_valid_handoff_pair(
    *,
    artifact_root: Path,
    db_path: Path,
    task_key: str,
) -> Path:
    """Write a synthetic but valid handoff + verifier_report pair on disk."""

    now = _utc_now_iso()
    verifier_run_id = "verifier-run-cli-test-0001"
    handoff_id = "handoff-cli-test-0001"
    expiration = {
        "kind": "queued_task_handoff",
        "default_max_age_minutes": 15,
        "max_age_minutes_override": None,
        "effective_max_age_minutes": 15,
        "max_age_minutes": 15,
        "max_age_source": "default",
        "confirmation_created_at": now,
        "now": now,
        "age_seconds": 0,
        "expired": False,
        "detail": None,
    }
    report = {
        "ok": True,
        "status": "valid",
        "schema_version": "scheduler_confirmation_verifier_report.v1",
        "source": "scheduler_confirmation_verifier",
        "verification_passed": True,
        "eligible_for_command_specific_confirm": True,
        "execution_allowed": False,
        "allowed_to_attempt": False,
        "execution_performed": False,
        "action_evidence_created": False,
        "task_key": task_key,
        "recommended_command_kind": "queued_task_handoff",
        "proposal_id": "proposal-id-cli",
        "proposal_hash": "proposal-hash-cli",
        "proposal_artifact_path": "/abs/conf/proposal.json",
        "proposal_item_id": "proposal-item-cli",
        "item_hash": "item-hash-cli",
        "confirmation_id": "confirmation-id-cli",
        "confirmation_artifact_path": "/abs/conf/confirmation.json",
        "confirmation_created_at": now,
        "expiration": expiration,
        "checks": [{"name": "smoke", "passed": True}],
        "safety": {
            "verifier_dry_run": True,
            "execution_allowed": False,
            "execution_performed": False,
            "action_evidence_created": False,
        },
    }
    report_path = (
        artifact_root
        / "scheduler_confirmation_verifier_reports"
        / verifier_run_id
        / "verifier_report.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "schema_version": VERIFIER_REPORT_ARTIFACT_SCHEMA_VERSION,
                "verifier_run_id": verifier_run_id,
                "created_at": now,
                "source": "intake_runner_handoff",
                "report": report,
                "safety": {
                    "dry_run_report_only": True,
                    "execution_allowed": False,
                    "execution_performed": False,
                    "action_evidence_created": False,
                    "executor_started": False,
                    "validators_started": False,
                },
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    handoff_path = (
        artifact_root
        / "intake_runner_handoffs"
        / handoff_id
        / "intake_runner_handoff.json"
    )
    handoff_payload = {
        "ok": True,
        "status": INTAKE_RUNNER_HANDOFF_STATUS_CREATED,
        "schema_version": INTAKE_RUNNER_HANDOFF_SCHEMA_VERSION,
        "handoff_id": handoff_id,
        "created_at": now,
        "source": "intake_runner_handoff",
        "mode": "confirmed",
        "db_path": str(db_path),
        "artifact_root": str(artifact_root),
        "artifact_path": str(handoff_path),
        "task_key": task_key,
        "recommended_command_kind": "queued_task_handoff",
        "proposal": {
            "proposal_id": "proposal-id-cli",
            "proposal_hash": "proposal-hash-cli",
            "proposal_artifact_path": "/abs/conf/proposal.json",
            "proposal_item_id": "proposal-item-cli",
            "item_hash": "item-hash-cli",
        },
        "confirmation": {
            "confirmation_id": "confirmation-id-cli",
            "confirmation_artifact_path": "/abs/conf/confirmation.json",
            "verification_status": "valid",
            "verification_passed": True,
            "eligible_for_command_specific_confirm": True,
        },
        "runner_contract": {
            "runner_may_start": False,
            "execution_allowed": False,
            "execution_performed": False,
            "executor_started": False,
            "validators_started": False,
            "action_evidence_created": False,
            "requires_future_runtime_gate": True,
        },
        "safety": {
            "handoff_only": True,
            "will_execute": False,
            "will_push": False,
            "will_create_pr": False,
            "will_merge": False,
            "will_approve": False,
            "will_reject": False,
            "will_cleanup": False,
            "will_delete_branch": False,
            "will_delete_worktree": False,
            "will_mutate_github": False,
            "will_mutate_db_as_action": False,
            "will_start_background_worker": False,
        },
        "verifier_report": {
            "verifier_run_id": verifier_run_id,
            "verifier_report_path": str(report_path),
            "artifact_type": "scheduler_confirmation_verifier_report",
            "schema_version": VERIFIER_REPORT_ARTIFACT_SCHEMA_VERSION,
            "persisted": True,
            "status": "valid",
            "verification_passed": True,
            "eligible_for_command_specific_confirm": True,
            "execution_allowed": False,
            "execution_performed": False,
            "action_evidence_created": False,
            "expiration": expiration,
        },
        "verifier_report_summary": {
            "schema_version": "scheduler_confirmation_verifier_report.v1",
            "status": "valid",
            "verification_passed": True,
            "eligible_for_command_specific_confirm": True,
            "execution_allowed": False,
            "execution_performed": False,
            "action_evidence_created": False,
            "failed_check_count": 0,
            "failed_check_names": [],
            "expiration": expiration,
        },
    }
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(
        json.dumps(handoff_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return handoff_path


class RunQueuedTaskHandoffScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.artifact_root = self.root / "artifacts"
        self.artifact_dir = self.artifact_root / "AT-HANDOFF-CLI-1"
        self.worktree_root = self.root / "worktrees"
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_task(self) -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-HANDOFF-CLI-1",
                project="agent-taskflow",
                board="agent-taskflow",
                title="CLI handoff test",
                status="queued",
                repo_path=self.repo,
                artifact_dir=self.artifact_dir,
            )
        )

    def _create_valid_package(self) -> None:
        create_task_execution_package(
            TaskExecutionPackageRequest(
                task_key="AT-HANDOFF-CLI-1",
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                dry_run=False,
                confirm=True,
            ),
            store=self.store,
        )

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _base_args(self) -> list[str]:
        return [
            "--task-key", "AT-HANDOFF-CLI-1",
            "--executor", "shell",
            "--repo-path", str(self.repo),
            "--db-path", str(self.db_path),
            "--artifact-root", str(self.artifact_root),
            "--worktree-root", str(self.worktree_root),
            "--base-branch", "main",
            "--validator", "pytest",
            "--skip-preflight",
        ]

    # 1. Default dry-run verifies package and writes/runs nothing.
    def test_default_dry_run_verifies_without_running(self) -> None:
        self._seed_task()
        self._create_valid_package()
        completed = self._run(self._base_args() + ["--dry-run"])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "preview")
        self.assertTrue(payload["package"]["verified"])
        self.assertFalse(payload["safety"]["approved_task_runner_started"])
        self.assertFalse(payload["safety"]["workspace_prepared"])
        self.assertFalse(payload["safety"]["executor_started"])
        # Phase C: dry-run never produces runtime audit evidence.
        self.assertIsNone(payload["runtime"])
        self.assertFalse(
            (self.artifact_dir / "runtime_handoff_executions").exists()
        )
        # The prompt and package files exist (the package writer placed them).
        self.assertTrue((self.artifact_dir / IMPLEMENTATION_PROMPT_FILENAME).exists())
        self.assertTrue((self.artifact_dir / PACKAGE_FILENAME).exists())
        # No worktree was created.
        self.assertFalse(self.worktree_root.exists())

    # 2. Missing task returns non-zero structured blocked JSON.
    def test_missing_task_returns_nonzero_blocked_json(self) -> None:
        completed = self._run(self._base_args() + ["--dry-run"])
        self.assertEqual(completed.returncode, 1, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["phase"], "selection")
        self.assertIn("Task not found", payload["error"])
        self.assertFalse(payload["safety"]["approved_task_runner_started"])

    # 3. Missing package returns non-zero structured blocked JSON.
    def test_missing_package_returns_nonzero_blocked_json(self) -> None:
        self._seed_task()
        completed = self._run(self._base_args() + ["--dry-run"])
        self.assertEqual(completed.returncode, 1, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["phase"], "package_verification")
        self.assertIn("Task execution package is missing", payload["error"])

    # 4. --dry-run and --confirm-handoff conflict.
    def test_dry_run_and_confirm_handoff_conflict(self) -> None:
        self._seed_task()
        self._create_valid_package()
        completed = self._run(self._base_args() + ["--dry-run", "--confirm-handoff"])
        self.assertNotEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["phase"], "cli")
        self.assertIn("mutually exclusive", payload["error"])
        # No worktree was created by this conflicting CLI call.
        self.assertFalse(self.worktree_root.exists())

    # 5. CLI emits well-formed JSON in dry-run path.
    def test_default_dry_run_emits_pretty_json_by_default(self) -> None:
        self._seed_task()
        self._create_valid_package()
        completed = self._run(self._base_args() + ["--dry-run"])
        self.assertEqual(completed.returncode, 0)
        # Pretty-printed by default: stdout contains newlines.
        self.assertIn("\n", completed.stdout.strip())
        # Compact mode produces single-line JSON.
        compact = self._run(self._base_args() + ["--dry-run", "--json"])
        self.assertEqual(compact.returncode, 0)
        self.assertEqual(compact.stdout.strip().count("\n"), 0)

    # 6. --confirm-handoff without --intake-runner-handoff-artifact-path
    # exits non-zero and explains the requirement.
    def test_confirm_handoff_without_handoff_path_exits_nonzero(self) -> None:
        self._seed_task()
        self._create_valid_package()
        completed = self._run(self._base_args() + ["--confirm-handoff"])
        self.assertNotEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["phase"], "cli")
        self.assertIn(
            "--intake-runner-handoff-artifact-path", payload["error"]
        )
        # No worktree was created.
        self.assertFalse(self.worktree_root.exists())
        self.assertFalse(
            payload["handoff"]["intake_runner_handoff_verified"]
        )
        # Phase C: short-circuit at CLI parse stage produces no
        # runtime execution evidence.
        self.assertIsNone(payload["runtime"])
        self.assertFalse(
            (self.artifact_dir / "runtime_handoff_executions").exists()
        )

    # 7. Default dry-run output reports the handoff path is required.
    def test_default_dry_run_reports_handoff_required(self) -> None:
        self._seed_task()
        self._create_valid_package()
        completed = self._run(self._base_args() + ["--dry-run"])
        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertTrue(
            payload["handoff"][
                "intake_runner_handoff_required_for_confirmed_execution"
            ]
        )
        self.assertIsNone(
            payload["handoff"]["intake_runner_handoff_artifact_path"]
        )
        self.assertFalse(
            payload["handoff"]["intake_runner_handoff_verified"]
        )

    # 8. Confirmed mode with a valid handoff path runs the package +
    #    handoff preflight and exits non-zero only because no real
    #    executor is configured (we are exercising CLI wiring, not
    #    the real runner).
    def test_confirm_handoff_with_valid_handoff_path_passes_preflight(
        self,
    ) -> None:
        self._seed_task()
        self._create_valid_package()
        handoff_path = _write_valid_handoff_pair(
            artifact_root=self.artifact_root,
            db_path=self.db_path,
            task_key="AT-HANDOFF-CLI-1",
        )
        completed = self._run(
            self._base_args()
            + [
                "--confirm-handoff",
                "--intake-runner-handoff-artifact-path",
                str(handoff_path),
            ]
        )
        payload = json.loads(completed.stdout)
        # The CLI passed both package + handoff preflight; whether the
        # downstream runner blocks or succeeds is not the contract this
        # test enforces. What this test guarantees is that the CLI is
        # past handoff_verification: handoff binding fields must be
        # populated and intake_runner_handoff_verified must be true.
        self.assertNotEqual(payload["phase"], "handoff_verification")
        self.assertTrue(
            payload["handoff"]["intake_runner_handoff_verified"]
        )
        self.assertEqual(
            payload["handoff"]["intake_runner_handoff_artifact_path"],
            str(handoff_path),
        )
        self.assertEqual(
            payload["handoff"]["verifier_run_id"],
            "verifier-run-cli-test-0001",
        )
        # Phase C: once the CLI reaches the runtime audit boundary in
        # confirmed mode, runtime references must be surfaced on the
        # result so the operator can locate the runtime audit
        # artifact and confirm the runtime_execution_id.
        self.assertIsNotNone(payload["runtime"])
        self.assertIsInstance(
            payload["runtime"]["runtime_execution_id"], str
        )
        self.assertTrue(
            payload["runtime"]["runtime_execution_id"].startswith(
                "runtime-execution-"
            )
        )
        artifact_path_str = payload["runtime"][
            "runtime_execution_artifact_path"
        ]
        self.assertIsNotNone(artifact_path_str)
        artifact_path = Path(artifact_path_str)
        self.assertTrue(artifact_path.exists())
        self.assertTrue(
            payload["runtime"]["runtime_preflight_event_recorded"]
        )
        self.assertTrue(payload["runtime"]["not_action_evidence"])
        self.assertTrue(payload["runtime"]["not_validation_authority"])


if __name__ == "__main__":
    unittest.main()
