"""Tests for scripts/run_local_validation.py helper behavior."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_local_validation.py"


def _load_runner_module():
    spec = importlib.util.spec_from_file_location("run_local_validation", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LocalValidationRunnerTests(unittest.TestCase):
    def test_dependency_import_detection_reports_missing_dependency(self) -> None:
        runner = _load_runner_module()

        with mock.patch.object(runner, "import_dependency") as import_dependency:
            import_dependency.side_effect = [
                (True, None),
                (False, "No module named 'uvicorn'"),
            ]

            ok, missing = runner.check_required_dependencies(["fastapi", "uvicorn"])

        self.assertFalse(ok)
        self.assertEqual(missing, ["uvicorn: No module named 'uvicorn'"])

    def test_openspec_detection_uses_path_lookup(self) -> None:
        runner = _load_runner_module()

        with mock.patch.object(runner.shutil, "which", return_value="/usr/bin/openspec"):
            self.assertEqual(runner.find_openspec(), "/usr/bin/openspec")

    def test_command_list_construction_uses_python_executable_and_fake_pi(self) -> None:
        runner = _load_runner_module()

        checks = runner.build_required_checks("/tmp/project/.venv/bin/python")

        self.assertEqual(
            [check.name for check in checks],
            [
                "Mission Control golden path smoke",
                "PiExecutor golden path smoke (fake Pi)",
                "unit tests",
                "compileall",
            ],
        )
        for check in checks:
            self.assertEqual(check.command[0], "/tmp/project/.venv/bin/python")

        pi_command = checks[1].command
        self.assertEqual(
            pi_command,
            [
                "/tmp/project/.venv/bin/python",
                "scripts/run_pi_executor_golden_path_smoke.py",
                "--keep-workspace",
            ],
        )
        self.assertNotIn("--real-pi", pi_command)
        self.assertNotIn("--confirm-real-pi", pi_command)

    def test_openspec_check_is_skipped_when_unavailable(self) -> None:
        runner = _load_runner_module()

        check = runner.build_openspec_check(openspec_path="")

        self.assertEqual(check.status, "skipped")
        self.assertEqual(check.return_code, None)
        self.assertFalse(check.required)
        self.assertIn("not available", check.reason)

    def test_openspec_check_runs_when_available(self) -> None:
        runner = _load_runner_module()

        check = runner.build_openspec_check(openspec_path="/usr/bin/openspec")

        self.assertEqual(check.name, "openspec validate")
        self.assertEqual(check.command, ["openspec", "validate", "--all", "--no-interactive"])
        self.assertFalse(check.required)

    def test_nonzero_exit_decision_only_counts_required_failures(self) -> None:
        runner = _load_runner_module()

        optional_failure = runner.CheckResult(
            name="openspec validate",
            command=["openspec", "validate", "--all", "--no-interactive"],
            status="failed",
            return_code=1,
            required=False,
        )
        required_pass = runner.CheckResult(
            name="unit tests",
            command=["python", "-m", "unittest"],
            status="passed",
            return_code=0,
            required=True,
        )
        required_failure = runner.CheckResult(
            name="compileall",
            command=["python", "-m", "compileall"],
            status="failed",
            return_code=1,
            required=True,
        )

        self.assertFalse(runner.should_exit_nonzero([optional_failure, required_pass]))
        self.assertTrue(runner.should_exit_nonzero([required_failure]))

    def test_script_does_not_add_forbidden_behavior_or_frontend_surface(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8").lower()

        self.assertNotIn("github", text)
        self.assertNotIn("pull request", text)
        self.assertNotIn("merge", text)
        self.assertNotIn("push", text)
        self.assertNotIn("cleanup", text)
        self.assertNotIn("delete", text)
        self.assertNotIn("mission-control/", text)
        self.assertNotIn("shell=true", text)


if __name__ == "__main__":
    unittest.main()
