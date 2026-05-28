from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_taskflow.approved_task_runner import (
    APPROVED_TASK_STATUS,
    ApprovedTaskRunRequest,
    ApprovedTaskRunnerError,
    RUN_STATUS_BLOCKED,
    run_approved_task,
)
from agent_taskflow.executors.base import Executor, ExecutorContext, ExecutorResult
from agent_taskflow.executors.manual import NoopExecutor
from agent_taskflow.models import TaskRecord
from agent_taskflow.preflight import PreflightCheck, PreflightResult
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.validators.base import Validator, ValidatorContext, ValidatorResult
from agent_taskflow.validators.policy import PolicyCheckValidator
from agent_taskflow.workspace_manager import WorkspacePreparationResult


REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeExecutor(Executor):
    def __init__(
        self,
        *,
        name: str = "fake",
        status: str = "completed",
        summary: str = "executor finished",
    ) -> None:
        self.name = name
        self.status = status
        self.summary = summary
        self.calls: list[ExecutorContext] = []

    def run(self, context: ExecutorContext) -> ExecutorResult:
        self.calls.append(context)
        log_path = context.artifact_dir / f"{self.name}.log"
        log_path.write_text(f"{self.name} log for {context.task_key}\n", encoding="utf-8")
        return ExecutorResult(
            executor=self.name,
            status=self.status,
            exit_code=0 if self.status == "completed" else 1,
            log_path=log_path,
            summary=self.summary,
            artifacts={"log": log_path},
        )


class FakeValidator(Validator):
    def __init__(
        self,
        *,
        name: str = "fake-validator",
        status: str = "passed",
        summary: str = "validator finished",
    ) -> None:
        self.name = name
        self.status = status
        self.summary = summary
        self.calls: list[ValidatorContext] = []

    def run(self, context: ValidatorContext) -> ValidatorResult:
        self.calls.append(context)
        log_path = context.artifact_dir / f"{self.name}.log"
        log_path.write_text(f"{self.name} log for {context.task_key}\n", encoding="utf-8")
        return ValidatorResult(
            validator=self.name,
            status=self.status,
            exit_code=0 if self.status == "passed" else 1,
            log_path=log_path,
            summary=self.summary,
            artifacts={"log": log_path},
        )


def _preflight_result(*, ok: bool = True, status: str = "passed", summary: str = "preflight ok") -> PreflightResult:
    check = PreflightCheck(
        name="python_environment",
        kind="python_runtime",
        required=True,
        status="passed" if ok else "failed",
        summary=summary,
    )
    return PreflightResult(
        ok=ok,
        status=status,
        strict=False,
        executor="noop",
        validators=("policy",),
        python={"executable": "python3"},
        checks=(check,),
        missing_required=(),
        missing_optional=(),
        recommended_commands=(),
    )


class ApprovedTaskRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.worktree_root = self.root / "worktrees"
        self._init_repo()
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _init_repo(self) -> None:
        self._git("init", "-b", "main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.repo / "README.md").write_text("agent-taskflow\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial commit")

    def _add_task(
        self,
        task_key: str,
        *,
        status: str = "queued",
        artifact_dir: Path | None = None,
        title: str = "Task",
    ) -> Path:
        resolved_artifact_dir = artifact_dir or (self.artifact_root / task_key)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title=title,
                status=status,
                repo_path=self.repo,
                artifact_dir=resolved_artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )
        return resolved_artifact_dir

    def _request(
        self,
        *,
        task_key: str = "AT-GH-401",
        executor: str = "noop",
        confirm_approved_task: bool = True,
        validators: tuple[str, ...] = ("policy",),
        dry_run: bool = False,
        preflight: bool = True,
        command: tuple[str, ...] | None = None,
        base_branch: str = "main",
        model: str | None = None,
        provider: str | None = None,
        tools: tuple[str, ...] | None = None,
        pi_bin: str | None = None,
    ) -> ApprovedTaskRunRequest:
        return ApprovedTaskRunRequest(
            task_key=task_key,
            executor=executor,
            repo_path=self.repo,
            db_path=self.db_path,
            artifact_root=self.artifact_root,
            worktree_root=self.repo / ".worktrees",
            base_branch=base_branch,
            validators=validators,
            confirm_approved_task=confirm_approved_task,
            dry_run=dry_run,
            preflight=preflight,
            command=command,
            model=model,
            provider=provider,
            tools=tools,
            pi_bin=pi_bin,
        )

    def test_missing_confirmation_flag_refuses_to_run(self) -> None:
        self._add_task("AT-GH-401")

        result = run_approved_task(
            self._request(confirm_approved_task=False),
            store=self.store,
            preflight_runner=lambda **kwargs: _preflight_result(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "confirmation")
        self.assertEqual(result.status, RUN_STATUS_BLOCKED)
        self.assertIn("--confirm-approved-task", result.error or "")
        self.assertFalse(result.safety["human_approval_confirmed"])
        self.assertFalse(result.safety["workspace_prepared"])
        self.assertFalse(result.safety["executor_started"])
        self.assertFalse(result.safety["validators_started"])

    def test_non_queued_task_refuses_to_run(self) -> None:
        self._add_task("AT-GH-402", status="blocked")

        result = run_approved_task(
            self._request(task_key="AT-GH-402"),
            store=self.store,
            preflight_runner=lambda **kwargs: _preflight_result(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "selection")
        self.assertEqual(result.status, RUN_STATUS_BLOCKED)
        self.assertIn("must be queued", result.error or "")
        self.assertEqual(self.store.get_task("AT-GH-402").status, "blocked")

    def test_invalid_executor_refuses_to_run(self) -> None:
        result = run_approved_task(
            self._request(executor="not-a-real-executor"),
            store=self.store,
            preflight_runner=lambda **kwargs: _preflight_result(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "selection")
        self.assertEqual(result.status, RUN_STATUS_BLOCKED)
        self.assertIn("Unknown executor", result.error or "")

    def test_preflight_failure_prevents_workspace_preparation_and_executor_dispatch(self) -> None:
        self._add_task("AT-GH-403")

        preflight = _preflight_result(ok=False, status="failed", summary="missing pytest")
        with mock.patch("agent_taskflow.approved_task_runner.prepare_task_workspace") as prepare_mock:
            result = run_approved_task(
                self._request(task_key="AT-GH-403"),
                store=self.store,
                preflight_runner=lambda **kwargs: preflight,
            )

        prepare_mock.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "preflight")
        self.assertEqual(result.status, RUN_STATUS_BLOCKED)
        self.assertIn("missing pytest", result.error or "")
        self.assertFalse(result.safety["workspace_prepared"])
        self.assertFalse(result.safety["executor_started"])

    def test_workspace_preparation_failure_prevents_executor_dispatch(self) -> None:
        self._add_task("AT-GH-404")
        blocked_workspace = WorkspacePreparationResult(
            task_key="AT-GH-404",
            repo_path=self.repo,
            worktree_path=self.repo / ".worktrees" / "AT-GH-404",
            branch="task/AT-GH-404",
            base_branch="main",
            base_sha="deadbeef",
            status="blocked",
            summary="worktree blocked",
        )

        with mock.patch("agent_taskflow.approved_task_runner.prepare_task_workspace", return_value=blocked_workspace) as prepare_mock, mock.patch("agent_taskflow.approved_task_runner.get_executor") as get_executor_mock:
            result = run_approved_task(
                self._request(task_key="AT-GH-404"),
                store=self.store,
                preflight_runner=lambda **kwargs: _preflight_result(),
            )

        prepare_mock.assert_called_once()
        get_executor_mock.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "workspace")
        self.assertEqual(result.status, RUN_STATUS_BLOCKED)
        self.assertIn("worktree blocked", result.error or "")
        self.assertFalse(result.safety["workspace_prepared"])
        self.assertFalse(result.safety["executor_started"])

    def test_executor_failure_records_evidence_and_ends_blocked(self) -> None:
        self._add_task("AT-GH-405")
        failing_executor = FakeExecutor(name="noop", status="failed", summary="executor failed")

        result = run_approved_task(
            self._request(task_key="AT-GH-405"),
            store=self.store,
            executor_registry={"noop": failing_executor},
            validator_registry={"policy": PolicyCheckValidator()},
            preflight_runner=lambda **kwargs: _preflight_result(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "executor")
        self.assertEqual(result.status, RUN_STATUS_BLOCKED)
        self.assertEqual(self.store.get_task("AT-GH-405").status, RUN_STATUS_BLOCKED)
        self.assertEqual(len(self.store.list_executor_runs("AT-GH-405")), 1)
        self.assertGreaterEqual(len(self.store.list_task_artifacts("AT-GH-405")), 1)
        self.assertTrue(result.executor_run["started"])
        self.assertTrue(result.executor_run["finished"])
        self.assertFalse(result.executor_run["ok"])
        self.assertIn("executor failed", result.error or "")
        self.assertFalse(result.safety["branch_pushed"])
        self.assertFalse(result.safety["pr_created"])
        self.assertFalse(result.safety["merged"])
        self.assertFalse(result.safety["approved"])
        self.assertFalse(result.safety["cleanup_performed"])

    def test_validator_failure_records_evidence_and_ends_blocked(self) -> None:
        self._add_task("AT-GH-406")
        failing_validator = FakeValidator(name="policy", status="failed", summary="validator failed")

        result = run_approved_task(
            self._request(task_key="AT-GH-406"),
            store=self.store,
            executor_registry={"noop": NoopExecutor()},
            validator_registry={"policy": failing_validator},
            preflight_runner=lambda **kwargs: _preflight_result(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "validation")
        self.assertEqual(result.status, RUN_STATUS_BLOCKED)
        self.assertEqual(self.store.get_task("AT-GH-406").status, RUN_STATUS_BLOCKED)
        self.assertEqual(len(self.store.list_validation_results("AT-GH-406")), 1)
        self.assertTrue(result.executor_run["ok"])
        self.assertEqual(result.validators[0]["name"], "policy")
        self.assertFalse(result.validators[0]["ok"])
        self.assertIn("validator failed", result.error or "")

    def test_executor_success_and_validator_success_end_waiting_approval(self) -> None:
        self._add_task("AT-GH-407")

        result = run_approved_task(
            self._request(task_key="AT-GH-407", validators=("policy",)),
            store=self.store,
            executor_registry={"noop": NoopExecutor()},
            validator_registry={"policy": PolicyCheckValidator()},
            preflight_runner=lambda **kwargs: _preflight_result(),
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, APPROVED_TASK_STATUS)
        self.assertEqual(result.phase, APPROVED_TASK_STATUS)
        self.assertEqual(result.summary["final_task_status"], APPROVED_TASK_STATUS)
        self.assertEqual(self.store.get_task("AT-GH-407").status, APPROVED_TASK_STATUS)
        self.assertIsNotNone(self.store.get_task_worktree("AT-GH-407"))
        self.assertGreaterEqual(len(result.artifacts), 1)
        self.assertTrue(result.safety["human_approval_required"])
        self.assertTrue(result.safety["human_approval_confirmed"])
        self.assertTrue(result.safety["task_status_changed"])
        self.assertTrue(result.safety["workspace_prepared"])
        self.assertTrue(result.safety["validators_started"])
        self.assertFalse(result.safety["branch_pushed"])
        self.assertFalse(result.safety["pr_created"])
        self.assertFalse(result.safety["merged"])
        self.assertFalse(result.safety["approved"])
        self.assertFalse(result.safety["cleanup_performed"])

    def test_opencode_without_model_is_explicitly_blocked(self) -> None:
        self._add_task("AT-GH-409")

        result = run_approved_task(
            self._request(task_key="AT-GH-409", executor="opencode"),
            store=self.store,
            preflight_runner=lambda **kwargs: _preflight_result(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "executor")
        self.assertEqual(result.status, RUN_STATUS_BLOCKED)
        self.assertIn("requires a model", result.error or "")
        self.assertEqual(self.store.get_task("AT-GH-409").status, RUN_STATUS_BLOCKED)
        self.assertFalse(result.safety["executor_started"])

    def test_request_executor_profile_flows_to_real_executor(self) -> None:
        self._add_task("AT-GH-410")
        captured: dict[str, object] = {}
        fake_executor = FakeExecutor(name="pi", status="completed")

        def fake_get_executor(name, **kwargs):
            captured["name"] = name
            captured["kwargs"] = kwargs
            return fake_executor

        with mock.patch(
            "agent_taskflow.approved_task_runner.get_executor",
            side_effect=fake_get_executor,
        ):
            result = run_approved_task(
                self._request(
                    task_key="AT-GH-410",
                    executor="pi",
                    validators=("policy",),
                    model="claude-sonnet-4-6",
                    provider="anthropic",
                    tools=("read", "write"),
                    pi_bin="/custom/pi",
                ),
                store=self.store,
                validator_registry={"policy": PolicyCheckValidator()},
                preflight_runner=lambda **kwargs: _preflight_result(),
            )

        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.status, APPROVED_TASK_STATUS)
        self.assertEqual(captured["name"], "pi")
        self.assertEqual(captured["kwargs"]["model"], "claude-sonnet-4-6")
        self.assertEqual(captured["kwargs"]["provider"], "anthropic")
        self.assertEqual(captured["kwargs"]["tools"], ["read", "write"])
        self.assertEqual(captured["kwargs"]["pi_bin"], "/custom/pi")
        task = self.store.get_task("AT-GH-410")
        self.assertEqual(task.model, "claude-sonnet-4-6")
        self.assertEqual(task.provider, "anthropic")
        self.assertEqual(task.tools, ["read", "write"])
        self.assertEqual(task.pi_bin, "/custom/pi")

    def test_noop_execution_still_works_with_profile_metadata(self) -> None:
        self._add_task("AT-GH-411")

        result = run_approved_task(
            self._request(
                task_key="AT-GH-411",
                executor="noop",
                validators=("policy",),
                model="claude-sonnet-4-6",
                provider="anthropic",
            ),
            store=self.store,
            executor_registry={"noop": NoopExecutor()},
            validator_registry={"policy": PolicyCheckValidator()},
            preflight_runner=lambda **kwargs: _preflight_result(),
        )

        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.status, APPROVED_TASK_STATUS)
        self.assertEqual(self.store.get_task("AT-GH-411").status, APPROVED_TASK_STATUS)
        self.assertFalse(result.safety["branch_pushed"])
        self.assertFalse(result.safety["pr_created"])
        self.assertFalse(result.safety["merged"])
        self.assertFalse(result.safety["approved"])
        self.assertFalse(result.safety["cleanup_performed"])

    def test_dry_run_does_not_prepare_workspace_or_mutate_db(self) -> None:
        self._add_task("AT-GH-408")
        before_status = self.store.get_task("AT-GH-408").status
        before_events = len(self.store.list_task_events("AT-GH-408"))
        before_artifacts = len(self.store.list_task_artifacts("AT-GH-408"))

        result = run_approved_task(
            self._request(task_key="AT-GH-408", dry_run=True, confirm_approved_task=False),
            store=self.store,
            preflight_runner=lambda **kwargs: _preflight_result(),
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "preview")
        self.assertEqual(result.phase, "preview")
        self.assertEqual(self.store.get_task("AT-GH-408").status, before_status)
        self.assertEqual(len(self.store.list_task_events("AT-GH-408")), before_events)
        self.assertEqual(len(self.store.list_task_artifacts("AT-GH-408")), before_artifacts)
        self.assertIsNone(self.store.get_task_worktree("AT-GH-408"))
        self.assertTrue(result.safety["read_only"])
        self.assertFalse(result.safety["workspace_prepared"])
        self.assertFalse(result.safety["executor_started"])
        self.assertFalse(result.safety["validators_started"])

    def test_runner_and_script_source_do_not_reference_forbidden_helpers(self) -> None:
        runner_text = (REPO_ROOT / "agent_taskflow" / "approved_task_runner.py").read_text(encoding="utf-8").lower()
        forbidden = [
            "git push",
            "gh pr create",
            "gh pr merge",
            "merge_pull_request",
            "create_pull_request",
            "push_task_branch",
            "delete_worktree",
            "delete_branch",
            "cleanup(",
            "run_recommended",
            "from_recommendation",
            "recommend_next_tasks",
        ]
        for item in forbidden:
            self.assertNotIn(item, runner_text)


if __name__ == "__main__":
    unittest.main()
