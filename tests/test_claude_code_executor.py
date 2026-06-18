"""Tests for the Claude Code Bounded Implementer Executor (v0.2.7)."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_taskflow.executors import (
    CLAUDE_CODE_EXECUTION_ARTIFACT_FILENAME,
    CLAUDE_CODE_EXECUTION_SCHEMA_VERSION,
    CLAUDE_CODE_PROMPT_FILENAME,
    ClaudeCodeExecutor,
    ExecutorContext,
    build_claude_code_executor,
    check_claude_code_preflight,
    get_executor,
    list_executor_names,
    render_claude_code_implementer_prompt,
)


class ClaudeCodeTestCase(unittest.TestCase):
    def make_context(
        self,
        tmp_path: Path,
        *,
        repo_root: Path | None | str = "default",
        worktree_exists: bool = True,
        prompt: str | None = None,
        timeout_seconds: int | None = None,
    ) -> ExecutorContext:
        repo = tmp_path / "repo"
        repo.mkdir(exist_ok=True)
        worktree_path = tmp_path / "worktree"
        artifact_dir = tmp_path / "artifacts"
        if worktree_exists:
            worktree_path.mkdir(exist_ok=True)
        artifact_dir.mkdir(exist_ok=True)

        resolved_repo_root: Path | None
        if repo_root == "default":
            resolved_repo_root = repo
        else:
            resolved_repo_root = repo_root  # type: ignore[assignment]

        prompt_path: Path | None = None
        if prompt is not None:
            prompt_path = tmp_path / "implementation_prompt.md"
            prompt_path.write_text(prompt, encoding="utf-8")

        return ExecutorContext(
            task_key="AT-GH-123",
            project="agent-taskflow",
            worktree_path=worktree_path,
            artifact_dir=artifact_dir,
            prompt_path=prompt_path,
            repo_root=resolved_repo_root,
            timeout_seconds=timeout_seconds,
        )

    def read_execution_artifact(self, context: ExecutorContext) -> dict:
        path = context.artifact_dir / CLAUDE_CODE_EXECUTION_ARTIFACT_FILENAME
        return json.loads(path.read_text(encoding="utf-8"))


class PromptGenerationTests(ClaudeCodeTestCase):
    def render(self) -> str:
        return render_claude_code_implementer_prompt(
            task_key="AT-GH-123",
            worktree_path=Path("/repo/.worktrees/AT-GH-123"),
            repo_root=Path("/repo"),
            task_summary="Implement the widget.",
        )

    def test_prompt_includes_task_key(self) -> None:  # (1)
        self.assertIn("AT-GH-123", self.render())

    def test_prompt_includes_worktree_path(self) -> None:  # (2)
        self.assertIn("/repo/.worktrees/AT-GH-123", self.render())

    def test_prompt_states_no_approval_authority(self) -> None:  # (3)
        self.assertIn("approval authority", self.render())
        self.assertIn("may not approve", self.render())

    def test_prompt_states_no_validation_authority(self) -> None:  # (4)
        self.assertIn("validation authority", self.render())
        self.assertIn("may not decide validators", self.render())

    def test_prompt_forbids_push_pr_merge_delete_cleanup(self) -> None:  # (5)
        prompt = self.render()
        self.assertIn("Do not push branches.", prompt)
        self.assertIn("Do not open or modify pull requests.", prompt)
        self.assertIn("Do not merge.", prompt)
        self.assertIn("Do not delete branches or worktrees.", prompt)
        self.assertIn("Do not run cleanup.", prompt)

    def test_prompt_embeds_task_summary(self) -> None:
        self.assertIn("Implement the widget.", self.render())

    def test_prompt_states_validators_and_human_decide(self) -> None:
        prompt = self.render()
        self.assertIn("Deterministic validators", prompt)
        self.assertIn("Human final review", prompt)


class DryRunTests(ClaudeCodeTestCase):
    def test_dry_run_creates_execution_artifact_and_prompt(self) -> None:  # (6)
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            result = ClaudeCodeExecutor().run(context)

            self.assertEqual(result.status, "completed")
            prompt_path = context.artifact_dir / CLAUDE_CODE_PROMPT_FILENAME
            self.assertTrue(prompt_path.exists())
            artifact = self.read_execution_artifact(context)
            self.assertEqual(artifact["status"], "dry_run")
            self.assertEqual(
                artifact["schema_version"], CLAUDE_CODE_EXECUTION_SCHEMA_VERSION
            )

    def test_dry_run_does_not_invoke_subprocess(self) -> None:  # (7)
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            with patch(
                "agent_taskflow.executors.claude_code.subprocess.run"
            ) as run_mock:
                result = ClaudeCodeExecutor().run(context)

            self.assertEqual(result.status, "completed")
            run_mock.assert_not_called()

    def test_dry_run_cwd_is_prepared_worktree(self) -> None:  # (11)
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            ClaudeCodeExecutor().run(context)
            artifact = self.read_execution_artifact(context)
            self.assertEqual(artifact["cwd"], str(context.worktree_path))

    def test_dry_run_command_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            ClaudeCodeExecutor().run(context)
            artifact = self.read_execution_artifact(context)
            self.assertEqual(artifact["command"], [])
            self.assertFalse(artifact["invocation_enabled"])

    def test_dry_run_uses_prompt_path_as_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), prompt="Build a thing safely.")
            ClaudeCodeExecutor().run(context)
            prompt_path = context.artifact_dir / CLAUDE_CODE_PROMPT_FILENAME
            self.assertIn(
                "Build a thing safely.", prompt_path.read_text(encoding="utf-8")
            )


class PreflightTests(ClaudeCodeTestCase):
    def test_missing_task_key_blocks(self) -> None:  # (8)
        result = check_claude_code_preflight(
            task_key="",
            repo_root="/repo",
            worktree_path="/repo/.worktrees/x",
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("task_key" in e for e in result.blocking_errors))

    def test_missing_worktree_path_blocks(self) -> None:  # (9)
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), worktree_exists=True)
            # Point at a non-existent worktree directory.
            missing = Path(tmp) / "missing-worktree"
            ctx = ExecutorContext(
                task_key=context.task_key,
                project=context.project,
                worktree_path=missing,
                artifact_dir=context.artifact_dir,
                repo_root=context.repo_root,
            )
            result = ClaudeCodeExecutor().run(ctx)
            self.assertEqual(result.status, "blocked")
            artifact = self.read_execution_artifact(ctx)
            self.assertEqual(artifact["status"], "blocked")
            self.assertTrue(
                any("worktree path" in e for e in artifact["blocking_errors"])
            )

    def test_missing_repo_root_blocks(self) -> None:  # (10)
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), repo_root=None)
            result = ClaudeCodeExecutor().run(context)
            self.assertEqual(result.status, "blocked")
            artifact = self.read_execution_artifact(context)
            self.assertTrue(
                any("repo root" in e for e in artifact["blocking_errors"])
            )

    def test_worktree_root_containment_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            other_root = Path(tmp) / "elsewhere"
            other_root.mkdir()
            executor = ClaudeCodeExecutor(worktree_root=other_root)
            result = executor.run(context)
            self.assertEqual(result.status, "blocked")
            artifact = self.read_execution_artifact(context)
            self.assertTrue(
                any("worktree root" in e for e in artifact["blocking_errors"])
            )

    def test_enable_invocation_without_command_raises(self) -> None:  # (17)
        with self.assertRaises(ValueError):
            ClaudeCodeExecutor(enable_invocation=True)


class AuthorityArtifactTests(ClaudeCodeTestCase):
    def test_artifact_records_no_authority_and_human_review(self) -> None:  # (12-16)
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            ClaudeCodeExecutor().run(context)
            artifact = self.read_execution_artifact(context)
            self.assertEqual(artifact["validation_authority"], "none")
            self.assertEqual(artifact["approval_authority"], "none")
            self.assertEqual(artifact["merge_authority"], "none")
            self.assertEqual(artifact["cleanup_authority"], "none")
            self.assertIs(artifact["human_review_required"], True)


class RealInvocationTests(ClaudeCodeTestCase):
    def make_side_effect(self, *, returncode: int = 0):
        calls = []

        def side_effect(command, **kwargs):
            calls.append((command, kwargs))
            if command[:1] == ["git"]:
                return subprocess.CompletedProcess(
                    args=command, returncode=0, stdout=" M README.md\n"
                )
            return subprocess.CompletedProcess(
                args=command,
                returncode=returncode,
                stdout="claude stdout\n",
                stderr="claude stderr\n",
            )

        return calls, side_effect

    def test_real_invocation_requires_explicit_command(self) -> None:  # (17)
        executor = build_claude_code_executor(
            command=["claude", "-p"], enable_invocation=True
        )
        self.assertEqual(executor.command, ["claude", "-p"])

    def test_real_invocation_captures_stdout_stderr(self) -> None:  # (18)
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            calls, side_effect = self.make_side_effect(returncode=0)
            executor = ClaudeCodeExecutor(
                command=["claude", "-p"], enable_invocation=True
            )
            with patch(
                "agent_taskflow.executors.claude_code.subprocess.run",
                side_effect=side_effect,
            ):
                result = executor.run(context)

            self.assertEqual(result.status, "completed")
            stdout_path = result.artifacts["claude_code_stdout"]
            stderr_path = result.artifacts["claude_code_stderr"]
            self.assertIn("claude stdout", stdout_path.read_text(encoding="utf-8"))
            self.assertIn("claude stderr", stderr_path.read_text(encoding="utf-8"))

            artifact = self.read_execution_artifact(context)
            self.assertEqual(artifact["status"], "completed")
            self.assertEqual(artifact["exit_code"], 0)

            # Claude command was invoked with cwd = prepared worktree.  (11)
            claude_call = next(c for c in calls if c[0][:1] == ["claude"])
            self.assertEqual(claude_call[1]["cwd"], context.worktree_path)
            self.assertFalse(claude_call[1]["shell"])
            self.assertEqual(claude_call[0][-1], result.artifacts["claude_code_prompt"]
                             .read_text(encoding="utf-8"))

    def test_nonzero_exit_records_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            _, side_effect = self.make_side_effect(returncode=3)
            executor = ClaudeCodeExecutor(
                command=["claude", "-p"], enable_invocation=True
            )
            with patch(
                "agent_taskflow.executors.claude_code.subprocess.run",
                side_effect=side_effect,
            ):
                result = executor.run(context)
            self.assertEqual(result.status, "failed")
            artifact = self.read_execution_artifact(context)
            self.assertEqual(artifact["status"], "failed")
            self.assertEqual(artifact["exit_code"], 3)

    def test_timeout_recorded_deterministically(self) -> None:  # (19)
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), timeout_seconds=5)
            executor = ClaudeCodeExecutor(
                command=["claude", "-p"], enable_invocation=True
            )

            def side_effect(command, **kwargs):
                if command[:1] == ["git"]:
                    return subprocess.CompletedProcess(
                        args=command, returncode=0, stdout=""
                    )
                raise subprocess.TimeoutExpired(cmd=command, timeout=5)

            with patch(
                "agent_taskflow.executors.claude_code.subprocess.run",
                side_effect=side_effect,
            ):
                result = executor.run(context)

            self.assertEqual(result.status, "failed")
            artifact = self.read_execution_artifact(context)
            self.assertEqual(artifact["status"], "timed_out")
            self.assertIs(artifact["timed_out"], True)
            self.assertIsNone(artifact["exit_code"])

    def test_command_not_found_records_tool_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            executor = ClaudeCodeExecutor(
                command=["missing-claude", "-p"], enable_invocation=True
            )

            def side_effect(command, **kwargs):
                if command[:1] == ["git"]:
                    return subprocess.CompletedProcess(
                        args=command, returncode=0, stdout=""
                    )
                raise FileNotFoundError("missing-claude")

            with patch(
                "agent_taskflow.executors.claude_code.subprocess.run",
                side_effect=side_effect,
            ):
                result = executor.run(context)

            self.assertEqual(result.status, "blocked")
            artifact = self.read_execution_artifact(context)
            self.assertEqual(artifact["status"], "tool_error")


class RegistryTests(unittest.TestCase):
    def test_registry_lists_claude_code(self) -> None:
        self.assertIn("claude-code", list_executor_names())

    def test_get_executor_returns_claude_code(self) -> None:
        executor = get_executor("claude-code")
        self.assertIsInstance(executor, ClaudeCodeExecutor)
        self.assertFalse(executor.enable_invocation)

    def test_get_executor_claude_code_opt_in(self) -> None:
        executor = get_executor(
            "claude-code",
            claude_command=["claude", "-p"],
            claude_enable_invocation=True,
        )
        self.assertIsInstance(executor, ClaudeCodeExecutor)
        self.assertTrue(executor.enable_invocation)


if __name__ == "__main__":
    unittest.main()
