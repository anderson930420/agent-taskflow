"""Tests for the P4-e unified execution observability summary."""

from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace

from agent_taskflow.execution_engine_contract import (
    ExecutionEngineArtifactRef,
    ExecutionEngineResult,
    ExecutionEngineSafety,
    ExecutionEngineStepResult,
)
from agent_taskflow.execution_observability import (
    EXECUTION_OBSERVABILITY_SUMMARY_SCHEMA_VERSION,
    RESULT_TYPE_APPROVED_TASK_RUNNER_PAYLOAD,
    RESULT_TYPE_EXECUTION_ENGINE_RESULT,
    RESULT_TYPE_SCHEDULER_TICK_PAYLOAD,
    SUMMARY_SOURCE_APPROVED_TASK_RUNNER,
    SUMMARY_SOURCE_MANUAL_ENGINE_FACADE,
    SUMMARY_SOURCE_SCHEDULER_TICK,
    ExecutionObservedArtifact,
    ExecutionObservedSafety,
    ExecutionObservedStep,
    ExecutionObserverProfile,
    UnifiedExecutionSummary,
    summarize_approved_task_runner_payload,
    summarize_execution_engine_result,
    summarize_scheduler_tick_payload,
    to_observability_dict,
)


def _engine_result() -> ExecutionEngineResult:
    return ExecutionEngineResult(
        ok=True,
        task_key="AT-GH-900",
        status="waiting_approval",
        summary="Approved task runner reached waiting_approval.",
        next_operator_action="operator_review",
        safety=ExecutionEngineSafety(executor_started=True, validator_started=True),
        steps=(
            ExecutionEngineStepResult(
                name="executor", status="passed", summary="executor ok"
            ),
            ExecutionEngineStepResult(name="validators", status="passed"),
        ),
        artifacts=(
            ExecutionEngineArtifactRef(
                artifact_type="worker_log", path="/tmp/at/worker.log"
            ),
        ),
        metadata={"runner_dry_run": False, "adapter": "approved_task_runner"},
    )


def _approved_mapping_payload() -> dict:
    return {
        "ok": True,
        "status": "waiting_approval",
        "phase": "complete",
        "task_key": "AT-GH-901",
        "executor": "noop",
        "model": "claude",
        "provider": "anthropic",
        "tools": ["read", "write"],
        "dry_run": False,
        "preflight": {"ran": True, "ok": True, "status": "preflight ok"},
        "workspace": {"prepared": True, "summary": "worktree ready"},
        "executor_run": {"started": True, "ok": True, "summary": "executor ran"},
        "validators": [
            {"name": "pytest", "ok": True, "status": "passed"},
            {"name": "policy", "ok": True, "status": "passed"},
        ],
        "artifacts": [
            {"kind": "mission_contract", "path": "/tmp/at/contract.json"},
            {"kind": "executor_log", "path": "/tmp/at/executor.log"},
        ],
        "safety": {
            "human_approval_required": True,
            "approved": False,
            "merged": False,
            "branch_pushed": False,
            "executor_started": True,
            "validators_started": True,
            "cleanup_performed": False,
            "background_worker_started": False,
        },
        "next_allowed_actions": ["operator_review", "operator_reject"],
    }


def _scheduler_tick_payload() -> dict:
    return {
        "ok": True,
        "schema_version": "github_issue_one_task_scheduler_tick.v1",
        "source": "github_issue_one_task_scheduler_tick",
        "status": "execution_completed",
        "mode": "confirmed",
        "repo": "anderson930420/agent-taskflow",
        "lock": {"path": "/tmp/at.lock", "acquired": True, "contended": False},
        "runner_config": {
            "configured": True,
            "executor": "pi",
            "validators": ["pytest", "changed-files"],
            "worktree_root": "/tmp/agent-taskflow-worktrees",
            "base_branch": "main",
            "model": "claude",
            "provider": "anthropic",
            "tools": ["read"],
        },
        "publication_config": {
            "publish_after_execution": False,
            "mode": "execution_only",
            "next_operator_action": "run explicit task-to-draft-pr workflow",
        },
        "selected_task_key": "AT-GH-902",
        "selected_issue": {"number": 902, "title": "Do the thing"},
        "safety": {
            "scheduled_tick": True,
            "one_task_only": True,
            "dry_run": False,
            "github_mutated": False,
            "branch_pushed": False,
            "approved": False,
            "merged": False,
            "cleanup_performed": False,
        },
    }


