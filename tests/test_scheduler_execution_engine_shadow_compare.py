"""Tests for the P5-c scheduler ExecutionEngine shadow / compare layer.

The compare layer is a pure, behavior-free diagnostic: it inspects a legacy
scheduler tick payload and an engine-shaped ``ExecutionEngineRequest`` produced
by the P5-b builder, and reports matches, mismatches, and warnings. These tests
assert the comparison rules and that the layer never executes, wires, or touches
any runtime path or the filesystem.
"""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

import agent_taskflow.scheduler_execution_engine_shadow_compare as compare_module
from agent_taskflow.execution_engine_contract import (
    REQUEST_SOURCE_MANUAL,
    REQUEST_SOURCE_SCHEDULED_TICK,
    ExecutionEngineExecutorProfile,
    ExecutionEngineRequest,
    ExecutionEngineValidatorProfile,
    ExecutionEngineWorkspaceProfile,
)
from agent_taskflow.scheduler_execution_engine_request_builder import (
    SchedulerExecutionEngineRequestBuildInput,
    build_scheduler_execution_engine_request,
)
from agent_taskflow.scheduler_execution_engine_shadow_compare import (
    SCHEDULER_EXECUTION_ENGINE_SHADOW_COMPARE_SCHEMA_VERSION,
    SCHEDULER_EXECUTION_ENGINE_SHADOW_COMPARE_SOURCE,
    SchedulerExecutionEngineShadowCompareInput,
    SchedulerExecutionEngineShadowCompareResult,
    compare_scheduler_tick_to_engine_request,
    scheduler_execution_engine_shadow_compare_to_json_dict,
)


TASK_KEY = "AT-P5C"
REPO = "anderson930420/agent-taskflow"
REPO_PATH = Path("/tmp/agent-taskflow")
ARTIFACT_DIR = Path("/tmp/agent-taskflow-artifacts/AT-P5C")


def make_engine_request(**overrides: object) -> ExecutionEngineRequest:
    """Build a scheduled-tick engine request via the P5-b builder."""

    values: dict[str, object] = {
        "task_key": TASK_KEY,
        "repo": REPO,
        "local_repo_path": REPO_PATH,
        "artifact_dir": ARTIFACT_DIR,
        "executor": "pi",
        "model": "claude-sonnet-4-6",
        "validators": ("pytest",),
    }
    values.update(overrides)
    return build_scheduler_execution_engine_request(
        SchedulerExecutionEngineRequestBuildInput(**values)  # type: ignore[arg-type]
    )


def _good_engine_metadata() -> dict[str, object]:
    return {
        "publish_after_execution": False,
        "mode": "execution_only",
        "execution_only": True,
        "one_task_only": True,
        "scheduler_tick": True,
    }


def make_raw_request(
    *,
    task_key: str = TASK_KEY,
    project: str = REPO,
    source: str = REQUEST_SOURCE_SCHEDULED_TICK,
    metadata: dict[str, object] | None = None,
) -> ExecutionEngineRequest:
    """Construct an engine request directly, bypassing the builder.

    Used only for the negative engine-metadata cases the builder cannot
    produce (the builder always stamps correct execution-only safety markers).
    """

    return ExecutionEngineRequest(
        task_key=task_key,
        project=project,
        source=source,
        executor_profile=ExecutionEngineExecutorProfile(executor="pi"),
        validator_profile=ExecutionEngineValidatorProfile(validators=("pytest",)),
        workspace=ExecutionEngineWorkspaceProfile(
            repo_path=REPO_PATH,
            artifact_dir=ARTIFACT_DIR,
        ),
        metadata=_good_engine_metadata() if metadata is None else metadata,
    )


