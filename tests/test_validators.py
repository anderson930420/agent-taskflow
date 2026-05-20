from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_taskflow.validators import (
    OpenSpecValidator,
    PytestValidator,
    ValidatorContext,
    ValidatorResult,
    get_validator,
    list_validator_names,
)


class ValidatorTestCase(unittest.TestCase):
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
            task_key=" AT-0006 ",
            project="agent-taskflow",
            worktree_path=worktree_path,
            artifact_dir=artifact_dir,
            timeout_seconds=timeout_seconds,
        )


class ValidatorContextTests(ValidatorTestCase):
    def test_validator_context_accepts_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            self.assertEqual(context.task_key, "AT-0006")
            self.assertEqual(context.project, "agent-taskflow")
            self.assertTrue(context.worktree_path.is_absolute())
            self.assertTrue(context.artifact_dir.is_absolute())

    def test_validator_context_rejects_relative_worktree_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()

            with self.assertRaisesRegex(ValueError, "worktree_path must be absolute"):
                ValidatorContext(
                    task_key="AT-0006",
                    project="agent-taskflow",
                    worktree_path=Path("relative-worktree"),
                    artifact_dir=artifact_dir,
                )

    def test_validator_context_rejects_relative_artifact_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree_path = Path(tmp) / "worktree"
            worktree_path.mkdir()

            with self.assertRaisesRegex(ValueError, "artifact_dir must be absolute"):
                ValidatorContext(
                    task_key="AT-0006",
                    project="agent-taskflow",
                    worktree_path=worktree_path,
                    artifact_dir=Path("relative-artifacts"),
                )

    def test_validator_context_rejects_secret_like_env_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree_path = Path(tmp) / "worktree"
            artifact_dir = Path(tmp) / "artifacts"
            worktree_path.mkdir()
            artifact_dir.mkdir()

            with self.assertRaisesRegex(ValueError, "secret-like key"):
                ValidatorContext(
                    task_key="AT-0006",
                    project="agent-taskflow",
                    worktree_path=worktree_path,
                    artifact_dir=artifact_dir,
                    env={"API_TOKEN": "should-not-be-stored"},
                )

    def test_validator_context_rejects_empty_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree_path = Path(tmp) / "worktree"
            artifact_dir = Path(tmp) / "artifacts"
            worktree_path.mkdir()
            artifact_dir.mkdir()

            with self.assertRaisesRegex(ValueError, "project must not be empty"):
                ValidatorContext(
                    task_key="AT-0006",
                    project="   ",
                    worktree_path=worktree_path,
                    artifact_dir=artifact_dir,
                )

    def test_validator_context_rejects_non_positive_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                ValueError,
                "timeout_seconds must be positive when provided",
            ):
                self.make_context(Path(tmp), timeout_seconds=0)

    def test_validator_context_rejects_non_string_env_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree_path = Path(tmp) / "worktree"
            artifact_dir = Path(tmp) / "artifacts"
            worktree_path.mkdir()
            artifact_dir.mkdir()

            with self.assertRaisesRegex(TypeError, "env value for 'FOO' must be a string"):
                ValidatorContext(
                    task_key="AT-0006",
                    project="agent-taskflow",
                    worktree_path=worktree_path,
                    artifact_dir=artifact_dir,
                    env={"FOO": 123},  # type: ignore[dict-item]
                )

    def test_validator_context_preserves_valid_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree_path = Path(tmp) / "worktree"
            artifact_dir = Path(tmp) / "artifacts"
            worktree_path.mkdir()
            artifact_dir.mkdir()

            context = ValidatorContext(
                task_key="AT-0006",
                project="agent-taskflow",
                worktree_path=worktree_path,
                artifact_dir=artifact_dir,
                env={"FOO": "bar", " BAZ ": "qux"},
            )

            self.assertEqual(context.env, {"FOO": "bar", "BAZ": "qux"})


