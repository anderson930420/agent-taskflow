"""Compile (syntax) validator for Agent Taskflow."""

from __future__ import annotations

import os
import sys
from pathlib import Path

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

#: Paths ``compileall -x`` skips. It is matched against each file path relative
#: to the worktree (``./...``), one path component at a time, so the worktree's
#: own location (for example under ``.worktrees/``) never matches.
COMPILE_EXCLUDE_REGEX = r"(^|/)(\.git|node_modules|\.venv|\.worktrees)(/|$)"


class CompileValidator(Validator):
    """Byte-compile every Python file in the verified task worktree.

    The validator runs, with ``shell=False`` and the worktree as its working
    directory::

        <python> -I -m compileall -q -f -x <COMPILE_EXCLUDE_REGEX> .

    It is a fast syntax gate: deterministic, no AI, no network, and it never
    imports or runs the code it checks. ``-I`` (isolated mode) keeps the
    worktree off ``sys.path``, so a worktree file named like a standard-library
    module (``py_compile.py``, say) cannot replace the compiler and fake a pass.
    ``-f`` recompiles every file, so a stale or planted ``__pycache__`` entry
    whose timestamp and size match a broken source cannot hide it either.
    It writes no source file; the bytecode it writes goes to ``__pycache__/``,
    which the repository ignores. Output goes to ``compile.log`` in the task
    artifact directory. Exit 0 passes; anything else fails.

    The command is fixed; only the interpreter can be chosen.

    Parameters
    ----------
    python_bin : str | None
        The interpreter to run. Defaults to ``sys.executable``.
    """

    name = "compile"

    def __init__(self, python_bin: str | None = None) -> None:
        resolved = sys.executable if python_bin is None else python_bin.strip()
        if not resolved:
            raise ValueError("python_bin must not be empty")
        arguments = ["-I", "-m", "compileall", "-q", "-f", "-x", COMPILE_EXCLUDE_REGEX, "."]
        self._command = _validate_command([resolved, *arguments])

        # Only the fixed arguments are screened: the dangerous-fragment check
        # matches substrings, so an interpreter path such as /opt/platform/...
        # would be refused for containing "rm".
        danger = _check_dangerous(arguments)
        if danger:
            raise ValueError(danger)

    @property
    def command(self) -> list[str]:
        """Return the command that this validator will run."""
        return list(self._command)

    def _log_path(self, artifact_dir: Path) -> Path:
        return artifact_dir / "compile.log"

    def run(self, context: ValidatorContext) -> ValidatorResult:
        """Run the compile command and return a ValidatorResult."""
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
            "Compile validation passed."
            if status == "passed"
            else (
                f"Compile validation failed with exit code {completed.returncode}; "
                "see compile.log for the files that did not compile."
            )
        )

        return ValidatorResult(
            validator=self.name,
            status=status,
            exit_code=completed.returncode,
            log_path=log_path,
            summary=summary,
            artifacts={"log": log_path, **process_artifacts},
        )


__all__ = ["COMPILE_EXCLUDE_REGEX", "CompileValidator"]
