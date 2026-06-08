"""Tests for the P4-d opt-in ExecutionEngine facade CLI."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_taskflow.execution_engine_contract import ExecutionEngineResult


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_execution_engine_approved_task.py"


def load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "run_execution_engine_approved_task_under_test", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCRIPT_MODULE = load_script_module()

REQUIRED_ARGS = [
    "--task-key",
    "AT-GH-900",
    "--repo-path",
    "/tmp/agent-taskflow",
    "--artifact-dir",
    "/tmp/agent-taskflow-artifacts/AT-GH-900",
]


def run_main(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = SCRIPT_MODULE.main(argv)
    return code, buffer.getvalue()


def fake_result(**overrides: Any) -> ExecutionEngineResult:
    values: dict[str, Any] = {
        "ok": True,
        "task_key": "AT-GH-900",
        "status": "dry_run",
        "summary": "Approved task runner preview.",
        "next_operator_action": "operator_review",
    }
    values.update(overrides)
    return ExecutionEngineResult(**values)


class HelpAndFlagTests(unittest.TestCase):
    def test_help_includes_confirm_flag(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--confirm-execution-engine-run", result.stdout)

    def test_help_exposes_conservative_safety_language(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )
        help_text = result.stdout.lower()

        self.assertIn("dry-run is the default", help_text)
        self.assertIn("does not approve, merge", help_text)
        self.assertIn("mutate github", help_text)
        self.assertIn("human review remains the final gate", help_text)

    def test_parser_has_no_destructive_action_flags(self) -> None:
        parser = SCRIPT_MODULE.build_parser()
        options: list[str] = []
        for action in parser._actions:
            options.extend(action.option_strings)
        joined = " ".join(options).lower()

        forbidden = (
            "merge",
            "cleanup",
            "clean-up",
            "archive",
            "closeout",
            "close-out",
            "close-issue",
            "issue-close",
            "create-pr",
            "pr-create",
            "draft-pr",
            "publish",
            "push",
            "delete-branch",
            "branch-delete",
            "delete-worktree",
            "worktree-delete",
            "approve",
        )
        for token in forbidden:
            self.assertNotIn(token, joined, f"unexpected flag fragment: {token}")


class DryRunPathTests(unittest.TestCase):
    def test_default_dry_run_does_not_require_confirmation(self) -> None:
        captured: dict[str, Any] = {}

        def fake_run(request: Any) -> ExecutionEngineResult:
            captured["request"] = request
            return fake_result()

        with mock.patch.object(
            SCRIPT_MODULE, "run_manual_execution_engine_request", side_effect=fake_run
        ) as patched:
            code, out = run_main([*REQUIRED_ARGS, "--json"])

        self.assertEqual(code, 0)
        patched.assert_called_once()
        # Default request is dry-run and routed through the facade.
        self.assertTrue(captured["request"].dry_run)
        self.assertEqual(captured["request"].source, "manual")
        payload = json.loads(out)
        self.assertEqual(payload["status"], "dry_run")
        self.assertTrue(payload["ok"])

    def test_json_output_is_valid(self) -> None:
        with mock.patch.object(
            SCRIPT_MODULE,
            "run_manual_execution_engine_request",
            return_value=fake_result(),
        ):
            code, out = run_main([*REQUIRED_ARGS, "--json"])

        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["task_key"], "AT-GH-900")
        self.assertEqual(payload["status"], "dry_run")
        # Compact JSON: single line.
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_pretty_json_output_is_valid(self) -> None:
        with mock.patch.object(
            SCRIPT_MODULE,
            "run_manual_execution_engine_request",
            return_value=fake_result(),
        ):
            code, out = run_main([*REQUIRED_ARGS, "--json", "--pretty"])

        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["task_key"], "AT-GH-900")
        # Pretty JSON spans multiple lines.
        self.assertGreater(len(out.strip().splitlines()), 1)

    def test_text_output_includes_status_task_key_and_next_action(self) -> None:
        with mock.patch.object(
            SCRIPT_MODULE,
            "run_manual_execution_engine_request",
            return_value=fake_result(
                status="dry_run", next_operator_action="operator_review"
            ),
        ):
            code, out = run_main(list(REQUIRED_ARGS))

        self.assertEqual(code, 0)
        self.assertIn("status", out)
        self.assertIn("dry_run", out)
        self.assertIn("task key", out)
        self.assertIn("AT-GH-900", out)
        self.assertIn("next action", out)
        self.assertIn("operator_review", out)


class ConfirmationGateTests(unittest.TestCase):
    def test_non_dry_run_without_confirmation_is_blocked(self) -> None:
        with mock.patch.object(
            SCRIPT_MODULE, "run_manual_execution_engine_request"
        ) as patched:
            code, out = run_main([*REQUIRED_ARGS, "--no-dry-run", "--json"])

        self.assertEqual(code, 1)
        patched.assert_not_called()
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertIn(
            "--confirm-execution-engine-run",
            json.dumps(payload),
        )
        safety = payload["safety"]
        self.assertTrue(safety["human_review_required"])
        self.assertFalse(safety["approved"])
        self.assertFalse(safety["merged"])
        self.assertFalse(safety["github_mutated"])
        self.assertFalse(safety["cleanup_performed"])
        self.assertFalse(safety["branch_deleted"])
        self.assertFalse(safety["worktree_deleted"])

    def test_non_dry_run_with_confirmation_calls_adapter(self) -> None:
        captured: dict[str, Any] = {}

        def fake_run(request: Any) -> ExecutionEngineResult:
            captured["request"] = request
            return fake_result(status="waiting_approval", ok=True)

        with mock.patch.object(
            SCRIPT_MODULE, "run_manual_execution_engine_request", side_effect=fake_run
        ) as patched:
            code, out = run_main(
                [
                    *REQUIRED_ARGS,
                    "--no-dry-run",
                    "--confirm-execution-engine-run",
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        patched.assert_called_once()
        self.assertFalse(captured["request"].dry_run)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "waiting_approval")
        self.assertTrue(payload["ok"])


class ObservabilitySummaryTests(unittest.TestCase):
    """P4-f opt-in UnifiedExecutionSummary emission tests."""

    SCHEMA_VERSION = "execution_observability_summary.v1"
    SOURCE = "manual_engine_facade"

    def _help_text(self) -> str:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_help_includes_include_observability_summary(self) -> None:
        self.assertIn("--include-observability-summary", self._help_text())

    def test_help_includes_observability_summary_only(self) -> None:
        self.assertIn("--observability-summary-only", self._help_text())

    def test_default_json_output_keeps_engine_result_shape(self) -> None:
        with mock.patch.object(
            SCRIPT_MODULE,
            "run_manual_execution_engine_request",
            return_value=fake_result(),
        ):
            code, out = run_main([*REQUIRED_ARGS, "--json"])

        self.assertEqual(code, 0)
        payload = json.loads(out)
        # Original ExecutionEngineResult shape, no observability wrapping.
        self.assertEqual(payload["task_key"], "AT-GH-900")
        self.assertEqual(payload["status"], "dry_run")
        self.assertNotIn("execution_engine_result", payload)
        self.assertNotIn("observability_summary", payload)

    def test_include_observability_summary_json_emits_both(self) -> None:
        with mock.patch.object(
            SCRIPT_MODULE,
            "run_manual_execution_engine_request",
            return_value=fake_result(),
        ):
            code, out = run_main(
                [*REQUIRED_ARGS, "--include-observability-summary", "--json"]
            )

        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertIn("execution_engine_result", payload)
        self.assertIn("observability_summary", payload)
        # The raw result is the unchanged contract serialization.
        self.assertEqual(payload["execution_engine_result"]["task_key"], "AT-GH-900")
        self.assertEqual(payload["execution_engine_result"]["status"], "dry_run")

        summary = payload["observability_summary"]
        self.assertEqual(summary["schema_version"], self.SCHEMA_VERSION)
        self.assertEqual(summary["source"], self.SOURCE)
        self.assertEqual(summary["task_key"], "AT-GH-900")

    def test_observability_summary_only_json_emits_only_summary(self) -> None:
        with mock.patch.object(
            SCRIPT_MODULE,
            "run_manual_execution_engine_request",
            return_value=fake_result(),
        ):
            code, out = run_main(
                [*REQUIRED_ARGS, "--observability-summary-only", "--json"]
            )

        self.assertEqual(code, 0)
        payload = json.loads(out)
        # Only the unified summary, not the wrapped/raw shapes.
        self.assertNotIn("execution_engine_result", payload)
        self.assertNotIn("observability_summary", payload)
        self.assertEqual(payload["schema_version"], self.SCHEMA_VERSION)
        self.assertEqual(payload["source"], self.SOURCE)
        self.assertEqual(payload["task_key"], "AT-GH-900")
        self.assertTrue(payload["ok"])

    def test_observability_summary_only_implies_json(self) -> None:
        with mock.patch.object(
            SCRIPT_MODULE,
            "run_manual_execution_engine_request",
            return_value=fake_result(),
        ):
            code, out = run_main([*REQUIRED_ARGS, "--observability-summary-only"])

        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["schema_version"], self.SCHEMA_VERSION)
        # Compact JSON: single line by default.
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_observability_summary_only_pretty_is_valid_json(self) -> None:
        with mock.patch.object(
            SCRIPT_MODULE,
            "run_manual_execution_engine_request",
            return_value=fake_result(),
        ):
            code, out = run_main(
                [*REQUIRED_ARGS, "--observability-summary-only", "--pretty"]
            )

        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["source"], self.SOURCE)
        # Pretty JSON spans multiple lines.
        self.assertGreater(len(out.strip().splitlines()), 1)

    def test_non_dry_run_without_confirm_summary_only_is_blocked(self) -> None:
        with mock.patch.object(
            SCRIPT_MODULE, "run_manual_execution_engine_request"
        ) as patched:
            code, out = run_main(
                [
                    *REQUIRED_ARGS,
                    "--no-dry-run",
                    "--observability-summary-only",
                ]
            )

        self.assertEqual(code, 1)
        patched.assert_not_called()
        payload = json.loads(out)
        # A blocked result, normalized into the unified summary shape.
        self.assertEqual(payload["schema_version"], self.SCHEMA_VERSION)
        self.assertEqual(payload["source"], self.SOURCE)
        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["task_key"], "AT-GH-900")

    def test_text_output_with_include_observability_summary(self) -> None:
        with mock.patch.object(
            SCRIPT_MODULE,
            "run_manual_execution_engine_request",
            return_value=fake_result(),
        ):
            code, out = run_main(
                [*REQUIRED_ARGS, "--include-observability-summary"]
            )

        self.assertEqual(code, 0)
        # Existing text summary is preserved.
        self.assertIn("status", out)
        self.assertIn("AT-GH-900", out)
        # Plus the read-only observability section.
        self.assertIn("observability summary", out)
        self.assertIn("source", out)
        self.assertIn(self.SOURCE, out)
        self.assertIn("schema_version", out)

    def test_new_flags_expose_no_destructive_commands(self) -> None:
        parser = SCRIPT_MODULE.build_parser()
        new_flags: list[str] = []
        for action in parser._actions:
            if action.dest in (
                "include_observability_summary",
                "observability_summary_only",
            ):
                new_flags.extend(action.option_strings)

        # Both opt-in flags are present.
        self.assertIn("--include-observability-summary", new_flags)
        self.assertIn("--observability-summary-only", new_flags)

        joined = " ".join(new_flags).lower()
        forbidden = (
            "merge",
            "cleanup",
            "clean-up",
            "archive",
            "closeout",
            "close-out",
            "close-issue",
            "issue-close",
            "create-pr",
            "pr-create",
            "draft-pr",
            "publish",
            "push",
            "delete-branch",
            "branch-delete",
            "delete-worktree",
            "worktree-delete",
            "approve",
        )
        for token in forbidden:
            self.assertNotIn(token, joined, f"unexpected flag fragment: {token}")


if __name__ == "__main__":
    unittest.main()
