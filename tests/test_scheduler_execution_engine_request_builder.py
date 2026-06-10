"""Tests for the P5-b scheduler ExecutionEngine request builder.

The builder is a pure, behavior-free contract mapping. These tests assert the
value mapping and the validation rules, and that building a request never
touches the filesystem or any runtime path.
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

import agent_taskflow.scheduler_execution_engine_request_builder as builder_module
from agent_taskflow.execution_engine_contract import (
    REQUEST_SOURCE_SCHEDULED_TICK,
    ExecutionEngineRequest,
)
from agent_taskflow.scheduler_execution_engine_request_builder import (
    SCHEDULER_EXECUTION_ENGINE_REQUEST_BUILDER_SCHEMA_VERSION,
    SCHEDULER_EXECUTION_ENGINE_REQUEST_BUILDER_SOURCE,
    SchedulerExecutionEngineRequestBuildInput,
    build_scheduler_execution_engine_request,
    scheduler_execution_engine_request_to_json_dict,
)


def make_input(
    **overrides: object,
) -> SchedulerExecutionEngineRequestBuildInput:
    values: dict[str, object] = {
        "task_key": "AT-P5B",
        "repo": "anderson930420/agent-taskflow",
        "local_repo_path": Path("/tmp/agent-taskflow"),
        "artifact_dir": Path("/tmp/agent-taskflow-artifacts/AT-P5B"),
        "executor": "pi",
    }
    values.update(overrides)
    return SchedulerExecutionEngineRequestBuildInput(**values)  # type: ignore[arg-type]


class SchedulerExecutionEngineRequestBuilderTests(unittest.TestCase):
    def test_valid_input_builds_request(self) -> None:
        request = build_scheduler_execution_engine_request(make_input())

        self.assertIsInstance(request, ExecutionEngineRequest)

    def test_source_is_scheduled_tick(self) -> None:
        request = build_scheduler_execution_engine_request(make_input())

        self.assertEqual(request.source, REQUEST_SOURCE_SCHEDULED_TICK)

    def test_core_fields_are_mapped(self) -> None:
        request = build_scheduler_execution_engine_request(
            make_input(dry_run=False, preflight=False)
        )

        self.assertEqual(request.task_key, "AT-P5B")
        self.assertEqual(request.project, "anderson930420/agent-taskflow")
        self.assertFalse(request.dry_run)
        self.assertFalse(request.preflight)

    def test_executor_profile_is_mapped(self) -> None:
        request = build_scheduler_execution_engine_request(
            make_input(
                executor="pi",
                model="claude-sonnet-4-6",
                provider="anthropic",
                tools=("git", "pytest"),
                pi_bin="/usr/local/bin/pi",
            )
        )

        profile = request.executor_profile
        self.assertEqual(profile.executor, "pi")
        self.assertEqual(profile.model, "claude-sonnet-4-6")
        self.assertEqual(profile.provider, "anthropic")
        self.assertEqual(profile.tools, ("git", "pytest"))
        self.assertEqual(profile.pi_bin, "/usr/local/bin/pi")

    def test_validator_profile_is_mapped(self) -> None:
        request = build_scheduler_execution_engine_request(
            make_input(validators=("pytest", "changed-files"))
        )

        self.assertEqual(
            request.validator_profile.validators,
            ("pytest", "changed-files"),
        )

    def test_workspace_is_mapped(self) -> None:
        request = build_scheduler_execution_engine_request(
            make_input(
                worktree_root=Path("/tmp/worktrees"),
                task_worktree_path=Path("/tmp/worktrees/AT-P5B"),
            )
        )

        workspace = request.workspace
        self.assertEqual(workspace.repo_path, Path("/tmp/agent-taskflow"))
        self.assertEqual(
            workspace.artifact_dir,
            Path("/tmp/agent-taskflow-artifacts/AT-P5B"),
        )
        self.assertEqual(workspace.worktree_root, Path("/tmp/worktrees"))
        self.assertEqual(
            workspace.task_worktree_path,
            Path("/tmp/worktrees/AT-P5B"),
        )

    def test_evidence_paths_are_mapped(self) -> None:
        request = build_scheduler_execution_engine_request(
            make_input(
                runtime_handoff_path=Path("/tmp/handoff.json"),
                verifier_report_path=Path("/tmp/verifier.json"),
            )
        )

        self.assertEqual(
            request.runtime_handoff_path, Path("/tmp/handoff.json")
        )
        self.assertEqual(
            request.verifier_report_path, Path("/tmp/verifier.json")
        )

    def test_evidence_paths_default_to_none(self) -> None:
        request = build_scheduler_execution_engine_request(make_input())

        self.assertIsNone(request.runtime_handoff_path)
        self.assertIsNone(request.verifier_report_path)

    def test_metadata_includes_builder_and_safety_markers(self) -> None:
        request = build_scheduler_execution_engine_request(
            make_input(confirmed=True)
        )

        metadata = request.metadata
        self.assertEqual(
            metadata["schema_version"],
            SCHEDULER_EXECUTION_ENGINE_REQUEST_BUILDER_SCHEMA_VERSION,
        )
        self.assertEqual(
            metadata["builder_source"],
            SCHEDULER_EXECUTION_ENGINE_REQUEST_BUILDER_SOURCE,
        )
        self.assertEqual(metadata["repo"], "anderson930420/agent-taskflow")
        self.assertIs(metadata["confirmed"], True)
        self.assertIs(metadata["publish_after_execution"], False)
        self.assertEqual(metadata["mode"], "execution_only")
        self.assertIs(metadata["execution_only"], True)
        self.assertIs(metadata["one_task_only"], True)
        self.assertIs(metadata["scheduler_tick"], True)

    def test_metadata_includes_selection_and_operator_fields(self) -> None:
        request = build_scheduler_execution_engine_request(
            make_input(
                selected_issue_number=42,
                selected_candidate_key="ISSUE-42",
                operator="anderson",
                operator_note="confirmed one-task run",
            )
        )

        metadata = request.metadata
        self.assertEqual(metadata["selected_issue_number"], 42)
        self.assertEqual(metadata["selected_candidate_key"], "ISSUE-42")
        self.assertEqual(metadata["operator"], "anderson")
        self.assertEqual(metadata["operator_note"], "confirmed one-task run")

    def test_metadata_omits_absent_optional_fields(self) -> None:
        request = build_scheduler_execution_engine_request(make_input())

        metadata = request.metadata
        self.assertNotIn("selected_issue_number", metadata)
        self.assertNotIn("selected_candidate_key", metadata)
        self.assertNotIn("operator", metadata)
        self.assertNotIn("operator_note", metadata)

    def test_blank_operator_fields_are_stripped_to_none(self) -> None:
        build_input = make_input(
            operator="  ",
            operator_note="\t",
            selected_candidate_key="",
        )

        self.assertIsNone(build_input.operator)
        self.assertIsNone(build_input.operator_note)
        self.assertIsNone(build_input.selected_candidate_key)

    def test_caller_metadata_is_copied_into_request_metadata(self) -> None:
        request = build_scheduler_execution_engine_request(
            make_input(metadata={"tick_id": "tick-7", "attempt": 1})
        )

        self.assertEqual(
            request.metadata["caller_metadata"],
            {"tick_id": "tick-7", "attempt": 1},
        )

    def test_caller_metadata_mutation_does_not_mutate_request(self) -> None:
        caller_metadata: dict[str, object] = {
            "tick_id": "tick-7",
            "labels": ["one-task"],
        }
        request = build_scheduler_execution_engine_request(
            make_input(metadata=caller_metadata)
        )

        caller_metadata["tick_id"] = "mutated"
        caller_metadata["labels"].append("mutated")  # type: ignore[union-attr]

        self.assertEqual(
            request.metadata["caller_metadata"],
            {"tick_id": "tick-7", "labels": ["one-task"]},
        )

    def test_tools_and_validators_normalize_to_tuples(self) -> None:
        build_input = make_input(
            tools=["git", "pytest"],
            validators=["pytest", "changed-files"],
        )
        request = build_scheduler_execution_engine_request(build_input)

        self.assertEqual(build_input.tools, ("git", "pytest"))
        self.assertEqual(
            build_input.validators, ("pytest", "changed-files")
        )
        self.assertEqual(
            request.executor_profile.tools, ("git", "pytest")
        )
        self.assertEqual(
            request.validator_profile.validators,
            ("pytest", "changed-files"),
        )

    def test_blank_task_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "task_key must not be empty"):
            make_input(task_key="  ")

    def test_invalid_repo_is_rejected(self) -> None:
        for repo in ("", "agent-taskflow", "/agent-taskflow", "owner/", "a/b/c"):
            with self.assertRaisesRegex(
                ValueError, "repo must be in owner/name form"
            ):
                make_input(repo=repo)

    def test_relative_local_repo_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "local_repo_path must be absolute"
        ):
            make_input(local_repo_path=Path("relative/repo"))

    def test_relative_artifact_dir_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "artifact_dir must be absolute"
        ):
            make_input(artifact_dir=Path("relative/artifacts"))

    def test_blank_executor_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "executor must not be empty"):
            make_input(executor="  ")

    def test_publish_after_execution_true_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "publish_after_execution must be False"
        ):
            make_input(publish_after_execution=True)

    def test_execution_only_false_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "execution_only must be True"
        ):
            make_input(execution_only=False)

    def test_request_serializes_to_json_dict(self) -> None:
        import json

        request = build_scheduler_execution_engine_request(
            make_input(
                selected_issue_number=42,
                metadata={"tick_id": "tick-7"},
            )
        )

        payload = scheduler_execution_engine_request_to_json_dict(request)

        self.assertIsInstance(payload, dict)
        # json.dumps raises if anything is not JSON-compatible.
        json.dumps(payload)
        self.assertEqual(payload["task_key"], "AT-P5B")
        self.assertEqual(payload["source"], REQUEST_SOURCE_SCHEDULED_TICK)

    def test_builder_does_not_touch_the_filesystem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_repo = Path(tmp) / "missing-repo"
            missing_artifacts = Path(tmp) / "missing-artifacts" / "AT-P5B"

            build_scheduler_execution_engine_request(
                make_input(
                    local_repo_path=missing_repo,
                    artifact_dir=missing_artifacts,
                )
            )

            self.assertFalse(missing_repo.exists())
            self.assertFalse(missing_artifacts.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])


class SchedulerExecutionEngineRequestBuilderPurityTests(unittest.TestCase):
    """The module must stay a pure builder with no runtime call surface."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = Path(builder_module.__file__).read_text(encoding="utf-8")
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

    def test_public_api_is_builder_only(self) -> None:
        self.assertEqual(
            set(builder_module.__all__),
            {
                "SCHEDULER_EXECUTION_ENGINE_REQUEST_BUILDER_SCHEMA_VERSION",
                "SCHEDULER_EXECUTION_ENGINE_REQUEST_BUILDER_SOURCE",
                "SchedulerExecutionEngineRequestBuildInput",
                "build_scheduler_execution_engine_request",
                "scheduler_execution_engine_request_to_json_dict",
            },
        )


if __name__ == "__main__":
    unittest.main()
