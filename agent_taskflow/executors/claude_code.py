"""Claude Code Bounded Implementer Executor for Agent Taskflow (v0.2.7).

Claude Code is added here as a *bounded implementer* executor. The core
semantic is::

    Claude Code writes code; it does not decide whether the task is done.

Claude Code may read task/spec context, read the prepared worktree path,
generate an implementer prompt, optionally invoke a configured Claude Code
command (only when explicitly enabled), write execution artifacts/logs, and
return an execution status. It has no validation, approval, merge, cleanup,
branch-deletion, worktree-deletion, or scheduler/lifecycle authority.

By default this executor is prompt-only (a ``dry_run``): it generates the
implementer prompt and a deterministic execution artifact but never invokes
Claude Code. Real invocation is opt-in and requires an explicitly configured
command plus ``enable_invocation=True``. Execution always runs with ``cwd`` set
to the prepared worktree, delivers the implementer prompt over stdin (never as
an argv element), captures stdout/stderr, records the exit code, and enforces
the context timeout.

The deterministic validators, the Codex advisory artifact contract validator,
the Codex advisory evidence gate, and human final review remain the authorities
that decide whether a task may reach ``waiting_approval`` and then be approved.
This module never pushes branches, opens PRs, merges, deletes branches or
worktrees, runs cleanup, mutates approval records, or changes scheduler
lifecycle behavior.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from agent_taskflow.executors.base import Executor, ExecutorContext, ExecutorResult
from agent_taskflow.models import utc_now_iso


CLAUDE_CODE_EXECUTOR_NAME = "claude-code"
CLAUDE_CODE_PROMPT_FILENAME = "claude-code-implementer-prompt.md"
CLAUDE_CODE_EXECUTION_ARTIFACT_FILENAME = "claude-code-execution.json"
CLAUDE_CODE_EXECUTION_SCHEMA_VERSION = "claude_code_executor.v1"

# Internal artifact status vocabulary. This is richer than the constrained
# ExecutorResult status vocabulary so the artifact can faithfully record what
# the bounded executor attempt actually did. ``completed`` here means only that
# the bounded executor attempt completed — it does NOT mean validators passed,
# the task was approved, or the task reached ``waiting_approval``.
CLAUDE_CODE_ARTIFACT_STATUSES = frozenset(
    {"dry_run", "completed", "failed", "timed_out", "blocked", "tool_error"}
)



@dataclass(frozen=True)
class ClaudeCodePreflightResult:
    """Deterministic preflight outcome for a Claude Code executor run."""

    ok: bool
    blocking_errors: tuple[str, ...]
    warnings: tuple[str, ...]


def check_claude_code_preflight(
    *,
    task_key: str | None,
    repo_root: Path | str | None,
    worktree_path: Path | str | None,
    worktree_root: Path | str | None = None,
    enable_invocation: bool = False,
    command: Sequence[str] | None = None,
) -> ClaudeCodePreflightResult:
    """Validate Claude Code executor preconditions without side effects.

    Returns a structured result rather than raising so the executor can record a
    blocked artifact and a blocked executor result instead of crashing.
    """

    errors: list[str] = []
    warnings: list[str] = []

    if task_key is None or not str(task_key).strip():
        errors.append("task_key is required")

    if repo_root is None:
        errors.append("repo root is required")
    else:
        repo_root_path = Path(repo_root)
        if not repo_root_path.exists():
            errors.append(f"repo root does not exist: {repo_root_path}")
        elif not repo_root_path.is_dir():
            errors.append(f"repo root is not a directory: {repo_root_path}")

    resolved_worktree: Path | None = None
    if worktree_path is None:
        errors.append("worktree path is required")
    else:
        worktree = Path(worktree_path)
        if not worktree.exists():
            errors.append(f"worktree path does not exist: {worktree}")
        elif not worktree.is_dir():
            errors.append(f"worktree path is not a directory: {worktree}")
        else:
            resolved_worktree = worktree

    if worktree_root is not None and resolved_worktree is not None:
        root = Path(worktree_root)
        try:
            resolved_worktree.resolve().relative_to(root.resolve())
        except ValueError:
            errors.append(
                "worktree path is not inside the configured worktree root: "
                f"{resolved_worktree} not under {root}"
            )

    if enable_invocation and not _normalize_command(command):
        errors.append(
            "real invocation is enabled but no Claude Code command is configured"
        )

    return ClaudeCodePreflightResult(
        ok=not errors,
        blocking_errors=tuple(errors),
        warnings=tuple(warnings),
    )


def render_claude_code_implementer_prompt(
    *,
    task_key: str,
    worktree_path: Path,
    repo_root: Path | None,
    task_summary: str | None,
) -> str:
    """Render the deterministic Claude Code bounded implementer prompt.

    The prompt explicitly scopes Claude Code to a bounded implementer role with
    no approval, validation, merge, cleanup, or deletion authority. It is fully
    determined by its inputs so repeated generation is reproducible.
    """

    summary_body = (task_summary or "").strip() or "(no task/spec summary available)"
    repo_root_line = str(repo_root) if repo_root is not None else "(not provided)"

    return "\n".join(
        [
            f"# Claude Code Bounded Implementer Executor — {task_key}",
            "",
            "You are running as the **Claude Code Bounded Implementer Executor**.",
            "",
            "Core rule:",
            "",
            "> Claude Code writes code; it does not decide whether the task is done.",
            "",
            "## Task binding",
            "",
            f"- Task key: {task_key}",
            f"- Prepared worktree path: {worktree_path}",
            f"- Repository root: {repo_root_line}",
            "",
            "## Task / spec summary",
            "",
            summary_body,
            "",
            "## Allowed role",
            "",
            "- You are a bounded implementation worker.",
            "- Make only the minimal, safe code/doc/test changes the task requires.",
            "- Keep ALL work inside the prepared worktree shown above.",
            "",
            "## Disallowed authority",
            "",
            "You do NOT have any of the following authority:",
            "",
            "- approval authority — you may not approve the task.",
            "- validation authority — you may not decide validators passed.",
            "- merge authority — you may not merge.",
            "- cleanup authority — you may not run cleanup.",
            "- deletion authority — you may not delete branches or worktrees.",
            "- scheduler/lifecycle authority — you may not change task lifecycle state.",
            "",
            "## Hard prohibitions",
            "",
            "- Do not push branches.",
            "- Do not open or modify pull requests.",
            "- Do not merge.",
            "- Do not delete branches or worktrees.",
            "- Do not run cleanup.",
            "- Do not mutate GitHub issues, labels, or projects.",
            "- Do not set the task to waiting_approval or any approval state.",
            "",
            "## Expected output",
            "",
            "- Code changes only, inside the prepared worktree.",
            "- Report the changed files you produced.",
            "- Report the commands you ran.",
            "",
            "## Who decides completion",
            "",
            "- Deterministic validators run after you and decide pass/fail.",
            "- A Codex advisory artifact is required evidence, not a decision.",
            "- Human final review decides approval. Your run is implementation only.",
            "",
        ]
    )


class ClaudeCodeExecutor(Executor):
    """Bounded implementer executor backed by Claude Code.

    Prompt-only / dry-run by default. Real invocation is opt-in and requires
    both ``enable_invocation=True`` and an explicitly configured ``command``.
    """

    name = CLAUDE_CODE_EXECUTOR_NAME

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        enable_invocation: bool = False,
        worktree_root: Path | str | None = None,
    ) -> None:
        self.command = _normalize_command(command)
        self.enable_invocation = bool(enable_invocation)
        self.worktree_root = Path(worktree_root) if worktree_root is not None else None

        if self.enable_invocation and not self.command:
            raise ValueError(
                "ClaudeCodeExecutor real invocation requires an explicit command"
            )

    def run(self, context: ExecutorContext) -> ExecutorResult:
        # The artifact directory is the safe place to write evidence; create it
        # first so a blocked attempt can still record its execution artifact.
        context.artifact_dir.mkdir(parents=True, exist_ok=True)

        preflight = check_claude_code_preflight(
            task_key=context.task_key,
            repo_root=context.repo_root,
            worktree_path=context.worktree_path,
            worktree_root=self.worktree_root,
            enable_invocation=self.enable_invocation,
            command=self.command,
        )
        if not preflight.ok:
            summary = "Claude Code executor preflight failed: " + "; ".join(
                preflight.blocking_errors
            )
            artifact_path = self._write_execution_artifact(
                context,
                status="blocked",
                started_at=None,
                finished_at=None,
                exit_code=None,
                timed_out=False,
                blocking_errors=list(preflight.blocking_errors),
                warnings=list(preflight.warnings),
                prompt_path=None,
                stdout_path=None,
                stderr_path=None,
            )
            return ExecutorResult(
                executor=self.name,
                status="blocked",
                exit_code=None,
                log_path=None,
                summary=summary,
                artifacts={"claude_code_execution": artifact_path},
            )

        prompt_path = context.artifact_dir / CLAUDE_CODE_PROMPT_FILENAME
        prompt_text = render_claude_code_implementer_prompt(
            task_key=context.task_key,
            worktree_path=context.worktree_path,
            repo_root=context.repo_root,
            task_summary=self._load_task_summary(context),
        )
        prompt_path.write_text(prompt_text, encoding="utf-8")

        if not self.enable_invocation:
            return self._dry_run_result(context, prompt_path)

        return self._invoke_result(context, prompt_path, prompt_text)

    # -- dry run -----------------------------------------------------------

    def _dry_run_result(
        self, context: ExecutorContext, prompt_path: Path
    ) -> ExecutorResult:
        now = utc_now_iso()
        artifact_path = self._write_execution_artifact(
            context,
            status="dry_run",
            started_at=now,
            finished_at=now,
            exit_code=None,
            timed_out=False,
            blocking_errors=[],
            warnings=[],
            prompt_path=prompt_path,
            stdout_path=None,
            stderr_path=None,
        )
        return ExecutorResult(
            executor=self.name,
            # dry_run maps to the constrained "completed" executor status: the
            # bounded executor attempt completed. This is NOT task approval,
            # validator success, or waiting_approval.
            status="completed",
            exit_code=None,
            log_path=None,
            summary=(
                "Claude Code executor dry-run: implementer prompt generated; "
                "Claude Code was not invoked."
            ),
            artifacts={
                "claude_code_prompt": prompt_path,
                "claude_code_execution": artifact_path,
            },
        )

    # -- opt-in real invocation -------------------------------------------

    def _invoke_result(
        self,
        context: ExecutorContext,
        prompt_path: Path,
        prompt_text: str,
    ) -> ExecutorResult:
        assert self.command  # guaranteed by preflight + constructor
        # The implementer prompt is delivered over stdin, never as an argv
        # element: argv would leak prompt content through process listings and
        # can exceed the OS argument-size limit for large task summaries.
        command = list(self.command)

        stdout_path = context.artifact_dir / "claude-code-stdout.log"
        stderr_path = context.artifact_dir / "claude-code-stderr.log"

        run_env = None
        if context.env is not None:
            run_env = os.environ.copy()
            run_env.update(context.env)

        started_at = utc_now_iso()
        completed: subprocess.CompletedProcess[str] | None = None
        timed_out = False
        tool_error: str | None = None

        try:
            completed = subprocess.run(
                command,
                cwd=context.worktree_path,
                input=prompt_text,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=context.timeout_seconds,
                env=run_env,
                text=True,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout_path.write_text(_decode_stream(exc.stdout), encoding="utf-8")
            stderr_path.write_text(_decode_stream(exc.stderr), encoding="utf-8")
        except OSError as exc:
            # Covers FileNotFoundError, PermissionError (command exists but is
            # not executable), NotADirectoryError, E2BIG, and other startup
            # failures. These must be recorded as a blocked result with an
            # execution artifact rather than escaping the executor.
            tool_error = f"Claude Code command failed to start: {exc}"
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(f"{tool_error}\n", encoding="utf-8")

        finished_at = utc_now_iso()

        if completed is not None:
            stdout_path.write_text(completed.stdout or "", encoding="utf-8")
            stderr_path.write_text(completed.stderr or "", encoding="utf-8")

        artifacts = {
            "claude_code_prompt": prompt_path,
            "claude_code_stdout": stdout_path,
            "claude_code_stderr": stderr_path,
        }

        if timed_out:
            artifact_path = self._write_execution_artifact(
                context,
                status="timed_out",
                started_at=started_at,
                finished_at=finished_at,
                exit_code=None,
                timed_out=True,
                blocking_errors=[
                    f"Claude Code timed out after {context.timeout_seconds} seconds"
                ],
                warnings=[],
                prompt_path=prompt_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            artifacts["claude_code_execution"] = artifact_path
            return ExecutorResult(
                executor=self.name,
                status="failed",
                exit_code=None,
                log_path=stdout_path,
                summary=(
                    f"Claude Code timed out after {context.timeout_seconds} seconds."
                ),
                artifacts=artifacts,
            )

        if tool_error is not None:
            artifact_path = self._write_execution_artifact(
                context,
                status="tool_error",
                started_at=started_at,
                finished_at=finished_at,
                exit_code=None,
                timed_out=False,
                blocking_errors=[tool_error],
                warnings=[],
                prompt_path=prompt_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            artifacts["claude_code_execution"] = artifact_path
            return ExecutorResult(
                executor=self.name,
                status="blocked",
                exit_code=None,
                log_path=stderr_path,
                summary=tool_error,
                artifacts=artifacts,
            )

        assert completed is not None
        ok = completed.returncode == 0
        artifact_status = "completed" if ok else "failed"
        artifact_path = self._write_execution_artifact(
            context,
            status=artifact_status,
            started_at=started_at,
            finished_at=finished_at,
            exit_code=completed.returncode,
            timed_out=False,
            blocking_errors=[],
            warnings=[],
            prompt_path=prompt_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        artifacts["claude_code_execution"] = artifact_path
        summary = (
            "Claude Code completed successfully."
            if ok
            else f"Claude Code failed with exit code {completed.returncode}."
        )
        return ExecutorResult(
            executor=self.name,
            status="completed" if ok else "failed",
            exit_code=completed.returncode,
            log_path=stdout_path,
            summary=summary,
            artifacts=artifacts,
        )

    # -- helpers -----------------------------------------------------------

    def _load_task_summary(self, context: ExecutorContext) -> str | None:
        if context.prompt_path is None:
            return None
        try:
            if not context.prompt_path.exists():
                return None
            text = context.prompt_path.read_text(encoding="utf-8")
        except OSError:
            return None
        return text.strip() or None

    def _changed_files(self, worktree_path: Path) -> list[str]:
        """Return changed file paths from ``git status --porcelain -z``.

        Best-effort evidence only; failures yield an empty list rather than
        raising. This never mutates the worktree.

        ``-z`` avoids quotePath escaping and represents rename/copy records as
        NUL-delimited path pairs. For rename/copy entries, porcelain v1 with
        ``-z`` reports the destination path first, followed by the source path;
        the destination is the useful evidence for reviewer-facing artifacts.
        """
        try:
            completed = subprocess.run(
                ["git", "-c", "core.quotePath=false", "status", "--porcelain", "-z"],
                cwd=worktree_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                check=False,
            )
        except OSError:
            return []

        if completed.returncode != 0:
            return []

        stdout = completed.stdout or b""
        if isinstance(stdout, str):
            raw = stdout
        else:
            raw = stdout.decode("utf-8", errors="surrogateescape")

        entries = raw.split("\0")
        changed: list[str] = []
        index = 0

        while index < len(entries):
            record = entries[index]
            index += 1

            if not record:
                continue

            if len(record) <= 3:
                entry = record.strip()
                if entry:
                    changed.append(entry)
                continue

            status = record[:2]
            path = record[3:]

            if path:
                changed.append(path)

            # With porcelain v1 -z, rename/copy entries include a second NUL
            # field containing the source path. Skip that source path because
            # the artifact should report the current destination path.
            if ("R" in status or "C" in status) and index < len(entries):
                index += 1

        return changed

    def _write_execution_artifact(
        self,
        context: ExecutorContext,
        *,
        status: str,
        started_at: str | None,
        finished_at: str | None,
        exit_code: int | None,
        timed_out: bool,
        blocking_errors: list[str],
        warnings: list[str],
        prompt_path: Path | None,
        stdout_path: Path | None,
        stderr_path: Path | None,
    ) -> Path:
        if status not in CLAUDE_CODE_ARTIFACT_STATUSES:
            raise ValueError(f"Invalid Claude Code artifact status: {status!r}")

        # Changed files are only meaningful after a real invocation attempt.
        changed_files: list[str] = []
        if status in {"completed", "failed", "timed_out"}:
            changed_files = self._changed_files(context.worktree_path)

        payload = {
            "schema_version": CLAUDE_CODE_EXECUTION_SCHEMA_VERSION,
            "executor": CLAUDE_CODE_EXECUTOR_NAME,
            "task_key": context.task_key,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "worktree_path": str(context.worktree_path),
            "repo_root": str(context.repo_root) if context.repo_root else None,
            "cwd": str(context.worktree_path),
            "command": list(self.command) if self.command else [],
            "invocation_enabled": self.enable_invocation,
            "prompt_path": str(prompt_path) if prompt_path is not None else None,
            "stdout_path": str(stdout_path) if stdout_path is not None else None,
            "stderr_path": str(stderr_path) if stderr_path is not None else None,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "blocking_errors": list(blocking_errors),
            "warnings": list(warnings),
            "changed_files": changed_files,
            # Authority invariants. Claude Code is a bounded implementer only.
            "validation_authority": "none",
            "approval_authority": "none",
            "merge_authority": "none",
            "cleanup_authority": "none",
            "human_review_required": True,
        }

        artifact_path = context.artifact_dir / CLAUDE_CODE_EXECUTION_ARTIFACT_FILENAME
        artifact_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return artifact_path


def _normalize_command(command: Sequence[str] | None) -> list[str]:
    if command is None:
        return []
    if isinstance(command, str):
        raise TypeError("command must be a sequence of strings, not a raw string")
    normalized: list[str] = []
    for part in command:
        if not isinstance(part, str):
            raise TypeError("command entries must be strings")
        stripped = part.strip()
        if not stripped:
            raise ValueError("command entries must not be empty")
        normalized.append(stripped)
    return normalized


def _decode_stream(stream: object) -> str:
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return str(stream)


__all__ = [
    "CLAUDE_CODE_ARTIFACT_STATUSES",
    "CLAUDE_CODE_EXECUTION_ARTIFACT_FILENAME",
    "CLAUDE_CODE_EXECUTION_SCHEMA_VERSION",
    "CLAUDE_CODE_EXECUTOR_NAME",
    "CLAUDE_CODE_PROMPT_FILENAME",
    "ClaudeCodeExecutor",
    "ClaudeCodePreflightResult",
    "check_claude_code_preflight",
    "render_claude_code_implementer_prompt",
]