class SerializationTests(unittest.TestCase):
    def test_summary_serializes_to_json_safe_dict(self) -> None:
        summary = summarize_execution_engine_result(_engine_result())
        payload = to_observability_dict(summary)
        # Must be round-trippable JSON.
        encoded = json.dumps(payload)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["schema_version"], EXECUTION_OBSERVABILITY_SUMMARY_SCHEMA_VERSION)
        self.assertEqual(decoded["source"], SUMMARY_SOURCE_MANUAL_ENGINE_FACADE)
        self.assertIsInstance(decoded["safety"], dict)
        self.assertIsInstance(decoded["profile"], dict)

    def test_tuple_fields_serialize_to_lists(self) -> None:
        summary = summarize_execution_engine_result(_engine_result())
        payload = to_observability_dict(summary)
        self.assertIsInstance(payload["steps"], list)
        self.assertIsInstance(payload["artifacts"], list)
        self.assertIsInstance(payload["profile"]["tools"], list)
        self.assertIsInstance(payload["profile"]["validators"], list)

    def test_path_like_artifact_path_is_string(self) -> None:
        summary = summarize_execution_engine_result(_engine_result())
        payload = to_observability_dict(summary)
        self.assertEqual(payload["artifacts"][0]["path"], "/tmp/at/worker.log")
        self.assertIsInstance(payload["artifacts"][0]["path"], str)

    def test_to_observability_dict_does_not_raise_on_unknown(self) -> None:
        # An arbitrary object is coerced to str rather than raising.
        sentinel = object()
        self.assertEqual(to_observability_dict(sentinel), str(sentinel))
        # Primitives and None pass through unchanged.
        self.assertIsNone(to_observability_dict(None))
        self.assertEqual(to_observability_dict(7), 7)


class SafetyDefaultTests(unittest.TestCase):
    def test_safety_defaults_are_conservative(self) -> None:
        safety = ExecutionObservedSafety()
        self.assertTrue(safety.human_review_required)
        self.assertTrue(safety.one_task_only)
        self.assertTrue(safety.execution_only)
        for flag in (
            "approved",
            "merged",
            "github_mutated",
            "issue_closed",
            "branch_pushed",
            "branch_deleted",
            "worktree_deleted",
            "cleanup_performed",
            "cron_modified",
            "daemon_started",
            "webhook_started",
            "background_worker_started",
            "scheduler_loop_started",
            "multi_task_batch_started",
            "executor_started",
            "validator_started",
        ):
            self.assertFalse(getattr(safety, flag), flag)

    def test_summary_default_safety_is_conservative(self) -> None:
        summary = UnifiedExecutionSummary(
            schema_version=EXECUTION_OBSERVABILITY_SUMMARY_SCHEMA_VERSION,
            source=SUMMARY_SOURCE_MANUAL_ENGINE_FACADE,
            ok=False,
        )
        self.assertTrue(summary.safety.human_review_required)
        self.assertFalse(summary.safety.merged)
        self.assertEqual(summary.profile, ExecutionObserverProfile())


class EngineResultSummaryTests(unittest.TestCase):
    def test_summarize_execution_engine_result(self) -> None:
        summary = summarize_execution_engine_result(_engine_result())
        self.assertEqual(summary.source, SUMMARY_SOURCE_MANUAL_ENGINE_FACADE)
        self.assertTrue(summary.ok)
        self.assertEqual(summary.task_key, "AT-GH-900")
        self.assertEqual(summary.status, "waiting_approval")
        self.assertEqual(summary.raw_status, "waiting_approval")
        self.assertEqual(summary.next_operator_action, "operator_review")
        self.assertEqual(
            summary.metadata["result_type"], RESULT_TYPE_EXECUTION_ENGINE_RESULT
        )
        self.assertEqual(summary.metadata["summary"], "Approved task runner reached waiting_approval.")
        # Safety mapped from the engine safety dataclass.
        self.assertTrue(summary.safety.executor_started)
        self.assertTrue(summary.safety.validator_started)
        self.assertFalse(summary.safety.merged)
        # dry_run inferred from result metadata.
        self.assertFalse(summary.dry_run)
        # Steps and artifacts mapped.
        self.assertEqual([s.name for s in summary.steps], ["executor", "validators"])
        self.assertEqual(summary.artifacts[0].artifact_type, "worker_log")
        self.assertEqual(summary.artifacts[0].path, "/tmp/at/worker.log")

    def test_custom_source_is_respected(self) -> None:
        summary = summarize_execution_engine_result(
            _engine_result(), source=SUMMARY_SOURCE_APPROVED_TASK_RUNNER
        )
        self.assertEqual(summary.source, SUMMARY_SOURCE_APPROVED_TASK_RUNNER)


