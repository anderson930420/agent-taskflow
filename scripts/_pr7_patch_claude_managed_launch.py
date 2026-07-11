#!/usr/bin/env python3
"""One-shot PR-7 patch; this file removes itself after applying."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "agent_taskflow/executors/claude_code.py"
text = TARGET.read_text(encoding="utf-8")

old_import = (
    "from agent_taskflow.atomic_write import atomic_write_json, atomic_write_text\n"
    "from agent_taskflow.executors.base import Executor, ExecutorContext, ExecutorResult\n"
)
new_import = (
    "from agent_taskflow.atomic_write import atomic_write_json, atomic_write_text\n"
    "from agent_taskflow.executor_launch import ExecutorLaunchSpec, run_managed_process\n"
    "from agent_taskflow.executors.base import Executor, ExecutorContext, ExecutorResult\n"
)
if old_import not in text:
    raise RuntimeError("Claude Code import anchor missing")
text = text.replace(old_import, new_import, 1)

start = text.index("    def _invoke_result(\n")
end = text.index("\n    # -- helpers", start)
replacement = '''    def _invoke_result(
        self,
        context: ExecutorContext,
        prompt_path: Path,
        prompt_text: str,
    ) -> ExecutorResult:
        assert self.command  # guaranteed by preflight + constructor
        # Prompt content stays on stdin and is never persisted in argv evidence.
        command = list(self.command)
        stdout_path = context.artifact_dir / "claude-code-stdout.log"
        stderr_path = context.artifact_dir / "claude-code-stderr.log"

        run_env = None
        if context.env is not None:
            run_env = os.environ.copy()
            run_env.update(context.env)

        started_at = utc_now_iso()
        completed: subprocess.CompletedProcess[str] | None = None
        managed = None
        timed_out = False
        tool_error: str | None = None
        blocked_by_kill = False

        if context.launch_binding is not None:
            managed = run_managed_process(
                context.launch_binding,
                ExecutorLaunchSpec(
                    executor_name=self.name,
                    argv=tuple(command),
                    cwd=context.worktree_path,
                    artifact_dir=context.artifact_dir,
                    timeout_seconds=context.timeout_seconds,
                    stdin_mode="text",
                    combined_output=False,
                    environment_keys=tuple((context.env or {}).keys()),
                ),
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                stdin_text=prompt_text,
                run_env=run_env,
            )
            timed_out = managed.timed_out
            blocked_by_kill = managed.kill_requested
            if managed.preflight_errors:
                tool_error = "Claude Code launch preflight failed: " + "; ".join(
                    managed.preflight_errors
                )
            elif managed.start_error is not None:
                tool_error = f"Claude Code command failed to start: {managed.start_error}"
            elif blocked_by_kill:
                tool_error = (
                    "Operator kill requested; Claude Code process group terminated."
                )
            elif not managed.verified_exit:
                tool_error = "Claude Code process-group exit could not be verified."
        else:
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
                atomic_write_text(stdout_path, _decode_stream(exc.stdout))
                atomic_write_text(stderr_path, _decode_stream(exc.stderr))
            except OSError as exc:
                tool_error = f"Claude Code command failed to start: {exc}"
                atomic_write_text(stdout_path, "")
                atomic_write_text(stderr_path, f"{tool_error}\\n")

        finished_at = utc_now_iso()
        if completed is not None:
            atomic_write_text(stdout_path, completed.stdout or "")
            atomic_write_text(stderr_path, completed.stderr or "")

        artifacts = {
            "claude_code_prompt": prompt_path,
            "claude_code_stdout": stdout_path,
            "claude_code_stderr": stderr_path,
        }
        if managed is not None:
            artifacts["executor_launch_spec"] = managed.launch_spec_path
            artifacts["executor_process_pid"] = managed.pid_manifest_path

        if timed_out:
            artifact_path = self._write_execution_artifact(
                context,
                status="timed_out",
                started_at=started_at,
                finished_at=finished_at,
                exit_code=managed.exit_code if managed is not None else None,
                timed_out=True,
                blocking_errors=[
                    f"Claude Code timed out after {context.timeout_seconds} seconds"
                ],
                warnings=[
                    f"process_group_verified_exit={managed.verified_exit}"
                ] if managed is not None else [],
                prompt_path=prompt_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            artifacts["claude_code_execution"] = artifact_path
            return ExecutorResult(
                executor=self.name,
                status="failed",
                exit_code=managed.exit_code if managed is not None else None,
                log_path=stdout_path,
                summary=(
                    f"Claude Code timed out after {context.timeout_seconds} seconds."
                ),
                artifacts=artifacts,
            )

        if tool_error is not None:
            artifact_path = self._write_execution_artifact(
                context,
                status="blocked" if blocked_by_kill else "tool_error",
                started_at=started_at,
                finished_at=finished_at,
                exit_code=managed.exit_code if managed is not None else None,
                timed_out=False,
                blocking_errors=[tool_error],
                warnings=[
                    f"process_group_verified_exit={managed.verified_exit}"
                ] if managed is not None else [],
                prompt_path=prompt_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            artifacts["claude_code_execution"] = artifact_path
            return ExecutorResult(
                executor=self.name,
                status="blocked",
                exit_code=managed.exit_code if managed is not None else None,
                log_path=stderr_path,
                summary=tool_error,
                artifacts=artifacts,
            )

        returncode = managed.exit_code if managed is not None else completed.returncode
        ok = returncode == 0
        artifact_status = "completed" if ok else "failed"
        artifact_path = self._write_execution_artifact(
            context,
            status=artifact_status,
            started_at=started_at,
            finished_at=finished_at,
            exit_code=returncode,
            timed_out=False,
            blocking_errors=[],
            warnings=(
                [f"process_group_verified_exit={managed.verified_exit}"]
                if managed is not None
                else []
            ),
            prompt_path=prompt_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        artifacts["claude_code_execution"] = artifact_path
        summary = (
            "Claude Code completed successfully."
            if ok
            else f"Claude Code failed with exit code {returncode}."
        )
        if managed is not None and managed.termination_reason == "executor_descendant_cleanup":
            ok = False
            summary = (
                "Claude Code leader exited with live descendants; "
                "process group was terminated."
            )
        return ExecutorResult(
            executor=self.name,
            status="completed" if ok else "failed",
            exit_code=returncode,
            log_path=stdout_path,
            summary=summary,
            artifacts=artifacts,
        )
'''
text = text[:start] + replacement + text[end:]
TARGET.write_text(text, encoding="utf-8")

(ROOT / ".github/workflows/ci.yml").write_text(
    '''name: CI\n\non:\n  pull_request:\n  push:\n    branches:\n      - main\n\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n\n      - uses: actions/setup-python@v5\n        with:\n          python-version: "3.12"\n\n      - name: Install package and dependencies\n        run: python -m pip install -e .\n\n      - name: Run unit tests\n        run: PYTHONPATH=. python -m unittest discover -s tests\n\n      - name: Compile sources\n        run: PYTHONPATH=. python -m compileall agent_taskflow scripts tests\n''',
    encoding="utf-8",
)
Path(__file__).unlink()
