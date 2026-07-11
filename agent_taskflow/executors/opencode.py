"""OpenCode executor adapter for Agent Taskflow."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Sequence

from agent_taskflow.atomic_write import atomic_write_text
from agent_taskflow.executor_launch import ExecutorLaunchSpec, run_managed_process
from agent_taskflow.executors.base import Executor, ExecutorContext, ExecutorResult


_MAX_UNTRACKED_EMBED_SIZE = 64 * 1024
_UNTRACKED_BINARY_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".o", ".a", ".class",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".webp", ".svgz",
    ".pdf", ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z",
    ".whl", ".jar", ".war", ".bin", ".dat", ".wasm",
})


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
        untracked_path = context.artifact_dir / "untracked-files-after-opencode.txt"

        selected_model = self.model or context.model
        if selected_model is None:
            return self._blocked(
                "OpenCode executor requires a model from constructor or context."
            )
        if context.prompt_path is None:
            return self._blocked("OpenCode executor requires context.prompt_path.")
        if not context.prompt_path.exists():
            return self._blocked(
                f"OpenCode prompt_path does not exist: {context.prompt_path}"
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
        preamble = (
            f"Executor: {self.name}\n"
            f"Task: {context.task_key}\n"
            f"Project: {context.project}\n"
            f"Worktree: {context.worktree_path}\n"
            f"Command: {command[:-1]!r} + [prompt_text]\n"
            "Environment: not logged\n\n"
        )

        managed = None
        completed: subprocess.CompletedProcess[str] | None = None
        start_error: str | None = None
        start_status: str | None = None
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
                    environment_keys=tuple((context.env or {}).keys()),
                    redacted_arg_indexes=(len(command) - 1,),
                ),
                stdout_path=log_path,
                run_env=run_env,
                preamble=preamble,
            )
            if managed.preflight_errors:
                start_error = "OpenCode launch preflight failed: " + "; ".join(
                    managed.preflight_errors
                )
                start_status = "blocked"
            elif managed.start_error is not None:
                start_error = f"OpenCode binary failed to start: {managed.start_error}"
                start_status = "blocked"
            elif managed.kill_requested:
                start_error = "Operator kill requested; OpenCode process group terminated."
                start_status = "blocked"
            elif managed.timed_out:
                start_error = (
                    f"OpenCode timed out after {context.timeout_seconds} seconds; "
                    f"verified_exit={managed.verified_exit}."
                )
                start_status = "failed"
            elif not managed.verified_exit:
                start_error = "OpenCode process-group exit could not be verified."
                start_status = "blocked"
        else:
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
            untracked_path=untracked_path,
        )
        artifacts = {
            "opencode_log": log_path,
            "git_status": git_status_path,
            "git_diff": git_diff_path,
            "untracked_files": untracked_path,
        }
        if managed is not None:
            artifacts["executor_launch_spec"] = managed.launch_spec_path
            artifacts["executor_process_pid"] = managed.pid_manifest_path
        if start_error is not None:
            return ExecutorResult(
                executor=self.name,
                status=start_status or "failed",
                exit_code=managed.exit_code if managed is not None else None,
                log_path=log_path,
                summary=self._append_capture_notes(start_error, capture_notes),
                artifacts=artifacts,
            )

        returncode = managed.exit_code if managed is not None else completed.returncode
        status = "completed" if returncode == 0 else "failed"
        summary = (
            "OpenCode completed successfully."
            if status == "completed"
            else f"OpenCode failed with exit code {returncode}."
        )
        if managed is not None and managed.termination_reason == "executor_descendant_cleanup":
            status = "failed"
            summary = (
                "OpenCode leader exited with live descendants; process group was terminated."
            )
        return ExecutorResult(
            executor=self.name,
            status=status,
            exit_code=returncode,
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
        untracked_path: Path,
    ) -> list[str]:
        notes: list[str] = []
        status_result = self._capture_command(
            ["git", "status", "--short"], cwd=worktree_path, output_path=git_status_path
        )
        if status_result is not None:
            notes.append(status_result)
        diff_result = self._capture_command(
            ["git", "diff"], cwd=worktree_path, output_path=git_diff_path
        )
        if diff_result is not None:
            notes.append(diff_result)
        untracked_result = self._capture_untracked_files(
            worktree_path=worktree_path, output_path=untracked_path
        )
        if untracked_result is not None:
            notes.append(untracked_result)
        return notes

    def _capture_untracked_files(
        self, *, worktree_path: Path, output_path: Path
    ) -> str | None:
        try:
            completed = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=worktree_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                shell=False,
                check=False,
            )
        except FileNotFoundError as exc:
            atomic_write_text(output_path, f"Failed to list untracked files: {exc}\n")
            return "untracked-files artifact capture failed to start."
        if completed.returncode != 0:
            atomic_write_text(output_path, completed.stdout or "")
            return (
                "git ls-files --others --exclude-standard artifact capture failed "
                f"with exit code {completed.returncode}."
            )
        rel_paths = [
            line.strip()
            for line in (completed.stdout or "").splitlines()
            if line.strip()
        ]
        sections = [f"Untracked files: {len(rel_paths)}"]
        for rel_path in rel_paths:
            sections.append(self._render_untracked_entry(worktree_path, rel_path))
        atomic_write_text(output_path, "\n".join(sections) + "\n")
        return None

    def _render_untracked_entry(self, worktree_path: Path, rel_path: str) -> str:
        header = f"=== {rel_path} ==="
        file_path = worktree_path / rel_path
        try:
            size = file_path.stat().st_size
        except OSError as exc:
            return f"{header}\nsize: unknown\n[skipped: stat failed: {exc}]"
        lines = [header, f"size: {size} bytes"]
        if file_path.suffix.lower() in _UNTRACKED_BINARY_SUFFIXES:
            lines.append("[skipped: binary file type]")
            return "\n".join(lines)
        if size > _MAX_UNTRACKED_EMBED_SIZE:
            lines.append(
                f"[skipped: exceeds {_MAX_UNTRACKED_EMBED_SIZE}-byte content cap]"
            )
            return "\n".join(lines)
        try:
            raw = file_path.read_bytes()
        except OSError as exc:
            lines.append(f"[skipped: unreadable: {exc}]")
            return "\n".join(lines)
        if b"\x00" in raw:
            lines.append("[skipped: binary content]")
            return "\n".join(lines)
        lines.append("content:")
        lines.append(raw.decode("utf-8", errors="replace"))
        return "\n".join(lines)

    def _capture_command(
        self, command: list[str], *, cwd: Path, output_path: Path
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
            atomic_write_text(output_path, f"Failed to start command: {exc}\n")
            return f"{command[0]} artifact capture failed to start."
        atomic_write_text(output_path, completed.stdout or "")
        if completed.returncode != 0:
            return (
                f"{' '.join(command)} artifact capture failed with exit code "
                f"{completed.returncode}."
            )
        return None

    @staticmethod
    def _append_capture_notes(summary: str, notes: list[str]) -> str:
        return summary if not notes else summary + " " + " ".join(notes)


__all__ = ["OpenCodeExecutor"]