class ApprovedTaskRunnerSummaryTests(unittest.TestCase):
    def test_summarize_mapping_payload(self) -> None:
        summary = summarize_approved_task_runner_payload(_approved_mapping_payload())
        self.assertEqual(summary.source, SUMMARY_SOURCE_APPROVED_TASK_RUNNER)
        self.assertTrue(summary.ok)
        self.assertEqual(summary.task_key, "AT-GH-901")
        self.assertEqual(summary.status, "waiting_approval")
        self.assertFalse(summary.dry_run)
        self.assertEqual(summary.next_operator_action, "operator_review")
        self.assertEqual(
            summary.metadata["result_type"],
            RESULT_TYPE_APPROVED_TASK_RUNNER_PAYLOAD,
        )
        # Profile mapping.
        self.assertEqual(summary.profile.executor, "noop")
        self.assertEqual(summary.profile.model, "claude")
        self.assertEqual(summary.profile.provider, "anthropic")
        self.assertEqual(summary.profile.tools, ("read", "write"))
        self.assertEqual(summary.profile.validators, ("pytest", "policy"))
        # Safety alias mapping.
        self.assertTrue(summary.safety.human_review_required)
        self.assertTrue(summary.safety.executor_started)
        self.assertTrue(summary.safety.validator_started)
        self.assertFalse(summary.safety.merged)

    def test_known_sections_become_steps(self) -> None:
        summary = summarize_approved_task_runner_payload(_approved_mapping_payload())
        names = [step.name for step in summary.steps]
        self.assertEqual(
            names,
            ["preflight", "workspace", "executor", "validators", "status_transition"],
        )
        by_name = {step.name: step for step in summary.steps}
        self.assertEqual(by_name["preflight"].status, "passed")
        self.assertEqual(by_name["workspace"].status, "passed")
        self.assertEqual(by_name["executor"].status, "passed")
        self.assertEqual(by_name["validators"].status, "passed")
        self.assertEqual(by_name["status_transition"].status, "completed")

    def test_artifacts_from_list_shape(self) -> None:
        summary = summarize_approved_task_runner_payload(_approved_mapping_payload())
        types = [artifact.artifact_type for artifact in summary.artifacts]
        self.assertEqual(types, ["mission_contract", "executor_log"])
        self.assertEqual(summary.artifacts[0].path, "/tmp/at/contract.json")

    def test_summarize_attribute_payload(self) -> None:
        payload = SimpleNamespace(
            ok=True,
            task_status="waiting_approval",
            task_key="AT-GH-903",
            executor="pi",
            dry_run=False,
            safety={"human_approval_required": True, "approved": False},
            artifacts=[
                SimpleNamespace(
                    artifact_type="review_log",
                    path="/tmp/at/review.log",
                    description="validator log",
                )
            ],
        )
        summary = summarize_approved_task_runner_payload(payload)
        self.assertTrue(summary.ok)
        self.assertEqual(summary.task_key, "AT-GH-903")
        self.assertEqual(summary.status, "waiting_approval")
        self.assertEqual(summary.profile.executor, "pi")
        self.assertEqual(summary.artifacts[0].artifact_type, "review_log")
        self.assertEqual(summary.artifacts[0].description, "validator log")

    def test_failed_validator_marks_step_failed(self) -> None:
        payload = _approved_mapping_payload()
        payload["validators"] = [
            {"name": "pytest", "ok": False, "status": "failed"},
        ]
        payload["ok"] = False
        payload["status"] = "blocked"
        summary = summarize_approved_task_runner_payload(payload)
        by_name = {step.name: step for step in summary.steps}
        self.assertEqual(by_name["validators"].status, "failed")
        self.assertEqual(by_name["status_transition"].status, "blocked")


