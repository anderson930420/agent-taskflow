"""Lint validator for Agent Taskflow."""

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
    _AUTO_FIX_FRAGMENTS,
    _check_dangerous,
    _validate_command,
    run_command,
)


class LintValidator(Validator):
    """Run a lint/static-style command inside the verified task worktree.

    The validator uses subprocess.run with shell=False to invoke an explicit
    linting command.  By default it runs ``python3 -m ruff check .`` but the
    command can be replaced via the ``command`` constructor argument.

    The validator is deterministic (no AI, no network, no file modification)
    and writes its output to ``lint.log`` in the task artifact directory.

    Command safety
    --------------
    In addition to the base dangerous-fragment check, this validator also
    rejects commands that contain auto-fix / auto-write fragments::

        --fix, --write, --apply

    These flags are rejected because a lint validator should only report
    issues, not modify source files.

    Parameters
    ----------
    command : Sequence[str]
        The command to run, e.g. ``["python3", "-m", "ruff", "check", "."]``.
        Defaults to ``["python3", "-m", "ruff", "check", "."]``.
    """

    name = "lint"

    def __init__(
        self,
        command: Sequence[str] | None = None,
    ) -> None:
        if command is None:
            _default: list[str] = ["python3", "-m", "ruff", "check", "."]
        else:
            _default = []

        self._command = _validate_command(
            command if command is not None else _default
        )

        danger = _check_dangerous(self._command)
        if danger:
            raise ValueError(danger)

        # Reject auto-fix / auto-write flags.
        for fragment in _AUTO_FIX_FRAGMENTS:
            if fragment in self._command:
                raise ValueError(
                    f"command contains auto-fix fragment {fragment!r}; "
                    f"lint validators must not auto-modify files"
                )

    @property
    def command(self) -> list[str]:
        """Return the command that this validator will run."""
        return list(self._command)

    def _log_path(self, artifact_dir: Path) -> Path:
        return artifact_dir / "lint.log"

    def run(self, context: ValidatorContext) -> ValidatorResult:
        """Run the lint command and return a ValidatorResult."""
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
            "Lint validation passed."
            if status == "passed"
            else f"Lint validation failed with exit code {completed.returncode}."
        )

        return ValidatorResult(
            validator=self.name,
            status=status,
            exit_code=completed.returncode,
            log_path=log_path,
            summary=summary,
            artifacts={"log": log_path, **process_artifacts},
        )


__all__ = ["LintValidator"]
