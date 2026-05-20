"""Pytest validator for Agent Taskflow."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from agent_taskflow.validators.base import (
    Validator,
    ValidatorContext,
    ValidatorResult,
)


def _validate_args(args: Sequence[str] | None, field_name: str) -> list[str]:
    if args is None:
        return []
    if isinstance(args, str):
        raise TypeError(f"{field_name} must be a sequence of strings, not a raw string")

    normalized = list(args)
    for part in normalized:
        if not isinstance(part, str):
            raise TypeError(f"{field_name} entries must be strings")
        if not part:
            raise ValueError(f"{field_name} entries must not be empty")

    return normalized


class PytestValidator(Validator):
    """Run python3 -m pytest inside the verified task worktree."""

    name = "pytest"

    def __init__(
        self,
        python_bin: str | None = None,
        extra_args: Sequence[str] | None = None,
    ) -> None:
        # Default to the interpreter running the orchestration process so
        # the validator inherits the project's .venv pytest install instead
        # of resolving "python3" against PATH, which may pick a system
        # interpreter without pytest available.
        if python_bin is None:
            resolved = sys.executable
        else:
            resolved = python_bin.strip()
        if not resolved:
            raise ValueError("python_bin must not be empty")
        self.python_bin = resolved
        self.extra_args = _validate_args(extra_args, "extra_args")

    @property
    def command(self) -> list[str]:
        """Return the command used by this validator."""
        return [self.python_bin, "-m", "pytest", *self.extra_args]

    def _log_path(self, artifact_dir: Path) -> Path:
        return artifact_dir / "pytest.log"

    def run(self, context: ValidatorContext) -> ValidatorResult:
        context.artifact_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._log_path(context.artifact_dir)
        command = self.command

        run_env = None
        if context.env is not None:
            run_env = os.environ.copy()
            run_env.update(context.env)

        with log_path.open("w", encoding="utf-8") as log_file:
            log_file.write(f"Validator: {self.name}\n")
            log_file.write(f"Task: {context.task_key}\n")
            log_file.write(f"Project: {context.project}\n")
            log_file.write(f"Worktree: {context.worktree_path}\n")
            log_file.write(f"Command: {command!r}\n")
            log_file.write("Environment: not logged\n\n")
            log_file.flush()

            try:
                completed = subprocess.run(
                    command,
                    cwd=context.worktree_path,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    timeout=context.timeout_seconds,
                    env=run_env,
                    text=True,
                    shell=False,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                summary = (
                    f"Pytest validation timed out after "
                    f"{context.timeout_seconds} seconds."
                )
                log_file.write(f"\n{summary}\n")
                return ValidatorResult(
                    validator=self.name,
                    status="failed",
                    exit_code=None,
                    log_path=log_path,
                    summary=summary,
                    artifacts={"log": log_path},
                )
            except FileNotFoundError as exc:
                summary = f"Pytest validation command failed to start: {exc}"
                log_file.write(f"\n{summary}\n")
                return ValidatorResult(
                    validator=self.name,
                    status="blocked",
                    exit_code=None,
                    log_path=log_path,
                    summary=summary,
                    artifacts={"log": log_path},
                )

        status = "passed" if completed.returncode == 0 else "failed"
        summary = (
            "Pytest validation passed."
            if status == "passed"
            else f"Pytest validation failed with exit code {completed.returncode}."
        )

        return ValidatorResult(
            validator=self.name,
            status=status,
            exit_code=completed.returncode,
            log_path=log_path,
            summary=summary,
            artifacts={"log": log_path},
        )


__all__ = ["PytestValidator"]
