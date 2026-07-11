"""OpenSpec validator for Agent Taskflow."""

from __future__ import annotations

import os
from typing import Sequence

from agent_taskflow.validators.base import Validator, ValidatorContext, ValidatorResult
from agent_taskflow.validators.command import run_command


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


class OpenSpecValidator(Validator):
    name = "openspec"

    def __init__(self, openspec_bin: str = "openspec", args: Sequence[str] | None = None) -> None:
        self.openspec_bin = openspec_bin.strip()
        if not self.openspec_bin:
            raise ValueError("openspec_bin must not be empty")
        self.args = _validate_args(
            args if args is not None else ["validate", "--all", "--no-interactive"],
            "args",
        )

    @property
    def command(self) -> list[str]:
        return [self.openspec_bin, *self.args]

    def run(self, context: ValidatorContext) -> ValidatorResult:
        if not (context.worktree_path / "openspec").exists():
            return ValidatorResult(
                validator=self.name, status="skipped", exit_code=None, log_path=None,
                summary="openspec directory not found", artifacts={},
            )
        run_env = None
        if context.env is not None:
            run_env = os.environ.copy()
            run_env.update(context.env)
        completed, log_path, error_summary, error_status, process_artifacts = run_command(
            validator_name="openspec-validate",
            command=self.command,
            worktree_path=context.worktree_path,
            artifact_dir=context.artifact_dir,
            timeout_seconds=context.timeout_seconds,
            run_env=run_env,
            launch_binding=context.launch_binding,
        )
        if completed is None:
            return ValidatorResult(
                validator=self.name, status=error_status, exit_code=None,
                log_path=log_path, summary=error_summary,
                artifacts={"log": log_path, **process_artifacts},
            )
        status = "passed" if completed.returncode == 0 else "failed"
        summary = (
            "OpenSpec validation passed."
            if status == "passed"
            else f"OpenSpec validation failed with exit code {completed.returncode}."
        )
        return ValidatorResult(
            validator=self.name, status=status, exit_code=completed.returncode,
            log_path=log_path, summary=summary,
            artifacts={"log": log_path, **process_artifacts},
        )


__all__ = ["OpenSpecValidator"]
