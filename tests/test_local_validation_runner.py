"""Tests for scripts/run_local_validation.py helper behavior."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_local_validation.py"


def _load_runner_module():
    from agent_taskflow.cli import local_validation as module

    return module


class LocalValidationRunnerTests(unittest.TestCase):
    def test_repo_checkout_detection_accepts_source_tree_shape(self) -> None:
        runner = _load_runner_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            (root / "agent_taskflow").mkdir()
            (root / "scripts").mkdir()
            (root / "tests").mkdir()

            self.assertTrue(runner.is_repo_checkout(root))

    def test_repo_checkout_detection_rejects_site_packages_shape(self) -> None:
        runner = _load_runner_module()
        with tempfile.TemporaryDirectory() as tmp:
            site_packages = Path(tmp) / "site-packages"
            site_packages.mkdir()

            self.assertFalse(runner.is_repo_checkout(site_packages))

    def test_main_returns_clear_error_outside_repo_checkout(self) -> None:
        runner = _load_runner_module()
        with tempfile.TemporaryDirectory() as tmp:
            site_packages = Path(tmp) / "site-packages"
            site_packages.mkdir()
            stderr = io.StringIO()

            with mock.patch.object(runner, "REPO_ROOT", site_packages):
                with contextlib.redirect_stderr(stderr):
                    rc = runner.main([])

        self.assertEqual(rc, 2)
        self.assertIn("repository checkout", stderr.getvalue())
        self.assertIn(str(site_packages), stderr.getvalue())

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
                "workflow contract validation",
                "workflow policy validation",
                "Mission Control golden path smoke",
                "PiExecutor golden path smoke (fake Pi)",
                "unit tests",
                "compileall",
            ],
        )
        for check in checks:
            self.assertEqual(check.command[0], "/tmp/project/.venv/bin/python")

        workflow_command = checks[0].command
        self.assertEqual(
            workflow_command,
            [
                "/tmp/project/.venv/bin/python",
                "scripts/validate_workflow_contract.py",
            ],
        )

        policy_command = checks[1].command
        self.assertEqual(
            policy_command,
            [
                "/tmp/project/.venv/bin/python",
                "scripts/validate_workflow_policy.py",
            ],
        )

        pi_command = checks[3].command
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
