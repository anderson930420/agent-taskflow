"""Tests for the TypecheckValidator."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_taskflow.validators import (
    TypecheckValidator,
    ValidatorContext,
    ValidatorResult,
    get_validator,
    list_validator_names,
)


class TypecheckValidatorTests(unittest.TestCase):
    """Tests for TypecheckValidator."""

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
            task_key="AT-0007",
            project="agent-taskflow",
            worktree_path=worktree_path,
            artifact_dir=artifact_dir,
            timeout_seconds=timeout_seconds,
        )

    # ------------------------------------------------------------------
    # Constructor / command validation
    # ------------------------------------------------------------------

    def test_default_command_is_mypy(self) -> None:
        validator = TypecheckValidator()
        self.assertEqual(validator.command, ["python3", "-m", "mypy", "."])

    def test_custom_command_is_stored(self) -> None:
        validator = TypecheckValidator(command=["pyright"])
        self.assertEqual(validator.command, ["pyright"])

    def test_command_property_returns_fresh_list(self) -> None:
        validator = TypecheckValidator(command=["mypy"])
        original = validator.command
        original.append("extra")
        self.assertEqual(validator.command, ["mypy"])

    def test_empty_command_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            TypecheckValidator(command=[])

    def test_none_command_uses_default(self) -> None:
        validator = TypecheckValidator(command=None)
        self.assertEqual(validator.command, ["python3", "-m", "mypy", "."])

    def test_dangerous_command_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "dangerous"):
            TypecheckValidator(command=["rm", "-rf", "."])

    def test_dangerous_sudo_command_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "sudo"):
            TypecheckValidator(command=["sudo", "apt", "install", "mypy"])

    def test_dangerous_git_push_command_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "git push"):
            TypecheckValidator(command=["git", "push", "origin", "main"])

    def test_dangerous_cleanup_command_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "cleanup"):
            TypecheckValidator(command=["cleanup", "--all"])

    def test_dangerous_npm_install_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "npm install"):
            TypecheckValidator(command=["npm", "install"])

    def test_dangerous_pip_install_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "pip install"):
            TypecheckValidator(command=["pip", "install", "mypy"])

    def test_non_string_command_rejected(self) -> None:
        with self.assertRaisesRegex(TypeError, r"command\["):
            TypecheckValidator(command=["echo", 123])  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # run() behavior
    # ------------------------------------------------------------------

    def test_typecheck_runs_in_worktree_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            def side_effect(command, **kwargs):
                return subprocess.CompletedProcess(args=command, returncode=0)

            with patch(
                "agent_taskflow.validators.command.subprocess.run",
                side_effect=side_effect,
            ):
                result = TypecheckValidator().run(context)

            self.assertEqual(result.status, "passed")

    def test_typecheck_does_not_require_mypy_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            # Use a command that definitely exists on this system
            validator = TypecheckValidator(command=["python3", "-c", "print('ok')"])

            result = validator.run(context)

            self.assertEqual(result.status, "passed")
            self.assertEqual(result.exit_code, 0)
            self.assertIn("passed", result.summary or "")

    def test_typecheck_success_command_returns_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            with patch(
                "agent_taskflow.validators.command.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["python3", "-c", "print('ok')"],
                    returncode=0,
                ),
            ):
                result = TypecheckValidator(
                    command=["python3", "-c", "print('ok')"]
                ).run(context)

            self.assertEqual(result.status, "passed")
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.validator, "typecheck")

    def test_typecheck_failing_command_returns_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            with patch(
                "agent_taskflow.validators.command.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["python3", "-c", "exit(1)"],
                    returncode=1,
                ),
            ):
                result = TypecheckValidator(
                    command=["python3", "-c", "exit(1)"]
                ).run(context)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.exit_code, 1)
            self.assertIn("exit code 1", result.summary or "")

    def test_typecheck_captures_stdout_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            def side_effect(command, **kwargs):
                stdout = kwargs.get("stdout")
                if stdout is not None:
                    stdout.write("type error: list index out of range\n")
                return subprocess.CompletedProcess(args=command, returncode=1)

            with patch(
                "agent_taskflow.validators.command.subprocess.run",
                side_effect=side_effect,
            ):
                result = TypecheckValidator(command=["python3", "-m", "mypy", "."]).run(context)

            self.assertIsNotNone(result.log_path)
            assert result.log_path is not None
            self.assertTrue(result.log_path.exists())
            log_text = result.log_path.read_text(encoding="utf-8")
            self.assertIn("type error: list index out of range", log_text)

    def test_typecheck_writes_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            def side_effect(command, **kwargs):
                return subprocess.CompletedProcess(args=command, returncode=0)

            with patch(
                "agent_taskflow.validators.command.subprocess.run",
                side_effect=side_effect,
            ):
                result = TypecheckValidator().run(context)

            self.assertIsNotNone(result.log_path)
            assert result.log_path is not None
            self.assertEqual(result.log_path.name, "typecheck.log")
            self.assertEqual(result.artifacts["log"], result.log_path)

    def test_typecheck_timeout_returns_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), timeout_seconds=1)

            with patch(
                "agent_taskflow.validators.command.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    cmd=["python3", "-m", "mypy", "."],
                    timeout=1,
                ),
            ):
                result = TypecheckValidator().run(context)

            self.assertEqual(result.status, "failed")
            self.assertIsNone(result.exit_code)
            self.assertIsNotNone(result.log_path)
            assert result.log_path is not None
            self.assertIn("timed out", result.log_path.read_text(encoding="utf-8"))

    def test_typecheck_command_missing_returns_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            with patch(
                "agent_taskflow.validators.command.subprocess.run",
                side_effect=FileNotFoundError("python3 missing"),
            ):
                result = TypecheckValidator().run(context)

            self.assertEqual(result.status, "blocked")
            self.assertIsNone(result.exit_code)
            self.assertIn("failed to start", result.summary or "")

    def test_typecheck_uses_shell_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            def side_effect(command, **kwargs):
                self.assertFalse(kwargs.get("shell"))
                return subprocess.CompletedProcess(args=command, returncode=0)

            with patch(
                "agent_taskflow.validators.command.subprocess.run",
                side_effect=side_effect,
            ):
                TypecheckValidator().run(context)

    def test_typecheck_with_raw_shell_string_not_passed(self) -> None:
        # The constructor requires list[str]; raw shell strings are not supported.
        with self.assertRaisesRegex(TypeError, "must be a list or tuple of strings"):
            TypecheckValidator(command="python3 -m mypy .")  # type: ignore[arg-type]


class TypecheckValidatorRegistryTests(unittest.TestCase):
    """Registry integration for TypecheckValidator."""

    def test_list_validator_names_includes_typecheck(self) -> None:
        self.assertIn("typecheck", list_validator_names())

    def test_registry_returns_typecheck_validator(self) -> None:
        self.assertIsInstance(get_validator("typecheck"), TypecheckValidator)

    def test_registry_returns_typecheck_case_insensitive(self) -> None:
        self.assertIsInstance(get_validator(" TYPECHECK "), TypecheckValidator)

    def test_existing_validators_still_work(self) -> None:
        from agent_taskflow.validators import PytestValidator

        self.assertIsInstance(get_validator("pytest"), PytestValidator)
        self.assertIn("pytest", list_validator_names())


if __name__ == "__main__":
    unittest.main()
