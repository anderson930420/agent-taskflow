from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_taskflow.executors import (
    ExecutorContext,
    ExecutorResult,
    ManualExecutor,
    ShellExecutor,
    build_shell_executor,
    get_executor,
    list_executor_names,
)


class ExecutorTestCase(unittest.TestCase):
    def make_context(
        self,
        tmp_path: Path,
        *,
        timeout_seconds: int | None = None,
    ) -> ExecutorContext:
        worktree_path = tmp_path / "worktree"
        artifact_dir = tmp_path / "artifacts"
        worktree_path.mkdir()
        artifact_dir.mkdir()

        return ExecutorContext(
            task_key=" AT-0004 ",
            project="agent-taskflow",
            worktree_path=worktree_path,
            artifact_dir=artifact_dir,
            timeout_seconds=timeout_seconds,
        )


class ExecutorContextTests(ExecutorTestCase):
    def test_executor_context_accepts_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            self.assertEqual(context.task_key, "AT-0004")
            self.assertEqual(context.project, "agent-taskflow")
            self.assertTrue(context.worktree_path.is_absolute())
            self.assertTrue(context.artifact_dir.is_absolute())

    def test_executor_context_rejects_relative_worktree_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()

            with self.assertRaisesRegex(ValueError, "worktree_path must be absolute"):
                ExecutorContext(
                    task_key="AT-0004",
                    project="agent-taskflow",
                    worktree_path=Path("relative-worktree"),
                    artifact_dir=artifact_dir,
                )

    def test_executor_context_rejects_relative_artifact_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree_path = Path(tmp) / "worktree"
            worktree_path.mkdir()

            with self.assertRaisesRegex(ValueError, "artifact_dir must be absolute"):
                ExecutorContext(
                    task_key="AT-0004",
                    project="agent-taskflow",
                    worktree_path=worktree_path,
                    artifact_dir=Path("relative-artifacts"),
                )

    def test_executor_context_rejects_relative_prompt_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree_path = Path(tmp) / "worktree"
            artifact_dir = Path(tmp) / "artifacts"
            worktree_path.mkdir()
            artifact_dir.mkdir()

            with self.assertRaisesRegex(ValueError, "prompt_path must be absolute"):
                ExecutorContext(
                    task_key="AT-0004",
                    project="agent-taskflow",
                    worktree_path=worktree_path,
                    artifact_dir=artifact_dir,
                    prompt_path=Path("prompt.md"),
                )

    def test_executor_context_rejects_secret_like_env_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree_path = Path(tmp) / "worktree"
            artifact_dir = Path(tmp) / "artifacts"
            worktree_path.mkdir()
            artifact_dir.mkdir()

            with self.assertRaisesRegex(ValueError, "secret-like key"):
                ExecutorContext(
                    task_key="AT-0004",
                    project="agent-taskflow",
                    worktree_path=worktree_path,
                    artifact_dir=artifact_dir,
                    env={"API_TOKEN": "should-not-be-stored"},
                )


class ExecutorResultTests(unittest.TestCase):
    def test_executor_result_rejects_relative_log_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "log_path must be absolute"):
            ExecutorResult(
                executor="shell",
                status="completed",
                log_path=Path("relative.log"),
            )

    def test_executor_result_rejects_relative_artifact_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "artifacts\\[log\\] must be absolute"):
            ExecutorResult(
                executor="shell",
                status="completed",
                artifacts={"log": Path("relative.log")},
            )

    def test_executor_result_rejects_unknown_status(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid executor result status"):
            ExecutorResult(
                executor="shell",
                status="waiting_for_review",
            )


class ManualExecutorTests(ExecutorTestCase):
    def test_manual_executor_does_not_modify_worktree_and_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            context = self.make_context(tmp_path)
            before = sorted(path.relative_to(context.worktree_path) for path in context.worktree_path.rglob("*"))

            result = ManualExecutor().run(context)

            after = sorted(path.relative_to(context.worktree_path) for path in context.worktree_path.rglob("*"))
            self.assertEqual(before, after)
            self.assertEqual(result.executor, "manual")
            self.assertEqual(result.status, "skipped")
            self.assertIsNone(result.log_path)
            self.assertEqual(result.artifacts, {})

    def test_manual_executor_can_return_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))

            result = ManualExecutor(status="blocked").run(context)

            self.assertEqual(result.status, "blocked")


