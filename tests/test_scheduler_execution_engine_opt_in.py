"""Unit tests for P5-d: the scheduler ExecutionEngine opt-in routing helper.

These tests cover the pure / narrow helper in
``agent_taskflow/scheduler_execution_engine_opt_in.py`` in isolation: building
the engine-shaped request, routing one task through an injected engine exactly
once, the shadow / compare evidence, the failure path, and the governance
safety invariants. The scheduler-tick integration is tested separately in
``tests/test_github_issue_one_task_scheduler_tick.py``.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_taskflow.execution_engine_contract import (
    REQUEST_SOURCE_SCHEDULED_TICK,
    ExecutionEngineRequest,
    ExecutionEngineResult,
    ExecutionEngineSafety,
)
from agent_taskflow.scheduler_execution_engine_opt_in import (
    SCHEDULER_EXECUTION_ENGINE_OPT_IN_SCHEMA_VERSION,
    SCHEDULER_EXECUTION_ENGINE_OPT_IN_SOURCE,
    build_scheduler_tick_execution_engine_request,
    route_scheduler_tick_through_execution_engine,
)


def scheduler_request(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "repo": "anderson930420/agent-taskflow",
        "local_repo_path": Path("/tmp/agent-taskflow-p5d/repo"),
        "artifact_root": Path("/tmp/agent-taskflow-p5d/artifacts"),
        "executor": "shell",
        "model": "claude-sonnet-4-6",
        "provider": "anthropic",
        "tools": ("read", "write"),
        "pi_bin": "pi",
        "validators": ("pytest", "policy"),
        "worktree_root": Path("/tmp/agent-taskflow-p5d/worktrees"),
        "approved_task_preflight": True,
        "operator": "codex",
        "operator_note": "p5-d opt-in test",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def tick_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "status": "execution_completed",
        "mode": "confirmed",
        "repo": "anderson930420/agent-taskflow",
        "selected_task_key": "AT-GH-808",
        "publication_config": {
            "publish_after_execution": False,
            "mode": "execution_only",
        },
        "automation": {"selected_issue": {"number": 808}},
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


class RecordingEngine:
    def __init__(self, result: ExecutionEngineResult | None = None) -> None:
        self.calls: list[ExecutionEngineRequest] = []
        self._result = result

    def execute(self, request: ExecutionEngineRequest) -> ExecutionEngineResult:
        self.calls.append(request)
        if self._result is not None:
            return self._result
        return ExecutionEngineResult(
            ok=True,
            task_key=request.task_key,
            status="waiting_approval",
            summary="recording engine result",
            safety=ExecutionEngineSafety(),
        )


class BuildRequestTests(unittest.TestCase):
    def test_request_carries_scheduled_tick_source_and_invariants(self) -> None:
        request = build_scheduler_tick_execution_engine_request(
            scheduler_request(),
            task_key="AT-GH-808",
            selected_issue_number=808,
        )
        self.assertEqual(request.source, REQUEST_SOURCE_SCHEDULED_TICK)
        self.assertEqual(request.task_key, "AT-GH-808")
        self.assertEqual(request.project, "anderson930420/agent-taskflow")
        self.assertFalse(request.dry_run)
        self.assertEqual(request.executor_profile.executor, "shell")
        self.assertEqual(request.executor_profile.model, "claude-sonnet-4-6")
        self.assertEqual(request.validator_profile.validators, ("pytest", "policy"))

        metadata = request.metadata
        self.assertIs(metadata["publish_after_execution"], False)
        self.assertEqual(metadata["mode"], "execution_only")
        self.assertIs(metadata["execution_only"], True)
        self.assertIs(metadata["one_task_only"], True)
        self.assertIs(metadata["scheduler_tick"], True)
        self.assertEqual(metadata["selected_issue_number"], 808)

    def test_executor_defaults_to_noop_when_unset(self) -> None:
        request = build_scheduler_tick_execution_engine_request(
            scheduler_request(executor=None),
            task_key="AT-GH-1",
        )
        self.assertEqual(request.executor_profile.executor, "noop")

    def test_request_is_never_publication(self) -> None:
        request = build_scheduler_tick_execution_engine_request(
            scheduler_request(),
            task_key="AT-GH-1",
        )
        # The P5-b builder forbids publication; the opt-in path is execution-only.
        self.assertIs(request.metadata["publish_after_execution"], False)


class RouteThroughEngineTests(unittest.TestCase):
    def test_engine_called_exactly_once_and_block_is_evidence(self) -> None:
        engine = RecordingEngine()
        block = route_scheduler_tick_through_execution_engine(
            scheduler_request(),
            tick_payload(),
            engine=engine,
        )

        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(engine.calls[0].source, REQUEST_SOURCE_SCHEDULED_TICK)
        self.assertEqual(engine.calls[0].task_key, "AT-GH-808")

        self.assertEqual(
            block["schema_version"],
            SCHEDULER_EXECUTION_ENGINE_OPT_IN_SCHEMA_VERSION,
        )
        self.assertEqual(block["source"], SCHEDULER_EXECUTION_ENGINE_OPT_IN_SOURCE)
        self.assertTrue(block["enabled"])
        self.assertTrue(block["executed"])
        self.assertTrue(block["ok"])
        self.assertEqual(block["status"], "waiting_approval")
        self.assertEqual(block["engine_invocation_count"], 1)
        self.assertEqual(block["request_source"], REQUEST_SOURCE_SCHEDULED_TICK)
        self.assertEqual(block["selected_task_key"], "AT-GH-808")

        # Request / result / compare evidence is present and JSON-compatible.
        self.assertEqual(block["request"]["source"], REQUEST_SOURCE_SCHEDULED_TICK)
        self.assertIs(block["request_summary"]["publish_after_execution"], False)
        self.assertEqual(block["request_summary"]["mode"], "execution_only")
        self.assertTrue(block["result_summary"]["ok"])
        self.assertTrue(block["shadow_compare"]["matched"])
        self.assertEqual(block["shadow_compare"]["mismatches"], [])
        self.assertIsNotNone(block["observability_summary"])
        json.dumps(block)  # must not raise

    def test_engine_path_safety_is_evidence_only(self) -> None:
        block = route_scheduler_tick_through_execution_engine(
            scheduler_request(),
            tick_payload(),
            engine=RecordingEngine(),
        )
        safety = block["safety"]
        self.assertFalse(safety["approval_authority"])
        self.assertFalse(safety["approved"])
        self.assertFalse(safety["merged"])
        self.assertFalse(safety["branch_pushed"])
        self.assertFalse(safety["draft_pr_created"])
        self.assertFalse(safety["cleanup_performed"])
        self.assertFalse(safety["branch_deleted"])
        self.assertFalse(safety["worktree_deleted"])
        self.assertFalse(safety["daemon_started"])
        self.assertFalse(safety["webhook_started"])
        self.assertFalse(safety["background_worker_started"])
        self.assertFalse(safety["scheduler_loop_started"])
        self.assertFalse(safety["multi_task_batch_started"])
        self.assertTrue(safety["execution_only"])
        self.assertTrue(safety["human_review_required"])

    def test_no_selected_task_skips_engine(self) -> None:
        engine = RecordingEngine()
        block = route_scheduler_tick_through_execution_engine(
            scheduler_request(),
            tick_payload(selected_task_key=None, automation=None),
            engine=engine,
        )
        self.assertEqual(len(engine.calls), 0)
        self.assertFalse(block["executed"])
        self.assertEqual(block["status"], "not_executed")
        self.assertEqual(block["reason"], "no_selected_task_for_engine_path")
        self.assertIsNone(block["request"])

    def test_engine_failure_returns_structured_block(self) -> None:
        class RaisingEngine:
            def execute(self, request: ExecutionEngineRequest) -> ExecutionEngineResult:
                raise RuntimeError("boom")

        block = route_scheduler_tick_through_execution_engine(
            scheduler_request(),
            tick_payload(),
            engine=RaisingEngine(),
        )
        self.assertTrue(block["executed"])
        self.assertFalse(block["ok"])
        self.assertEqual(block["status"], "engine_error")
        self.assertIn("boom", block["error"])
        self.assertIsNone(block["result"])
        # The shadow compare is produced before execution, so it is still present.
        self.assertIsNotNone(block["shadow_compare"])
        self.assertFalse(block["safety"]["approved"])
        self.assertFalse(block["safety"]["merged"])
        json.dumps(block)

    def test_engine_returning_non_result_is_blocked(self) -> None:
        class BadEngine:
            def execute(self, request: ExecutionEngineRequest) -> Any:
                return {"ok": True}

        block = route_scheduler_tick_through_execution_engine(
            scheduler_request(),
            tick_payload(),
            engine=BadEngine(),
        )
        self.assertFalse(block["ok"])
        self.assertEqual(block["status"], "engine_error")
        self.assertIn("non-ExecutionEngineResult", block["error"])

    def test_failed_engine_result_is_surfaced(self) -> None:
        failed = ExecutionEngineResult(
            ok=False,
            task_key="AT-GH-808",
            status="validator_failed",
            summary="validator failed",
            safety=ExecutionEngineSafety(),
        )
        block = route_scheduler_tick_through_execution_engine(
            scheduler_request(),
            tick_payload(),
            engine=RecordingEngine(result=failed),
        )
        self.assertTrue(block["executed"])
        self.assertFalse(block["ok"])
        self.assertEqual(block["status"], "validator_failed")


class HelperSourceSafetyTests(unittest.TestCase):
    def test_helper_source_has_no_loop_or_destructive_operations(self) -> None:
        source = Path(
            "agent_taskflow/scheduler_execution_engine_opt_in.py"
        ).read_text(encoding="utf-8")
        forbidden = (
            "while True",
            "schedule.every",
            "asyncio.sleep",
            "threading.Thread",
            "Thread(",
            "merge_pull_request",
            "record_approval_decision(",
            "delete_worktree",
            "git worktree remove",
            "git branch -d",
            "git push",
            "import subprocess",
            "subprocess.run",
        )
        for needle in forbidden:
            self.assertNotIn(needle, source, needle)


if __name__ == "__main__":
    unittest.main()
