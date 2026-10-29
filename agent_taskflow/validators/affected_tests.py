"""Run the deterministic affected-test selection as a managed validator."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

from agent_taskflow.affected_tests import changed_paths, format_selection, select_tests
from agent_taskflow.atomic_write import atomic_write_text
from agent_taskflow.validators.base import Validator, ValidatorContext, ValidatorResult
from agent_taskflow.validators.command import run_command


class AffectedTestsValidator(Validator):
    name = "affected-tests"

    def __init__(self, python_bin: str | None = None) -> None:
        self.python_bin = python_bin or sys.executable

    def run(self, context: ValidatorContext) -> ValidatorResult:
        log = context.artifact_dir / "affected-tests.log"
        context.artifact_dir.mkdir(parents=True, exist_ok=True)
        try:
            paths = changed_paths(context.worktree_path, base_commit=context.base_commit,
                                  base_branch=context.base_branch or "main")
            selection = select_tests(context.worktree_path, paths)
        except (OSError, subprocess.CalledProcessError) as exc:
            atomic_write_text(log, f"Could not select affected tests: {exc}\n")
            return ValidatorResult(self.name, "failed", log_path=log, summary="Could not select affected tests", artifacts={"log": log})
        header = format_selection(selection)
        if not selection:
            atomic_write_text(log, header)
            return ValidatorResult(self.name, "passed", exit_code=0, log_path=log, summary="no affected tests", artifacts={"log": log})
        command = [self.python_bin, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        if importlib.util.find_spec("xdist") is not None:
            command += ["-n", "4"]
        command += list(selection)
        env = None if context.env is None else {**os.environ, **context.env}
        completed, log, summary, status, artifacts = run_command(
            validator_name=self.name, command=command, worktree_path=context.worktree_path,
            artifact_dir=context.artifact_dir, timeout_seconds=context.timeout_seconds,
            run_env=env, launch_binding=context.launch_binding)
        output = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        atomic_write_text(log, header + "\n" + output)
        if completed is None:
            return ValidatorResult(self.name, status, log_path=log, summary=summary, artifacts={"log": log, **artifacts})
        passed = completed.returncode in (0, 5)
        return ValidatorResult(self.name, "passed" if passed else "failed", exit_code=completed.returncode,
                               log_path=log, summary=f"affected tests {'passed' if passed else 'failed'} (exit {completed.returncode})",
                               artifacts={"log": log, **artifacts})
