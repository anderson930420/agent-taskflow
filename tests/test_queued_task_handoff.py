from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent_taskflow.intake_runner_handoff import (
    SCHEMA_VERSION as INTAKE_RUNNER_HANDOFF_SCHEMA_VERSION,
    STATUS_CREATED as INTAKE_RUNNER_HANDOFF_STATUS_CREATED,
    VERIFIER_REPORT_ARTIFACT_SCHEMA_VERSION,
)
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.queued_task_handoff import (
    APPROVED_TASK_STATUS,
    INTAKE_RUNNER_HANDOFF_RECOMMENDED_COMMAND_KIND,
    QueuedTaskHandoffRequest,
    QueuedTaskHandoffResult,
    run_queued_task_handoff,
)
from agent_taskflow.task_execution_package import (
    IMPLEMENTATION_PROMPT_FILENAME,
    PACKAGE_FILENAME,
    SCHEMA_VERSION,
    TaskExecutionPackageRequest,
    create_task_execution_package,
)


@dataclass
class FakeApprovedTaskRunnerResult:
    ok: bool
    status: str
    phase: str
    task_key: str
    executor: str
    dry_run: bool = False
    preflight: dict[str, Any] = field(default_factory=dict)
    workspace: dict[str, Any] = field(default_factory=dict)
    executor_run: dict[str, Any] = field(default_factory=dict)
    validators: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    safety: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "phase": self.phase,
            "task_key": self.task_key,
            "executor": self.executor,
            "dry_run": self.dry_run,
            "preflight": self.preflight,
            "workspace": self.workspace,
            "executor_run": self.executor_run,
            "validators": self.validators,
            "artifacts": self.artifacts,
            "summary": self.summary,
            "safety": self.safety,
            "error": self.error,
        }


class _RunnerSpy:
    def __init__(self, result: FakeApprovedTaskRunnerResult) -> None:
        self.result = result
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def __call__(self, request: Any, **kwargs: Any) -> FakeApprovedTaskRunnerResult:
        self.calls.append((request, kwargs))
        return self.result


def _waiting_approval_runner_result(task_key: str, executor: str) -> FakeApprovedTaskRunnerResult:
    return FakeApprovedTaskRunnerResult(
        ok=True,
        status=APPROVED_TASK_STATUS,
        phase=APPROVED_TASK_STATUS,
        task_key=task_key,
        executor=executor,
        safety={
            "workspace_prepared": True,
            "executor_started": True,
            "validators_started": True,
            "db_written": True,
            "artifact_written": True,
            "branch_pushed": False,
            "pr_created": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
        },
    )


def _blocked_runner_result(task_key: str, executor: str, error: str) -> FakeApprovedTaskRunnerResult:
    return FakeApprovedTaskRunnerResult(
        ok=False,
        status="blocked",
        phase="executor",
        task_key=task_key,
        executor=executor,
        error=error,
        safety={
            "workspace_prepared": True,
            "executor_started": True,
            "validators_started": False,
            "db_written": True,
            "artifact_written": True,
            "branch_pushed": False,
            "pr_created": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
        },
    )


