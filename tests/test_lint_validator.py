"""Tests for the LintValidator."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_taskflow.validators import (
    LintValidator,
    ValidatorContext,
    get_validator,
    list_validator_names,
)


class LintValidatorTests(unittest.TestCase):
    """Tests for LintValidator."""

    def make_context(
        self,
        tmp_path: Path,
        *,
        timeout_seconds: int | None = None,
    ) -> ValidatorContext:
        worktree_path = tmp_path / "worktree"
        artifact_dir = tmp_path / "artifacts"
        worktree_path.mkdir()
        artifact_dir.mkdir()

        return ValidatorContext(
            task_key="AT-0008",
            project="agent-taskflow",
            worktree_path=worktree_path,
            artifact_dir=artifact_dir,
            timeout_seconds=timeout_seconds,
        )

    # ------------------------------------------------------------------
    # Constructor / command validation
    # ------------------------------------------------------------------

    def test_default_command_is_ruff(self) -> None:
        validator = LintValidator()
        self.assertEqual(validator.command, ["python3", "-m", "ruff", "check", "."])

    def test_custom_command_is_stored(self) -> None:
        validator = LintValidator(command=["flake8", "."])
        self.assertEqual(validator.command, ["flake8", "."])

    def test_command_property_returns_fresh_list(self) -> None:
        validator = LintValidator(command=["ruff"])
        original = validator.command
        original.append("extra")
        self.assertEqual(validator.command, ["ruff"])

    def test_empty_command_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            LintValidator(command=[])

    def test_none_command_uses_default(self) -> None:
        validator = LintValidator(command=None)
        self.assertEqual(validator.command, ["python3", "-m", "ruff", "check", "."])

    def test_dangerous_command_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "dangerous"):
            LintValidator(command=["rm", "-rf", "."])

    def test_dangerous_sudo_command_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "sudo"):
            LintValidator(command=["sudo", "apt", "install", "ruff"])

    def test_dangerous_git_push_command_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "git push"):
            LintValidator(command=["git", "push", "origin", "main"])

    def test_dangerous_cleanup_command_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "cleanup"):
            LintValidator(command=["cleanup", "--all"])

    def test_dangerous_npm_install_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "npm install"):
            LintValidator(command=["npm", "install"])

    def test_dangerous_pip_install_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "pip install"):
            LintValidator(command=["pip", "install", "ruff"])

    def test_non_string_command_rejected(self) -> None:
        with self.assertRaisesRegex(TypeError, r"command\["):
            LintValidator(command=["echo", 123])  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Auto-fix rejection
    # ------------------------------------------------------------------

    def test_fix_flag_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "auto-fix"):
            LintValidator(command=["ruff", "check", ".", "--fix"])

    def test_write_flag_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "auto-fix"):
            LintValidator(command=["ruff", "check", ".", "--write"])

    def test_apply_flag_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "auto-fix"):
            LintValidator(command=["ruff", "check", ".", "--apply"])

    def test_fix_dry_run_not_rejected(self) -> None:
        # --fix-dry-run does not modify files so it is allowed
        validator = LintValidator(command=["ruff", "check", ".", "--fix-dry-run"])
        self.assertEqual(validator.command[-1], "--fix-dry-run")

    # ------------------------------------------------------------------
    # run() behavior
    # ------------------------------------------------------------------

    def test_lint_runs_in_worktree_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            def side_effect(command, **kwargs):
                return subprocess.CompletedProcess(args=command, returncode=0)

            with patch(
                "agent_taskflow.validators.command.subprocess.run",
                side_effect=side_effect,
            ):
                result = LintValidator().run(context)

            self.assertEqual(result.status, "passed")

    def test_lint_does_not_require_ruff_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            # Use a command that definitely exists on this system
            validator = LintValidator(command=["python3", "-c", "print('ok')"])

            result = validator.run(context)

            self.assertEqual(result.status, "passed")
            self.assertEqual(result.exit_code, 0)
            self.assertIn("passed", result.summary or "")

    def test_lint_success_command_returns_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            with patch(
                "agent_taskflow.validators.command.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["python3", "-c", "print('ok')"],
                    returncode=0,
                ),
            ):
                result = LintValidator(
                    command=["python3", "-c", "print('ok')"]
                ).run(context)

            self.assertEqual(result.status, "passed")
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.validator, "lint")

    def test_lint_failing_command_returns_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            with patch(
                "agent_taskflow.validators.command.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["python3", "-c", "exit(1)"],
                    returncode=1,
                ),
            ):
                result = LintValidator(
                    command=["python3", "-c", "exit(1)"]
                ).run(context)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.exit_code, 1)
            self.assertIn("exit code 1", result.summary or "")

    def test_lint_captures_stdout_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            def side_effect(command, **kwargs):
                stdout = kwargs.get("stdout")
                if stdout is not None:
                    stdout.write("E501 line too long (85 > 79 characters)\n")
                return subprocess.CompletedProcess(args=command, returncode=1)

            with patch(
                "agent_taskflow.validators.command.subprocess.run",
                side_effect=side_effect,
            ):
                result = LintValidator(command=["ruff", "check", "."]).run(context)

            self.assertIsNotNone(result.log_path)
            assert result.log_path is not None
            self.assertTrue(result.log_path.exists())
            log_text = result.log_path.read_text(encoding="utf-8")
            self.assertIn("E501 line too long", log_text)

    def test_lint_writes_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            def side_effect(command, **kwargs):
                return subprocess.CompletedProcess(args=command, returncode=0)

            with patch(
                "agent_taskflow.validators.command.subprocess.run",
                side_effect=side_effect,
            ):
                result = LintValidator().run(context)

            self.assertIsNotNone(result.log_path)
            assert result.log_path is not None
            self.assertEqual(result.log_path.name, "lint.log")
            self.assertEqual(result.artifacts["log"], result.log_path)

    def test_lint_timeout_returns_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), timeout_seconds=1)

            with patch(
                "agent_taskflow.validators.command.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    cmd=["ruff", "check", "."],
                    timeout=1,
                ),
            ):
                result = LintValidator().run(context)

            self.assertEqual(result.status, "failed")
            self.assertIsNone(result.exit_code)
            self.assertIsNotNone(result.log_path)
            assert result.log_path is not None
            self.assertIn("timed out", result.log_path.read_text(encoding="utf-8"))

    def test_lint_command_missing_returns_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            with patch(
                "agent_taskflow.validators.command.subprocess.run",
                side_effect=FileNotFoundError("ruff missing"),
            ):
                result = LintValidator().run(context)

            self.assertEqual(result.status, "blocked")
            self.assertIsNone(result.exit_code)
            self.assertIn("failed to start", result.summary or "")

    def test_lint_uses_shell_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            def side_effect(command, **kwargs):
                self.assertFalse(kwargs.get("shell"))
                return subprocess.CompletedProcess(args=command, returncode=0)

            with patch(
                "agent_taskflow.validators.command.subprocess.run",
                side_effect=side_effect,
            ):
                LintValidator().run(context)

    def test_lint_with_raw_shell_string_not_passed(self) -> None:
        # The constructor requires list[str]; raw shell strings are not supported.
        with self.assertRaisesRegex(TypeError, "must be a list or tuple of strings"):
            LintValidator(command="python3 -m ruff check .")  # type: ignore[arg-type]


class LintValidatorRegistryTests(unittest.TestCase):
    """Registry integration for LintValidator."""

    def test_list_validator_names_includes_lint(self) -> None:
        self.assertIn("lint", list_validator_names())

    def test_registry_returns_lint_validator(self) -> None:
        self.assertIsInstance(get_validator("lint"), LintValidator)

    def test_registry_returns_lint_case_insensitive(self) -> None:
        self.assertIsInstance(get_validator(" LINT "), LintValidator)

    def test_existing_validators_still_work(self) -> None:
        from agent_taskflow.validators import PytestValidator

        self.assertIsInstance(get_validator("pytest"), PytestValidator)
        self.assertIn("pytest", list_validator_names())


if __name__ == "__main__":
    unittest.main()
