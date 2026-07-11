"""Shared command-execution helpers for shell-based validators."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

from agent_taskflow.executor_launch import (
    ExecutorLaunchBinding,
    ExecutorLaunchSpec,
    run_managed_process,
)

_DANGEROUS_FRAGMENTS = frozenset({
    "rm", "sudo", "git push", "git merge", "gh pr merge", "cleanup",
    "npm install", "pip install", "curl", "wget",
})
_AUTO_FIX_FRAGMENTS = frozenset({"--fix", "--write", "--apply"})


def _validate_command(command: Sequence[str]) -> list[str]:
    if not command:
        raise ValueError("command must not be empty")
    if not isinstance(command, (list, tuple)):
        raise TypeError(
            f"command must be a list or tuple of strings, not {type(command).__name__!r}"
        )
    normalized: list[str] = []
    for i, part in enumerate(command):
        if not isinstance(part, str):
            raise TypeError(
                f"command[{i}] must be a string, not {type(part).__name__!r}"
            )
        if not part:
            raise ValueError(f"command[{i}] must not be empty")
        normalized.append(part)
    return normalized


def _check_dangerous(command: Sequence[str]) -> str | None:
    joined = " ".join(command).lower()
    for fragment in _DANGEROUS_FRAGMENTS:
        if fragment in joined:
            return f"command contains dangerous fragment: {fragment!r}"
    return None


def run_command(
    validator_name: str,
    command: list[str],
    worktree_path: Path,
    artifact_dir: Path,
    timeout_seconds: int | None,
    run_env: dict[str, str] | None,
    launch_binding: ExecutorLaunchBinding | None = None,
) -> tuple[subprocess.CompletedProcess[str] | None, Path, str, str, dict[str, Path]]:
    """Run a validator command through legacy or Attempt-managed process launch."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    log_path = artifact_dir / f"{validator_name}.log"
    managed_artifacts: dict[str, Path] = {}

    if launch_binding is not None:
        preamble = (
            f"Validator: {validator_name}\n"
            f"Command: {command!r}\n"
            f"Worktree: {worktree_path}\n"
            "Environment: not logged\n"
        )
        spec = ExecutorLaunchSpec(
            executor_name=validator_name,
            process_role="validator",
            argv=tuple(command),
            cwd=worktree_path,
            artifact_dir=artifact_dir,
            timeout_seconds=timeout_seconds,
            stdin_mode="devnull",
            combined_output=True,
            environment_keys=tuple((run_env or {}).keys()),
        )
        managed = run_managed_process(
            launch_binding,
            spec,
            stdout_path=log_path,
            run_env=run_env,
            preamble=preamble,
        )
        managed_artifacts = {
            "launch_spec": managed.launch_spec_path,
            "pid_manifest": managed.pid_manifest_path,
        }
        if managed.start_error:
            return (
                None,
                log_path,
                f"{validator_name} validation command failed to start: {managed.start_error}",
                "blocked",
                managed_artifacts,
            )
        if managed.timed_out:
            return (
                None,
                log_path,
                f"{validator_name} validation timed out after {timeout_seconds} seconds.",
                "failed",
                managed_artifacts,
            )
        if managed.kill_requested:
            return (
                None,
                log_path,
                f"{validator_name} validation aborted by operator kill request.",
                "blocked",
                managed_artifacts,
            )
        if not managed.verified_exit:
            return (
                None,
                log_path,
                f"{validator_name} validator process-group exit could not be verified; verified_exit=false.",
                "blocked",
                managed_artifacts,
            )
        if managed.termination_reason == "validator_descendant_cleanup":
            return (
                None,
                log_path,
                f"{validator_name} validator leader exited with live descendants; "
                "the process group was terminated and verified.",
                "blocked",
                managed_artifacts,
            )
        completed = subprocess.CompletedProcess(command, managed.exit_code or 0)
        return completed, log_path, "", "", managed_artifacts

    with log_path.open("w", encoding="utf-8") as log_file:
        log_file.write(f"Validator: {validator_name}\n")
        log_file.write(f"Command: {command!r}\n")
        log_file.write(f"Worktree: {worktree_path}\n")
        log_file.write("Environment: not logged\n\n")
        log_file.flush()
        try:
            completed = subprocess.run(
                command,
                cwd=worktree_path,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=timeout_seconds,
                env=run_env,
                text=True,
                shell=False,
                check=False,
            )
            return completed, log_path, "", "", managed_artifacts
        except subprocess.TimeoutExpired:
            summary = (
                f"{validator_name} validation timed out after "
                f"{timeout_seconds} seconds."
            )
            log_file.write(f"\n{summary}\n")
            return None, log_path, summary, "failed", managed_artifacts
        except FileNotFoundError as exc:
            summary = f"{validator_name} validation command failed to start: {exc}"
            log_file.write(f"\n{summary}\n")
            return None, log_path, summary, "blocked", managed_artifacts


__all__ = [
    "_AUTO_FIX_FRAGMENTS",
    "_DANGEROUS_FRAGMENTS",
    "_check_dangerous",
    "_validate_command",
    "run_command",
]
