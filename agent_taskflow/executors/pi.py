"""Pi CLI executor adapter for Agent Taskflow."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from agent_taskflow.executors.base import Executor, ExecutorContext, ExecutorResult


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

        # Validate prompt_path
        if context.prompt_path is None:
            return self._blocked("Pi executor requires context.prompt_path.")

        if not context.prompt_path.exists():
            return self._blocked(
                f"Pi executor prompt_path does not exist: {context.prompt_path}",
            )

        # Verify prompt file is not empty
        prompt_text = context.prompt_path.read_text(encoding="utf-8")
        if not prompt_text.strip():
            return self._blocked("Pi executor prompt is empty.")

        context.artifact_dir.mkdir(parents=True, exist_ok=True)
        log_path = context.artifact_dir / "pi-executor.log"

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

    def _blocked(self, summary: str) -> ExecutorResult:
        return ExecutorResult(
            executor=self.name,
            status="blocked",
            exit_code=None,
            log_path=None,
            summary=summary,
            artifacts={},
        )


__all__ = ["PiExecutor"]