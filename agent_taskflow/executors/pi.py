"""Pi CLI executor adapter for Agent Taskflow.

This executor is a pure pass-through to the Pi CLI. It does not call any AI,
run validators, approve tasks, or mutate dispatcher state. Canonical Attempt
runs use PR-7 managed process groups; plain local contexts retain the historical
synchronous compatibility path.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from agent_taskflow.executor_launch import ExecutorLaunchSpec, run_managed_process
from agent_taskflow.executors.base import Executor, ExecutorContext, ExecutorResult
from agent_taskflow.executors.pi_protocol import (
    load_contract_for_pi,
    render_pi_mission_prompt,
    write_pi_mission_prompt,
)
from agent_taskflow.executors.pi_orchestrator import (
    build_pi_mission_plan,
    write_pi_mission_plan,
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
        self.tools = [item.strip() for item in (tools or [])]
        self.env = env
        self.pi_bin = pi_bin
        self.no_session = no_session

    def run(self, context: ExecutorContext) -> ExecutorResult:
        context.artifact_dir.mkdir(parents=True, exist_ok=True)
        log_path = context.artifact_dir / "pi-executor.log"
        prompt_text: str | None = None
        protocol_prompt_path: Path | None = None
        plan_path: Path | None = None
        prompt_source = "legacy"

        contract = load_contract_for_pi(context.artifact_dir)
        if contract is not None:
            mission_plan = build_pi_mission_plan(contract)
            plan_path = write_pi_mission_plan(context.artifact_dir, mission_plan)
            original_prompt: str | None = None
            if context.prompt_path is not None and context.prompt_path.exists():
                try:
                    raw = context.prompt_path.read_text(encoding="utf-8")
                    if raw.strip():
                        original_prompt = raw
                except OSError:
                    pass
            rendered = render_pi_mission_prompt(
                contract,
                original_prompt=original_prompt,
                mission_plan=mission_plan,
            )
            protocol_prompt_path = write_pi_mission_prompt(
                context.artifact_dir, rendered
            )
            prompt_text = rendered
            prompt_source = "protocol"
        elif context.prompt_path is not None:
            if not context.prompt_path.exists():
                return self._blocked(
                    f"Pi executor prompt_path does not exist: {context.prompt_path}",
                    log_path,
                )
            try:
                prompt_text = context.prompt_path.read_text(encoding="utf-8")
            except OSError as exc:
                return self._blocked(
                    f"Pi executor failed to read prompt_path: {exc}", log_path
                )
            if not prompt_text.strip():
                return self._blocked("Pi executor prompt is empty.", log_path)
        else:
            return self._blocked(
                "Pi executor requires either context.prompt_path or a "
                "mission_contract.json in the artifact directory.",
                log_path,
            )

        assert prompt_text is not None
        command = self._build_command(prompt_text)
        run_env: dict[str, str] | None = None
        if self.env is not None or context.env is not None:
            run_env = os.environ.copy()
            if self.env is not None:
                run_env.update(self.env)
            if context.env is not None:
                run_env.update(context.env)
        display_command = [*command[:-1], "<prompt_text>"]
        preamble = (
            f"Executor: {self.name}\n"
            f"Task: {context.task_key}\n"
            f"Project: {context.project}\n"
            f"Worktree: {context.worktree_path}\n"
            f"Command: {display_command}\n"
            f"Prompt source: {prompt_source}\n"
            + (
                f"Protocol prompt: {protocol_prompt_path}\n"
                if protocol_prompt_path is not None
                else ""
            )
            + (f"Mission plan: {plan_path}\n" if plan_path is not None else "")
            + "Environment: not logged\n\n"
        )

        artifacts: dict[str, Path] = {"pi_log": log_path}
        if protocol_prompt_path is not None:
            artifacts["pi_mission_prompt"] = protocol_prompt_path
        if plan_path is not None:
            artifacts["pi_mission_plan"] = plan_path

        if context.launch_binding is not None:
            managed = run_managed_process(
                context.launch_binding,
                ExecutorLaunchSpec(
                    executor_name=self.name,
                    argv=tuple(command),
                    cwd=context.worktree_path,
                    artifact_dir=context.artifact_dir,
                    timeout_seconds=context.timeout_seconds,
                    stdin_mode="devnull",
                    combined_output=True,
                    environment_keys=tuple(
                        set((self.env or {}).keys()) | set((context.env or {}).keys())
                    ),
                    redacted_arg_indexes=(len(command) - 1,),
                ),
                stdout_path=log_path,
                run_env=run_env,
                preamble=preamble,
            )
            artifacts["executor_launch_spec"] = managed.launch_spec_path
            artifacts["executor_process_pid"] = managed.pid_manifest_path
            if managed.preflight_errors:
                return ExecutorResult(
                    executor=self.name,
                    status="blocked",
                    exit_code=None,
                    log_path=log_path,
                    summary="Pi launch preflight failed: "
                    + "; ".join(managed.preflight_errors),
                    artifacts=artifacts,
                )
            if managed.start_error is not None:
                return ExecutorResult(
                    executor=self.name,
                    status="blocked",
                    exit_code=None,
                    log_path=log_path,
                    summary=f"Pi binary failed to start: {managed.start_error}",
                    artifacts=artifacts,
                )
            if managed.kill_requested:
                return ExecutorResult(
                    executor=self.name,
                    status="blocked",
                    exit_code=managed.exit_code,
                    log_path=log_path,
                    summary="Operator kill requested; Pi process group terminated.",
                    artifacts=artifacts,
                )
            if managed.timed_out:
                return ExecutorResult(
                    executor=self.name,
                    status="failed",
                    exit_code=managed.exit_code,
                    log_path=log_path,
                    summary=(
                        f"Pi CLI timed out after {context.timeout_seconds} seconds; "
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
                    summary="Pi process-group exit could not be verified.",
                    artifacts=artifacts,
                )
            status = "completed" if managed.exit_code == 0 else "failed"
            summary = (
                "Pi CLI completed successfully."
                if status == "completed"
                else f"Pi CLI failed with exit code {managed.exit_code}."
            )
            if managed.termination_reason == "executor_descendant_cleanup":
                status = "failed"
                summary = (
                    "Pi leader exited with live descendants; process group was terminated."
                )
            return ExecutorResult(
                executor=self.name,
                status=status,
                exit_code=managed.exit_code,
                log_path=log_path,
                summary=summary,
                artifacts=artifacts,
            )

        completed: subprocess.CompletedProcess[str] | None = None
        start_error: str | None = None
        start_status = "failed"
        with log_path.open("w", encoding="utf-8") as log_file:
            log_file.write(preamble)
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
                log_file.write(f"\n{start_error}\n")
            except FileNotFoundError as exc:
                start_error = f"Pi binary failed to start: {exc}"
                start_status = "blocked"
                log_file.write(f"\n{start_error}\n")
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