def make_legacy(**overrides: object) -> dict[str, object]:
    """Return a complete, fully-matching legacy scheduler tick payload."""

    legacy: dict[str, object] = {
        "ok": True,
        "schema_version": "github_issue_one_task_scheduler_tick.v1",
        "source": "github_issue_one_task_scheduler_tick",
        "status": "waiting_approval",
        "mode": "confirmed",
        "repo": REPO,
        "selected_task_key": TASK_KEY,
        "automation": {
            "selected_task_key": TASK_KEY,
            "publication": {
                "publish_after_execution": False,
                "mode": "execution_only",
            },
            "safety": {"one_task_only": True, "github_mutated": False},
        },
        "publication": {
            "publish_after_execution": False,
            "mode": "execution_only",
        },
        "runner_config": {
            "configured": True,
            "executor": "pi",
            "validators": ["pytest"],
        },
        "safety": {
            "scheduled_tick": True,
            "one_task_only": True,
            "scheduler_loop_started": False,
            "background_worker_started": False,
            "multi_task_batch_started": False,
            "github_mutated": False,
            "approved": False,
            "merged": False,
        },
    }
    legacy.update(overrides)
    return legacy


def make_input(
    *,
    legacy: dict[str, object] | None = None,
    request: ExecutionEngineRequest | None = None,
    metadata: dict[str, object] | None = None,
) -> SchedulerExecutionEngineShadowCompareInput:
    return SchedulerExecutionEngineShadowCompareInput(
        legacy_scheduler_tick=make_legacy() if legacy is None else legacy,
        engine_request=make_engine_request() if request is None else request,
        metadata={} if metadata is None else metadata,
    )


class ShadowCompareMatchTests(unittest.TestCase):
    def test_matching_legacy_and_engine_request(self) -> None:
        result = compare_scheduler_tick_to_engine_request(make_input())

        self.assertIsInstance(
            result, SchedulerExecutionEngineShadowCompareResult
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.matched)
        self.assertEqual(result.mismatches, ())
        self.assertEqual(result.warnings, ())
        self.assertEqual(
            result.schema_version,
            SCHEDULER_EXECUTION_ENGINE_SHADOW_COMPARE_SCHEMA_VERSION,
        )
        self.assertEqual(
            result.source, SCHEDULER_EXECUTION_ENGINE_SHADOW_COMPARE_SOURCE
        )
        self.assertEqual(result.legacy_status, "waiting_approval")
        self.assertEqual(result.legacy_selected_task_key, TASK_KEY)
        self.assertEqual(result.engine_task_key, TASK_KEY)
        self.assertEqual(result.engine_source, REQUEST_SOURCE_SCHEDULED_TICK)

    def test_task_key_falls_back_to_nested_automation(self) -> None:
        legacy = make_legacy()
        legacy["selected_task_key"] = None  # forces nested fallback
        result = compare_scheduler_tick_to_engine_request(
            make_input(legacy=legacy)
        )

        self.assertTrue(result.matched)
        self.assertEqual(result.legacy_selected_task_key, TASK_KEY)


