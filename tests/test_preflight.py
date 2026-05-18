"""Tests for real executor preflight checks."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.preflight import run_preflight


class PreflightTests(unittest.TestCase):
    def make_repo(self) -> tuple[tempfile.TemporaryDirectory[str], Path, dict[str, str]]:
        tmp = tempfile.TemporaryDirectory()
        repo_root = Path(tmp.name)
        repo_venv = repo_root / ".venv"
        repo_venv.mkdir()
        return tmp, repo_root, {"VIRTUAL_ENV": str(repo_venv)}

    def fake_find_spec(self, present: set[str]):
        def finder(name: str) -> object | None:
            return object() if name in present else None

        return finder

    def fake_which(self, present: dict[str, str] | None = None):
        values = present or {}

        def finder(name: str) -> str | None:
            return values.get(name)

        return finder

    def test_pytest_required_and_present_passes(self) -> None:
        tmp, repo_root, environ = self.make_repo()
        self.addCleanup(tmp.cleanup)

        result = run_preflight(
            validators=["pytest"],
            repo_root=repo_root,
            environ=environ,
            find_spec=self.fake_find_spec({"pytest", "fastapi", "uvicorn"}),
            which=self.fake_which(),
        )

        checks = {check.name: check for check in result.checks}
        self.assertTrue(result.ok)
        self.assertEqual(checks["pytest"].status, "passed")
        self.assertTrue(checks["pytest"].required)

    def test_pytest_required_and_missing_fails(self) -> None:
        tmp, repo_root, environ = self.make_repo()
        self.addCleanup(tmp.cleanup)

        result = run_preflight(
            validators=["pytest"],
            repo_root=repo_root,
            environ=environ,
            find_spec=self.fake_find_spec({"fastapi", "uvicorn"}),
            which=self.fake_which(),
        )

        checks = {check.name: check for check in result.checks}
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "failed")
        self.assertEqual(checks["pytest"].status, "failed")
        self.assertIn("pytest", result.missing_required)

    def test_pytest_not_selected_is_skipped(self) -> None:
        tmp, repo_root, environ = self.make_repo()
        self.addCleanup(tmp.cleanup)

        result = run_preflight(
            validators=["openspec"],
            repo_root=repo_root,
            environ=environ,
            find_spec=self.fake_find_spec({"fastapi", "uvicorn"}),
            which=self.fake_which({"openspec": "/usr/bin/openspec"}),
        )

        checks = {check.name: check for check in result.checks}
        self.assertEqual(checks["pytest"].status, "skipped")
        self.assertFalse(checks["pytest"].required)

    def test_openspec_missing_by_default_warns_without_failing(self) -> None:
        tmp, repo_root, environ = self.make_repo()
        self.addCleanup(tmp.cleanup)

        result = run_preflight(
            validators=["pytest", "openspec"],
            repo_root=repo_root,
            environ=environ,
            find_spec=self.fake_find_spec({"pytest", "fastapi", "uvicorn"}),
            which=self.fake_which(),
        )

        checks = {check.name: check for check in result.checks}
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "warning")
        self.assertEqual(checks["openspec"].status, "warning")
        self.assertFalse(checks["openspec"].required)
        self.assertIn("openspec", result.missing_optional)

    def test_openspec_missing_when_required_fails(self) -> None:
        tmp, repo_root, environ = self.make_repo()
        self.addCleanup(tmp.cleanup)

        result = run_preflight(
            validators=["pytest", "openspec"],
            require_openspec=True,
            repo_root=repo_root,
            environ=environ,
            find_spec=self.fake_find_spec({"pytest", "fastapi", "uvicorn"}),
            which=self.fake_which(),
        )

        checks = {check.name: check for check in result.checks}
        self.assertFalse(result.ok)
        self.assertEqual(checks["openspec"].status, "failed")
        self.assertTrue(checks["openspec"].required)
        self.assertIn("openspec", result.missing_required)

    def test_virtual_env_unset_warns_by_default(self) -> None:
        tmp, repo_root, _ = self.make_repo()
        self.addCleanup(tmp.cleanup)

        result = run_preflight(
            validators=["pytest"],
            repo_root=repo_root,
            environ={},
            find_spec=self.fake_find_spec({"pytest", "fastapi", "uvicorn"}),
            which=self.fake_which(),
        )

        checks = {check.name: check for check in result.checks}
        self.assertTrue(result.ok)
        self.assertEqual(checks["python_environment"].status, "warning")
        self.assertFalse(checks["python_environment"].required)

    def test_strict_mode_fails_when_not_using_repo_venv(self) -> None:
        tmp, repo_root, _ = self.make_repo()
        self.addCleanup(tmp.cleanup)

        result = run_preflight(
            validators=["pytest"],
            strict=True,
            repo_root=repo_root,
            environ={},
            find_spec=self.fake_find_spec({"pytest", "fastapi", "uvicorn"}),
            which=self.fake_which(),
        )

        checks = {check.name: check for check in result.checks}
        self.assertFalse(result.ok)
        self.assertEqual(checks["python_environment"].status, "failed")
        self.assertIn("python_environment", result.missing_required)

    def test_executor_pi_checks_availability_without_requiring_it_by_default(self) -> None:
        tmp, repo_root, environ = self.make_repo()
        self.addCleanup(tmp.cleanup)
        calls: list[str] = []

        def which(name: str) -> str | None:
            calls.append(name)
            return None

        result = run_preflight(
            validators=["pytest"],
            executor="pi",
            repo_root=repo_root,
            environ=environ,
            find_spec=self.fake_find_spec({"pytest", "fastapi", "uvicorn"}),
            which=which,
            pi_paths=(),
        )

        checks = {check.name: check for check in result.checks}
        self.assertIn("pi", calls)
        self.assertTrue(result.ok)
        self.assertEqual(checks["pi"].status, "warning")
        self.assertFalse(checks["pi"].required)

    def test_executor_opencode_checks_availability_without_requiring_it_by_default(self) -> None:
        tmp, repo_root, environ = self.make_repo()
        self.addCleanup(tmp.cleanup)
        calls: list[str] = []

        def which(name: str) -> str | None:
            calls.append(name)
            return None

        result = run_preflight(
            validators=["pytest"],
            executor="opencode",
            repo_root=repo_root,
            environ=environ,
            find_spec=self.fake_find_spec({"pytest", "fastapi", "uvicorn"}),
            which=which,
        )

        checks = {check.name: check for check in result.checks}
        self.assertIn("opencode", calls)
        self.assertTrue(result.ok)
        self.assertEqual(checks["opencode"].status, "warning")
        self.assertFalse(checks["opencode"].required)

    def test_output_includes_runtime_checks_missing_and_recommendations(self) -> None:
        tmp, repo_root, environ = self.make_repo()
        self.addCleanup(tmp.cleanup)

        result = run_preflight(
            validators=["pytest", "openspec"],
            repo_root=repo_root,
            environ=environ,
            find_spec=self.fake_find_spec({"pytest", "fastapi", "uvicorn"}),
            which=self.fake_which(),
        )
        data = result.to_dict()

        self.assertEqual(data["python"]["executable"], sys.executable)
        self.assertIn("version", data["python"])
        self.assertEqual(data["python"]["virtual_env"], environ["VIRTUAL_ENV"])
        self.assertIn("checks", data)
        self.assertIn("missing_required", data)
        self.assertIn("recommended_commands", data)
        self.assertIn("python3 scripts/run_local_validation.py", data["recommended_commands"])


if __name__ == "__main__":
    unittest.main()
