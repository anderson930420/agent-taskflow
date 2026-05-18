"""Tests for scripts/run_real_executor_preflight.py."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from agent_taskflow.preflight import PreflightCheck, PreflightResult
from scripts import run_real_executor_preflight as script


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_real_executor_preflight.py"


def make_result(*, ok: bool = True) -> PreflightResult:
    status = "passed" if ok else "failed"
    missing_required = () if ok else ("pytest",)
    return PreflightResult(
        ok=ok,
        status=status,
        strict=False,
        executor="pi",
        validators=("pytest", "openspec"),
        python={
            "executable": "/tmp/repo/.venv/bin/python",
            "version": "3.x",
            "virtual_env": "/tmp/repo/.venv",
            "repo_venv": "/tmp/repo/.venv",
            "using_repo_venv": True,
            "active_venv_exists": True,
        },
        checks=(
            PreflightCheck(
                name="pytest",
                kind="python_import",
                required=True,
                status=status,
                summary="pytest check",
            ),
        ),
        missing_required=missing_required,
        missing_optional=(),
    )


class RunRealExecutorPreflightScriptTests(unittest.TestCase):
    def test_help_succeeds(self) -> None:
        parser = script.build_parser()

        with self.assertRaises(SystemExit) as raised:
            with contextlib.redirect_stdout(io.StringIO()):
                parser.parse_args(["--help"])

        self.assertEqual(raised.exception.code, 0)

    def test_default_command_emits_valid_json(self) -> None:
        with mock.patch.object(script, "run_preflight", return_value=make_result(ok=True)):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = script.main([])

        self.assertEqual(exit_code, 0)
        data = json.loads(stdout.getvalue())
        self.assertTrue(data["ok"])
        self.assertEqual(data["validators"], ["pytest", "openspec"])

    def test_missing_pytest_exits_nonzero(self) -> None:
        with mock.patch.object(script, "run_preflight", return_value=make_result(ok=False)):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = script.main(["--validators", "pytest"])

        self.assertEqual(exit_code, 1)
        data = json.loads(stdout.getvalue())
        self.assertFalse(data["ok"])
        self.assertIn("pytest", data["missing_required"])

    def test_validators_pytest_requires_pytest_by_default(self) -> None:
        with mock.patch.object(script, "run_preflight", return_value=make_result(ok=True)) as run:
            with contextlib.redirect_stdout(io.StringIO()):
                script.main(["--validators", "pytest"])

        self.assertEqual(run.call_args.kwargs["validators"], "pytest")
        self.assertIsNone(run.call_args.kwargs["require_pytest"])

    def test_validators_openspec_does_not_require_openspec_by_default(self) -> None:
        with mock.patch.object(script, "run_preflight", return_value=make_result(ok=True)) as run:
            with contextlib.redirect_stdout(io.StringIO()):
                script.main(["--validators", "openspec"])

        self.assertEqual(run.call_args.kwargs["validators"], "openspec")
        self.assertFalse(run.call_args.kwargs["require_openspec"])

    def test_require_openspec_is_forwarded(self) -> None:
        with mock.patch.object(script, "run_preflight", return_value=make_result(ok=False)) as run:
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = script.main(["--validators", "openspec", "--require-openspec"])

        self.assertEqual(exit_code, 1)
        self.assertTrue(run.call_args.kwargs["require_openspec"])

    def test_static_safety_constraints(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8").lower()
        module_text = (REPO_ROOT / "agent_taskflow" / "preflight.py").read_text(
            encoding="utf-8"
        ).lower()
        combined = text + "\n" + module_text

        forbidden = [
            "pip install",
            "subprocess.run",
            "dispatcher",
            "git push",
            "gh pr create",
            "gh pr merge",
            "git merge",
            "git rebase",
            "git reset",
            "cleanup",
            "shell=true",
        ]
        for needle in forbidden:
            with self.subTest(needle=needle):
                self.assertNotIn(needle, combined)

        self.assertNotIn("subprocess", combined)


if __name__ == "__main__":
    unittest.main()