class ShadowCompareMismatchTests(unittest.TestCase):
    def test_task_key_mismatch_is_detected(self) -> None:
        legacy = make_legacy()
        legacy["selected_task_key"] = "AT-OTHER"
        legacy["automation"] = {"selected_task_key": "AT-OTHER"}
        result = compare_scheduler_tick_to_engine_request(
            make_input(legacy=legacy)
        )

        self.assertFalse(result.matched)
        self.assertTrue(any("task_key mismatch" in m for m in result.mismatches))

    def test_repo_project_mismatch_is_detected(self) -> None:
        legacy = make_legacy(repo="someone-else/other-repo")
        result = compare_scheduler_tick_to_engine_request(
            make_input(legacy=legacy)
        )

        self.assertFalse(result.matched)
        self.assertTrue(
            any("repo/project mismatch" in m for m in result.mismatches)
        )

    def test_engine_publish_after_execution_true_mismatch(self) -> None:
        metadata = _good_engine_metadata()
        metadata["publish_after_execution"] = True
        result = compare_scheduler_tick_to_engine_request(
            make_input(request=make_raw_request(metadata=metadata))
        )

        self.assertFalse(result.matched)
        self.assertTrue(
            any(
                "publish_after_execution must be False" in m
                for m in result.mismatches
            )
        )

    def test_engine_mode_not_execution_only_mismatch(self) -> None:
        metadata = _good_engine_metadata()
        metadata["mode"] = "publication"
        result = compare_scheduler_tick_to_engine_request(
            make_input(request=make_raw_request(metadata=metadata))
        )

        self.assertFalse(result.matched)
        self.assertTrue(
            any('mode must be "execution_only"' in m for m in result.mismatches)
        )

    def test_engine_execution_only_false_mismatch(self) -> None:
        metadata = _good_engine_metadata()
        metadata["execution_only"] = False
        result = compare_scheduler_tick_to_engine_request(
            make_input(request=make_raw_request(metadata=metadata))
        )

        self.assertFalse(result.matched)
        self.assertTrue(
            any("execution_only must be True" in m for m in result.mismatches)
        )

    def test_legacy_publish_after_execution_true_mismatch(self) -> None:
        legacy = make_legacy()
        legacy["publication"] = {
            "publish_after_execution": True,
            "mode": "execution_only",
        }
        result = compare_scheduler_tick_to_engine_request(
            make_input(legacy=legacy)
        )

        self.assertFalse(result.matched)
        self.assertTrue(
            any(
                "legacy publish_after_execution must be False" in m
                for m in result.mismatches
            )
        )

    def test_legacy_publication_mode_not_execution_only_mismatch(self) -> None:
        legacy = make_legacy()
        legacy["publication"] = {
            "publish_after_execution": False,
            "mode": "publication",
        }
        result = compare_scheduler_tick_to_engine_request(
            make_input(legacy=legacy)
        )

        self.assertFalse(result.matched)
        self.assertTrue(
            any(
                'legacy publication mode must be "execution_only"' in m
                for m in result.mismatches
            )
        )

    def test_engine_one_task_only_missing_mismatch(self) -> None:
        metadata = _good_engine_metadata()
        del metadata["one_task_only"]
        result = compare_scheduler_tick_to_engine_request(
            make_input(request=make_raw_request(metadata=metadata))
        )

        self.assertFalse(result.matched)
        self.assertTrue(
            any("one_task_only must be True" in m for m in result.mismatches)
        )

    def test_engine_scheduler_tick_false_mismatch(self) -> None:
        metadata = _good_engine_metadata()
        metadata["scheduler_tick"] = False
        result = compare_scheduler_tick_to_engine_request(
            make_input(request=make_raw_request(metadata=metadata))
        )

        self.assertFalse(result.matched)
        self.assertTrue(
            any("scheduler_tick must be True" in m for m in result.mismatches)
        )

    def test_legacy_scheduler_loop_started_mismatch(self) -> None:
        self._assert_legacy_safety_mismatch("scheduler_loop_started")

    def test_legacy_background_worker_started_mismatch(self) -> None:
        self._assert_legacy_safety_mismatch("background_worker_started")

    def test_legacy_multi_task_batch_started_mismatch(self) -> None:
        self._assert_legacy_safety_mismatch("multi_task_batch_started")

    def test_legacy_safety_approved_mismatch(self) -> None:
        self._assert_legacy_safety_mismatch("approved")

    def test_legacy_safety_merged_mismatch(self) -> None:
        self._assert_legacy_safety_mismatch("merged")

    def _assert_legacy_safety_mismatch(self, marker: str) -> None:
        legacy = make_legacy()
        safety = dict(legacy["safety"])  # type: ignore[arg-type]
        safety[marker] = True
        legacy["safety"] = safety
        result = compare_scheduler_tick_to_engine_request(
            make_input(legacy=legacy)
        )

        self.assertFalse(result.matched, msg=marker)
        self.assertTrue(
            any(marker in m for m in result.mismatches), msg=marker
        )


