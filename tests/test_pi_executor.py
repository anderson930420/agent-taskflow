"""Tests for the Pi CLI executor adapter."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_taskflow.executors import (
    ExecutorContext,
    PiExecutor,
    build_pi_executor,
    get_executor,
    list_executor_names,
)


class PiExecutorTestCase(unittest.TestCase):
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
            task_key="AT-0012",
            project="agent-taskflow",
            worktree_path=worktree_path,
            artifact_dir=artifact_dir,
            prompt_path=resolved_prompt_path,
            model=model,
        )

    def make_subprocess_side_effect(
        self,
        *,
        pi_returncode: int = 0,
    ):
        calls: list[list[str]] = []

        def side_effect(command, **kwargs):
            calls.append(command)
            self.assertFalse(kwargs.get("shell"))

            self.assertEqual(command[0], "pi")
            self.assertIn("-p", command)
            prompt_index = command.index("-p")
            stdout = kwargs.get("stdout")
            if stdout is not None:
                stdout.write(f"[pi] prompt: {command[prompt_index + 1]}\n")

            return subprocess.CompletedProcess(
                args=command,
                returncode=pi_returncode,
            )

        return calls, side_effect


class PiConstructorTests(PiExecutorTestCase):
    def test_constructor_accepts_provider_model_tools(self) -> None:
        executor = PiExecutor(
            provider="minimax",
            model="minimax-01",
            tools=["Read", "Write", "Bash"],
        )

        self.assertEqual(executor.provider, "minimax")
        self.assertEqual(executor.model, "minimax-01")
        self.assertEqual(executor.tools, ["Read", "Write", "Bash"])
        self.assertEqual(executor.pi_bin, "pi")
        self.assertTrue(executor.no_session)

    def test_constructor_accepts_empty_tools(self) -> None:
        executor = PiExecutor(tools=[])
        self.assertEqual(executor.tools, [])

    def test_constructor_rejects_empty_provider(self) -> None:
        with self.assertRaisesRegex(ValueError, "provider must not be empty"):
            PiExecutor(provider="   ")

    def test_constructor_rejects_empty_model(self) -> None:
        with self.assertRaisesRegex(ValueError, "model must not be empty"):
            PiExecutor(model="")

    def test_constructor_rejects_non_string_tools(self) -> None:
        with self.assertRaisesRegex(TypeError, "tools entries must be strings"):
            PiExecutor(tools=[123])  # type: ignore[arg-type]

    def test_constructor_rejects_empty_tool_in_list(self) -> None:
        with self.assertRaisesRegex(ValueError, "tools entries must not be empty"):
            PiExecutor(tools=["Read", ""])

    def test_constructor_accepts_custom_pi_bin(self) -> None:
        executor = PiExecutor(pi_bin="/usr/local/bin/pi")
        self.assertEqual(executor.pi_bin, "/usr/local/bin/pi")

    def test_constructor_rejects_empty_pi_bin(self) -> None:
        with self.assertRaisesRegex(ValueError, "pi_bin must not be empty"):
            PiExecutor(pi_bin="   ")

    def test_constructor_accepts_no_session_false(self) -> None:
        executor = PiExecutor(no_session=False)
        self.assertFalse(executor.no_session)


class PiCommandConstructionTests(PiExecutorTestCase):
    def test_command_uses_no_session_flag_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), prompt="Do it.")
            calls, side_effect = self.make_subprocess_side_effect()

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                side_effect=side_effect,
            ):
                PiExecutor().run(context)

            pi_call = calls[0]
            self.assertIn("--no-session", pi_call)

    def test_command_omits_no_session_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), prompt="Do it.")
            calls, side_effect = self.make_subprocess_side_effect()

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                side_effect=side_effect,
            ):
                PiExecutor(no_session=False).run(context)

            pi_call = calls[0]
            self.assertNotIn("--no-session", pi_call)

    def test_command_includes_provider_when_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), prompt="Do it.")
            calls, side_effect = self.make_subprocess_side_effect()

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                side_effect=side_effect,
            ):
                PiExecutor(provider="minimax").run(context)

            pi_call = calls[0]
            self.assertIn("--provider", pi_call)
            self.assertEqual(pi_call[pi_call.index("--provider") + 1], "minimax")

    def test_command_includes_model_when_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), prompt="Do it.")
            calls, side_effect = self.make_subprocess_side_effect()

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                side_effect=side_effect,
            ):
                PiExecutor(model="minimax-01").run(context)

            pi_call = calls[0]
            self.assertIn("--model", pi_call)
            self.assertEqual(pi_call[pi_call.index("--model") + 1], "minimax-01")

    def test_command_uses_single_comma_separated_tools_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), prompt="Do it.")
            calls, side_effect = self.make_subprocess_side_effect()

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                side_effect=side_effect,
            ):
                PiExecutor(tools=["Read", "Write"]).run(context)

            pi_call = calls[0]
            self.assertIn("--tools", pi_call)
            tools_index = pi_call.index("--tools")
            self.assertEqual(pi_call[tools_index + 1], "Read,Write")
            # Should NOT have repeated --tool flags
            self.assertEqual(pi_call.count("--tools"), 1)

    def test_command_omits_tools_flag_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), prompt="Do it.")
            calls, side_effect = self.make_subprocess_side_effect()

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                side_effect=side_effect,
            ):
                PiExecutor(tools=[]).run(context)

            pi_call = calls[0]
            self.assertNotIn("--tools", pi_call)

    def test_command_uses_minus_p_flag_for_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), prompt="Do the task.")
            calls, side_effect = self.make_subprocess_side_effect()

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                side_effect=side_effect,
            ):
                PiExecutor().run(context)

            pi_call = calls[0]
            self.assertIn("-p", pi_call)
            prompt_index = pi_call.index("-p")
            self.assertEqual(pi_call[prompt_index + 1], "Do the task.")

    def test_command_uses_cwd_equals_worktree_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), prompt="Do it.")
            calls, side_effect = self.make_subprocess_side_effect()

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                side_effect=side_effect,
            ) as run_mock:
                PiExecutor().run(context)

            first_call_kwargs = run_mock.call_args_list[0].kwargs
            self.assertEqual(first_call_kwargs["cwd"], context.worktree_path)
            self.assertFalse(first_call_kwargs["shell"])


class PiBlockedTests(PiExecutorTestCase):
    def test_missing_prompt_path_returns_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(
                Path(tmp),
                prompt_path=None,
            )
            result = PiExecutor().run(context)

            self.assertEqual(result.status, "blocked")
            self.assertIsNone(result.exit_code)
            self.assertIsNone(result.log_path)
            self.assertIn("requires context.prompt_path", result.summary or "")

    def test_nonexistent_prompt_path_returns_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_prompt = Path(tmp) / "missing_prompt.md"
            context = self.make_context(
                Path(tmp),
                prompt_path=missing_prompt,
            )
            result = PiExecutor().run(context)

            self.assertEqual(result.status, "blocked")
            self.assertIn("does not exist", result.summary or "")

    def test_empty_prompt_file_returns_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            empty_prompt = tmp_path = Path(tmp)
            worktree_path = tmp_path / "worktree"
            artifact_dir = tmp_path / "artifacts"
            worktree_path.mkdir()
            artifact_dir.mkdir()
            empty_prompt_path = tmp_path / "empty_prompt.md"
            empty_prompt_path.write_text("   ", encoding="utf-8")

            context = ExecutorContext(
                task_key="AT-0012",
                project="agent-taskflow",
                worktree_path=worktree_path,
                artifact_dir=artifact_dir,
                prompt_path=empty_prompt_path,
            )
            result = PiExecutor().run(context)

            self.assertEqual(result.status, "blocked")
            self.assertIn("empty", result.summary or "")


class PiResultTests(PiExecutorTestCase):
    def test_zero_exit_code_returns_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), prompt="Do it.")
            _, side_effect = self.make_subprocess_side_effect(pi_returncode=0)

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                side_effect=side_effect,
            ):
                result = PiExecutor().run(context)

            self.assertEqual(result.executor, "pi")
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.exit_code, 0)
            self.assertIsNotNone(result.log_path)
            assert result.log_path is not None
            self.assertTrue(result.log_path.exists())
            self.assertEqual(result.artifacts["pi_log"], result.log_path)

    def test_nonzero_exit_code_returns_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), prompt="Do it.")
            _, side_effect = self.make_subprocess_side_effect(pi_returncode=7)

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                side_effect=side_effect,
            ):
                result = PiExecutor().run(context)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.exit_code, 7)

    def test_missing_pi_binary_returns_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), prompt="Do it.")
            calls: list[list[str]] = []

            def side_effect(command, **kwargs):
                calls.append(command)
                raise FileNotFoundError("pi not found")

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                side_effect=side_effect,
            ):
                result = PiExecutor(pi_bin="pi").run(context)

            self.assertEqual(result.status, "blocked")
            self.assertIsNone(result.exit_code)
            self.assertIn("failed to start", result.summary or "")
            self.assertIsNotNone(result.log_path)
            assert result.log_path is not None
            self.assertTrue(result.log_path.exists())

    def test_log_file_contains_command_info(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), prompt="Do it.")
            _, side_effect = self.make_subprocess_side_effect()

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                side_effect=side_effect,
            ):
                result = PiExecutor(provider="minimax", model="test-model").run(context)

            self.assertEqual(result.status, "completed")
            assert result.log_path is not None
            log_text = result.log_path.read_text(encoding="utf-8")
            self.assertIn("Executor: pi", log_text)
            self.assertIn("Task: AT-0012", log_text)
            self.assertIn("Worktree:", log_text)
            self.assertIn("--provider", log_text)
            self.assertIn("minimax", log_text)


class PiEnvTests(PiExecutorTestCase):
    def test_constructor_env_passed_to_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), prompt="Do it.")
            executor = PiExecutor(env={"MY_VAR": "from_constructor"})

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                wraps=lambda *a, **kw: subprocess.CompletedProcess(args=a[0] if a else [], returncode=0),
            ) as run_mock:
                executor.run(context)

            call_kwargs = run_mock.call_args_list[0].kwargs
            self.assertIsNotNone(call_kwargs.get("env"))
            self.assertEqual(call_kwargs["env"]["MY_VAR"], "from_constructor")

    def test_context_env_passed_to_subprocess(self) -> None:
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), prompt="Do it.")
            context = ExecutorContext(
                task_key=context.task_key,
                project=context.project,
                worktree_path=context.worktree_path,
                artifact_dir=context.artifact_dir,
                prompt_path=context.prompt_path,
                env={"CTX_VAR": "from_context"},
            )

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                wraps=lambda *a, **kw: subprocess.CompletedProcess(args=a[0] if a else [], returncode=0),
            ) as run_mock:
                PiExecutor().run(context)

            call_kwargs = run_mock.call_args_list[0].kwargs
            self.assertIsNotNone(call_kwargs.get("env"))
            self.assertEqual(call_kwargs["env"]["CTX_VAR"], "from_context")

    def test_context_env_overrides_constructor_env(self) -> None:
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), prompt="Do it.")
            context = ExecutorContext(
                task_key=context.task_key,
                project=context.project,
                worktree_path=context.worktree_path,
                artifact_dir=context.artifact_dir,
                prompt_path=context.prompt_path,
                env={"OVERRIDE_ME": "from_context"},
            )
            executor = PiExecutor(env={"OVERRIDE_ME": "from_constructor"})

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                wraps=lambda *a, **kw: subprocess.CompletedProcess(args=a[0] if a else [], returncode=0),
            ) as run_mock:
                executor.run(context)

            call_kwargs = run_mock.call_args_list[0].kwargs
            self.assertEqual(call_kwargs["env"]["OVERRIDE_ME"], "from_context")

    def test_no_env_passed_when_neither_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), prompt="Do it.")
            self.assertIsNone(context.env)

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                wraps=lambda *a, **kw: subprocess.CompletedProcess(args=a[0] if a else [], returncode=0),
            ) as run_mock:
                PiExecutor().run(context)

            call_kwargs = run_mock.call_args_list[0].kwargs
            self.assertIsNone(call_kwargs.get("env"))


class PiRegistryTests(unittest.TestCase):
    def test_registry_lists_pi(self) -> None:
        self.assertIn("pi", list_executor_names())

    def test_registry_returns_pi_executor(self) -> None:
        executor = get_executor("pi", provider="minimax", model="test-model")

        self.assertIsInstance(executor, PiExecutor)

    def test_registry_returns_pi_executor_with_tools(self) -> None:
        executor = get_executor(
            "pi",
            provider="minimax",
            model="test-model",
            tools=["Read", "Write"],
        )

        self.assertIsInstance(executor, PiExecutor)
        self.assertEqual(executor.provider, "minimax")
        self.assertEqual(executor.model, "test-model")
        self.assertEqual(executor.tools, ["Read", "Write"])

    def test_build_pi_executor_returns_pi_executor(self) -> None:
        executor = build_pi_executor(
            provider="minimax",
            model="test-model",
            tools=["Read"],
        )

        self.assertIsInstance(executor, PiExecutor)
        self.assertEqual(executor.tools, ["Read"])


if __name__ == "__main__":
    unittest.main()