class ValidatorResultTests(unittest.TestCase):
    def test_validator_result_rejects_relative_log_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "log_path must be absolute"):
            ValidatorResult(
                validator="pytest",
                status="passed",
                log_path=Path("relative.log"),
            )

    def test_validator_result_rejects_relative_artifact_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "artifacts\\[log\\] must be absolute"):
            ValidatorResult(
                validator="pytest",
                status="passed",
                artifacts={"log": Path("relative.log")},
            )

    def test_validator_result_rejects_invalid_status(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid validator result status"):
            ValidatorResult(
                validator="pytest",
                status="waiting_for_review",
            )


class PytestValidatorTests(ValidatorTestCase):
    def test_pytest_validator_uses_expected_command_and_worktree_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            validator = PytestValidator()

            def side_effect(command, **kwargs):
                stdout = kwargs.get("stdout")
                if stdout is not None:
                    stdout.write("pytest mock output\n")
                return subprocess.CompletedProcess(args=command, returncode=0)

            with patch(
                "agent_taskflow.validators.pytest.subprocess.run",
                side_effect=side_effect,
            ) as run_mock:
                result = validator.run(context)

            self.assertEqual(result.status, "passed")
            self.assertEqual(
                run_mock.call_args.args[0],
                [sys.executable, "-m", "pytest"],
            )
            self.assertEqual(run_mock.call_args.kwargs["cwd"], context.worktree_path)
            self.assertEqual(run_mock.call_args.kwargs["shell"], False)

    def test_pytest_validator_writes_stdout_stderr_to_pytest_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            def side_effect(command, **kwargs):
                stdout = kwargs.get("stdout")
                if stdout is not None:
                    stdout.write("mock stdout and stderr\n")
                return subprocess.CompletedProcess(args=command, returncode=0)

            with patch(
                "agent_taskflow.validators.pytest.subprocess.run",
                side_effect=side_effect,
            ):
                result = PytestValidator().run(context)

            self.assertEqual(result.status, "passed")
            self.assertIsNotNone(result.log_path)
            assert result.log_path is not None
            self.assertEqual(result.log_path, context.artifact_dir / "pytest.log")
            self.assertTrue(result.log_path.exists())
            log_text = result.log_path.read_text(encoding="utf-8")
            self.assertIn(
                f"Command: {[sys.executable, '-m', 'pytest']!r}",
                log_text,
            )
            self.assertIn("Environment: not logged", log_text)
            self.assertIn("mock stdout and stderr", log_text)

    def test_pytest_validator_exit_code_zero_returns_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            with patch(
                "agent_taskflow.validators.pytest.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["python3", "-m", "pytest"],
                    returncode=0,
                ),
            ):
                result = PytestValidator().run(context)

            self.assertEqual(result.status, "passed")
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.artifacts["log"], result.log_path)

    def test_pytest_validator_nonzero_exit_code_returns_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            with patch(
                "agent_taskflow.validators.pytest.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["python3", "-m", "pytest"],
                    returncode=7,
                ),
            ):
                result = PytestValidator().run(context)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.exit_code, 7)
            self.assertIn("exit code 7", result.summary or "")

    def test_pytest_validator_timeout_returns_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), timeout_seconds=1)

            with patch(
                "agent_taskflow.validators.pytest.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    cmd=["python3", "-m", "pytest"],
                    timeout=1,
                ),
            ):
                result = PytestValidator().run(context)

            self.assertEqual(result.status, "failed")
            self.assertIsNone(result.exit_code)
            self.assertIsNotNone(result.log_path)
            assert result.log_path is not None
            self.assertIn("timed out", result.log_path.read_text(encoding="utf-8"))

    def test_pytest_validator_command_missing_returns_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            with patch(
                "agent_taskflow.validators.pytest.subprocess.run",
                side_effect=FileNotFoundError("python3 missing"),
            ):
                result = PytestValidator().run(context)

            self.assertEqual(result.status, "blocked")
            self.assertIsNone(result.exit_code)
            self.assertIn("failed to start", result.summary or "")

    def test_pytest_validator_extra_args_are_appended(self) -> None:
        validator = PytestValidator(extra_args=["tests", "-q"])

        self.assertEqual(
            validator.command,
            [sys.executable, "-m", "pytest", "tests", "-q"],
        )

    def test_pytest_validator_defaults_to_sys_executable(self) -> None:
        # The default interpreter must be the one driving the orchestration
        # process so the validator inherits the project's .venv pytest
        # install instead of resolving "python3" against PATH.
        validator = PytestValidator()
        self.assertEqual(validator.python_bin, sys.executable)
        self.assertEqual(validator.command, [sys.executable, "-m", "pytest"])

    def test_pytest_validator_explicit_python_bin_override_is_preserved(
        self,
    ) -> None:
        validator = PytestValidator(python_bin="custom-python")
        self.assertEqual(validator.python_bin, "custom-python")
        self.assertEqual(
            validator.command,
            ["custom-python", "-m", "pytest"],
        )

    def test_pytest_validator_registry_default_uses_sys_executable(self) -> None:
        validator = get_validator("pytest")
        self.assertIsInstance(validator, PytestValidator)
        assert isinstance(validator, PytestValidator)  # for type-checker
        self.assertEqual(validator.python_bin, sys.executable)

    def test_pytest_validator_registry_explicit_python_bin_is_preserved(
        self,
    ) -> None:
        validator = get_validator("pytest", python_bin="custom-python")
        assert isinstance(validator, PytestValidator)
        self.assertEqual(validator.python_bin, "custom-python")