class ShadowCompareWarningTests(unittest.TestCase):
    def test_missing_legacy_publication_markers_warn_not_mismatch(self) -> None:
        legacy = make_legacy()
        del legacy["publication"]
        automation = dict(legacy["automation"])  # type: ignore[arg-type]
        automation.pop("publication", None)
        legacy["automation"] = automation
        result = compare_scheduler_tick_to_engine_request(
            make_input(legacy=legacy)
        )

        self.assertTrue(result.matched)
        self.assertTrue(
            any("publication markers absent" in w for w in result.warnings)
        )

    def test_missing_legacy_safety_markers_warn_not_mismatch(self) -> None:
        legacy = make_legacy()
        del legacy["safety"]
        result = compare_scheduler_tick_to_engine_request(
            make_input(legacy=legacy)
        )

        self.assertTrue(result.matched)
        self.assertTrue(
            any("safety markers absent" in w for w in result.warnings)
        )

    def test_missing_legacy_runner_config_warns(self) -> None:
        legacy = make_legacy()
        del legacy["runner_config"]
        automation = dict(legacy["automation"])  # type: ignore[arg-type]
        automation.pop("runner_config", None)
        automation.pop("runner", None)
        legacy["automation"] = automation
        result = compare_scheduler_tick_to_engine_request(
            make_input(legacy=legacy)
        )

        self.assertTrue(result.matched)
        self.assertTrue(
            any("runner config absent" in w for w in result.warnings)
        )


class ShadowCompareInputValidationTests(unittest.TestCase):
    def test_non_scheduled_tick_source_is_rejected(self) -> None:
        request = make_raw_request(source=REQUEST_SOURCE_MANUAL)
        with self.assertRaisesRegex(
            ValueError, "engine_request.source must be"
        ):
            SchedulerExecutionEngineShadowCompareInput(
                legacy_scheduler_tick=make_legacy(),
                engine_request=request,
            )

    def test_non_mapping_legacy_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            TypeError, "legacy_scheduler_tick must be a mapping"
        ):
            SchedulerExecutionEngineShadowCompareInput(
                legacy_scheduler_tick=["not", "a", "mapping"],  # type: ignore[arg-type]
                engine_request=make_engine_request(),
            )

    def test_caller_metadata_is_copied_defensively(self) -> None:
        metadata: dict[str, object] = {"tick_id": "tick-7", "labels": ["one"]}
        build_input = make_input(metadata=metadata)

        metadata["tick_id"] = "mutated"
        metadata["labels"].append("mutated")  # type: ignore[union-attr]

        self.assertEqual(build_input.metadata["tick_id"], "tick-7")
        self.assertEqual(build_input.metadata["labels"], ["one"])

    def test_legacy_mutation_after_construction_does_not_mutate_result(
        self,
    ) -> None:
        legacy = make_legacy()
        result = compare_scheduler_tick_to_engine_request(
            make_input(legacy=legacy)
        )

        legacy["selected_task_key"] = "MUTATED"
        legacy["repo"] = "mutated/repo"
        legacy["safety"]["approved"] = True  # type: ignore[index]

        self.assertEqual(result.legacy_selected_task_key, TASK_KEY)
        self.assertTrue(result.matched)
        self.assertEqual(result.summary["legacy_repo"], REPO)


