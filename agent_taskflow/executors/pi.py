"""Pi CLI executor adapter for Agent Taskflow.

This executor is a pure pass-through to the Pi CLI. It does not call any AI,
does not run validators, does not approve tasks, and does not modify the
dispatcher state. It is a deterministic executor backend.

When a mission_contract.json is present in the task artifact directory,
this executor renders a Pi Mission Protocol prompt (pi_mission_prompt.md) and
uses that as its input. This gives Pi explicit governance context and
constraint information as structured text rather than relying on system
prompt injection alone.

When no mission contract is present, the executor falls back to reading
context.prompt_path (the legacy implementation_prompt.md) to preserve
backward compatibility with existing tasks.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from agent_taskflow.executors.base import Executor, ExecutorContext, ExecutorResult
from agent_taskflow.executors.pi_protocol import (
    load_contract_for_pi,
    render_pi_mission_prompt,
    write_pi_mission_prompt,
)


class PiExecutor(Executor):
    """Run the Pi CLI inside a verified task worktree."""

    name = "pi"

    def __init__(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        tools: list[str] | None = None,
        env: dict[str, str] | None = None,
        pi_bin: str = "pi",
        no_session: bool = True,
    ) -> None:
        if provider is not None:
            provider = provider.strip()
            if not provider:
                raise ValueError("provider must not be empty when provided")

        if model is not None:
            model = model.strip()
            if not model:
                raise ValueError("model must not be empty when provided")

        if tools is not None:
            tools = list(tools)
            for tool in tools:
                if not isinstance(tool, str):
                    raise TypeError("tools entries must be strings")
                if not tool.strip():
                    raise ValueError("tools entries must not be empty")

        pi_bin = pi_bin.strip()
        if not pi_bin:
            raise ValueError("pi_bin must not be empty")

        self.provider = provider
        self.model = model
        self.tools = [t.strip() for t in (tools or [])]
        self.env = env
        self.pi_bin = pi_bin
        self.no_session = no_session

    def run(self, context: ExecutorContext) -> ExecutorResult:
        """Execute the Pi CLI for the supplied task context."""

        context.artifact_dir.mkdir(parents=True, exist_ok=True)
        log_path = context.artifact_dir / "pi-executor.log"

        # Determine which prompt text to use.
        # Priority:
        # 1. mission_contract.json + pi_mission_prompt.md (Phase 23 protocol path)
        # 2. context.prompt_path (legacy path for backward compatibility)
        prompt_text: str | None = None
        protocol_prompt_path: Path | None = None
        prompt_source: str = "legacy"

        contract = load_contract_for_pi(context.artifact_dir)
        if contract is not None:
            # Read the original prompt text from context.prompt_path if available.
            original_prompt: str | None = None
            if context.prompt_path is not None and context.prompt_path.exists():
                try:
                    raw = context.prompt_path.read_text(encoding="utf-8")
                    if raw.strip():
                        original_prompt = raw
                except OSError:
                    pass

            rendered = render_pi_mission_prompt(contract, original_prompt=original_prompt)
            protocol_prompt_path = write_pi_mission_prompt(context.artifact_dir, rendered)
            prompt_text = rendered
            prompt_source = "protocol"
        elif context.prompt_path is not None:
            # Legacy path: use context.prompt_path directly.
            if not context.prompt_path.exists():
                return self._blocked(
                    f"Pi executor prompt_path does not exist: {context.prompt_path}",
                    log_path,
                )
            try:
                prompt_text = context.prompt_path.read_text(encoding="utf-8")
            except OSError as exc:
                return self._blocked(
                    f"Pi executor failed to read prompt_path: {exc}",
                    log_path,
                )
            if not prompt_text.strip():
                return self._blocked(
                    "Pi executor prompt is empty.",
                    log_path,
                )
        else:
            return self._blocked(
                "Pi executor requires either context.prompt_path or a "
                "mission_contract.json in the artifact directory.",
                log_path,
            )

        assert prompt_text is not None
        command = self._build_command(prompt_text)

        run_env: dict[str, str] | None = None
        has_constructor_env = self.env is not None
        has_context_env = context.env is not None
        if has_constructor_env or has_context_env:
            run_env = os.environ.copy()
            if has_constructor_env:
                run_env.update(self.env)
            if has_context_env:
                run_env.update(context.env)

        completed: subprocess.CompletedProcess[str] | None = None
        start_error: str | None = None
        start_status: str = "failed"

        with log_path.open("w", encoding="utf-8") as log_file:
            log_file.write(f"Executor: {self.name}\n")
            log_file.write(f"Task: {context.task_key}\n")
            log_file.write(f"Project: {context.project}\n")
            log_file.write(f"Worktree: {context.worktree_path}\n")
            log_file.write(f"Command: {command}\n")
            log_file.write(f"Prompt source: {prompt_source}\n")
            if prompt_source == "protocol":
                log_file.write(f"Protocol prompt: {protocol_prompt_path}\n")
            log_file.write("Environment: not logged\n\n")
            log_file.flush()

            try:
                completed = subprocess.run(
                    command,
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
                start_error = (
                    f"Pi CLI timed out after {context.timeout_seconds} seconds."
                )
                start_status = "failed"
                log_file.write(f"\n{start_error}\n")
            except FileNotFoundError as exc:
                start_error = f"Pi binary failed to start: {exc}"
                start_status = "blocked"
                log_file.write(f"\n{start_error}\n")

        artifacts: dict[str, Path] = {"pi_log": log_path}
        if protocol_prompt_path is not None:
            artifacts["pi_mission_prompt"] = protocol_prompt_path

        if start_error is not None:
            return ExecutorResult(
                executor=self.name,
                status=start_status,
                exit_code=None,
                log_path=log_path,
                summary=start_error,
                artifacts=artifacts,
            )

        assert completed is not None
        status = "completed" if completed.returncode == 0 else "failed"
        summary = (
            "Pi CLI completed successfully."
            if status == "completed"
            else f"Pi CLI failed with exit code {completed.returncode}."
        )

        return ExecutorResult(
            executor=self.name,
            status=status,
            exit_code=completed.returncode,
            log_path=log_path,
            summary=summary,
            artifacts=artifacts,
        )

    def _build_command(self, prompt_text: str) -> list[str]:
        """Build the pi CLI command argument list."""

        command: list[str] = [self.pi_bin]

        if self.no_session:
            command.append("--no-session")

        if self.provider is not None:
            command.extend(["--provider", self.provider])

        if self.model is not None:
            command.extend(["--model", self.model])

        if self.tools:
            command.extend(["--tools", ",".join(self.tools)])

        command.extend(["-p", prompt_text])

        return command

    def _blocked(self, summary: str, log_path: Path | None = None) -> ExecutorResult:
        return ExecutorResult(
            executor=self.name,
            status="blocked",
            exit_code=None,
            log_path=log_path,
            summary=summary,
            artifacts={"pi_log": log_path} if log_path is not None else {},
        )


__all__ = ["PiExecutor"]