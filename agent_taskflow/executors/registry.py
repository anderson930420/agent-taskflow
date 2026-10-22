"""Executor registry for built-in Agent Taskflow executors."""

from __future__ import annotations

from typing import Sequence

from agent_taskflow.executors.base import Executor
from agent_taskflow.executors.claude_code import ClaudeCodeExecutor
from agent_taskflow.executors.manual import ManualExecutor, NoopExecutor
from agent_taskflow.executors.shell import ShellExecutor


def _normalize_name(name: str) -> str:
    normalized = name.strip().lower()
    if not normalized:
        raise ValueError("executor name must not be empty")
    return normalized


def get_executor(
    name: str,
    *,
    command: Sequence[str] | None = None,
    claude_command: Sequence[str] | None = None,
    claude_enable_invocation: bool = False,
    worktree_root: str | None = None,
    claude_model: str | None = None,
) -> Executor:
    """Return a built-in executor by name."""

    normalized = _normalize_name(name)

    if normalized == "manual":
        return ManualExecutor()
    if normalized == "noop":
        return NoopExecutor()
    if normalized == "shell":
        if command is None:
            raise ValueError("shell executor requires command")
        return ShellExecutor(command)
    if normalized == "claude-code":
        return ClaudeCodeExecutor(
            command=claude_command,
            enable_invocation=claude_enable_invocation,
            worktree_root=worktree_root,
            model=claude_model,
        )

    raise ValueError(f"Unknown executor: {name!r}")


def build_shell_executor(command: Sequence[str], *, name: str = "shell") -> ShellExecutor:
    """Build a shell executor with an explicit command."""

    return ShellExecutor(command, name=name)


def build_claude_code_executor(
    *,
    command: Sequence[str] | None = None,
    enable_invocation: bool = False,
    worktree_root: str | None = None,
) -> ClaudeCodeExecutor:
    """Build a Claude Code bounded implementer executor.

    Prompt-only / dry-run by default. Real invocation requires both
    ``enable_invocation=True`` and an explicit ``command``.
    """

    return ClaudeCodeExecutor(
        command=command,
        enable_invocation=enable_invocation,
        worktree_root=worktree_root,
    )


def list_executor_names() -> list[str]:
    """Return supported executor names."""

    return ["manual", "noop", "shell", "claude-code"]


__all__ = [
    "build_claude_code_executor",
    "build_shell_executor",
    "get_executor",
    "list_executor_names",
]
