"""Tests for the Level 6A runtime_handoff_execution_from_handoff module."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "run_minimal_runtime_handoff_execution_smoke.py"

from agent_taskflow.runtime_handoff_execution_from_handoff import (
    RUNTIME_EXECUTION_ARTIFACT_TYPE,
    RUNTIME_FINISHED_EVENT_TYPE,
    RUNTIME_PREFLIGHT_EVENT_TYPE,
    RUNTIME_STARTED_EVENT_TYPE,
    RuntimeHandoffExecutionError,
    RuntimeHandoffExecutionRequest,
    check_runtime_handoff_preflight,
    run_runtime_handoff_execution_from_handoff,
)
from agent_taskflow.store import TaskMirrorStore


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "run_minimal_runtime_handoff_execution_smoke",
        SMOKE_SCRIPT,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _seed_to_handoff(workspace_root: Path, task_key: str) -> dict[str, Any]:
    """Seed a fully populated chain up through Level 5A handoff."""

    from agent_taskflow.intake_runner_handoff_from_verifier_report import (
        IntakeRunnerHandoffFromVerifierReportRequest,
        create_intake_runner_handoff_from_verifier_report,
    )
    from agent_taskflow.models import TaskRecord
    from agent_taskflow.scheduler_candidate_proposals import (
        SchedulerCandidateProposalRequest,
        create_scheduler_proposal_from_candidate,
    )
    from agent_taskflow.scheduler_confirmation_from_proposal import (
        SchedulerConfirmationFromProposalRequest,
        create_scheduler_confirmation_from_proposal,
    )
    from agent_taskflow.scheduler_confirmation_verifier_report import (
        SchedulerConfirmationVerifierReportRequest,
        create_scheduler_confirmation_verifier_report,
    )

    db_path = workspace_root / "state.db"
    repo_path = workspace_root / "repo"
    artifact_root = workspace_root / "artifacts"
    repo_path.mkdir(parents=True, exist_ok=True)
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_dir = artifact_root / task_key
    artifact_dir.mkdir(parents=True, exist_ok=True)

    store = TaskMirrorStore(db_path)
    store.init_db()
    store.upsert_task(
        TaskRecord(
            task_key=task_key,
            project="agent-taskflow",
            board="agent-taskflow",
            title="L6A test",
            status="queued",
            repo_path=repo_path,
            artifact_dir=artifact_dir,
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-01T00:00:00Z",
        )
    )

    proposal = create_scheduler_proposal_from_candidate(
        SchedulerCandidateProposalRequest(
            task_key=task_key,
            db_path=db_path,
            artifact_root=artifact_root,
            dry_run=False,
            confirm_create_proposal=True,
            expected_status="queued",
            expected_recommended_command_kind="create_task_execution_package",
        )
    )["proposal"]
    proposal_path = Path(str(proposal["proposal_artifact_path"]))

    confirmation = create_scheduler_confirmation_from_proposal(
        SchedulerConfirmationFromProposalRequest(
            db_path=db_path,
            artifact_root=artifact_root,
            task_key=task_key,
            proposal_item_id=proposal["proposal_item_id"],
            proposal_hash=proposal["proposal_hash"],
            proposal_id=proposal["proposal_id"],
            item_hash=proposal["item_hash"],
            recommended_command_kind=proposal["recommended_command_kind"],
            expected_status="queued",
            proposal_artifact_path=proposal_path,
            dry_run=False,
            confirm_create_confirmation=True,
        )
    )["confirmation"]
    confirmation_path = Path(str(confirmation["artifact_path"]))

    verifier_report = create_scheduler_confirmation_verifier_report(
        SchedulerConfirmationVerifierReportRequest(
            db_path=db_path,
            artifact_root=artifact_root,
            task_key=task_key,
            confirmation_id=confirmation["confirmation_id"],
            proposal_hash=confirmation["proposal_hash"],
            proposal_item_id=confirmation["proposal_item_id"],
            item_hash=confirmation["item_hash"],
            recommended_command_kind=confirmation["recommended_command_kind"],
            confirmation_artifact_path=confirmation_path,
            dry_run=False,
            confirm_create_verifier_report=True,
        )
    )["verifier_report"]
    report_path = Path(str(verifier_report["artifact_path"]))

    handoff = create_intake_runner_handoff_from_verifier_report(
        IntakeRunnerHandoffFromVerifierReportRequest(
            db_path=db_path,
            artifact_root=artifact_root,
            task_key=task_key,
            verifier_report_id=verifier_report["verifier_report_id"],
            confirmation_id=verifier_report["confirmation_id"],
            proposal_hash=verifier_report["proposal_hash"],
            proposal_item_id=verifier_report["proposal_item_id"],
            item_hash=verifier_report["item_hash"],
            recommended_command_kind=verifier_report["recommended_command_kind"],
            verifier_report_artifact_path=report_path,
            dry_run=False,
            confirm_create_handoff=True,
        )
    )["handoff"]
    handoff_path = Path(str(handoff["artifact_path"]))

    return {
        "db_path": db_path,
        "artifact_root": artifact_root,
        "task_key": task_key,
        "store": store,
        "handoff": handoff,
        "handoff_path": handoff_path,
    }


def _make_request(seeded: dict[str, Any], **overrides: Any) -> RuntimeHandoffExecutionRequest:
    handoff = seeded["handoff"]
    fields = {
        "db_path": seeded["db_path"],
        "artifact_root": seeded["artifact_root"],
        "task_key": seeded["task_key"],
        "handoff_id": handoff["handoff_id"],
        "verifier_report_id": handoff["verifier_report_id"],
        "confirmation_id": handoff["confirmation_id"],
        "proposal_hash": handoff["proposal_hash"],
        "proposal_item_id": handoff["proposal_item_id"],
        "item_hash": handoff["item_hash"],
        "recommended_command_kind": handoff["recommended_command_kind"],
        "handoff_artifact_path": seeded["handoff_path"],
    }
    fields.update(overrides)
    return RuntimeHandoffExecutionRequest(**fields)


class _FakeRunner:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "ok": True,
            "status": "completed",
            "phase": "fake",
            "summary": "ok",
            "artifacts": {},
            "safety": {
                "executor_started": False,
                "validators_started": False,
                "github_mutated": False,
            },
        }


class RuntimeHandoffExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        self.seeded = _seed_to_handoff(self.workspace, "AT-L6A-CORE-TEST")

    def test_preflight_valid_handoff_passes_and_is_read_only(self) -> None:
        events_before = len(self.seeded["store"].list_task_events(self.seeded["task_key"]))
        artifacts_before = len(self.seeded["store"].list_task_artifacts(self.seeded["task_key"]))
        preflight = check_runtime_handoff_preflight(_make_request(self.seeded))
        self.assertTrue(preflight["preflight_passed"], preflight)
        self.assertEqual(preflight["reasons"], [])
        self.assertEqual(
            events_before,
            len(self.seeded["store"].list_task_events(self.seeded["task_key"])),
        )
        self.assertEqual(
            artifacts_before,
            len(self.seeded["store"].list_task_artifacts(self.seeded["task_key"])),
        )

    def test_dry_run_writes_nothing_and_does_not_call_runner(self) -> None:
        events_before = len(self.seeded["store"].list_task_events(self.seeded["task_key"]))
        artifacts_before = len(self.seeded["store"].list_task_artifacts(self.seeded["task_key"]))
        fake = _FakeRunner()
        result = run_runtime_handoff_execution_from_handoff(
            _make_request(self.seeded, dry_run=True),
            approved_task_runner_fn=fake,
        )
        self.assertEqual(result["status"], "dry_run")
        self.assertTrue(result["ok"])
        self.assertEqual(fake.calls, 0)
        self.assertEqual(
            events_before,
            len(self.seeded["store"].list_task_events(self.seeded["task_key"])),
        )
        self.assertEqual(
            artifacts_before,
            len(self.seeded["store"].list_task_artifacts(self.seeded["task_key"])),
        )

    def test_confirmed_mode_requires_confirmation_flag(self) -> None:
        with self.assertRaises(RuntimeHandoffExecutionError):
            run_runtime_handoff_execution_from_handoff(
                _make_request(
                    self.seeded,
                    dry_run=False,
                    confirm_run_approved_task_runner=False,
                ),
                approved_task_runner_fn=_FakeRunner(),
            )

    def test_confirmed_run_records_three_runtime_audit_events_and_artifact(self) -> None:
        fake = _FakeRunner()
        result = run_runtime_handoff_execution_from_handoff(
            _make_request(
                self.seeded,
                dry_run=False,
                confirm_run_approved_task_runner=True,
            ),
            approved_task_runner_fn=fake,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(fake.calls, 1)
        store = self.seeded["store"]
        kinds = [
            event.event_type
            for event in store.list_task_events(self.seeded["task_key"])
            if event.event_type
            in (
                RUNTIME_PREFLIGHT_EVENT_TYPE,
                RUNTIME_STARTED_EVENT_TYPE,
                RUNTIME_FINISHED_EVENT_TYPE,
            )
        ]
        self.assertEqual(
            kinds,
            [
                RUNTIME_PREFLIGHT_EVENT_TYPE,
                RUNTIME_STARTED_EVENT_TYPE,
                RUNTIME_FINISHED_EVENT_TYPE,
            ],
        )
        runtime_artifacts = [
            artifact
            for artifact in store.list_task_artifacts(self.seeded["task_key"])
            if artifact.artifact_type == RUNTIME_EXECUTION_ARTIFACT_TYPE
        ]
        self.assertEqual(len(runtime_artifacts), 1)
        payload = json.loads(Path(runtime_artifacts[0].path).read_text())
        self.assertTrue(payload["approved_task_runner_called"])
        self.assertTrue(payload["runner_returned"])
        self.assertTrue(payload["runner_ok"])
        self.assertTrue(payload["safety"]["runtime_started"])
        self.assertFalse(payload["safety"]["approved"])
        self.assertFalse(payload["safety"]["merged"])

    def test_preflight_failure_does_not_call_runner(self) -> None:
        fake = _FakeRunner()
        result = run_runtime_handoff_execution_from_handoff(
            _make_request(
                self.seeded,
                handoff_id="handoff-nonexistent",
                dry_run=False,
                confirm_run_approved_task_runner=True,
            ),
            approved_task_runner_fn=fake,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "preflight_failed")
        self.assertEqual(fake.calls, 0)

    def test_duplicate_runtime_execution_blocks_second_run(self) -> None:
        fake = _FakeRunner()
        run_runtime_handoff_execution_from_handoff(
            _make_request(
                self.seeded,
                dry_run=False,
                confirm_run_approved_task_runner=True,
            ),
            approved_task_runner_fn=fake,
        )
        result = run_runtime_handoff_execution_from_handoff(
            _make_request(
                self.seeded,
                dry_run=False,
                confirm_run_approved_task_runner=True,
            ),
            approved_task_runner_fn=fake,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "preflight_failed")
        self.assertIn("duplicate_runtime_execution", result["reasons"])
        self.assertEqual(fake.calls, 1)

    def test_runner_non_ok_still_records_finished_event(self) -> None:
        def runner(**kwargs: Any) -> dict[str, Any]:
            return {"ok": False, "status": "failed", "phase": "fake", "error": "bad"}

        result = run_runtime_handoff_execution_from_handoff(
            _make_request(
                self.seeded,
                dry_run=False,
                confirm_run_approved_task_runner=True,
            ),
            approved_task_runner_fn=runner,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "executed_with_failure")
        store = self.seeded["store"]
        finished = [
            event
            for event in store.list_task_events(self.seeded["task_key"])
            if event.event_type == RUNTIME_FINISHED_EVENT_TYPE
        ]
        self.assertEqual(len(finished), 1)

    def test_source_does_not_import_executor_or_validator_modules(self) -> None:
        source = Path(REPO_ROOT / "agent_taskflow/runtime_handoff_execution_from_handoff.py").read_text()
        for forbidden in (
            "from agent_taskflow.executors",
            "from agent_taskflow.validators",
            "from agent_taskflow.dispatcher",
            "from agent_taskflow.branch_push",
            "from agent_taskflow.draft_pr",
            "import subprocess",
        ):
            self.assertNotIn(forbidden, source, f"unexpected import: {forbidden}")


if __name__ == "__main__":
    unittest.main()
