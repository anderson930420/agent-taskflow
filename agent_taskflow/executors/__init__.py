"""Built-in executors for Agent Taskflow."""

from agent_taskflow.executors.base import (
    EXECUTOR_RESULT_STATUSES,
    Executor,
    ExecutorContext,
    ExecutorResult,
    validate_executor_result_status,
)
from agent_taskflow.executors.claude_code import (
    CLAUDE_CODE_EXECUTION_ARTIFACT_FILENAME,
    CLAUDE_CODE_EXECUTION_SCHEMA_VERSION,
    CLAUDE_CODE_EXECUTOR_NAME,
    CLAUDE_CODE_PROMPT_FILENAME,
    ClaudeCodeExecutor,
    ClaudeCodePreflightResult,
    check_claude_code_preflight,
    render_claude_code_implementer_prompt,
)
from agent_taskflow.executors.manual import ManualExecutor, NoopExecutor
from agent_taskflow.executors.opencode import OpenCodeExecutor
from agent_taskflow.executors.pi import PiExecutor
from agent_taskflow.executors.registry import (
    build_claude_code_executor,
    build_opencode_executor,
    build_pi_executor,
    build_shell_executor,
    get_executor,
    list_executor_names,
)
from agent_taskflow.executors.shell import ShellExecutor

__all__ = [
    "CLAUDE_CODE_EXECUTION_ARTIFACT_FILENAME",
    "CLAUDE_CODE_EXECUTION_SCHEMA_VERSION",
    "CLAUDE_CODE_EXECUTOR_NAME",
    "CLAUDE_CODE_PROMPT_FILENAME",
    "EXECUTOR_RESULT_STATUSES",
    "ClaudeCodeExecutor",
    "ClaudeCodePreflightResult",
    "Executor",
    "ExecutorContext",
    "ExecutorResult",
    "ManualExecutor",
    "NoopExecutor",
    "OpenCodeExecutor",
    "PiExecutor",
    "ShellExecutor",
    "build_claude_code_executor",
    "build_opencode_executor",
    "build_pi_executor",
    "build_shell_executor",
    "check_claude_code_preflight",
    "get_executor",
    "list_executor_names",
    "render_claude_code_implementer_prompt",
    "validate_executor_result_status",
]
