"""Tests for scripts/confirm_draft_pr.py."""

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

from agent_taskflow.draft_pr_confirm import DraftPrConfirmRequest, confirm_draft_pr
from agent_taskflow.github_issue_ingestion import GitHubIssueSnapshot, render_issue_spec
from agent_taskflow.mission_contract import build_mission_contract, write_mission_contract
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore
from scripts import confirm_draft_pr as script


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "confirm_draft_pr.py"


@dataclass
class FakeCompletedProcess:
    returncode: int
    stdout: str
    stderr: str = ""


class FakeGhRunner:
    def __init__(
        self,
        *,
        list_stdout: str = "[]\n",
        create_stdout: str = "https://github.com/anderson930420/agent-taskflow/pull/123\n",
        create_returncode: int = 0,
        create_stderr: str = "",
        view_stdout: str = "",
        view_returncode: int = 0,
        view_stderr: str = "",
    ) -> None:
        self.list_stdout = list_stdout
        self.create_stdout = create_stdout
        self.create_returncode = create_returncode
        self.create_stderr = create_stderr
        self.view_stdout = view_stdout
        self.view_returncode = view_returncode
        self.view_stderr = view_stderr
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> FakeCompletedProcess:
        self.calls.append({"args": args, "kwargs": kwargs})
        if args[:3] == ["gh", "pr", "list"]:
            return FakeCompletedProcess(returncode=0, stdout=self.list_stdout)
        if args[:3] == ["gh", "pr", "create"]:
            return FakeCompletedProcess(
                returncode=self.create_returncode,
                stdout=self.create_stdout,
                stderr=self.create_stderr,
            )
        if args[:3] == ["gh", "pr", "view"]:
            return FakeCompletedProcess(
                returncode=self.view_returncode,
                stdout=self.view_stdout,
                stderr=self.view_stderr,
            )
        raise AssertionError(f"unexpected command: {args}")


class ConfirmDraftPrScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.task_key = "AT-DF-CLI-001"
        self.branch = f"task/{self.task_key}"
        self.base_sha = self._init_repo()
        self.head_sha = self._git("rev-parse", "HEAD").stdout.strip()
        self._seed_task()

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
        (self.repo / "README.md").write_text("# draft pr cli test\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")
        self._git("switch", "-c", self.branch)
        (self.repo / "feature.txt").write_text("feature\n", encoding="utf-8")
        self._git("add", "feature.txt")
        self._git("commit", "-m", "feature")
        return self._git("rev-parse", "main").stdout.strip()

    def _issue_snapshot(self) -> GitHubIssueSnapshot:
        return GitHubIssueSnapshot(
            number=1002,
            title="Draft PR CLI task",
            body="Task body",
            state="open",
            labels=("ready",),
            author="octocat",
            url="https://github.com/anderson930420/agent-taskflow/issues/1002",
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-02T00:00:00Z",
        )

    def _seed_task(
        self,
        *,
        status: str = "waiting_approval",
        with_branch_push: bool = True,
        push_ok: bool = True,
        with_approval: bool = False,
    ) -> None:
        artifact_dir = self.artifact_root / self.task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=self.task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Draft PR CLI task",
                status=status,
                repo_path=self.repo,
                artifact_dir=artifact_dir,
            )
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=self.task_key,
                repo_path=self.repo,
                worktree_path=self.repo,
                branch=self.branch,
                base_branch="main",
                base_sha=self.base_sha,
                status="active",
            )
        )
        issue_spec_path = artifact_dir / "issue_spec.md"
        issue_spec_path.write_text(
            render_issue_spec(
                repo="anderson930420/agent-taskflow",
                task_key=self.task_key,
                issue=self._issue_snapshot(),
                ingested_at="2026-05-03T00:00:00Z",
            ),
            encoding="utf-8",
        )
        self.store.record_task_artifact(self.task_key, "issue_spec", issue_spec_path)
        contract = build_mission_contract(
            task_key=self.task_key,
            goal="Confirm draft PR via CLI",
            repo_path=self.repo,
            worktree_path=self.repo,
            artifact_dir=artifact_dir,
            executor="noop",
            required_validators=("pytest",),
        )
        write_mission_contract(contract, artifact_dir=artifact_dir)
        executor_log = artifact_dir / "executor.log"
        executor_log.write_text("executor log\n", encoding="utf-8")
        run_id = self.store.create_executor_run(self.task_key, "noop")
        self.store.finish_executor_run(
            self.task_key,
            run_id,
            executor="noop",
            status="completed",
            exit_code=0,
            summary="executor summary",
            log_path=executor_log,
            artifacts={"log": executor_log},
        )
        self.store.record_task_artifact(self.task_key, "worker_log", executor_log)
        validator_log = artifact_dir / "pytest.log"
        validator_log.write_text("validator log\n", encoding="utf-8")
        self.store.record_validation_result(
            self.task_key,
            "pytest",
            status="passed",
            exit_code=0,
            summary="validator summary",
            log_path=validator_log,
            artifacts={"log": validator_log},
        )
        self.store.record_task_artifact(self.task_key, "review_log", validator_log)
        if with_branch_push:
            branch_push_dir = self.artifact_root / "branch_push" / self.task_key
            branch_push_dir.mkdir(parents=True, exist_ok=True)
            branch_push_path = branch_push_dir / "branch_push.json"
            payload = {
                "kind": "branch_push_completed",
                "artifact_type": "branch_push",
                "task_key": self.task_key,
                "task_status": status,
                "remote": "origin",
                "branch": self.branch,
                "refspec": f"HEAD:{self.branch}",
                "worktree_path": str(self.repo),
                "base_branch": "main",
                "base_sha": self.base_sha,
                "head_sha": self.head_sha,
                "dry_run_performed": True,
                "dry_run_ok": True,
                "push_performed": True,
                "push_ok": push_ok,
                "dry_run_stdout_summary": "dry-run ok",
                "dry_run_stderr_summary": "",
                "push_stdout_summary": "push ok" if push_ok else "push failed",
                "push_stderr_summary": "" if push_ok else "push failed",
                "pushed_at": "2026-05-04T00:00:00Z",
                "pushed_commit_sha": self.head_sha,
                "branch_pushed": push_ok,
                "pr_created": False,
                "merged": False,
                "approved": False,
                "cleanup_performed": False,
                "requires_human_confirmation": True,
                "safety": {
                    "human_confirmation_required": True,
                    "human_confirmation_confirmed": push_ok,
                    "task_status_changed": False,
                    "workspace_prepared": False,
                    "executor_started": False,
                    "validators_started": False,
                    "branch_pushed": push_ok,
                    "pr_created": False,
                    "merged": False,
                    "approved": False,
                    "cleanup_performed": False,
                    "branch_deleted": False,
                    "worktree_deleted": False,
                    "force_push": False,
                    "background_worker_started": False,
                },
            }
            branch_push_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            self.store.record_task_artifact(self.task_key, "branch_push", branch_push_path)
            self.store.record_task_event(
                self.task_key,
                "branch_push_completed",
                "branch_push_confirm",
                message="Branch push confirmed and completed",
                payload=payload,
            )
        if with_approval:
            self.store.record_approval_decision(
                self.task_key,
                "accepted",
                decided_by="human-reviewer",
                notes="Looks good",
            )

    def _run_main(self, argv: list[str], *, runner=None) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                exit_code = script.main(argv, runner=runner)
            except SystemExit as exc:
                exit_code = int(exc.code or 0)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def _base_args(self) -> list[str]:
        return [
            "--task-key",
            self.task_key,
            "--db-path",
            str(self.db_path),
            "--repo",
            "anderson930420/agent-taskflow",
            "--repo-path",
            str(self.repo),
            "--artifact-root",
            str(self.artifact_root),
            "--json",
        ]

    def test_script_requires_task_key(self) -> None:
        exit_code, _stdout, stderr = self._run_main(
            [
                "--db-path",
                str(self.db_path),
                "--repo",
                "anderson930420/agent-taskflow",
                "--repo-path",
                str(self.repo),
                "--json",
            ]
        )

        self.assertNotEqual(exit_code, 0)
        self.assertIn("--task-key", stderr)

    def test_script_requires_confirm_for_actual_creation(self) -> None:
        runner = FakeGhRunner()
        exit_code, stdout, _stderr = self._run_main(self._base_args(), runner=runner)

        self.assertNotEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("--confirm-draft-pr", payload["error"])
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))

    def test_script_supports_dry_run_without_actual_creation(self) -> None:
        runner = FakeGhRunner(list_stdout="[]\n")
        exit_code, stdout, _stderr = self._run_main(
            self._base_args() + ["--dry-run"],
            runner=runner,
        )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse(payload["draft_pr"]["created"])
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))

    def test_script_dry_run_without_review_evidence_still_succeeds(self) -> None:
        runner = FakeGhRunner(list_stdout="[]\n")
        exit_code, stdout, _stderr = self._run_main(
            self._base_args() + ["--dry-run"],
            runner=runner,
        )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "dry_run")
        self.assertTrue(payload["handoff"]["ready_for_draft_pr_review"])
        self.assertFalse(
            any("approval/review evidence" in warning for warning in payload["warnings"])
        )

    def test_script_prints_valid_json(self) -> None:
        runner = FakeGhRunner(
            list_stdout="[]\n",
            create_stdout="https://github.com/anderson930420/agent-taskflow/pull/123\n",
            view_stdout=json.dumps(
                {
                    "url": "https://github.com/anderson930420/agent-taskflow/pull/123",
                    "number": 123,
                    "headRefName": self.branch,
                    "baseRefName": "main",
                    "isDraft": True,
                    "title": "AT-DF-CLI-001: Draft PR CLI task",
                    "body": "Task: AT-DF-CLI-001\n",
                    "state": "OPEN",
                }
            ),
        )
        exit_code, stdout, _stderr = self._run_main(
            self._base_args() + ["--confirm-draft-pr"],
            runner=runner,
        )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "draft_pr_created")
        self.assertTrue(payload["draft_pr"]["created"])
        self.assertEqual(payload["draft_pr"]["number"], 123)

    def test_script_confirm_without_review_evidence_still_creates_draft_pr(self) -> None:
        runner = FakeGhRunner(
            list_stdout="[]\n",
            create_stdout="https://github.com/anderson930420/agent-taskflow/pull/125\n",
            view_stdout=json.dumps(
                {
                    "url": "https://github.com/anderson930420/agent-taskflow/pull/125",
                    "number": 125,
                    "headRefName": self.branch,
                    "baseRefName": "main",
                    "isDraft": True,
                    "title": "AT-DF-CLI-001: Draft PR CLI task",
                    "body": "Task: AT-DF-CLI-001\n",
                    "state": "OPEN",
                }
            ),
        )
        exit_code, stdout, _stderr = self._run_main(
            self._base_args() + ["--confirm-draft-pr"],
            runner=runner,
        )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "draft_pr_created")
        self.assertTrue(payload["draft_pr"]["created"])
        self.assertFalse(
            any("approval/review evidence" in warning for warning in payload["warnings"])
        )
        self.assertTrue(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))

    def test_script_rejects_non_waiting_task_by_default(self) -> None:
        self._seed_task(status="blocked")
        runner = FakeGhRunner()
        exit_code, stdout, _stderr = self._run_main(
            self._base_args() + ["--dry-run"],
            runner=runner,
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
                self.task_key,
                "--db-path",
                str(missing_db),
                "--repo",
                "anderson930420/agent-taskflow",
                "--repo-path",
                str(self.repo),
                "--json",
            ]
        )

        self.assertNotEqual(exit_code, 0)
        self.assertFalse(missing_db.exists())
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "not_found")

    def test_script_does_not_update_task_status(self) -> None:
        runner = FakeGhRunner(
            list_stdout="[]\n",
            create_stdout="https://github.com/anderson930420/agent-taskflow/pull/124\n",
            view_stdout=json.dumps(
                {
                    "url": "https://github.com/anderson930420/agent-taskflow/pull/124",
                    "number": 124,
                    "headRefName": self.branch,
                    "baseRefName": "main",
                    "isDraft": True,
                    "title": "AT-DF-CLI-001: Draft PR CLI task",
                    "body": "Task: AT-DF-CLI-001\n",
                    "state": "OPEN",
                }
            ),
        )
        exit_code, stdout, _stderr = self._run_main(
            self._base_args() + ["--confirm-draft-pr"],
            runner=runner,
        )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "draft_pr_created")
        self.assertEqual(self.store.get_task(self.task_key).status, "waiting_approval")
        self.assertTrue(
            any(event.event_type == "draft_pr_created" for event in self.store.list_task_events(self.task_key))
        )

    def test_script_does_not_prepare_worktree_or_run_executor_or_validators(self) -> None:
        runner = FakeGhRunner(list_stdout="[]\n")
        before_executor_runs = len(self.store.list_executor_runs(self.task_key))
        before_validation_results = len(self.store.list_validation_results(self.task_key))
        exit_code, stdout, _stderr = self._run_main(
            self._base_args() + ["--dry-run"],
            runner=runner,
        )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "dry_run")
        self.assertEqual(before_executor_runs, len(self.store.list_executor_runs(self.task_key)))
        self.assertEqual(before_validation_results, len(self.store.list_validation_results(self.task_key)))

    def test_script_does_not_push_merge_approve_or_cleanup(self) -> None:
        text = Path(SCRIPT_PATH).read_text(encoding="utf-8")
        forbidden = [
            "gh pr merge",
            "gh pr review --approve",
            "gh pr ready",
            "gh pr close",
            "gh issue close",
            "git push",
            "git merge",
            "git reset --hard",
            "git clean",
            "git branch -D",
            "git worktree remove",
            "prepare_worktree",
            "dispatch",
            "webhook",
            "cron",
            "polling",
            "daemon",
            "while True",
        ]
        for token in forbidden:
            self.assertNotIn(token, text)

    def test_script_static_no_branch_worktree_deletion_helpers(self) -> None:
        text = Path(SCRIPT_PATH).read_text(encoding="utf-8")
        self.assertNotIn("delete_branch", text)
        self.assertNotIn("delete_worktree", text)


if __name__ == "__main__":
    unittest.main()
