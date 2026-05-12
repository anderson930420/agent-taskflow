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
            # Phase 23: log_path is created even when blocked so the reason is traceable.
            self.assertIsNotNone(result.log_path)
            self.assertIn("mission_contract.json", result.summary or "")

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


class PiProtocolIntegrationTests(PiExecutorTestCase):
    """Phase 23: Pi Mission Protocol integration tests.

    These tests verify that PiExecutor uses the protocol prompt when
    mission_contract.json exists, and falls back to legacy behavior when it
    does not.
    """

    def _write_contract(self, artifact_dir: Path, **overrides) -> None:
        contract = {
            "schema_version": "1",
            "task_key": "AT-0012",
            "goal": "Implement the feature",
            "repo_path": str(artifact_dir.parent / "repo"),
            "worktree_path": str(artifact_dir.parent / "worktree"),
            "artifact_dir": str(artifact_dir),
            "executor": "pi",
            "required_validators": ["pytest", "policy"],
            "forbidden_actions": ["push", "merge"],
            "expected_artifacts": ["executor_log"],
            "human_approval_required": True,
            "governance_rules": ["agent-taskflow is the control plane."],
        }
        contract.update(overrides)
        (artifact_dir / "mission_contract.json").write_text(
            __import__("json").dumps(contract), encoding="utf-8"
        )

    def make_protocol_context(
        self,
        tmp_path: Path,
        *,
        with_contract: bool = True,
        with_prompt_file: bool = True,
    ) -> ExecutorContext:
        worktree_path = tmp_path / "worktree"
        artifact_dir = tmp_path / "artifacts"
        worktree_path.mkdir()
        artifact_dir.mkdir()

        prompt_path = tmp_path / "implementation_prompt.md"
        if with_prompt_file:
            prompt_path.write_text("Original prompt text.", encoding="utf-8")

        if with_contract:
            self._write_contract(artifact_dir)

        return ExecutorContext(
            task_key="AT-0012",
            project="agent-taskflow",
            worktree_path=worktree_path,
            artifact_dir=artifact_dir,
            prompt_path=prompt_path if with_prompt_file else None,
        )

    def test_with_contract_writes_pi_mission_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_protocol_context(Path(tmp), with_contract=True)
            _, side_effect = self.make_subprocess_side_effect()

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                side_effect=side_effect,
            ):
                result = PiExecutor().run(context)

            self.assertEqual(result.status, "completed")
            protocol_path = context.artifact_dir / "pi_mission_prompt.md"
            self.assertTrue(protocol_path.exists())
            content = protocol_path.read_text(encoding="utf-8")
            self.assertIn("# Pi Mission Protocol", content)
            self.assertIn("Implement the feature", content)
            self.assertIn("pytest", content)

    def test_protocol_prompt_includes_governance_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_protocol_context(Path(tmp), with_contract=True)
            _, side_effect = self.make_subprocess_side_effect()

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                side_effect=side_effect,
            ):
                PiExecutor().run(context)

            content = (context.artifact_dir / "pi_mission_prompt.md").read_text(encoding="utf-8")
            self.assertIn("Do NOT approve", content)
            self.assertIn("Do NOT push", content)
            self.assertIn("Do NOT merge", content)
            self.assertIn("Human approval is the final gate", content)
            self.assertIn("cannot replace deterministic validators", content)

    def test_command_uses_protocol_prompt_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_protocol_context(Path(tmp), with_contract=True)
            calls, side_effect = self.make_subprocess_side_effect()

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                side_effect=side_effect,
            ):
                PiExecutor().run(context)

            pi_call = calls[0]
            self.assertIn("-p", pi_call)
            prompt_index = pi_call.index("-p")
            # Protocol content should be the rendered prompt, not raw "Original prompt text."
            self.assertIn("# Pi Mission Protocol", pi_call[prompt_index + 1])
            self.assertIn("Implement the feature", pi_call[prompt_index + 1])

    def test_log_mentions_protocol_prompt_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_protocol_context(Path(tmp), with_contract=True)
            _, side_effect = self.make_subprocess_side_effect()

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                side_effect=side_effect,
            ):
                result = PiExecutor().run(context)

            assert result.log_path is not None
            log_text = result.log_path.read_text(encoding="utf-8")
            self.assertIn("Prompt source: protocol", log_text)
            self.assertIn("pi_mission_prompt.md", log_text)

    def test_legacy_fallback_uses_prompt_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # No contract, but prompt file exists -> legacy path
            context = self.make_protocol_context(
                Path(tmp), with_contract=False, with_prompt_file=True
            )
            calls, side_effect = self.make_subprocess_side_effect()

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                side_effect=side_effect,
            ):
                PiExecutor().run(context)

            pi_call = calls[0]
            prompt_index = pi_call.index("-p")
            # Legacy path uses the original prompt file content
            self.assertEqual(pi_call[prompt_index + 1], "Original prompt text.")

    def test_legacy_fallback_log_mentions_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_protocol_context(
                Path(tmp), with_contract=False, with_prompt_file=True
            )
            _, side_effect = self.make_subprocess_side_effect()

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                side_effect=side_effect,
            ):
                result = PiExecutor().run(context)

            assert result.log_path is not None
            log_text = result.log_path.read_text(encoding="utf-8")
            self.assertIn("Prompt source: legacy", log_text)

    def test_without_contract_nor_prompt_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_protocol_context(
                Path(tmp), with_contract=False, with_prompt_file=False
            )
            result = PiExecutor().run(context)

            self.assertEqual(result.status, "blocked")
            self.assertIsNotNone(result.log_path)
            self.assertIn("mission_contract.json", result.summary or "")

    def test_artifact_includes_protocol_prompt_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_protocol_context(Path(tmp), with_contract=True)
            _, side_effect = self.make_subprocess_side_effect()

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                side_effect=side_effect,
            ):
                result = PiExecutor().run(context)

            self.assertIn("pi_mission_prompt", result.artifacts)
            self.assertEqual(
                result.artifacts["pi_mission_prompt"],
                context.artifact_dir / "pi_mission_prompt.md",
            )

    def test_protocol_with_secret_prompt_omits_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Contract is valid, but original prompt has secrets
            (Path(tmp) / "implementation_prompt.md").write_text(
                '''The token is: "api_secret": "sk-testsecret1234567890"
Please use this to authenticate.''', encoding="utf-8"
            )
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            self._write_contract(artifact_dir)

            worktree_path = Path(tmp) / "worktree"
            worktree_path.mkdir(parents=True, exist_ok=True)

            context = ExecutorContext(
                task_key="AT-0012",
                project="agent-taskflow",
                worktree_path=worktree_path,
                artifact_dir=artifact_dir,
                prompt_path=Path(tmp) / "implementation_prompt.md",
            )
            _, side_effect = self.make_subprocess_side_effect()

            with patch(
                "agent_taskflow.executors.pi.subprocess.run",
                side_effect=side_effect,
            ):
                PiExecutor().run(context)

            protocol_content = (
                (artifact_dir / "pi_mission_prompt.md").read_text(encoding="utf-8")
            )
            # Secret should be redacted / omitted
            self.assertNotIn("sk-testsecret1234567890", protocol_content)
            # The secret is redacted
            self.assertNotIn("sk-testsecret1234567890", protocol_content)

    def test_no_validator_self_invocation(self) -> None:
        # Verify that the protocol path does not import any validator module.
        # This prevents the protocol renderer from accidentally calling validators.
        import sys

        # Snapshot of validator modules before protocol rendering.
        pre_validator_modules = {k for k in sys.modules if "validator" in k.lower()}

        # Import and run protocol helpers.
        from agent_taskflow.executors.pi_protocol import (
            render_pi_mission_prompt,
            write_pi_mission_prompt,
            load_contract_for_pi,
        )

        # Import the module explicitly.
        import agent_taskflow.executors.pi_protocol as pp_module  # noqa: F401

        post_validator_modules = {k for k in sys.modules if "validator" in k.lower()}
        new_validator_modules = post_validator_modules - pre_validator_modules

        # pi_protocol should not transitively import any validator module.
        self.assertEqual(
            set(),
            new_validator_modules,
            f"pi_protocol transitively imported validator modules: {new_validator_modules}",
        )


if __name__ == "__main__":
    unittest.main()