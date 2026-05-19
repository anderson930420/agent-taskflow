"""Tests for scripts/confirm_branch_push.py."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.github_issue_ingestion import GitHubIssueSnapshot, render_issue_spec
from agent_taskflow.mission_contract import build_mission_contract, write_mission_contract
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore
from scripts import confirm_branch_push as script


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "confirm_branch_push.py"


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


class ConfirmBranchPushScriptTests(unittest.TestCase):
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
        (self.repo / "README.md").write_text("# branch push confirm cli\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")
        self._git("switch", "-c", "task/AT-BP-CLI-001")
        (self.repo / "feature.txt").write_text("feature\n", encoding="utf-8")
        self._git("add", "feature.txt")
        self._git("commit", "-m", "feature")
        return self._git("rev-parse", "main").stdout.strip()

    def _issue_snapshot(self) -> GitHubIssueSnapshot:
        return GitHubIssueSnapshot(
            number=1002,
            title="Branch push confirm CLI task",
            body="Task body",
            state="open",
            labels=("ready",),
            author="octocat",
            url="https://github.com/anderson930420/agent-taskflow/issues/1002",
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-02T00:00:00Z",
        )

    def _seed_ready_task(
        self,
        *,
        task_key: str = "AT-BP-CLI-001",
        status: str = "waiting_approval",
        branch: str = "task/AT-BP-CLI-001",
        repo_path: Path | None = None,
        with_issue_spec: bool = True,
        with_executor: bool = True,
        with_validator: bool = True,
    ) -> Path:
        repo = repo_path or self.repo
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Branch push confirm CLI task",
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
                base_branch="main",
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

    def _run_main(
        self,
        argv: list[str],
        *,
        runner: FakePushRunner | None = None,
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                exit_code = script.main(argv, runner=runner)
            except SystemExit as exc:
                exit_code = int(exc.code or 0)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_script_requires_task_key(self) -> None:
        exit_code, _stdout, stderr = self._run_main(
            [
                "--repo-path",
                str(self.repo),
                "--db-path",
                str(self.db_path),
                "--json",
            ]
        )

        self.assertNotEqual(exit_code, 0)
        self.assertIn("--task-key", stderr)

    def test_script_prints_valid_json(self) -> None:
        self._seed_ready_task()
        exit_code, stdout, _stderr = self._run_main(
            [
                "--task-key",
                "AT-BP-CLI-001",
                "--repo-path",
                str(self.repo),
                "--db-path",
                str(self.db_path),
                "--confirm-branch-push",
                "--json",
            ],
            runner=FakePushRunner(),
        )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["task_key"], "AT-BP-CLI-001")
        self.assertEqual(payload["status"], "pushed")

    def test_script_supports_pretty(self) -> None:
        self._seed_ready_task()
        exit_code, stdout, _stderr = self._run_main(
            [
                "--task-key",
                "AT-BP-CLI-001",
                "--repo-path",
                str(self.repo),
                "--db-path",
                str(self.db_path),
                "--dry-run",
                "--pretty",
            ],
            runner=FakePushRunner(),
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("\n  ", stdout)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "dry_run")

    def test_script_requires_confirm_branch_push_for_actual_push(self) -> None:
        self._seed_ready_task()
        exit_code, stdout, _stderr = self._run_main(
            [
                "--task-key",
                "AT-BP-CLI-001",
                "--repo-path",
                str(self.repo),
                "--db-path",
                str(self.db_path),
                "--json",
            ],
            runner=FakePushRunner(),
        )

        self.assertNotEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("--confirm-branch-push", payload["error"])

    def test_script_supports_dry_run_without_actual_push(self) -> None:
        self._seed_ready_task()
        runner = FakePushRunner()
        exit_code, stdout, _stderr = self._run_main(
            [
                "--task-key",
                "AT-BP-CLI-001",
                "--repo-path",
                str(self.repo),
                "--db-path",
                str(self.db_path),
                "--dry-run",
                "--json",
            ],
            runner=runner,
        )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse(payload["branch_pushed"])
        self.assertEqual(
            runner.calls,
            [["git", "push", "--dry-run", "origin", "HEAD:task/AT-BP-CLI-001"]],
        )

    def test_script_rejects_non_waiting_task_by_default(self) -> None:
        self._seed_ready_task(status="blocked")
        exit_code, stdout, _stderr = self._run_main(
            [
                "--task-key",
                "AT-BP-CLI-001",
                "--repo-path",
                str(self.repo),
                "--db-path",
                str(self.db_path),
                "--confirm-branch-push",
                "--json",
            ],
            runner=FakePushRunner(),
        )

        self.assertNotEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("waiting_approval", payload["error"])

    def test_script_handles_missing_db_without_creating_file(self) -> None:
        missing_db = self.root / "missing.db"
        exit_code, stdout, _stderr = self._run_main(
            [
                "--task-key",
                "AT-BP-CLI-001",
                "--repo-path",
                str(self.repo),
                "--db-path",
                str(missing_db),
                "--json",
            ],
            runner=FakePushRunner(),
        )

        self.assertNotEqual(exit_code, 0)
        self.assertFalse(missing_db.exists())
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("SQLite state DB not found", payload["error"])

    def test_script_does_not_update_task_status(self) -> None:
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("update_task_status", text)

    def test_script_does_not_prepare_worktree(self) -> None:
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("prepare_task_workspace", text)
        self.assertNotIn("prepare_worktree", text)

    def test_script_does_not_dispatch_executor(self) -> None:
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("dispatch", text)

    def test_script_does_not_run_validators(self) -> None:
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("run_validators", text)
        self.assertNotIn("record_validation_result", text)

    def test_script_does_not_create_pr(self) -> None:
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("create_draft_pr", text)
        self.assertNotIn("create_pull_request", text)
        self.assertNotIn("gh pr create", text)

    def test_script_does_not_merge(self) -> None:
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("merge_pull_request", text)
        self.assertNotIn("gh pr merge", text)
        self.assertNotIn("git merge", text)

    def test_script_safety_block_marks_pr_merge_approval_cleanup_and_deletions_false(self) -> None:
        self._seed_ready_task()
        exit_code, stdout, _stderr = self._run_main(
            [
                "--task-key",
                "AT-BP-CLI-001",
                "--repo-path",
                str(self.repo),
                "--db-path",
                str(self.db_path),
                "--dry-run",
                "--json",
            ],
            runner=FakePushRunner(),
        )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertFalse(payload["safety"]["pr_created"])
        self.assertFalse(payload["safety"]["merged"])
        self.assertFalse(payload["safety"]["approved"])
        self.assertFalse(payload["safety"]["cleanup_performed"])
        self.assertFalse(payload["safety"]["branch_deleted"])
        self.assertFalse(payload["safety"]["worktree_deleted"])

    def test_script_does_not_delete_branch_or_worktree(self) -> None:
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("git branch -D", text)
        self.assertNotIn("git worktree remove", text)


if __name__ == "__main__":
    unittest.main()
