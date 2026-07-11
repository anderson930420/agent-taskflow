"""Safe deterministic shell-command executor."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Sequence

from agent_taskflow.executor_launch import ExecutorLaunchSpec, run_managed_process
from agent_taskflow.executors.base import Executor, ExecutorContext, ExecutorResult


_SAFE_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_log_name(name: str) -> str:
    normalized = _SAFE_NAME_PATTERN.sub("-", name.strip()).strip(".-")
    return normalized or "shell"


def _validate_command(command: Sequence[str]) -> list[str]:
    if isinstance(command, str):
        raise TypeError("command must be a sequence of strings, not a raw string")

    normalized = list(command)
    if not normalized:
        raise ValueError("command must not be empty")

    for part in normalized:
        if not isinstance(part, str):
            raise TypeError("command entries must be strings")
        if not part:
            raise ValueError("command entries must not be empty")

    return normalized


class ShellExecutor(Executor):
    """Run a deterministic command inside the task worktree."""

    def __init__(self, command: Sequence[str], *, name: str = "shell") -> None:
        self.command = _validate_command(command)
        self.name = name.strip()
        if not self.name:
            raise ValueError("name must not be empty")

    def _log_path(self, artifact_dir: Path) -> Path:
        return artifact_dir / f"shell-{_safe_log_name(self.name)}.log"

    def run(self, context: ExecutorContext) -> ExecutorResult:
        context.artifact_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._log_path(context.artifact_dir)

        run_env = None
        if context.env is not None:
            run_env = os.environ.copy()
            run_env.update(context.env)

        preamble = (
            f"Executor: {self.name}\n"
            f"Task: {context.task_key}\n"
            f"Project: {context.project}\n"
            f"Worktree: {context.worktree_path}\n"
            f"Command: {self.command!r}\n"
            "Environment: not logged\n\n"
        )

        if context.launch_binding is not None:
            managed = run_managed_process(
                context.launch_binding,
                ExecutorLaunchSpec(
                    executor_name=self.name,
                    argv=tuple(self.command),
                    cwd=context.worktree_path,
                    artifact_dir=context.artifact_dir,
                    timeout_seconds=context.timeout_seconds,
                    stdin_mode="devnull",
                    combined_output=True,
                    environment_keys=tuple((context.env or {}).keys()),
                ),
                stdout_path=log_path,
                run_env=run_env,
                preamble=preamble,
            )
            artifacts = {
                "log": log_path,
                "executor_launch_spec": managed.launch_spec_path,
                "executor_process_pid": managed.pid_manifest_path,
            }
            if managed.preflight_errors or managed.start_error is not None:
                summary = (
                    "Executor launch preflight failed: "
                    + "; ".join(managed.preflight_errors)
                    if managed.preflight_errors
                    else f"Command failed to start: {managed.start_error}"
                )
                return ExecutorResult(
                    executor=self.name,
                    status="blocked",
                    exit_code=None,
                    log_path=log_path,
                    summary=summary,
                    artifacts=artifacts,
                )
            if managed.kill_requested:
                return ExecutorResult(
                    executor=self.name,
                    status="blocked",
                    exit_code=managed.exit_code,
                    log_path=log_path,
                    summary="Operator kill requested; executor process group terminated.",
                    artifacts=artifacts,
                )
            if managed.timed_out:
                return ExecutorResult(
                    executor=self.name,
                    status="failed",
                    exit_code=managed.exit_code,
                    log_path=log_path,
                    summary=(
                        f"Command timed out after {context.timeout_seconds} seconds; "
                        f"verified_exit={managed.verified_exit}."
                    ),
                    artifacts=artifacts,
                )
            if not managed.verified_exit:
                return ExecutorResult(
                    executor=self.name,
                    status="blocked",
                    exit_code=managed.exit_code,
                    log_path=log_path,
                    summary="Executor process-group exit could not be verified.",
                    artifacts=artifacts,
                )
            status = "completed" if managed.exit_code == 0 else "failed"
            summary = (
                "Command completed successfully."
                if status == "completed"
                else f"Command failed with exit code {managed.exit_code}."
            )
            if managed.termination_reason == "executor_descendant_cleanup":
                status = "failed"
                summary = "Executor leader exited with live descendants; process group was terminated."
            return ExecutorResult(
                executor=self.name,
                status=status,
                exit_code=managed.exit_code,
                log_path=log_path,
                summary=summary,
                artifacts=artifacts,
            )

        # Compatibility path for local tools and unit fixtures that are not bound
        # to a canonical Attempt. Canonical runtime execution never uses this path.
        with log_path.open("w", encoding="utf-8") as log_file:
            log_file.write(preamble)
            log_file.flush()

            try:
                completed = subprocess.run(
                    self.command,
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
                log_file.write(
                    f"\nCommand timed out after {context.timeout_seconds} seconds.\n"
                )
                return ExecutorResult(
                    executor=self.name,
                    status="failed",
                    exit_code=None,
                    log_path=log_path,
                    summary=(
                        f"Command timed out after {context.timeout_seconds} seconds."
                    ),
                    artifacts={"log": log_path},
                )
            except FileNotFoundError as exc:
                log_file.write(f"\nCommand failed to start: {exc}\n")
                return ExecutorResult(
                    executor=self.name,
                    status="failed",
                    exit_code=None,
                    log_path=log_path,
                    summary=f"Command failed to start: {exc}",
                    artifacts={"log": log_path},
                )

        status = "completed" if completed.returncode == 0 else "failed"
        summary = (
            "Command completed successfully."
            if status == "completed"
            else f"Command failed with exit code {completed.returncode}."
        )

        return ExecutorResult(
            executor=self.name,
            status=status,
            exit_code=completed.returncode,
            log_path=log_path,
            summary=summary,
            artifacts={"log": log_path},
        )


__all__ = ["ShellExecutor"]