class OpenSpecValidatorTests(ValidatorTestCase):
    def test_openspec_validator_skips_when_openspec_directory_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            result = OpenSpecValidator().run(context)

            self.assertEqual(result.status, "skipped")
            self.assertIsNone(result.log_path)
            self.assertEqual(result.artifacts, {})
            self.assertEqual(result.summary, "openspec directory not found")

    def test_openspec_validator_uses_expected_command_and_worktree_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            (context.worktree_path / "openspec").mkdir()

            def side_effect(command, **kwargs):
                stdout = kwargs.get("stdout")
                if stdout is not None:
                    stdout.write("openspec mock output\n")
                return subprocess.CompletedProcess(args=command, returncode=0)

            with patch(
                "agent_taskflow.validators.openspec.subprocess.run",
                side_effect=side_effect,
            ) as run_mock:
                result = OpenSpecValidator().run(context)

            self.assertEqual(result.status, "passed")
            self.assertEqual(
                run_mock.call_args.args[0],
                ["openspec", "validate", "--all", "--no-interactive"],
            )
            self.assertEqual(run_mock.call_args.kwargs["cwd"], context.worktree_path)
            self.assertEqual(run_mock.call_args.kwargs["shell"], False)

    def test_openspec_validator_writes_stdout_stderr_to_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            (context.worktree_path / "openspec").mkdir()

            def side_effect(command, **kwargs):
                stdout = kwargs.get("stdout")
                if stdout is not None:
                    stdout.write("mock openspec output\n")
                return subprocess.CompletedProcess(args=command, returncode=0)

            with patch(
                "agent_taskflow.validators.openspec.subprocess.run",
                side_effect=side_effect,
            ):
                result = OpenSpecValidator().run(context)

            self.assertEqual(result.status, "passed")
            self.assertIsNotNone(result.log_path)
            assert result.log_path is not None
            self.assertEqual(
                result.log_path,
                context.artifact_dir / "openspec-validate.log",
            )
            self.assertTrue(result.log_path.exists())
            log_text = result.log_path.read_text(encoding="utf-8")
            self.assertIn(
                "Command: ['openspec', 'validate', '--all', '--no-interactive']",
                log_text,
            )
            self.assertIn("Environment: not logged", log_text)
            self.assertIn("mock openspec output", log_text)

    def test_openspec_validator_exit_code_zero_returns_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            (context.worktree_path / "openspec").mkdir()

            with patch(
                "agent_taskflow.validators.openspec.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["openspec", "validate", "--all", "--no-interactive"],
                    returncode=0,
                ),
            ):
                result = OpenSpecValidator().run(context)

            self.assertEqual(result.status, "passed")
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.artifacts["log"], result.log_path)

    def test_openspec_validator_nonzero_exit_code_returns_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            (context.worktree_path / "openspec").mkdir()

            with patch(
                "agent_taskflow.validators.openspec.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["openspec", "validate", "--all", "--no-interactive"],
                    returncode=9,
                ),
            ):
                result = OpenSpecValidator().run(context)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.exit_code, 9)
            self.assertIn("exit code 9", result.summary or "")

    def test_openspec_validator_timeout_returns_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), timeout_seconds=1)
            (context.worktree_path / "openspec").mkdir()

            with patch(
                "agent_taskflow.validators.openspec.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    cmd=["openspec", "validate", "--all", "--no-interactive"],
                    timeout=1,
                ),
            ):
                result = OpenSpecValidator().run(context)

            self.assertEqual(result.status, "failed")
            self.assertIsNone(result.exit_code)
            self.assertIsNotNone(result.log_path)
            assert result.log_path is not None
            self.assertIn("timed out", result.log_path.read_text(encoding="utf-8"))

    def test_openspec_validator_command_missing_returns_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            (context.worktree_path / "openspec").mkdir()

            with patch(
                "agent_taskflow.validators.openspec.subprocess.run",
                side_effect=FileNotFoundError("openspec missing"),
            ):
                result = OpenSpecValidator().run(context)

            self.assertEqual(result.status, "blocked")
            self.assertIsNone(result.exit_code)
            self.assertIn("failed to start", result.summary or "")


class ValidatorRegistryTests(unittest.TestCase):
    def test_registry_lists_pytest_and_openspec(self) -> None:
        names = list_validator_names()

        self.assertIn("pytest", names)
        self.assertIn("openspec", names)

    def test_registry_returns_pytest_validator(self) -> None:
        self.assertIsInstance(get_validator("pytest"), PytestValidator)

    def test_registry_returns_openspec_validator(self) -> None:
        self.assertIsInstance(get_validator("openspec"), OpenSpecValidator)

    def test_registry_rejects_unknown_validator(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown validator"):
            get_validator("missing-validator")


if __name__ == "__main__":
    unittest.main()
