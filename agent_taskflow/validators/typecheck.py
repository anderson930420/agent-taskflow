"""Typecheck validator for Agent Taskflow."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from agent_taskflow.validators.base import (
    Validator,
    ValidatorContext,
    ValidatorResult,
)
from agent_taskflow.validators.command import (
    _check_dangerous,
    _validate_command,
    run_command,
)


class TypecheckValidator(Validator):
    """Run a static type-checking command inside the verified task worktree.

    The validator uses subprocess.run with shell=False to invoke an explicit
    type-checking command.  By default it runs ``python3 -m mypy .`` but the
    command can be replaced via the ``command`` constructor argument.

    The validator is deterministic (no AI, no network, no file modification)
    and writes its output to ``typecheck.log`` in the task artifact directory.

    Command safety
    --------------
    The following patterns are rejected before the subprocess is spawned:

    - empty command
    - any entry that is not a non-empty string
    - commands containing dangerous fragments::

        rm, sudo, git push, git merge, gh pr merge, cleanup,
        npm install, pip install, curl, wget

    Parameters
    ----------
    command : Sequence[str]
        The command to run, e.g. ``["python3", "-m", "mypy", "."]``.
        Defaults to ``["python3", "-m", "mypy", "."]``.
    """

    name = "typecheck"

    def __init__(
        self,
        command: Sequence[str] | None = None,
    ) -> None:
        if command is None:
            _default: list[str] = ["python3", "-m", "mypy", "."]
        else:
            _default = []

        self._command = _validate_command(
            command if command is not None else _default
        )

        danger = _check_dangerous(self._command)
        if danger:
            raise ValueError(danger)

    @property
    def command(self) -> list[str]:
        """Return the command that this validator will run."""
        return list(self._command)

    def _log_path(self, artifact_dir: Path) -> Path:
        return artifact_dir / "typecheck.log"

    def run(self, context: ValidatorContext) -> ValidatorResult:
        """Run the type-check command and return a ValidatorResult."""
        artifact_dir = context.artifact_dir
        worktree_path = context.worktree_path

        run_env = None
        if context.env is not None:
            run_env = os.environ.copy()
            run_env.update(context.env)

        completed, log_path, error_summary, error_status, process_artifacts = run_command(
            validator_name=self.name,
            command=self._command,
            worktree_path=worktree_path,
            artifact_dir=artifact_dir,
            timeout_seconds=context.timeout_seconds,
            run_env=run_env,
            launch_binding=context.launch_binding,
        )

        if completed is None:
            return ValidatorResult(
                validator=self.name,
                status=error_status,
                exit_code=None,
                log_path=log_path,
                summary=error_summary,
                artifacts={"log": log_path, **process_artifacts},
            )

        status = "passed" if completed.returncode == 0 else "failed"
        summary = (
            "Typecheck validation passed."
            if status == "passed"
            else f"Typecheck validation failed with exit code {completed.returncode}."
        )

        return ValidatorResult(
            validator=self.name,
            status=status,
            exit_code=completed.returncode,
            log_path=log_path,
            summary=summary,
            artifacts={"log": log_path, **process_artifacts},
        )


__all__ = ["TypecheckValidator"]
