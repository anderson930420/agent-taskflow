"""Tests for the lightweight WORKFLOW.md contract parser."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from agent_taskflow.workflow_contract import load_workflow_contract


REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT_WORKFLOW = REPO_ROOT / "WORKFLOW.md"


VALID_WORKFLOW_TEXT = """# Example Workflow Contract

## Purpose

This repo uses deterministic Python orchestration code with bounded
implementation workers.

## Component Ownership

Deterministic components own scheduling, execution wrapping, validation, and
review gates.

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

No self-approval or automatic merge.

## Future Machine-Readable Contract

A schema may be added later.
"""


class WorkflowContractTests(unittest.TestCase):
    def test_root_workflow_loads_successfully(self) -> None:
        contract = load_workflow_contract(ROOT_WORKFLOW)

        self.assertEqual(contract.source_path, ROOT_WORKFLOW)
        self.assertIn("agent-taskflow Workflow Contract", contract.raw_text)
        self.assertTrue(contract.has_deterministic_orchestration_boundary)

    def test_root_workflow_validates_successfully(self) -> None:
        contract = load_workflow_contract(ROOT_WORKFLOW)
        result = contract.validate()

        self.assertTrue(result.passed)
        self.assertEqual(result.errors, ())

    def test_missing_file_raises_clear_error(self) -> None:
        missing = REPO_ROOT / "missing-WORKFLOW.md"

        with self.assertRaisesRegex(FileNotFoundError, "WORKFLOW.md not found"):
            load_workflow_contract(missing)

    def test_missing_required_section_fails_validation(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "WORKFLOW.md"
            path.write_text(
                VALID_WORKFLOW_TEXT.replace("## Validation Policy\n", ""),
                encoding="utf-8",
            )

            result = load_workflow_contract(path).validate()

        self.assertFalse(result.passed)
        self.assertIn(
            "Missing required WORKFLOW.md section: Validation Policy",
            result.errors,
        )

    def test_parser_does_not_require_external_dependencies(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "WORKFLOW.md"
            path.write_text(VALID_WORKFLOW_TEXT, encoding="utf-8")

            contract = load_workflow_contract(path)

        self.assertTrue(contract.validate().passed)

    def test_parser_does_not_execute_shell_commands(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "WORKFLOW.md"
            path.write_text(VALID_WORKFLOW_TEXT, encoding="utf-8")

            with mock.patch.object(subprocess, "run") as run:
                contract = load_workflow_contract(path)
                result = contract.validate()

        run.assert_not_called()
        self.assertTrue(result.passed)

    def test_parser_preserves_raw_text(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "WORKFLOW.md"
            path.write_text(VALID_WORKFLOW_TEXT, encoding="utf-8")

            contract = load_workflow_contract(path)

        self.assertEqual(contract.raw_text, VALID_WORKFLOW_TEXT)

    def test_parser_reports_source_path(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "WORKFLOW.md"
            path.write_text(VALID_WORKFLOW_TEXT, encoding="utf-8")

            result = load_workflow_contract(path).validate()

        self.assertEqual(result.source_path, path)


if __name__ == "__main__":
    unittest.main()
