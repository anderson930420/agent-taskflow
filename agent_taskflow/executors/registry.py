"""Executor registry for built-in Agent Taskflow executors."""

from __future__ import annotations

from typing import Sequence

from agent_taskflow.executors.base import Executor
from agent_taskflow.executors.claude_code import ClaudeCodeExecutor
from agent_taskflow.executors.manual import ManualExecutor, NoopExecutor
from agent_taskflow.executors.opencode import OpenCodeExecutor
from agent_taskflow.executors.pi import PiExecutor
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
    model: str | None = None,
    opencode_bin: str = "opencode",
    extra_args: Sequence[str] | None = None,
    provider: str | None = None,
    tools: Sequence[str] | None = None,
    pi_bin: str = "pi",
    claude_command: Sequence[str] | None = None,
    claude_enable_invocation: bool = False,
    worktree_root: str | None = None,
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
    if normalized == "opencode":
        return OpenCodeExecutor(
            model=model,
            opencode_bin=opencode_bin,
            extra_args=extra_args,
        )
    if normalized == "pi":
        return PiExecutor(
            provider=provider,
            model=model,
            tools=list(tools) if tools is not None else None,
            pi_bin=pi_bin,
        )
    if normalized == "claude-code":
        return ClaudeCodeExecutor(
            command=claude_command,
            enable_invocation=claude_enable_invocation,
            worktree_root=worktree_root,
        )

    raise ValueError(f"Unknown executor: {name!r}")


def build_shell_executor(command: Sequence[str], *, name: str = "shell") -> ShellExecutor:
    """Build a shell executor with an explicit command."""

    return ShellExecutor(command, name=name)


def build_opencode_executor(
    *,
    model: str | None = None,
    opencode_bin: str = "opencode",
    extra_args: Sequence[str] | None = None,
) -> OpenCodeExecutor:
    """Build an OpenCode executor without checking external availability."""

    return OpenCodeExecutor(
        model=model,
        opencode_bin=opencode_bin,
        extra_args=extra_args,
    )


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


def build_pi_executor(
    *,
    provider: str | None = None,
    model: str | None = None,
    tools: Sequence[str] | None = None,
    env: dict[str, str] | None = None,
    pi_bin: str = "pi",
) -> PiExecutor:
    """Build a Pi executor without checking external availability."""

    return PiExecutor(
        provider=provider,
        model=model,
        tools=list(tools) if tools is not None else None,
        env=env,
        pi_bin=pi_bin,
    )


def list_executor_names() -> list[str]:
    """Return supported executor names."""

    return ["manual", "noop", "shell", "opencode", "pi", "claude-code"]


__all__ = [
    "build_claude_code_executor",
    "build_opencode_executor",
    "build_pi_executor",
    "build_shell_executor",
    "get_executor",
    "list_executor_names",
]
