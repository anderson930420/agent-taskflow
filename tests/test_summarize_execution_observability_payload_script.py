"""Tests for the P4-e read-only observability normalization CLI."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "summarize_execution_observability_payload.py"


def load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "summarize_execution_observability_payload_under_test", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCRIPT_MODULE = load_script_module()


ENGINE_PAYLOAD = {
    "ok": True,
    "task_key": "AT-GH-700",
    "status": "dry_run",
    "summary": "Approved task runner preview.",
    "next_operator_action": "operator_review",
    "safety": {"human_review_required": True, "executor_started": False},
    "steps": [{"name": "executor", "status": "skipped"}],
    "artifacts": [],
    "metadata": {"runner_dry_run": True},
}

SCHEDULER_PAYLOAD = {
    "ok": True,
    "status": "execution_completed",
    "mode": "confirmed",
    "selected_task_key": "AT-GH-701",
    "runner_config": {
        "executor": "pi",
        "validators": ["pytest"],
        "worktree_root": "/tmp/wt",
    },
    "publication_config": {"publish_after_execution": False, "mode": "execution_only"},
    "safety": {"scheduled_tick": True, "one_task_only": True, "dry_run": False},
}


def run_main(argv: list[str], stdin_text: str | None = None) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    stdin = io.StringIO(stdin_text or "")
    with mock.patch.object(sys, "stdin", stdin):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = SCRIPT_MODULE.main(argv)
    return code, out.getvalue(), err.getvalue()


class StdinNormalizationTests(unittest.TestCase):
    def test_stdin_manual_engine_facade(self) -> None:
        code, out, _ = run_main(
            ["--source", "manual_engine_facade", "--json"],
            stdin_text=json.dumps(ENGINE_PAYLOAD),
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["source"], "manual_engine_facade")
        self.assertEqual(payload["task_key"], "AT-GH-700")
        self.assertEqual(payload["status"], "dry_run")
        self.assertEqual(payload["metadata"]["result_type"], "ExecutionEngineResult")
        # Compact JSON is a single line.
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_default_source_is_manual_engine_facade(self) -> None:
        code, out, _ = run_main([], stdin_text=json.dumps(ENGINE_PAYLOAD))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["source"], "manual_engine_facade")


class FileNormalizationTests(unittest.TestCase):
    def test_file_scheduler_tick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tick.json"
            path.write_text(json.dumps(SCHEDULER_PAYLOAD), encoding="utf-8")
            code, out, _ = run_main(
                ["--source", "scheduler_tick", "--input", str(path), "--json"]
            )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["source"], "scheduler_tick")
        self.assertEqual(payload["task_key"], "AT-GH-701")
        self.assertEqual(payload["mode"], "confirmed")
        self.assertEqual(payload["publication_mode"], "execution_only")
        self.assertEqual(payload["profile"]["executor"], "pi")

    def test_missing_file_returns_nonzero(self) -> None:
        code, _, err = run_main(
            ["--source", "scheduler_tick", "--input", "/no/such/file.json"]
        )
        self.assertEqual(code, 1)
        self.assertIn("error", err.lower())


class OutputModeTests(unittest.TestCase):
    def test_pretty_json_is_valid(self) -> None:
        code, out, _ = run_main(
            ["--pretty"], stdin_text=json.dumps(ENGINE_PAYLOAD)
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["task_key"], "AT-GH-700")
        # Pretty JSON spans multiple lines.
        self.assertGreater(len(out.strip().splitlines()), 1)

    def test_text_output_includes_task_key_status_source(self) -> None:
        code, out, _ = run_main(
            ["--text"], stdin_text=json.dumps(ENGINE_PAYLOAD)
        )
        self.assertEqual(code, 0)
        self.assertIn("source", out)
        self.assertIn("manual_engine_facade", out)
        self.assertIn("task_key", out)
        self.assertIn("AT-GH-700", out)
        self.assertIn("status", out)
        self.assertIn("dry_run", out)


class ErrorHandlingTests(unittest.TestCase):
    def test_invalid_json_returns_nonzero(self) -> None:
        code, out, err = run_main([], stdin_text="this is not json")
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), "")
        self.assertIn("not valid json", err.lower())

    def test_invalid_source_rejected_by_argparse(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--source", "merge_and_publish"],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            input="{}",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice", result.stderr.lower())


class SafetyFlagTests(unittest.TestCase):
    def test_parser_has_no_mutation_flags(self) -> None:
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

    def test_source_choices_are_read_only_summarizers(self) -> None:
        parser = SCRIPT_MODULE.build_parser()
        source_action = next(
            action for action in parser._actions if action.dest == "source"
        )
        self.assertEqual(
            sorted(source_action.choices),
            ["approved_task_runner", "manual_engine_facade", "scheduler_tick"],
        )

    def test_help_states_read_only_safety(self) -> None:
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
        help_text = result.stdout.lower()
        self.assertIn("read-only", help_text)
        self.assertIn("human review remains the final gate", help_text)


if __name__ == "__main__":
    unittest.main()