class SchedulerTickSummaryTests(unittest.TestCase):
    def test_summarize_scheduler_tick_payload(self) -> None:
        summary = summarize_scheduler_tick_payload(_scheduler_tick_payload())
        self.assertEqual(summary.source, SUMMARY_SOURCE_SCHEDULER_TICK)
        self.assertTrue(summary.ok)
        self.assertEqual(summary.task_key, "AT-GH-902")
        self.assertEqual(summary.status, "execution_completed")
        self.assertEqual(summary.mode, "confirmed")
        self.assertFalse(summary.dry_run)
        self.assertEqual(
            summary.metadata["result_type"], RESULT_TYPE_SCHEDULER_TICK_PAYLOAD
        )
        self.assertEqual(summary.metadata["selected_issue"], {"number": 902, "title": "Do the thing"})
        self.assertIn("lock", summary.metadata)

    def test_runner_config_maps_profile(self) -> None:
        summary = summarize_scheduler_tick_payload(_scheduler_tick_payload())
        self.assertEqual(summary.profile.executor, "pi")
        self.assertEqual(summary.profile.model, "claude")
        self.assertEqual(summary.profile.validators, ("pytest", "changed-files"))
        self.assertEqual(summary.metadata["runner_worktree_root"], "/tmp/agent-taskflow-worktrees")

    def test_publication_config_maps_publication_mode(self) -> None:
        summary = summarize_scheduler_tick_payload(_scheduler_tick_payload())
        self.assertEqual(summary.publication_mode, "execution_only")
        # execution-only tick keeps execution_only safety True.
        self.assertTrue(summary.safety.execution_only)
        self.assertEqual(
            summary.next_operator_action, "run explicit task-to-draft-pr workflow"
        )

    def test_publication_enabled_flips_execution_only(self) -> None:
        payload = _scheduler_tick_payload()
        payload["publication_config"] = {
            "publish_after_execution": True,
            "mode": "publication",
        }
        summary = summarize_scheduler_tick_payload(payload)
        self.assertEqual(summary.publication_mode, "publication")
        self.assertFalse(summary.safety.execution_only)
        self.assertTrue(summary.metadata["publish_after_execution"])

    def test_safety_payload_maps_known_flags(self) -> None:
        payload = _scheduler_tick_payload()
        payload["safety"]["github_mutated"] = True
        payload["safety"]["branch_pushed"] = True
        summary = summarize_scheduler_tick_payload(payload)
        self.assertTrue(summary.safety.github_mutated)
        self.assertTrue(summary.safety.branch_pushed)
        self.assertTrue(summary.safety.one_task_only)
        # Absent flags stay at conservative defaults.
        self.assertFalse(summary.safety.merged)
        self.assertFalse(summary.safety.worktree_deleted)


class ArtifactShapeTests(unittest.TestCase):
    def test_artifacts_from_mapping_shape(self) -> None:
        payload = {
            "ok": True,
            "status": "completed",
            "artifacts": {
                "manifest": "/tmp/at/manifest.json",
                "worker_log": "/tmp/at/worker.log",
            },
        }
        summary = summarize_approved_task_runner_payload(payload)
        by_type = {a.artifact_type: a.path for a in summary.artifacts}
        self.assertEqual(by_type["manifest"], "/tmp/at/manifest.json")
        self.assertEqual(by_type["worker_log"], "/tmp/at/worker.log")

    def test_malformed_artifacts_are_skipped(self) -> None:
        payload = {
            "ok": True,
            "status": "completed",
            "artifacts": [{"kind": "x", "path": None}, {"kind": "y"}],
        }
        summary = summarize_approved_task_runner_payload(payload)
        self.assertEqual(summary.artifacts, ())


class RobustnessTests(unittest.TestCase):
    def test_missing_optional_fields_do_not_crash(self) -> None:
        summary = summarize_approved_task_runner_payload({})
        self.assertFalse(summary.ok)
        self.assertIsNone(summary.task_key)
        self.assertIsNone(summary.status)
        self.assertIsNone(summary.dry_run)
        self.assertEqual(summary.steps, ())
        self.assertEqual(summary.artifacts, ())

    def test_scheduler_missing_optional_fields_do_not_crash(self) -> None:
        summary = summarize_scheduler_tick_payload({"ok": False})
        self.assertFalse(summary.ok)
        self.assertIsNone(summary.task_key)
        self.assertIsNone(summary.publication_mode)
        self.assertEqual(summary.profile, ExecutionObserverProfile())

    def test_source_defaults_are_correct(self) -> None:
        self.assertEqual(
            summarize_execution_engine_result(_engine_result()).source,
            SUMMARY_SOURCE_MANUAL_ENGINE_FACADE,
        )
        self.assertEqual(
            summarize_approved_task_runner_payload({"ok": True}).source,
            SUMMARY_SOURCE_APPROVED_TASK_RUNNER,
        )
        self.assertEqual(
            summarize_scheduler_tick_payload({"ok": True}).source,
            SUMMARY_SOURCE_SCHEDULER_TICK,
        )

    def test_no_mutation_of_input_payloads(self) -> None:
        approved = _approved_mapping_payload()
        approved_copy = copy.deepcopy(approved)
        summarize_approved_task_runner_payload(approved)
        self.assertEqual(approved, approved_copy)

        tick = _scheduler_tick_payload()
        tick_copy = copy.deepcopy(tick)
        summarize_scheduler_tick_payload(tick)
        self.assertEqual(tick, tick_copy)


class DataclassShapeTests(unittest.TestCase):
    def test_step_metadata_defaults_to_empty(self) -> None:
        step = ExecutionObservedStep(name="x", status="passed")
        self.assertEqual(dict(step.metadata), {})

    def test_artifact_optional_description(self) -> None:
        artifact = ExecutionObservedArtifact(artifact_type="log", path="/tmp/x")
        self.assertIsNone(artifact.description)


if __name__ == "__main__":
    unittest.main()
