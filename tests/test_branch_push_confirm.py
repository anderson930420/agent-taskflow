"""Tests for agent_taskflow.branch_push_confirm."""

from __future__ import annotations

from dataclasses import dataclass
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.branch_push_confirm import (
    BranchPushConfirmError,
    BranchPushConfirmRequest,
    confirm_branch_push,
)
from agent_taskflow.github_issue_ingestion import GitHubIssueSnapshot, render_issue_spec
from agent_taskflow.mission_contract import build_mission_contract, write_mission_contract
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore


@dataclass
class FakeCompletedProcess:
    returncode: int
    stdout: str
    stderr: str = ""


class FakePushRunner:
    def __init__(
        self,
        *,
        dry_run_returncode: int = 0,
        push_returncode: int = 0,
        dry_run_stdout: str = "dry-run ok\n",
        dry_run_stderr: str = "",
        push_stdout: str = "push ok\n",
        push_stderr: str = "",
    ) -> None:
        self.dry_run_returncode = dry_run_returncode
        self.push_returncode = push_returncode
        self.dry_run_stdout = dry_run_stdout
        self.dry_run_stderr = dry_run_stderr
        self.push_stdout = push_stdout
        self.push_stderr = push_stderr
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> FakeCompletedProcess:
        self.calls.append(args)
        if args[:3] == ["git", "push", "--dry-run"]:
            return FakeCompletedProcess(
                returncode=self.dry_run_returncode,
                stdout=self.dry_run_stdout,
                stderr=self.dry_run_stderr,
            )
        if args[:2] == ["git", "push"] and "--dry-run" not in args:
            return FakeCompletedProcess(
                returncode=self.push_returncode,
                stdout=self.push_stdout,
                stderr=self.push_stderr,
            )
        raise AssertionError(f"unexpected command: {args}")


class BranchPushConfirmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.other_repo = self.root / "other-repo"
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.base_sha = self._init_repo()
        self._seed_ready_task()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd or self.repo,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.returncode != 0:
            self.fail(f"git {' '.join(args)} failed: {completed.stderr}")
        return completed

    def _init_repo(self) -> str:
        self.repo.mkdir(parents=True)
        self._git("init", "-b", "main")
        self._git("config", "user.email", "agent-taskflow@example.invalid")
        self._git("config", "user.name", "Agent Taskflow")
        (self.repo / "README.md").write_text("# branch push confirm\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")
        self._git("switch", "-c", "task/AT-BP-CONFIRM-001")
        (self.repo / "feature.txt").write_text("feature\n", encoding="utf-8")
        self._git("add", "feature.txt")
        self._git("commit", "-m", "feature")
        return self._git("rev-parse", "main").stdout.strip()

    def _issue_snapshot(self) -> GitHubIssueSnapshot:
        return GitHubIssueSnapshot(
            number=1001,
            title="Branch push confirm task",
            body="Task body",
            state="open",
            labels=("ready",),
            author="octocat",
            url="https://github.com/anderson930420/agent-taskflow/issues/1001",
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-02T00:00:00Z",
        )

    def _seed_ready_task(
        self,
        *,
        task_key: str = "AT-BP-CONFIRM-001",
        status: str = "waiting_approval",
        repo_path: Path | None = None,
        branch: str = "task/AT-BP-CONFIRM-001",
        with_issue_spec: bool = True,
        with_executor: bool = True,
        with_validator: bool = True,
        worktree_exists: bool = True,
        base_branch: str = "main",
    ) -> Path:
        repo = repo_path or self.repo
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        if worktree_exists:
            self.repo.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Branch push confirm task",
                status=status,
                repo_path=repo,
                artifact_dir=artifact_dir,
            )
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=task_key,
                repo_path=repo,
                worktree_path=self.repo,
                branch=branch,
                base_branch=base_branch,
                base_sha=self.base_sha,
                status="active",
            )
        )
        if with_issue_spec:
            issue_spec_path = artifact_dir / "issue_spec.md"
            issue_spec_path.write_text(
                render_issue_spec(
                    repo="anderson930420/agent-taskflow",
                    task_key=task_key,
                    issue=self._issue_snapshot(),
                    ingested_at="2026-05-03T00:00:00Z",
                ),
                encoding="utf-8",
            )
            self.store.record_task_artifact(task_key, "issue_spec", issue_spec_path)
        contract = build_mission_contract(
            task_key=task_key,
            goal="Confirm branch push after waiting approval",
            repo_path=repo,
            worktree_path=self.repo,
            artifact_dir=artifact_dir,
            executor="noop",
            required_validators=("pytest",),
        )
        write_mission_contract(contract, artifact_dir=artifact_dir)
        if with_executor:
            executor_log = artifact_dir / "executor.log"
            executor_log.write_text("executor log\n", encoding="utf-8")
            run_id = self.store.create_executor_run(task_key, "noop")
            self.store.finish_executor_run(
                task_key,
                run_id,
                executor="noop",
                status="completed",
                exit_code=0,
                summary="executor summary",
                log_path=executor_log,
                artifacts={"log": executor_log},
            )
            self.store.record_task_artifact(task_key, "worker_log", executor_log)
        if with_validator:
            validator_log = artifact_dir / "pytest.log"
            validator_log.write_text("validator log\n", encoding="utf-8")
            self.store.record_validation_result(
                task_key,
                "pytest",
                status="passed",
                exit_code=0,
                summary="validator summary",
                log_path=validator_log,
                artifacts={"log": validator_log},
            )
            self.store.record_task_artifact(task_key, "review_log", validator_log)
        return artifact_dir

    def _request(
        self,
        *,
        task_key: str = "AT-BP-CONFIRM-001",
        repo_path: Path | None = None,
        dry_run: bool = False,
        confirm: bool = False,
        allow_non_waiting: bool = False,
        branch: str | None = None,
        remote: str = "origin",
    ) -> BranchPushConfirmRequest:
        return BranchPushConfirmRequest(
            task_key=task_key,
            repo_path=repo_path or self.repo,
            db_path=self.db_path,
            artifact_root=self.artifact_root,
            remote=remote,
            branch=branch,
            dry_run=dry_run,
            confirm_branch_push=confirm,
            allow_non_waiting=allow_non_waiting,
        )

    def test_missing_task_returns_blocked_result(self) -> None:
        result = confirm_branch_push(
            self._request(task_key="AT-MISSING"),
            store=self.store,
            runner=FakePushRunner(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("Task not found", result.error or "")
        self.assertFalse(result.safety["branch_pushed"])

    def test_task_not_waiting_approval_is_rejected_by_default(self) -> None:
        self.store.update_task_status(
            "AT-BP-CONFIRM-001",
            "blocked",
            source="test",
        )

        result = confirm_branch_push(
            self._request(confirm=True),
            store=self.store,
            runner=FakePushRunner(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("waiting_approval", result.error or "")
        self.assertFalse(result.push_performed)

    def test_missing_confirm_flag_refuses_actual_push(self) -> None:
        runner = FakePushRunner()
        result = confirm_branch_push(
            self._request(),
            store=self.store,
            runner=runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("--confirm-branch-push", result.error or "")
        self.assertEqual(runner.calls, [["git", "push", "--dry-run", "origin", "HEAD:task/AT-BP-CONFIRM-001"]])
        self.assertFalse(result.push_performed)
        self.assertFalse(result.event_recorded)
        self.assertFalse(result.artifact_recorded)

    def test_dry_run_does_not_push_or_record_evidence(self) -> None:
        runner = FakePushRunner()
        result = confirm_branch_push(
            self._request(dry_run=True),
            store=self.store,
            runner=runner,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "dry_run")
        self.assertTrue(result.dry_run_performed)
        self.assertTrue(result.dry_run_ok)
        self.assertFalse(result.push_performed)
        self.assertFalse(result.event_recorded)
        self.assertFalse(result.artifact_recorded)
        self.assertEqual(runner.calls, [["git", "push", "--dry-run", "origin", "HEAD:task/AT-BP-CONFIRM-001"]])
        self.assertFalse((self.artifact_root / "branch_push" / "AT-BP-CONFIRM-001" / "branch_push.json").exists())

    def test_ready_task_with_confirmation_runs_dry_run_then_actual_push(self) -> None:
        runner = FakePushRunner()
        result = confirm_branch_push(
            self._request(confirm=True),
            store=self.store,
            runner=runner,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "pushed")
        self.assertEqual(
            runner.calls,
            [
                ["git", "push", "--dry-run", "origin", "HEAD:task/AT-BP-CONFIRM-001"],
                ["git", "push", "origin", "HEAD:task/AT-BP-CONFIRM-001"],
            ],
        )
        self.assertTrue(result.branch_push_json_path)
        self.assertTrue(Path(result.branch_push_json_path).is_file())
        self.assertTrue(result.event_recorded)
        self.assertTrue(result.artifact_recorded)
        payload = json.loads(Path(result.branch_push_json_path).read_text(encoding="utf-8"))
        self.assertEqual(payload["kind"], "branch_push_completed")
        self.assertTrue(payload["branch_pushed"])
        self.assertTrue(payload["push_ok"])
        self.assertFalse(payload["pr_created"])
        self.assertFalse(payload["merged"])
        self.assertFalse(payload["approved"])
        self.assertFalse(payload["cleanup_performed"])
        self.assertTrue(any(event.event_type == "branch_push_completed" for event in self.store.list_task_events("AT-BP-CONFIRM-001")))
        self.assertTrue(any(artifact.artifact_type == "branch_push" for artifact in self.store.list_task_artifacts("AT-BP-CONFIRM-001")))
        self.assertTrue(result.safety["branch_pushed"])
        self.assertFalse(result.safety["pr_created"])
        self.assertFalse(result.safety["merged"])
        self.assertFalse(result.safety["approved"])
        self.assertFalse(result.safety["cleanup_performed"])

    def test_failed_dry_run_prevents_actual_push(self) -> None:
        runner = FakePushRunner(dry_run_returncode=1, dry_run_stderr="dry-run failed")
        result = confirm_branch_push(
            self._request(confirm=True),
            store=self.store,
            runner=runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("git push --dry-run failed", result.error or "")
        self.assertEqual(runner.calls, [["git", "push", "--dry-run", "origin", "HEAD:task/AT-BP-CONFIRM-001"]])
        self.assertFalse(result.push_performed)
        self.assertFalse(result.event_recorded)
        self.assertFalse(result.artifact_recorded)

    def test_failed_actual_push_does_not_record_evidence(self) -> None:
        runner = FakePushRunner(push_returncode=1, push_stderr="push failed")
        result = confirm_branch_push(
            self._request(confirm=True),
            store=self.store,
            runner=runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("git push failed", result.error or "")
        self.assertEqual(
            runner.calls,
            [
                ["git", "push", "--dry-run", "origin", "HEAD:task/AT-BP-CONFIRM-001"],
                ["git", "push", "origin", "HEAD:task/AT-BP-CONFIRM-001"],
            ],
        )
        self.assertFalse(result.event_recorded)
        self.assertFalse(result.artifact_recorded)
        self.assertFalse(any(event.event_type == "branch_push_completed" for event in self.store.list_task_events("AT-BP-CONFIRM-001")))

    def test_missing_handoff_readiness_blocks_push(self) -> None:
        self._seed_ready_task(task_key="AT-BP-NO-VALIDATOR", branch="task/AT-BP-NO-VALIDATOR", with_validator=False)
        runner = FakePushRunner()

        result = confirm_branch_push(
            self._request(task_key="AT-BP-NO-VALIDATOR", branch="task/AT-BP-NO-VALIDATOR", confirm=True),
            store=self.store,
            runner=runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("validator", " ".join(result.warnings).lower())
        self.assertEqual(runner.calls, [])

    def test_blocking_warnings_block_push(self) -> None:
        self.other_repo.mkdir(parents=True, exist_ok=True)
        self._seed_ready_task()
        runner = FakePushRunner()

        result = confirm_branch_push(
            self._request(repo_path=self.other_repo, confirm=True),
            store=self.store,
            runner=runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertTrue(result.warnings)
        self.assertIn("does not match", " ".join(result.warnings))
        self.assertEqual(runner.calls, [])

    def test_invalid_branch_blocks_push(self) -> None:
        runner = FakePushRunner()
        result = confirm_branch_push(
            self._request(branch="bad branch", confirm=True),
            store=self.store,
            runner=runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("simple branch name", result.error or "")
        self.assertEqual(runner.calls, [])

    def test_main_branch_blocks_push(self) -> None:
        self._seed_ready_task(task_key="AT-BP-MAIN", branch="main")
        self._git("switch", "main")
        runner = FakePushRunner()

        result = confirm_branch_push(
            self._request(task_key="AT-BP-MAIN", branch="main", confirm=True),
            store=self.store,
            runner=runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("protected branch", result.error or "")
        self.assertEqual(runner.calls, [])

    def test_static_source_does_not_contain_force_push_commands(self) -> None:
        text = (
            Path(__file__).resolve().parents[1] / "agent_taskflow" / "branch_push_confirm.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("git push --force", text)
        self.assertNotIn("git push -f", text)
        self.assertNotIn("git push --force-with-lease", text)
        self.assertNotIn("git push --mirror", text)


if __name__ == "__main__":
    unittest.main()
