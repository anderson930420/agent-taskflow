from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_taskflow.executors import (
    ExecutorContext,
    OpenCodeExecutor,
    build_opencode_executor,
    get_executor,
    list_executor_names,
)


class OpenCodeExecutorTestCase(unittest.TestCase):
    def make_context(
        self,
        tmp_path: Path,
        *,
        model: str | None = "minimax-test-model",
        prompt: str | None = "Implement the task.",
        prompt_path: Path | None | str = "default",
    ) -> ExecutorContext:
        worktree_path = tmp_path / "worktree"
        artifact_dir = tmp_path / "artifacts"
        worktree_path.mkdir()
        artifact_dir.mkdir()

        resolved_prompt_path: Path | None
        if prompt_path == "default":
            resolved_prompt_path = tmp_path / "implementation_prompt.md"
            if prompt is not None:
                resolved_prompt_path.write_text(prompt, encoding="utf-8")
        else:
            resolved_prompt_path = prompt_path

        return ExecutorContext(
            task_key="AT-0005",
            project="agent-taskflow",
            worktree_path=worktree_path,
            artifact_dir=artifact_dir,
            prompt_path=resolved_prompt_path,
            model=model,
        )

    def make_subprocess_side_effect(
        self,
        *,
        opencode_returncode: int = 0,
        status_returncode: int = 0,
        diff_returncode: int = 0,
    ):
        calls: list[list[str]] = []

        def side_effect(command, **kwargs):
            calls.append(command)

            self.assertFalse(kwargs.get("shell"))

            if command[:2] == ["opencode", "run"]:
                stdout = kwargs.get("stdout")
                if stdout is not None:
                    stdout.write('{"event":"mock-opencode-output"}\n')
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=opencode_returncode,
                )

            if command == ["git", "status", "--short"]:
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=status_returncode,
                    stdout=" M README.md\n",
                )

            if command == ["git", "diff"]:
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=diff_returncode,
                    stdout="diff --git a/README.md b/README.md\n",
                )

            raise AssertionError(f"Unexpected command: {command!r}")

        return calls, side_effect


class OpenCodeBlockedTests(OpenCodeExecutorTestCase):
    def test_missing_model_returns_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), model=None)
            result = OpenCodeExecutor().run(context)

            self.assertEqual(result.status, "blocked")
            self.assertIsNone(result.exit_code)
            self.assertIsNone(result.log_path)
            self.assertIn("requires a model", result.summary or "")

    def test_missing_prompt_path_returns_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(
                Path(tmp),
                model="minimax-test-model",
                prompt_path=None,
            )
            result = OpenCodeExecutor().run(context)

            self.assertEqual(result.status, "blocked")
            self.assertIn("requires context.prompt_path", result.summary or "")

    def test_nonexistent_prompt_path_returns_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_prompt = Path(tmp) / "missing_prompt.md"
            context = self.make_context(
                Path(tmp),
                model="minimax-test-model",
                prompt_path=missing_prompt,
            )
            result = OpenCodeExecutor().run(context)

            self.assertEqual(result.status, "blocked")
            self.assertIn("does not exist", result.summary or "")


class OpenCodeCommandTests(OpenCodeExecutorTestCase):
    def test_opencode_command_uses_expected_arguments_and_worktree_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(
                Path(tmp),
                model="context-model",
                prompt="Please edit safely.",
            )
            calls, side_effect = self.make_subprocess_side_effect()

            with patch(
                "agent_taskflow.executors.opencode.subprocess.run",
                side_effect=side_effect,
            ) as run_mock:
                result = OpenCodeExecutor(model="constructor-model").run(context)

            self.assertEqual(result.status, "completed")

            opencode_call = calls[0]
            self.assertEqual(opencode_call[:2], ["opencode", "run"])
            self.assertIn("--dir", opencode_call)
            self.assertEqual(
                opencode_call[opencode_call.index("--dir") + 1],
                str(context.worktree_path),
            )
            self.assertIn("--model", opencode_call)
            self.assertEqual(
                opencode_call[opencode_call.index("--model") + 1],
                "constructor-model",
            )
            self.assertIn("--format", opencode_call)
            self.assertEqual(opencode_call[opencode_call.index("--format") + 1], "json")
            self.assertIn("--title", opencode_call)
            self.assertEqual(
                opencode_call[opencode_call.index("--title") + 1],
                "AT-0005 implementation",
            )
            self.assertEqual(opencode_call[-1], "Please edit safely.")

            first_call_kwargs = run_mock.call_args_list[0].kwargs
            self.assertEqual(first_call_kwargs["cwd"], context.worktree_path)
            self.assertFalse(first_call_kwargs["shell"])

    def test_context_model_is_used_when_constructor_model_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), model="context-model")
            calls, side_effect = self.make_subprocess_side_effect()

            with patch(
                "agent_taskflow.executors.opencode.subprocess.run",
                side_effect=side_effect,
            ):
                result = OpenCodeExecutor().run(context)

            self.assertEqual(result.status, "completed")
            opencode_call = calls[0]
            self.assertEqual(
                opencode_call[opencode_call.index("--model") + 1],
                "context-model",
            )

    def test_extra_args_are_added_before_prompt_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), prompt="Prompt body.")
            calls, side_effect = self.make_subprocess_side_effect()

            with patch(
                "agent_taskflow.executors.opencode.subprocess.run",
                side_effect=side_effect,
            ):
                result = OpenCodeExecutor(extra_args=["--debug"]).run(context)

            self.assertEqual(result.status, "completed")
            opencode_call = calls[0]
            self.assertIn("--debug", opencode_call)
            self.assertEqual(opencode_call[-1], "Prompt body.")