class ShellExecutorTests(ExecutorTestCase):
    def test_shell_executor_runs_python_command_successfully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            executor = ShellExecutor(
                [sys.executable, "-c", "print('hello')"],
                name="python-smoke",
            )

            result = executor.run(context)

            self.assertEqual(result.executor, "python-smoke")
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.exit_code, 0)
            self.assertIsNotNone(result.log_path)
            assert result.log_path is not None
            self.assertTrue(result.log_path.exists())
            self.assertIn("hello", result.log_path.read_text(encoding="utf-8"))
            self.assertEqual(result.artifacts["log"], result.log_path)

    def test_shell_executor_creates_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            result = ShellExecutor([sys.executable, "-c", "print('log ok')"]).run(
                context
            )

            self.assertIsNotNone(result.log_path)
            assert result.log_path is not None
            self.assertTrue(result.log_path.is_absolute())
            self.assertTrue(result.log_path.exists())
            self.assertIn("Environment: not logged", result.log_path.read_text())

    def test_shell_executor_nonzero_exit_code_returns_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            executor = ShellExecutor([sys.executable, "-c", "raise SystemExit(7)"])

            result = executor.run(context)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.exit_code, 7)

    def test_shell_executor_rejects_raw_shell_string(self) -> None:
        with self.assertRaisesRegex(TypeError, "not a raw string"):
            ShellExecutor("echo unsafe")  # type: ignore[arg-type]

    def test_shell_executor_calls_subprocess_with_shell_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp))
            executor = ShellExecutor([sys.executable, "-c", "print('patched')"])

            with patch(
                "agent_taskflow.executors.shell.subprocess.run",
                return_value=subprocess.CompletedProcess(args=executor.command, returncode=0),
            ) as run_mock:
                result = executor.run(context)

            self.assertEqual(result.status, "completed")
            self.assertEqual(run_mock.call_args.kwargs["shell"], False)
            self.assertEqual(run_mock.call_args.kwargs["cwd"], context.worktree_path)

    def test_shell_executor_timeout_returns_failed_and_writes_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.make_context(Path(tmp), timeout_seconds=1)
            executor = ShellExecutor([sys.executable, "-c", "import time; time.sleep(5)"])

            result = executor.run(context)

            self.assertEqual(result.status, "failed")
            self.assertIsNone(result.exit_code)
            self.assertIsNotNone(result.log_path)
            assert result.log_path is not None
            self.assertIn("timed out", result.log_path.read_text(encoding="utf-8"))

    def test_build_shell_executor_returns_named_shell_executor(self) -> None:
        executor = build_shell_executor([sys.executable, "-c", "print('x')"], name="check")

        self.assertIsInstance(executor, ShellExecutor)
        self.assertEqual(executor.name, "check")


class ExecutorRegistryTests(unittest.TestCase):
    def test_registry_lists_manual_noop_and_shell(self) -> None:
        names = list_executor_names()

        self.assertIn("manual", names)
        self.assertIn("noop", names)
        self.assertIn("shell", names)

    def test_registry_returns_manual_and_noop(self) -> None:
        self.assertIsInstance(get_executor("manual"), ManualExecutor)
        self.assertEqual(get_executor("noop").name, "noop")

    def test_registry_returns_shell_when_command_is_provided(self) -> None:
        executor = get_executor("shell", command=[sys.executable, "-c", "print('x')"])

        self.assertIsInstance(executor, ShellExecutor)

    def test_registry_requires_shell_command(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires command"):
            get_executor("shell")

    def test_registry_rejects_unknown_executor(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown executor"):
            get_executor("opencode")


if __name__ == "__main__":
    unittest.main()
