"""Shared command-execution helpers for shell-based validators."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Sequence

# Commands and command fragments that are never safe to run inside a validator.
# These are checked against individual command fragments, not parsed shell strings.
_DANGEROUS_FRAGMENTS = frozenset({
    "rm",
    "sudo",
    "git push",
    "git merge",
    "gh pr merge",
    "cleanup",
    "npm install",
    "pip install",
    "curl",
    "wget",
})

# Command fragments that indicate an auto-fix / auto-write intent.
# LintValidator should reject commands containing any of these.
_AUTO_FIX_FRAGMENTS = frozenset({
    "--fix",
    "--write",
    "--apply",
})


def _validate_command(command: Sequence[str]) -> list[str]:
    """Validate and return a command as a list[str], or raise ValueError."""
    if not command:
        raise ValueError("command must not be empty")

    if not isinstance(command, (list, tuple)):
        raise TypeError(
            f"command must be a list or tuple of strings, "
            f"not {type(command).__name__!r}"
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
    """Return an error message if the command contains dangerous fragments, else None."""
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
) -> tuple[subprocess.CompletedProcess | None, Path, str, str]:
    """Run an external command and write a log file.

    Returns (completed_process_or_None, log_path, summary, status).
    status is "failed" for timeout, "blocked" for FileNotFoundError,
    or "" on success.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    log_path = artifact_dir / f"{validator_name}.log"

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
            return completed, log_path, "", ""
        except subprocess.TimeoutExpired:
            summary = (
                f"{validator_name} validation timed out after "
                f"{timeout_seconds} seconds."
            )
            log_file.write(f"\n{summary}\n")
            return None, log_path, summary, "failed"
        except FileNotFoundError as exc:
            summary = f"{validator_name} validation command failed to start: {exc}"
            log_file.write(f"\n{summary}\n")
            return None, log_path, summary, "blocked"


__all__ = [
    "_AUTO_FIX_FRAGMENTS",
    "_DANGEROUS_FRAGMENTS",
    "_check_dangerous",
    "_validate_command",
    "run_command",
]
