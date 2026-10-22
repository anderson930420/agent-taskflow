"""Tests for the CompileValidator (the fast syntax gate)."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_taskflow.execution_policy import parse_execution_policy
from agent_taskflow.validators import (
    CompileValidator,
    ValidatorContext,
    get_validator,
    list_validator_names,
)
from agent_taskflow.validators.compile import COMPILE_EXCLUDE_REGEX

_BROKEN = "def broken(:\n    pass\n"


def _source_files(root: Path) -> dict[str, str]:
    """Every file outside ``__pycache__``, with its content."""
    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


class CompileValidatorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="compile-validator-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.worktree = self.root / "worktree"
        self.artifacts = self.root / "artifacts"
        self.worktree.mkdir()
        self.artifacts.mkdir()

    def context(self, *, timeout_seconds: int | None = 60) -> ValidatorContext:
        return ValidatorContext(
            task_key="AT-0042",
            project="agent-taskflow",
            worktree_path=self.worktree,
            artifact_dir=self.artifacts,
            timeout_seconds=timeout_seconds,
        )

    def write(self, relative: str, text: str) -> Path:
        path = self.worktree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class CompileValidatorCommandTests(CompileValidatorTestCase):
    def test_command_shape(self) -> None:
        validator = CompileValidator()
        self.assertEqual(validator.name, "compile")
        self.assertEqual(
            validator.command,
            [sys.executable, "-I", "-m", "compileall", "-q", "-f", "-x", COMPILE_EXCLUDE_REGEX, "."],
        )

    def test_python_bin_selects_only_the_interpreter(self) -> None:
        validator = CompileValidator(python_bin=" /opt/platform/bin/python3 ")
        self.assertEqual(validator.command[0], "/opt/platform/bin/python3")
        self.assertEqual(validator.command[1:], CompileValidator().command[1:])

    def test_empty_python_bin_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "python_bin"):
            CompileValidator(python_bin="  ")

    def test_command_property_returns_fresh_list(self) -> None:
        validator = CompileValidator()
        validator.command.append("--extra")
        self.assertNotIn("--extra", validator.command)

    def test_exclude_regex_matches_whole_path_components_only(self) -> None:
        rx = re.compile(COMPILE_EXCLUDE_REGEX)
        for excluded in (
            "./.git/hooks/x.py",
            "./node_modules/pkg/x.py",
            "./web/node_modules/pkg/x.py",
            "./.venv/lib/python3.12/site-packages/x.py",
            "./.worktrees/task-1/x.py",
        ):
            self.assertIsNotNone(rx.search(excluded), excluded)
        for included in (
            "./agent_taskflow/x.py",
            "./my.venv/x.py",
            "./node_modules_extra/x.py",
            "./.github/x.py",
            "./.gitignore_helpers.py",
            "./.worktrees_notes/x.py",
        ):
            self.assertIsNone(rx.search(included), included)

    def test_uses_shell_false_in_the_worktree(self) -> None:
        seen: dict[str, object] = {}

        def side_effect(command, **kwargs):
            seen.update(kwargs, command=command)
            return subprocess.CompletedProcess(args=command, returncode=0)

        with patch("agent_taskflow.validators.command.subprocess.run", side_effect=side_effect):
            CompileValidator().run(self.context())

        self.assertFalse(seen["shell"])
        self.assertEqual(seen["cwd"], self.worktree)
        self.assertEqual(seen["command"], CompileValidator().command)


class CompileValidatorRunTests(CompileValidatorTestCase):
    def test_passes_on_a_clean_tree(self) -> None:
        self.write("pkg/__init__.py", "")
        self.write("pkg/module.py", "def ok() -> int:\n    return 1\n")
        self.write("scripts/tool.py", "print('never executed by the gate')\n")

        result = CompileValidator().run(self.context())

        self.assertEqual(result.status, "passed", result.log_path.read_text(encoding="utf-8"))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.validator, "compile")
        self.assertEqual(result.summary, "Compile validation passed.")
        self.assertEqual(result.log_path, self.artifacts / "compile.log")
        self.assertEqual(result.artifacts, {"log": self.artifacts / "compile.log"})
        log = result.log_path.read_text(encoding="utf-8")
        self.assertIn("Validator: compile", log)
        self.assertIn("compileall", log)
        self.assertNotIn("never executed by the gate", log)

    def test_fails_on_a_syntax_error(self) -> None:
        self.write("pkg/module.py", "x = 1\n")
        self.write("pkg/broken.py", _BROKEN)

        result = CompileValidator().run(self.context())

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("exit code 1", result.summary or "")
        self.assertIn("compile.log", result.summary or "")
        log = result.log_path.read_text(encoding="utf-8")
        self.assertIn("broken.py", log)
        self.assertIn("SyntaxError", log)

    def test_excluded_directories_are_skipped(self) -> None:
        self.write("pkg/module.py", "x = 1\n")
        for excluded in (".git", "node_modules", ".venv", ".worktrees", "web/node_modules"):
            self.write(f"{excluded}/nested/broken.py", _BROKEN)

        result = CompileValidator().run(self.context())

        self.assertEqual(result.status, "passed", result.log_path.read_text(encoding="utf-8"))

    def test_similarly_named_directories_are_still_checked(self) -> None:
        self.write("node_modules_extra/broken.py", _BROKEN)

        result = CompileValidator().run(self.context())

        self.assertEqual(result.status, "failed")
        self.assertIn("node_modules_extra", result.log_path.read_text(encoding="utf-8"))

    def test_a_worktree_inside_dot_worktrees_is_still_checked(self) -> None:
        # Paths are matched relative to the worktree, so a Ticket worktree that
        # itself lives under .worktrees/ is not excluded wholesale.
        self.worktree = self.root / ".worktrees" / "task-at-0042"
        self.worktree.mkdir(parents=True)
        self.write("pkg/broken.py", _BROKEN)

        result = CompileValidator().run(self.context())

        self.assertEqual(result.status, "failed")

    def test_a_worktree_module_cannot_shadow_the_compiler(self) -> None:
        # Without -I, `python -m compileall` would import this file in place of
        # the standard library's py_compile and report a pass.
        self.write("py_compile.py", "raise SystemExit(0)\n")
        self.write("compileall.py", "raise SystemExit(0)\n")
        self.write("pkg/broken.py", _BROKEN)

        result = CompileValidator().run(self.context())

        self.assertEqual(result.status, "failed")
        self.assertIn("broken.py", result.log_path.read_text(encoding="utf-8"))

    def test_a_stale_bytecode_cache_cannot_hide_a_syntax_error(self) -> None:
        # Without -f, compileall skips a file whose __pycache__ entry records the
        # same mtime and size, so a planted or stale .pyc would mask the error.
        source = self.write("pkg/module.py", "x = 1\n")
        self.assertEqual(CompileValidator().run(self.context()).status, "passed")
        stat = source.stat()
        source.write_text("x = (\n", encoding="utf-8")  # same size, now broken
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))

        result = CompileValidator().run(self.context())

        self.assertEqual(result.status, "failed")
        self.assertIn("module.py", result.log_path.read_text(encoding="utf-8"))

    def test_writes_no_source_file(self) -> None:
        self.write("pkg/__init__.py", "")
        self.write("pkg/module.py", "x = 1\n")
        self.write("README.md", "docs\n")
        before = _source_files(self.worktree)

        result = CompileValidator().run(self.context())

        self.assertEqual(result.status, "passed")
        self.assertEqual(_source_files(self.worktree), before)

    def test_timeout_returns_failed(self) -> None:
        with patch(
            "agent_taskflow.validators.command.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["python"], timeout=1),
        ):
            result = CompileValidator().run(self.context(timeout_seconds=1))

        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.exit_code)
        self.assertIn("timed out", result.log_path.read_text(encoding="utf-8"))

    def test_missing_interpreter_returns_blocked(self) -> None:
        validator = CompileValidator(python_bin=str(self.root / "no-such-python"))

        result = validator.run(self.context())

        self.assertEqual(result.status, "blocked")
        self.assertIsNone(result.exit_code)
        self.assertIn("failed to start", result.summary or "")


class CompileValidatorRegistryTests(unittest.TestCase):
    def test_list_validator_names_includes_compile(self) -> None:
        self.assertIn("compile", list_validator_names())

    def test_registry_returns_compile_validator(self) -> None:
        self.assertIsInstance(get_validator("compile"), CompileValidator)
        self.assertIsInstance(get_validator(" COMPILE "), CompileValidator)

    def test_registry_passes_python_bin(self) -> None:
        validator = get_validator("compile", python_bin="/usr/bin/python3")
        self.assertEqual(validator.command[0], "/usr/bin/python3")

    def test_existing_validators_still_listed(self) -> None:
        for name in ("pytest", "openspec", "policy", "changed-files", "typecheck", "lint"):
            self.assertIn(name, list_validator_names())

    def test_execution_policy_accepts_compile_for_both_phases(self) -> None:
        block = {
            "executor": "claude-code",
            "argv": ["claude", "--print", "--model", "{model}", "--effort", "{effort}"],
            "model": "model-x",
            "effort": "low",
            "timeout_seconds": 60,
            "permission_profile": "test",
            "policy_version": "test-1",
            "implementation_validators": [{"name": "compile", "timeout_seconds": 300}],
            "integration_validators": [
                {
                    "name": "compile",
                    "command": ["python3", "-I", "-m", "compileall", "-q", "."],
                    "timeout_seconds": 300,
                }
            ],
        }
        policy = parse_execution_policy("demo", block, registry_path=Path("/registry.yaml"))
        self.assertEqual(policy.implementation_validator_names, ("compile",))
        self.assertEqual(policy.integration_validators[0].name, "compile")


if __name__ == "__main__":
    unittest.main()