class ShadowCompareSummaryTests(unittest.TestCase):
    def test_summary_includes_engine_executor_model_validators_workspace(
        self,
    ) -> None:
        request = make_engine_request(
            executor="pi",
            model="claude-sonnet-4-6",
            validators=("pytest", "changed-files"),
            worktree_root=Path("/tmp/worktrees"),
            task_worktree_path=Path("/tmp/worktrees/AT-P5C"),
        )
        result = compare_scheduler_tick_to_engine_request(
            make_input(request=request)
        )

        summary = result.summary
        self.assertEqual(summary["engine_executor"], "pi")
        self.assertEqual(summary["engine_model"], "claude-sonnet-4-6")
        self.assertEqual(
            summary["engine_validators"], ["pytest", "changed-files"]
        )
        workspace = summary["engine_workspace"]
        self.assertEqual(workspace["repo_path"], str(REPO_PATH))
        self.assertEqual(workspace["artifact_dir"], str(ARTIFACT_DIR))
        self.assertEqual(workspace["worktree_root"], "/tmp/worktrees")
        self.assertEqual(
            workspace["task_worktree_path"], "/tmp/worktrees/AT-P5C"
        )

    def test_result_helper_returns_json_compatible_dict(self) -> None:
        result = compare_scheduler_tick_to_engine_request(make_input())

        payload = scheduler_execution_engine_shadow_compare_to_json_dict(result)

        self.assertIsInstance(payload, dict)
        # json.dumps raises if anything is not JSON-compatible.
        json.dumps(payload)
        self.assertEqual(
            payload["schema_version"],
            SCHEDULER_EXECUTION_ENGINE_SHADOW_COMPARE_SCHEMA_VERSION,
        )
        self.assertTrue(payload["matched"])
        self.assertEqual(payload["mismatches"], [])

    def test_compare_does_not_touch_filesystem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_repo = Path(tmp) / "missing-repo"
            missing_artifacts = Path(tmp) / "missing-artifacts" / "AT-P5C"
            request = make_engine_request(
                local_repo_path=missing_repo,
                artifact_dir=missing_artifacts,
            )

            compare_scheduler_tick_to_engine_request(
                make_input(request=request)
            )

            self.assertFalse(missing_repo.exists())
            self.assertFalse(missing_artifacts.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])


class ShadowComparePurityTests(unittest.TestCase):
    """The module must stay a pure compare layer with no runtime call surface."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = Path(compare_module.__file__).read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_module_imports_no_runtime_modules(self) -> None:
        forbidden = (
            "approved_task_runner",
            "execution_engine_approved_task_adapter",
            "execution_engine_manual_runtime",
            "github_issue_one_task_scheduler_tick",
            "github_issue_one_task_automation",
            "subprocess",
            "sqlite3",
            "requests",
            "urllib",
            "socket",
            "shutil",
            "os",
        )
        imported: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        for name in imported:
            parts = name.split(".")
            for banned in forbidden:
                self.assertNotIn(banned, parts, msg=name)

    def test_module_does_not_call_engine_or_runtime_entrypoints(self) -> None:
        forbidden_calls = {
            "execute",
            "run_approved_task",
            "run",
            "Popen",
            "call",
            "check_call",
            "check_output",
            "system",
            "mkdir",
            "makedirs",
            "open",
            "write_text",
            "write_bytes",
            "unlink",
            "rmdir",
            "connect",
        }
        called: set[str] = set()
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
        self.assertEqual(called & forbidden_calls, set())

    def test_module_identifiers_do_not_reference_cron(self) -> None:
        identifiers = {
            node.id
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Name)
        } | {
            node.attr
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Attribute)
        }
        for identifier in identifiers:
            self.assertNotIn("cron", identifier.lower(), msg=identifier)

    def test_public_api_is_compare_only(self) -> None:
        self.assertEqual(
            set(compare_module.__all__),
            {
                "SCHEDULER_EXECUTION_ENGINE_SHADOW_COMPARE_SCHEMA_VERSION",
                "SCHEDULER_EXECUTION_ENGINE_SHADOW_COMPARE_SOURCE",
                "SchedulerExecutionEngineShadowCompareInput",
                "SchedulerExecutionEngineShadowCompareResult",
                "compare_scheduler_tick_to_engine_request",
                "scheduler_execution_engine_shadow_compare_to_json_dict",
            },
        )


if __name__ == "__main__":
    unittest.main()