class OpenCodeResultAndArtifactTests(OpenCodeExecutorTestCase):
    def test_exit_code_zero_returns_completed_and_writes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            _, side_effect = self.make_subprocess_side_effect(opencode_returncode=0)

            with patch(
                "agent_taskflow.executors.opencode.subprocess.run",
                side_effect=side_effect,
            ):
                result = OpenCodeExecutor().run(context)

            self.assertEqual(result.executor, "opencode")
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.exit_code, 0)
            self.assertIsNotNone(result.log_path)
            assert result.log_path is not None
            self.assertTrue(result.log_path.exists())
            self.assertIn(
                "mock-opencode-output",
                result.log_path.read_text(encoding="utf-8"),
            )
            self.assertEqual(result.artifacts["opencode_log"], result.log_path)
            self.assertTrue(result.artifacts["git_status"].exists())
            self.assertTrue(result.artifacts["git_diff"].exists())
            self.assertIn(
                "M README.md",
                result.artifacts["git_status"].read_text(encoding="utf-8"),
            )
            self.assertIn(
                "diff --git",
                result.artifacts["git_diff"].read_text(encoding="utf-8"),
            )

    def test_nonzero_exit_code_returns_failed_but_keeps_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            _, side_effect = self.make_subprocess_side_effect(opencode_returncode=7)

            with patch(
                "agent_taskflow.executors.opencode.subprocess.run",
                side_effect=side_effect,
            ):
                result = OpenCodeExecutor().run(context)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.exit_code, 7)
            self.assertTrue(result.artifacts["opencode_log"].exists())
            self.assertTrue(result.artifacts["git_status"].exists())
            self.assertTrue(result.artifacts["git_diff"].exists())

    def test_git_status_and_diff_are_attempted_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            calls, side_effect = self.make_subprocess_side_effect(opencode_returncode=2)

            with patch(
                "agent_taskflow.executors.opencode.subprocess.run",
                side_effect=side_effect,
            ):
                result = OpenCodeExecutor().run(context)

            self.assertEqual(result.status, "failed")
            self.assertIn(["git", "status", "--short"], calls)
            self.assertIn(["git", "diff"], calls)

    def test_git_capture_failure_is_reported_without_overriding_opencode_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            _, side_effect = self.make_subprocess_side_effect(
                opencode_returncode=5,
                status_returncode=128,
                diff_returncode=129,
            )

            with patch(
                "agent_taskflow.executors.opencode.subprocess.run",
                side_effect=side_effect,
            ):
                result = OpenCodeExecutor().run(context)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.exit_code, 5)
            self.assertIn("artifact capture failed", result.summary or "")

    def test_file_not_found_returns_blocked_and_still_attempts_git_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            calls: list[list[str]] = []

            def side_effect(command, **kwargs):
                calls.append(command)
                self.assertFalse(kwargs.get("shell"))
                if command[:2] == ["missing-opencode", "run"]:
                    raise FileNotFoundError("missing-opencode")
                if command == ["git", "status", "--short"]:
                    return subprocess.CompletedProcess(
                        args=command,
                        returncode=0,
                        stdout="",
                    )
                if command == ["git", "diff"]:
                    return subprocess.CompletedProcess(
                        args=command,
                        returncode=0,
                        stdout="",
                    )
                raise AssertionError(f"Unexpected command: {command!r}")

            with patch(
                "agent_taskflow.executors.opencode.subprocess.run",
                side_effect=side_effect,
            ):
                result = OpenCodeExecutor(opencode_bin="missing-opencode").run(context)

            self.assertEqual(result.status, "blocked")
            self.assertIsNone(result.exit_code)
            self.assertIn("failed to start", result.summary or "")
            self.assertIn(["git", "status", "--short"], calls)
            self.assertIn(["git", "diff"], calls)


class OpenCodeRegistryTests(unittest.TestCase):
    def test_registry_lists_opencode(self) -> None:
        self.assertIn("opencode", list_executor_names())

    def test_registry_returns_opencode_executor(self) -> None:
        executor = get_executor("opencode", model="minimax-test-model")

        self.assertIsInstance(executor, OpenCodeExecutor)

    def test_build_opencode_executor_returns_opencode_executor(self) -> None:
        executor = build_opencode_executor(model="minimax-test-model")

        self.assertIsInstance(executor, OpenCodeExecutor)


if __name__ == "__main__":
    unittest.main()