def _utc_now_iso(offset_seconds: int = 0) -> str:
    now = datetime.now(tz=timezone.utc) + timedelta(seconds=offset_seconds)
    return now.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class _HandoffFixture:
    """Build the on-disk intake_runner_handoff + verifier report pair.

    The factory writes both artifacts under ``artifact_root`` exactly
    as the Phase A intake_runner_handoff persister would. Tests can
    pass ``handoff_overrides`` / ``verifier_overrides`` /
    ``report_overrides`` to deliberately break individual fields.
    """

    def __init__(
        self,
        *,
        artifact_root: Path,
        db_path: Path,
        task_key: str,
        confirmation_created_at: str | None = None,
        effective_max_age_minutes: int = 15,
        proposal_hash: str = "proposal-hash-abc",
        proposal_item_id: str = "proposal-item-001",
        item_hash: str = "item-hash-xyz",
        confirmation_id: str = "confirmation-id-001",
        confirmation_artifact_path: str = "/abs/conf/confirmation.json",
        proposal_artifact_path: str = "/abs/conf/proposal.json",
        proposal_id: str = "proposal-id-001",
    ) -> None:
        self.artifact_root = artifact_root
        self.db_path = db_path
        self.task_key = task_key
        self.confirmation_created_at = (
            confirmation_created_at or _utc_now_iso()
        )
        self.effective_max_age_minutes = effective_max_age_minutes
        self.proposal_hash = proposal_hash
        self.proposal_item_id = proposal_item_id
        self.item_hash = item_hash
        self.confirmation_id = confirmation_id
        self.confirmation_artifact_path = confirmation_artifact_path
        self.proposal_artifact_path = proposal_artifact_path
        self.proposal_id = proposal_id

    def _verifier_report(
        self,
        report_overrides: dict[str, Any] | None,
    ) -> dict[str, Any]:
        expiration = {
            "kind": INTAKE_RUNNER_HANDOFF_RECOMMENDED_COMMAND_KIND,
            "default_max_age_minutes": self.effective_max_age_minutes,
            "max_age_minutes_override": None,
            "effective_max_age_minutes": self.effective_max_age_minutes,
            "max_age_minutes": self.effective_max_age_minutes,
            "max_age_source": "default",
            "confirmation_created_at": self.confirmation_created_at,
            "now": self.confirmation_created_at,
            "age_seconds": 0,
            "expired": False,
            "detail": None,
        }
        report: dict[str, Any] = {
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
            "task_key": self.task_key,
            "recommended_command_kind": (
                INTAKE_RUNNER_HANDOFF_RECOMMENDED_COMMAND_KIND
            ),
            "proposal_id": self.proposal_id,
            "proposal_hash": self.proposal_hash,
            "proposal_artifact_path": self.proposal_artifact_path,
            "proposal_item_id": self.proposal_item_id,
            "item_hash": self.item_hash,
            "confirmation_id": self.confirmation_id,
            "confirmation_artifact_path": (
                self.confirmation_artifact_path
            ),
            "confirmation_created_at": self.confirmation_created_at,
            "expiration": expiration,
            "checks": [{"name": "smoke", "passed": True}],
            "safety": {
                "verifier_dry_run": True,
                "execution_allowed": False,
                "execution_performed": False,
                "action_evidence_created": False,
            },
        }
        if report_overrides:
            for key, value in report_overrides.items():
                if key == "expiration" and isinstance(value, dict):
                    report["expiration"] = {**report["expiration"], **value}
                else:
                    report[key] = value
        return report

    def write(
        self,
        *,
        handoff_overrides: dict[str, Any] | None = None,
        verifier_overrides: dict[str, Any] | None = None,
        report_overrides: dict[str, Any] | None = None,
        verifier_run_id: str = "verifier-run-test-0001",
        handoff_id: str = "handoff-test-0001",
        omit_handoff_verifier_report_path: bool = False,
        omit_handoff_verifier_run_id: bool = False,
        verifier_report_subpath: str | None = None,
        skip_report_artifact: bool = False,
    ) -> Path:
        """Write the verifier report + handoff JSON; return the handoff path."""

        report = self._verifier_report(report_overrides)
        verifier_report_path = (
            self.artifact_root
            / "scheduler_confirmation_verifier_reports"
            / verifier_run_id
            / "verifier_report.json"
        )
        if verifier_report_subpath is not None:
            verifier_report_path = self.artifact_root / verifier_report_subpath

        report_artifact = {
            "schema_version": VERIFIER_REPORT_ARTIFACT_SCHEMA_VERSION,
            "verifier_run_id": verifier_run_id,
            "created_at": self.confirmation_created_at,
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
        }
        if verifier_overrides:
            report_artifact.update(verifier_overrides)

        if not skip_report_artifact:
            verifier_report_path.parent.mkdir(parents=True, exist_ok=True)
            verifier_report_path.write_text(
                json.dumps(report_artifact, indent=2, sort_keys=True),
                encoding="utf-8",
            )

        handoff_path = (
            self.artifact_root
            / "intake_runner_handoffs"
            / handoff_id
            / "intake_runner_handoff.json"
        )

        verifier_block: dict[str, Any] = {
            "verifier_run_id": (
                None if omit_handoff_verifier_run_id else verifier_run_id
            ),
            "verifier_report_path": (
                None
                if omit_handoff_verifier_report_path
                else str(verifier_report_path)
            ),
            "artifact_type": "scheduler_confirmation_verifier_report",
            "schema_version": VERIFIER_REPORT_ARTIFACT_SCHEMA_VERSION,
            "persisted": True,
            "status": "valid",
            "verification_passed": True,
            "eligible_for_command_specific_confirm": True,
            "execution_allowed": False,
            "execution_performed": False,
            "action_evidence_created": False,
            "expiration": report["expiration"],
        }

        handoff_payload: dict[str, Any] = {
            "ok": True,
            "status": INTAKE_RUNNER_HANDOFF_STATUS_CREATED,
            "schema_version": INTAKE_RUNNER_HANDOFF_SCHEMA_VERSION,
            "handoff_id": handoff_id,
            "created_at": self.confirmation_created_at,
            "source": "intake_runner_handoff",
            "mode": "confirmed",
            "db_path": str(self.db_path),
            "artifact_root": str(self.artifact_root),
            "artifact_path": str(handoff_path),
            "task_key": self.task_key,
            "recommended_command_kind": (
                INTAKE_RUNNER_HANDOFF_RECOMMENDED_COMMAND_KIND
            ),
            "proposal": {
                "proposal_id": self.proposal_id,
                "proposal_hash": self.proposal_hash,
                "proposal_artifact_path": self.proposal_artifact_path,
                "proposal_item_id": self.proposal_item_id,
                "item_hash": self.item_hash,
            },
            "confirmation": {
                "confirmation_id": self.confirmation_id,
                "confirmation_artifact_path": (
                    self.confirmation_artifact_path
                ),
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
            "verifier_report": verifier_block,
            "verifier_report_summary": {
                "schema_version": (
                    "scheduler_confirmation_verifier_report.v1"
                ),
                "status": "valid",
                "verification_passed": True,
                "eligible_for_command_specific_confirm": True,
                "execution_allowed": False,
                "execution_performed": False,
                "action_evidence_created": False,
                "failed_check_count": 0,
                "failed_check_names": [],
                "expiration": report["expiration"],
            },
        }

        if handoff_overrides:
            for key, value in handoff_overrides.items():
                if (
                    key in {"proposal", "confirmation", "runner_contract",
                            "safety", "verifier_report"}
                    and isinstance(value, dict)
                    and isinstance(handoff_payload.get(key), dict)
                ):
                    handoff_payload[key] = {**handoff_payload[key], **value}
                else:
                    handoff_payload[key] = value

        handoff_path.parent.mkdir(parents=True, exist_ok=True)
        handoff_path.write_text(
            json.dumps(handoff_payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return handoff_path


class QueuedTaskHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.artifact_root = self.root / "artifacts"
        self.artifact_dir = self.artifact_root / "AT-HANDOFF-1"
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_task(
        self,
        *,
        task_key: str = "AT-HANDOFF-1",
        status: str = "queued",
    ) -> TaskRecord:
        task = TaskRecord(
            task_key=task_key,
            project="agent-taskflow",
            board="agent-taskflow",
            title="Handoff test task",
            status=status,
            repo_path=self.repo,
            artifact_dir=self.artifact_dir,
        )
        self.store.upsert_task(task)
        return task

    def _create_valid_package(self) -> None:
        create_task_execution_package(
            TaskExecutionPackageRequest(
                task_key="AT-HANDOFF-1",
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                dry_run=False,
                confirm=True,
            ),
            store=self.store,
        )

    def _fixture(self, **overrides: Any) -> _HandoffFixture:
        kwargs: dict[str, Any] = {
            "artifact_root": self.artifact_root,
            "db_path": self.db_path,
            "task_key": "AT-HANDOFF-1",
        }
        kwargs.update(overrides)
        return _HandoffFixture(**kwargs)

    def _request(self, **overrides: Any) -> QueuedTaskHandoffRequest:
        kwargs: dict[str, Any] = {
            "task_key": "AT-HANDOFF-1",
            "executor": "shell",
            "repo_path": self.repo,
            "db_path": self.db_path,
            "artifact_root": self.artifact_root,
            "worktree_root": self.root / "worktrees",
            "base_branch": "main",
            "validators": ("pytest",),
            "command": ("echo", "noop"),
            "preflight": False,
            "dry_run": True,
            "confirm_handoff": False,
        }
        kwargs.update(overrides)
        return QueuedTaskHandoffRequest(**kwargs)

    # 1. Blocks when task does not exist.
    def test_blocks_when_task_missing(self) -> None:
        result = run_queued_task_handoff(self._request())
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.phase, "selection")
        self.assertIn("Task not found", result.error or "")

    # 2. Blocks when task status is not queued.
    def test_blocks_when_status_not_queued(self) -> None:
        self._seed_task(status="waiting_approval")
        self._create_valid_package()
        self.store.update_task_status(
            "AT-HANDOFF-1",
            "waiting_approval",
            source="test",
        )
        result = run_queued_task_handoff(self._request())
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.phase, "selection")
        self.assertIn("status=", result.error or "")

    # 3. Blocks when task_execution_package.json is missing.
    def test_blocks_when_package_missing(self) -> None:
        self._seed_task()
        result = run_queued_task_handoff(self._request())
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.phase, "package_verification")
        self.assertIn("Task execution package is missing", result.error or "")
        self.assertFalse(result.safety["package_verified"])

    # 4. Blocks when implementation_prompt.md is missing.
    def test_blocks_when_prompt_missing(self) -> None:
        self._seed_task()
        self._create_valid_package()
        (self.artifact_dir / IMPLEMENTATION_PROMPT_FILENAME).unlink()
        result = run_queued_task_handoff(self._request())
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "package_verification")
        self.assertIn("Implementation prompt is missing", result.error or "")

    # 5. Blocks when package JSON is invalid.
    def test_blocks_when_package_json_invalid(self) -> None:
        self._seed_task()
        self._create_valid_package()
        (self.artifact_dir / PACKAGE_FILENAME).write_text("{not json", encoding="utf-8")
        result = run_queued_task_handoff(self._request())
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "package_verification")
        self.assertIn("not valid JSON", result.error or "")

    # 6. Blocks when package schema_version is wrong.
    def test_blocks_when_schema_version_wrong(self) -> None:
        self._seed_task()
        self._create_valid_package()
        package_path = self.artifact_dir / PACKAGE_FILENAME
        data = json.loads(package_path.read_text(encoding="utf-8"))
        data["schema_version"] = "task_execution_package.vBOGUS"
        package_path.write_text(json.dumps(data), encoding="utf-8")
        result = run_queued_task_handoff(self._request())
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "package_verification")
        self.assertIn("schema_version", result.error or "")
        self.assertIn(SCHEMA_VERSION, result.error or "")

    # 7. Blocks when package task_key mismatches.
    def test_blocks_when_task_key_mismatch(self) -> None:
        self._seed_task()
        self._create_valid_package()
        package_path = self.artifact_dir / PACKAGE_FILENAME
        data = json.loads(package_path.read_text(encoding="utf-8"))
        data["task_key"] = "AT-WRONG-1"
        package_path.write_text(json.dumps(data), encoding="utf-8")
        result = run_queued_task_handoff(self._request())
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "package_verification")
        self.assertIn("task_key", result.error or "")

    # 8. Dry-run without handoff path verifies package and surfaces missing binding.
    def test_dry_run_without_handoff_path_verifies_and_flags_required(self) -> None:
        self._seed_task()
        self._create_valid_package()
        spy = _RunnerSpy(_waiting_approval_runner_result("AT-HANDOFF-1", "shell"))
        result = run_queued_task_handoff(
            self._request(),
            approved_task_runner=spy,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "preview")
        self.assertEqual(result.phase, "preview")
        self.assertTrue(result.package["verified"])
        self.assertEqual(result.package["schema_version"], SCHEMA_VERSION)
        self.assertEqual(spy.calls, [])
        self.assertFalse(result.safety["approved_task_runner_started"])
        self.assertFalse(result.safety["handoff_confirmed"])
        self.assertFalse(result.safety["workspace_prepared"])
        self.assertFalse(result.safety["executor_started"])
        self.assertIsNone(result.runner_result)
        self.assertTrue(
            result.handoff[
                "intake_runner_handoff_required_for_confirmed_execution"
            ]
        )
        self.assertIsNone(
            result.handoff["intake_runner_handoff_artifact_path"]
        )
        self.assertFalse(
            result.handoff["intake_runner_handoff_verified"]
        )
        self.assertIsNone(result.handoff["verifier_run_id"])
        self.assertIsNone(result.handoff["verifier_report_path"])

    # 9. Dry-run with valid handoff path runs preflight and reports verified binding.
    def test_dry_run_with_valid_handoff_reports_verified(self) -> None:
        self._seed_task()
        self._create_valid_package()
        fixture = self._fixture()
        handoff_path = fixture.write()
        spy = _RunnerSpy(_waiting_approval_runner_result("AT-HANDOFF-1", "shell"))
        result = run_queued_task_handoff(
            self._request(
                intake_runner_handoff_artifact_path=handoff_path,
            ),
            approved_task_runner=spy,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "preview")
        self.assertEqual(spy.calls, [])
        self.assertTrue(result.handoff["intake_runner_handoff_verified"])
        self.assertEqual(
            result.handoff["intake_runner_handoff_artifact_path"],
            str(handoff_path),
        )
        self.assertEqual(
            result.handoff["verifier_run_id"], "verifier-run-test-0001"
        )
        self.assertTrue(result.handoff["expiration_still_valid"])

    # 10. Confirmed mode without handoff path raises at request build.
    def test_request_rejects_confirmed_without_handoff_path(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "intake_runner_handoff_artifact_path"
        ):
            self._request(dry_run=False, confirm_handoff=True)

    # 11. Confirmed mode with non-existent handoff file returns blocked.
    def test_confirmed_mode_blocks_when_handoff_file_missing(self) -> None:
        self._seed_task()
        self._create_valid_package()
        bogus = self.artifact_root / "missing_handoff.json"
        spy = _RunnerSpy(_waiting_approval_runner_result("AT-HANDOFF-1", "shell"))
        result = run_queued_task_handoff(
            self._request(
                dry_run=False,
                confirm_handoff=True,
                intake_runner_handoff_artifact_path=bogus,
            ),
            approved_task_runner=spy,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.phase, "handoff_verification")
        self.assertIn("does not exist", result.error or "")
        self.assertEqual(spy.calls, [])
        self.assertFalse(
            result.handoff["intake_runner_handoff_verified"]
        )

    # 12. Confirmed mode blocks on invalid JSON handoff artifact.
    def test_confirmed_mode_blocks_on_invalid_json_handoff(self) -> None:
        self._seed_task()
        self._create_valid_package()
        bad = self.artifact_root / "bad_handoff.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("{not json", encoding="utf-8")
        spy = _RunnerSpy(_waiting_approval_runner_result("AT-HANDOFF-1", "shell"))
        result = run_queued_task_handoff(
            self._request(
                dry_run=False,
                confirm_handoff=True,
                intake_runner_handoff_artifact_path=bad,
            ),
            approved_task_runner=spy,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "handoff_verification")
        self.assertIn("not valid JSON", result.error or "")
        self.assertEqual(spy.calls, [])

    # 13. Confirmed mode blocks when handoff schema_version is wrong.
    def test_confirmed_mode_blocks_on_wrong_schema_version(self) -> None:
        self._seed_task()
        self._create_valid_package()
        fixture = self._fixture()
        handoff_path = fixture.write(
            handoff_overrides={"schema_version": "intake_runner_handoff.vBOGUS"}
        )
        spy = _RunnerSpy(_waiting_approval_runner_result("AT-HANDOFF-1", "shell"))
        result = run_queued_task_handoff(
            self._request(
                dry_run=False,
                confirm_handoff=True,
                intake_runner_handoff_artifact_path=handoff_path,
            ),
            approved_task_runner=spy,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "handoff_verification")
        self.assertIn("schema_version", result.error or "")
        self.assertEqual(spy.calls, [])

    # 14. Confirmed mode blocks when handoff task_key mismatches.
    def test_confirmed_mode_blocks_on_handoff_task_key_mismatch(self) -> None:
        self._seed_task()
        self._create_valid_package()
        fixture = self._fixture()
        handoff_path = fixture.write(
            handoff_overrides={"task_key": "AT-OTHER-TASK"}
        )
        spy = _RunnerSpy(_waiting_approval_runner_result("AT-HANDOFF-1", "shell"))
        result = run_queued_task_handoff(
            self._request(
                dry_run=False,
                confirm_handoff=True,
                intake_runner_handoff_artifact_path=handoff_path,
            ),
            approved_task_runner=spy,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "handoff_verification")
        self.assertIn("task_key", result.error or "")
        self.assertEqual(spy.calls, [])

    # 15. Confirmed mode blocks on recommended_command_kind mismatch.
    def test_confirmed_mode_blocks_on_wrong_command_kind(self) -> None:
        self._seed_task()
        self._create_valid_package()
        fixture = self._fixture()
        handoff_path = fixture.write(
            handoff_overrides={"recommended_command_kind": "branch_push_review"}
        )
        spy = _RunnerSpy(_waiting_approval_runner_result("AT-HANDOFF-1", "shell"))
        result = run_queued_task_handoff(
            self._request(
                dry_run=False,
                confirm_handoff=True,
                intake_runner_handoff_artifact_path=handoff_path,
            ),
            approved_task_runner=spy,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "handoff_verification")
        self.assertIn("recommended_command_kind", result.error or "")
        self.assertEqual(spy.calls, [])

    # 16. Confirmed mode blocks when handoff lacks verifier_report_path.
    def test_confirmed_mode_blocks_when_verifier_report_path_missing(self) -> None:
        self._seed_task()
        self._create_valid_package()
        fixture = self._fixture()
        handoff_path = fixture.write(omit_handoff_verifier_report_path=True)
        spy = _RunnerSpy(_waiting_approval_runner_result("AT-HANDOFF-1", "shell"))
        result = run_queued_task_handoff(
            self._request(
                dry_run=False,
                confirm_handoff=True,
                intake_runner_handoff_artifact_path=handoff_path,
            ),
            approved_task_runner=spy,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "handoff_verification")
        self.assertIn("verifier_report.verifier_report_path", result.error or "")
        self.assertEqual(spy.calls, [])

    # 17. Confirmed mode blocks when the verifier report artifact is gone.
    def test_confirmed_mode_blocks_when_verifier_report_artifact_missing(
        self,
    ) -> None:
        self._seed_task()
        self._create_valid_package()
        fixture = self._fixture()
        handoff_path = fixture.write()
        # Delete the verifier report artifact written by write().
        verifier_report_path = Path(
            json.loads(handoff_path.read_text())["verifier_report"][
                "verifier_report_path"
            ]
        )
        verifier_report_path.unlink()
        spy = _RunnerSpy(_waiting_approval_runner_result("AT-HANDOFF-1", "shell"))
        result = run_queued_task_handoff(
            self._request(
                dry_run=False,
                confirm_handoff=True,
                intake_runner_handoff_artifact_path=handoff_path,
            ),
            approved_task_runner=spy,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "handoff_verification")
        self.assertIn(
            "verifier report artifact does not exist", result.error or ""
        )
        self.assertEqual(spy.calls, [])

    # 18. Confirmed mode blocks when verifier report status != valid.
    def test_confirmed_mode_blocks_when_verifier_status_not_valid(self) -> None:
        self._seed_task()
        self._create_valid_package()
        fixture = self._fixture()
        handoff_path = fixture.write(
            report_overrides={"status": "invalid"}
        )
        spy = _RunnerSpy(_waiting_approval_runner_result("AT-HANDOFF-1", "shell"))
        result = run_queued_task_handoff(
            self._request(
                dry_run=False,
                confirm_handoff=True,
                intake_runner_handoff_artifact_path=handoff_path,
            ),
            approved_task_runner=spy,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "handoff_verification")
        self.assertIn("verifier report status", result.error or "")
        self.assertEqual(spy.calls, [])

    # 19. Confirmed mode blocks when verifier report claims execution_allowed.
    def test_confirmed_mode_blocks_when_verifier_execution_allowed_true(
        self,
    ) -> None:
        self._seed_task()
        self._create_valid_package()
        fixture = self._fixture()
        handoff_path = fixture.write(
            report_overrides={"execution_allowed": True}
        )
        spy = _RunnerSpy(_waiting_approval_runner_result("AT-HANDOFF-1", "shell"))
        result = run_queued_task_handoff(
            self._request(
                dry_run=False,
                confirm_handoff=True,
                intake_runner_handoff_artifact_path=handoff_path,
            ),
            approved_task_runner=spy,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "handoff_verification")
        self.assertIn("execution_allowed", result.error or "")
        self.assertEqual(spy.calls, [])

    # 20. Confirmed mode blocks on proposal_hash mismatch between artifacts.
    def test_confirmed_mode_blocks_on_proposal_hash_mismatch(self) -> None:
        self._seed_task()
        self._create_valid_package()
        fixture = self._fixture()
        handoff_path = fixture.write(
            report_overrides={"proposal_hash": "DIFFERENT-PROPOSAL-HASH"}
        )
        spy = _RunnerSpy(_waiting_approval_runner_result("AT-HANDOFF-1", "shell"))
        result = run_queued_task_handoff(
            self._request(
                dry_run=False,
                confirm_handoff=True,
                intake_runner_handoff_artifact_path=handoff_path,
            ),
            approved_task_runner=spy,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "handoff_verification")
        self.assertIn("proposal_hash", result.error or "")
        self.assertEqual(spy.calls, [])

    # 21. Confirmed mode blocks on item_hash mismatch between artifacts.
    def test_confirmed_mode_blocks_on_item_hash_mismatch(self) -> None:
        self._seed_task()
        self._create_valid_package()
        fixture = self._fixture()
        handoff_path = fixture.write(
            report_overrides={"item_hash": "DIFFERENT-ITEM-HASH"}
        )
        spy = _RunnerSpy(_waiting_approval_runner_result("AT-HANDOFF-1", "shell"))
        result = run_queued_task_handoff(
            self._request(
                dry_run=False,
                confirm_handoff=True,
                intake_runner_handoff_artifact_path=handoff_path,
            ),
            approved_task_runner=spy,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "handoff_verification")
        self.assertIn("item_hash", result.error or "")
        self.assertEqual(spy.calls, [])

    # 22. Confirmed mode blocks when the TTL has expired at execution time.
    def test_confirmed_mode_blocks_on_expired_confirmation_ttl(self) -> None:
        self._seed_task()
        self._create_valid_package()
        # Confirmation created 30 minutes ago; effective TTL is 15 min.
        old = _utc_now_iso(offset_seconds=-30 * 60)
        fixture = self._fixture(
            confirmation_created_at=old,
            effective_max_age_minutes=15,
        )
        handoff_path = fixture.write()
        spy = _RunnerSpy(_waiting_approval_runner_result("AT-HANDOFF-1", "shell"))
        result = run_queued_task_handoff(
            self._request(
                dry_run=False,
                confirm_handoff=True,
                intake_runner_handoff_artifact_path=handoff_path,
            ),
            approved_task_runner=spy,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "handoff_verification")
        self.assertIn("expiration", result.error or "")
        self.assertEqual(spy.calls, [])
        self.assertFalse(
            result.handoff["expiration_still_valid"]
        )

    # 23. Confirmed mode with valid handoff + verifier report calls the runner once.
    def test_confirmed_mode_with_valid_binding_calls_runner_once(self) -> None:
        self._seed_task()
        self._create_valid_package()
        fixture = self._fixture()
        handoff_path = fixture.write()
        spy = _RunnerSpy(_waiting_approval_runner_result("AT-HANDOFF-1", "shell"))
        result = run_queued_task_handoff(
            self._request(
                dry_run=False,
                confirm_handoff=True,
                intake_runner_handoff_artifact_path=handoff_path,
            ),
            approved_task_runner=spy,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, APPROVED_TASK_STATUS)
        self.assertEqual(len(spy.calls), 1)
        request_arg, kwargs = spy.calls[0]
        self.assertTrue(request_arg.confirm_approved_task)
        self.assertFalse(request_arg.dry_run)
        self.assertIn("store", kwargs)
        self.assertEqual(kwargs["store"].db_path, self.store.db_path)
        self.assertTrue(result.handoff["intake_runner_handoff_verified"])
        self.assertEqual(
            result.handoff["verifier_run_id"], "verifier-run-test-0001"
        )
        self.assertIsNotNone(result.handoff["verifier_report_path"])
        self.assertTrue(result.handoff["expiration_still_valid"])
        self.assertTrue(result.handoff["approved_task_runner_invoked"])
        self.assertTrue(result.safety["approved_task_runner_started"])
        self.assertFalse(result.safety["branch_pushed"])
        self.assertFalse(result.safety["pr_created"])
        self.assertFalse(result.safety["merged"])
        self.assertFalse(result.safety["approved"])
        self.assertFalse(result.safety["cleanup_performed"])
        self.assertFalse(result.safety["background_worker_started"])

    # 24. Runner error is surfaced and binding fields preserved on result.
    def test_confirmed_mode_runner_error_preserves_binding_fields(self) -> None:
        self._seed_task()
        self._create_valid_package()
        fixture = self._fixture()
        handoff_path = fixture.write()
        runner_result = _blocked_runner_result(
            "AT-HANDOFF-1",
            "shell",
            "Executor shell raised RuntimeError",
        )
        spy = _RunnerSpy(runner_result)
        result = run_queued_task_handoff(
            self._request(
                dry_run=False,
                confirm_handoff=True,
                intake_runner_handoff_artifact_path=handoff_path,
            ),
            approved_task_runner=spy,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.phase, "runner")
        self.assertIn("RuntimeError", result.error or "")
        self.assertTrue(result.handoff["intake_runner_handoff_verified"])
        self.assertEqual(
            result.handoff["verifier_run_id"], "verifier-run-test-0001"
        )

    # 25. Request rejects dry_run=True with confirm_handoff=True.
    def test_request_rejects_dry_run_with_confirm(self) -> None:
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            QueuedTaskHandoffRequest(
                task_key="AT-HANDOFF-1",
                executor="shell",
                repo_path=self.repo,
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                dry_run=True,
                confirm_handoff=True,
            )

    # 26. Request rejects dry_run=False with confirm_handoff=False.
    def test_request_rejects_non_dry_run_without_confirm(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires confirm_handoff=True"):
            QueuedTaskHandoffRequest(
                task_key="AT-HANDOFF-1",
                executor="shell",
                repo_path=self.repo,
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                dry_run=False,
                confirm_handoff=False,
            )

    # 27. Relative intake_runner_handoff_artifact_path is rejected.
    def test_request_rejects_relative_handoff_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute path"):
            QueuedTaskHandoffRequest(
                task_key="AT-HANDOFF-1",
                executor="shell",
                repo_path=self.repo,
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                dry_run=True,
                confirm_handoff=False,
                intake_runner_handoff_artifact_path=Path("not/absolute.json"),
            )


class QueuedTaskHandoffResultTests(unittest.TestCase):
    def test_result_to_dict_is_serializable(self) -> None:
        result = QueuedTaskHandoffResult(
            ok=True,
            status="preview",
            phase="preview",
            task_key="AT-HANDOFF-1",
            executor="shell",
            dry_run=True,
            package={"verified": True},
            handoff={"confirmed": False, "approved_task_runner_invoked": False},
            runner_result=None,
            safety={"read_only": True},
            error=None,
        )
        payload = result.to_dict()
        # Must JSON round-trip cleanly
        json.dumps(payload)


if __name__ == "__main__":
    unittest.main()
