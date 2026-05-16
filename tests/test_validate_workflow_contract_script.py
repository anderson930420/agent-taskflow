"""Tests for scripts/validate_workflow_contract.py."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "validate_workflow_contract.py"


VALID_WORKFLOW_TEXT = """# Example Workflow Contract

## Purpose

This repo uses deterministic Python orchestration code with bounded
implementation workers.

## Component Ownership

Component ownership is explicit.

## Task Lifecycle

queued -> running -> validating -> waiting_approval -> approved / rejected / blocked

## Workspace Policy

Each run should use an isolated workspace.

## Executor Policy

Executor adapters are deterministic wrappers.

## Validation Policy

Validators are proof-of-work gates.

## Changed-Files / Path Policy

Changed files are audited.

## Proof-of-Work Artifacts

Run evidence is preserved.

## Human Review Gate

Humans approve, reject, rerun, or block.

## Non-Goals

No self-approval.

## Future Machine-Readable Contract

A schema may be added later.
"""


def _load_script_module():
    spec = importlib.util.spec_from_file_location("validate_workflow_contract", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_main(argv: list[str]) -> tuple[int, str]:
    module = _load_script_module()
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = module.main(argv)
    return exit_code, stdout.getvalue()


class ValidateWorkflowContractScriptTests(unittest.TestCase):
    def test_valid_root_workflow_exits_zero(self) -> None:
        exit_code, output = _run_main([])

        self.assertEqual(exit_code, 0)
        self.assertIn("source path: WORKFLOW.md", output)
        self.assertIn("status: passed", output)

    def test_missing_required_section_exits_nonzero(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "WORKFLOW.md"
            path.write_text(
                VALID_WORKFLOW_TEXT.replace("## Validation Policy\n", ""),
                encoding="utf-8",
            )

            exit_code, output = _run_main([str(path)])

        self.assertNotEqual(exit_code, 0)
        self.assertIn("status: failed", output)
        self.assertIn("Missing required WORKFLOW.md section: Validation Policy", output)

    def test_missing_file_exits_nonzero(self) -> None:
        missing = REPO_ROOT / "does-not-exist-WORKFLOW.md"

        exit_code, output = _run_main([str(missing)])

        self.assertNotEqual(exit_code, 0)
        self.assertIn("status: failed", output)
        self.assertIn("WORKFLOW.md not found", output)

    def test_optional_path_argument_works(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "CUSTOM-WORKFLOW.md"
            path.write_text(VALID_WORKFLOW_TEXT, encoding="utf-8")

            exit_code, output = _run_main([str(path)])

        self.assertEqual(exit_code, 0)
        self.assertIn(f"source path: {path}", output)
        self.assertIn("status: passed", output)

    def test_output_includes_source_path_and_status(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "WORKFLOW.md"
            path.write_text(VALID_WORKFLOW_TEXT, encoding="utf-8")

            exit_code, output = _run_main([str(path)])

        self.assertEqual(exit_code, 0)
        self.assertIn(f"source path: {path}", output)
        self.assertIn("status: passed", output)

    def test_script_does_not_execute_external_commands(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "WORKFLOW.md"
            path.write_text(VALID_WORKFLOW_TEXT, encoding="utf-8")

            with mock.patch.object(subprocess, "run") as run:
                exit_code, output = _run_main([str(path)])

        run.assert_not_called()
        self.assertEqual(exit_code, 0)
        self.assertIn("status: passed", output)


if __name__ == "__main__":
    unittest.main()
