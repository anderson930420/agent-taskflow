"""Regression tests for the P5-d opt-in path after a successful legacy run.

The P5-d opt-in path runs *after* the legacy scheduler automation has already
completed and released the scheduler lock. The default engine facade is the
real ``ApprovedTaskRunnerExecutionEngineAdapter``, whose underlying
``run_approved_task`` requires the task to still be ``queued``. When the legacy
automation succeeded, the one selected task has already moved out of ``queued``
(typically to ``waiting_approval``), so the post-legacy opt-in invocation for
that same task cannot produce a clean, migration-usable engine candidate.

These tests pin that real adapter / status-gate behavior:

* the default adapter path blocks (it never forwards a confirmation, and the
  task is no longer queued), so the engine evidence block is non-clean;
* the P5-e fallback assessment classifies the candidate as not usable for
  future migration; and
* the legacy tick ``ok`` / ``status`` are preserved untouched.

This is expected P5-d behavior, documented in
``docs/scheduler-execution-engine-opt-in-path.md``: the current post-legacy
opt-in path is evidence-only and is not a live rollout-ready migration path.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from agent_taskflow import execution_engine_approved_task_adapter as adapter_module
from agent_taskflow.approved_task_runner import (
    ApprovedTaskRunRequest,
    run_approved_task,
)
from agent_taskflow.models import TaskRecord
from agent_taskflow.scheduler_execution_engine_opt_in import (
    route_scheduler_tick_through_execution_engine,
)
from agent_taskflow.store import TaskMirrorStore


TASK_KEY = "AT-GH-909"


def scheduler_request(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "repo": "anderson930420/agent-taskflow",
        "local_repo_path": Path("/tmp/agent-taskflow-p5d-post-legacy/repo"),
        "artifact_root": Path("/tmp/agent-taskflow-p5d-post-legacy/artifacts"),
        # ``None`` mirrors a tick without explicit runner config; the opt-in
        # request builder falls back to the safe ``noop`` executor.
        "executor": None,
        "model": None,
        "provider": None,
        "tools": None,
        "pi_bin": None,
        "validators": ("pytest", "policy"),
        "worktree_root": None,
        "approved_task_preflight": False,
        "operator": "codex",
        "operator_note": "p5-d post-legacy regression test",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def legacy_success_payload(**overrides: Any) -> dict[str, Any]:
    """A legacy tick payload after the confirmed automation succeeded.

    By this point the legacy run has already executed the one selected task,
    moved it out of ``queued`` (to ``waiting_approval``), and released the
    scheduler lock.
    """

    payload: dict[str, Any] = {
        "ok": True,
        "status": "execution_completed",
        "mode": "confirmed",
        "repo": "anderson930420/agent-taskflow",
        "selected_task_key": TASK_KEY,
        "publication_config": {
            "publish_after_execution": False,
            "mode": "execution_only",
        },
        "automation": {"selected_issue": {"number": 909}},
        "safety": {
            "one_task_only": True,
            "scheduler_loop_started": False,
            "background_worker_started": False,
            "multi_task_batch_started": False,
            "github_mutated": False,
            "approved": False,
            "merged": False,
        },
    }
    payload.update(overrides)
    return payload


def assert_non_clean_evidence_only(
    test: unittest.TestCase,
    block: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    """Shared invariants: non-clean engine candidate, legacy untouched."""

    test.assertTrue(block["executed"])
    test.assertFalse(block["ok"])
    test.assertEqual(block["status"], "blocked")
    test.assertEqual(block["engine"], "ApprovedTaskRunnerExecutionEngineAdapter")
    test.assertEqual(block["engine_invocation_count"], 1)

    assessment = block["fallback_assessment"]
    test.assertIs(assessment["fallback_required"], True)
    test.assertIn("engine_not_ok", assessment["fallback_reasons"])
    test.assertIn(
        "engine_failure_status:blocked", assessment["fallback_reasons"]
    )
    test.assertIs(
        assessment["engine_candidate_usable_for_future_migration"], False
    )

    # The engine result is never authority and the legacy decision stands.
    test.assertEqual(block["effective_authority"], "legacy_scheduler")
    test.assertIs(block["engine_authority"], False)
    test.assertIs(block["engine_result_accepted_as_authority"], False)
    test.assertIs(payload["ok"], True)
    test.assertEqual(payload["status"], "execution_completed")
    test.assertIs(assessment["summary"]["legacy_ok"], True)
    test.assertEqual(
        assessment["summary"]["legacy_status"], "execution_completed"
    )

    # Still evidence-only: no publication or destructive side effects.
    safety = block["safety"]
    for marker in (
        "approval_authority",
        "approved",
        "merged",
        "branch_pushed",
        "draft_pr_created",
        "cleanup_performed",
        "branch_deleted",
        "worktree_deleted",
    ):
        test.assertFalse(safety[marker], msg=marker)
    json.dumps(block)  # must not raise


class DefaultAdapterPostLegacyTests(unittest.TestCase):
    """The true default opt-in path (no injected engine) after legacy success."""

    def test_default_adapter_blocks_and_preserves_legacy_decision(self) -> None:
        payload = legacy_success_payload()

        # ``engine=None`` exercises the real default
        # ApprovedTaskRunnerExecutionEngineAdapter. The opt-in request is
        # dry_run=False, and the adapter does not forward an approved-task
        # confirmation, so the real runner blocks at its confirmation gate
        # before touching any database or filesystem state.
        block = route_scheduler_tick_through_execution_engine(
            scheduler_request(),
            payload,
            engine=None,
        )

        assert_non_clean_evidence_only(self, block, payload)
        metadata = block["result"]["metadata"]
        self.assertEqual(metadata["runner_phase"], "confirmation")
        self.assertIn("--confirm-approved-task", metadata["runner_error"])


class StatusGatePostLegacyTests(unittest.TestCase):
    """The queued-status gate blocks the engine even with confirmation."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        # The legacy confirmed run already executed this task: it is now
        # waiting_approval, no longer queued.
        self.store.upsert_task(
            TaskRecord(
                task_key=TASK_KEY,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Post-legacy status gate regression task",
                status="waiting_approval",
                repo_path=self.root / "repo",
                artifact_dir=self.root / "artifacts" / TASK_KEY,
                created_at="2026-06-01T00:00:00Z",
                updated_at="2026-06-01T00:00:00Z",
            )
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_status_gate_blocks_engine_candidate_after_legacy_success(
        self,
    ) -> None:
        payload = legacy_success_payload()

        # Run the real adapter and the real runner against the real store,
        # supplying only what a future migration stage would have to supply
        # anyway (the store location and the approved-task confirmation).
        # This proves the block is the queued-status gate itself, not just
        # the missing confirmation flag.
        def run_with_store(request: ApprovedTaskRunRequest) -> Any:
            return run_approved_task(
                replace(
                    request,
                    db_path=self.db_path,
                    confirm_approved_task=True,
                )
            )

        with mock.patch.object(
            adapter_module, "run_approved_task", side_effect=run_with_store
        ):
            block = route_scheduler_tick_through_execution_engine(
                scheduler_request(),
                payload,
                engine=None,
            )

        assert_non_clean_evidence_only(self, block, payload)
        metadata = block["result"]["metadata"]
        self.assertEqual(metadata["runner_phase"], "selection")
        self.assertIn("must be queued", metadata["runner_error"])
        self.assertIn("waiting_approval", metadata["runner_error"])

        # The status gate never mutates the task: it stays waiting_approval.
        self.assertEqual(self.store.get_task(TASK_KEY).status, "waiting_approval")


if __name__ == "__main__":
    unittest.main()
