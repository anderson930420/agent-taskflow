"""OpenCode executor adapter for Agent Taskflow."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Sequence

from agent_taskflow.executors.base import Executor, ExecutorContext, ExecutorResult


class OpenCodeExecutor(Executor):
    """Run OpenCode inside a verified task worktree."""

    name = "opencode"

    def __init__(
        self,
        model: str | None = None,
        opencode_bin: str = "opencode",
        extra_args: Sequence[str] | None = None,
    ) -> None:
        if model is not None:
            model = model.strip()
            if not model:
                raise ValueError("model must not be empty when provided")

        opencode_bin = opencode_bin.strip()
        if not opencode_bin:
            raise ValueError("opencode_bin must not be empty")

        self.model = model
        self.opencode_bin = opencode_bin
        self.extra_args = list(extra_args or [])

        for arg in self.extra_args:
            if not isinstance(arg, str):
                raise TypeError("extra_args entries must be strings")
            if not arg:
                raise ValueError("extra_args entries must not be empty")

    def run(self, context: ExecutorContext) -> ExecutorResult:
        context.artifact_dir.mkdir(parents=True, exist_ok=True)

        log_path = context.artifact_dir / "opencode-events.jsonl"
        git_status_path = context.artifact_dir / "git-status-after-opencode.txt"
        git_diff_path = context.artifact_dir / "diff-after-opencode.patch"

        selected_model = self.model or context.model
        if selected_model is None:
            return self._blocked(
                "OpenCode executor requires a model from constructor or context.",
            )

        if context.prompt_path is None:
            return self._blocked("OpenCode executor requires context.prompt_path.")

        if not context.prompt_path.exists():
            return self._blocked(
                f"OpenCode prompt_path does not exist: {context.prompt_path}",
            )

        prompt_text = context.prompt_path.read_text(encoding="utf-8")

        command = [
            self.opencode_bin,
            "run",
            "--dir",
            str(context.worktree_path),
            "--model",
            selected_model,
            "--format",
            "json",
            "--title",
            f"{context.task_key} implementation",
            *self.extra_args,
            prompt_text,
        ]

        run_env = None
        if context.env is not None:
            run_env = os.environ.copy()
            run_env.update(context.env)

        completed: subprocess.CompletedProcess[str] | None = None
        start_error: str | None = None
        start_status: str | None = None

        with log_path.open("w", encoding="utf-8") as log_file:
            log_file.write(f"Executor: {self.name}\n")
            log_file.write(f"Task: {context.task_key}\n")
            log_file.write(f"Project: {context.project}\n")
            log_file.write(f"Worktree: {context.worktree_path}\n")
            log_file.write(f"Command: {command[:-1]!r} + [prompt_text]\n")
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
                    f"OpenCode timed out after {context.timeout_seconds} seconds."
                )
                start_status = "failed"
                log_file.write(f"\n{start_error}\n")
            except FileNotFoundError as exc:
                start_error = f"OpenCode binary failed to start: {exc}"
                start_status = "blocked"
                log_file.write(f"\n{start_error}\n")

        capture_notes = self._capture_git_artifacts(
            worktree_path=context.worktree_path,
            git_status_path=git_status_path,
            git_diff_path=git_diff_path,
        )

        artifacts = {
            "opencode_log": log_path,
            "git_status": git_status_path,
            "git_diff": git_diff_path,
        }

        if start_error is not None:
            return ExecutorResult(
                executor=self.name,
                status=start_status or "failed",
                exit_code=None,
                log_path=log_path,
                summary=self._append_capture_notes(start_error, capture_notes),
                artifacts=artifacts,
            )

        assert completed is not None
        status = "completed" if completed.returncode == 0 else "failed"
        summary = (
            "OpenCode completed successfully."
            if status == "completed"
            else f"OpenCode failed with exit code {completed.returncode}."
        )

        return ExecutorResult(
            executor=self.name,
            status=status,
            exit_code=completed.returncode,
            log_path=log_path,
            summary=self._append_capture_notes(summary, capture_notes),
            artifacts=artifacts,
        )

    def _blocked(self, summary: str) -> ExecutorResult:
        return ExecutorResult(
            executor=self.name,
            status="blocked",
            exit_code=None,
            log_path=None,
            summary=summary,
            artifacts={},
        )

    def _capture_git_artifacts(
        self,
        *,
        worktree_path: Path,
        git_status_path: Path,
        git_diff_path: Path,
    ) -> list[str]:
        notes: list[str] = []

        status_result = self._capture_command(
            ["git", "status", "--short"],
            cwd=worktree_path,
            output_path=git_status_path,
        )
        if status_result is not None:
            notes.append(status_result)

        diff_result = self._capture_command(
            ["git", "diff"],
            cwd=worktree_path,
            output_path=git_diff_path,
        )
        if diff_result is not None:
            notes.append(diff_result)

        return notes

    def _capture_command(
        self,
        command: list[str],
        *,
        cwd: Path,
        output_path: Path,
    ) -> str | None:
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                shell=False,
                check=False,
            )
        except FileNotFoundError as exc:
            output_path.write_text(f"Failed to start command: {exc}\n", encoding="utf-8")
            return f"{command[0]} artifact capture failed to start."

        output_path.write_text(completed.stdout or "", encoding="utf-8")

        if completed.returncode != 0:
            return (
                f"{' '.join(command)} artifact capture failed with exit code "
                f"{completed.returncode}."
            )

        return None

    def _append_capture_notes(self, summary: str, notes: list[str]) -> str:
        if not notes:
            return summary
        return summary + " " + " ".join(notes)


__all__ = ["OpenCodeExecutor"]
