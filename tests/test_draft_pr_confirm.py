"""Tests for agent_taskflow.draft_pr_confirm."""

from __future__ import annotations

from dataclasses import dataclass
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.draft_pr_confirm import (
    DraftPrConfirmError,
    DraftPrConfirmRequest,
    confirm_draft_pr,
)
import agent_taskflow.draft_pr_confirm as draft_pr_confirm_module
from agent_taskflow.github_issue_ingestion import GitHubIssueSnapshot, render_issue_spec
from agent_taskflow.mission_contract import build_mission_contract, write_mission_contract
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore


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
        list_returncode: int = 0,
        create_stdout: str = "https://github.com/anderson930420/agent-taskflow/pull/123\n",
        create_returncode: int = 0,
        create_stderr: str = "",
        view_stdout: str = "",
        view_returncode: int = 0,
        view_stderr: str = "",
    ) -> None:
        self.list_stdout = list_stdout
        self.list_returncode = list_returncode
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
            return FakeCompletedProcess(
                returncode=self.list_returncode,
                stdout=self.list_stdout,
            )
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


class DraftPrConfirmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.task_key = "AT-DF-CONFIRM-001"
        self.branch = f"task/{self.task_key}"
        self.base_sha = self._init_repo()
        self.head_sha = self._git("rev-parse", "HEAD").stdout.strip()

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
        (self.repo / "README.md").write_text("# draft pr confirm test\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")
        self._git("switch", "-c", self.branch)
        (self.repo / "feature.txt").write_text("feature\n", encoding="utf-8")
        self._git("add", "feature.txt")
        self._git("commit", "-m", "feature")
        return self._git("rev-parse", "main").stdout.strip()

    def _issue_snapshot(self) -> GitHubIssueSnapshot:
        return GitHubIssueSnapshot(
            number=1001,
            title="Draft PR confirm task",
            body="Task body",
            state="open",
            labels=("ready",),
            author="octocat",
            url="https://github.com/anderson930420/agent-taskflow/issues/1001",
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-02T00:00:00Z",
        )

    def _gh_view_stdout(
        self,
        *,
        number: int,
        files: list[str] | None = None,
        commits: list[str] | None = None,
        title: str | None = None,
        body: str | None = None,
        head: str | None = None,
        base: str = "main",
        state: str = "OPEN",
        is_draft: bool = True,
        url: str | None = None,
    ) -> str:
        return json.dumps(
            {
                "url": url or f"https://github.com/anderson930420/agent-taskflow/pull/{number}",
                "number": number,
                "headRefName": head or self.branch,
                "baseRefName": base,
                "isDraft": is_draft,
                "title": title or "AT-DF-CONFIRM-001: Draft PR confirm task",
                "body": body or "Task: AT-DF-CONFIRM-001\n",
                "state": state,
                "commits": [{"oid": oid} for oid in (commits or [self.head_sha])],
                "files": [{"path": path} for path in (files or ["feature.txt"])],
            }
        )

    def _seed_task(
        self,
        *,
        status: str = "waiting_approval",
        with_issue_spec: bool = True,
        with_worktree: bool = True,
        with_executor: bool = True,
        with_validator: bool = True,
        with_branch_push: bool = True,
        push_ok: bool = True,
        with_approval: bool = False,
        branch: str | None = None,
        base_branch: str = "main",
        repo_path: Path | None = None,
    ) -> Path:
        task_branch = branch or self.branch
        repo = repo_path or self.repo
        artifact_dir = self.artifact_root / self.task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=self.task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Draft PR confirm task",
                status=status,
                repo_path=repo,
                artifact_dir=artifact_dir,
            )
        )
        if with_worktree:
            self.store.upsert_task_worktree(
                TaskWorktreeRecord(
                    task_key=self.task_key,
                    repo_path=repo,
                    worktree_path=self.repo,
                    branch=task_branch,
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
                    task_key=self.task_key,
                    issue=self._issue_snapshot(),
                    ingested_at="2026-05-03T00:00:00Z",
                ),
                encoding="utf-8",
            )
            self.store.record_task_artifact(self.task_key, "issue_spec", issue_spec_path)
        contract = build_mission_contract(
            task_key=self.task_key,
            goal="Confirm draft PR creation",
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
        if with_validator:
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
                "branch": task_branch,
                "refspec": f"HEAD:{task_branch}",
                "worktree_path": str(self.repo),
                "base_branch": base_branch,
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
        return artifact_dir

    def _request(
        self,
        *,
        dry_run: bool = False,
        confirm: bool = False,
        allow_non_waiting: bool = False,
        base: str | None = None,
        head: str | None = None,
        title: str | None = None,
        body_file: Path | None = None,
        repo_path: Path | None = None,
        repo: str = "anderson930420/agent-taskflow",
    ) -> DraftPrConfirmRequest:
        return DraftPrConfirmRequest(
            task_key=self.task_key,
            repo=repo,
            repo_path=repo_path or self.repo,
            db_path=self.db_path,
            artifact_root=self.artifact_root,
            base=base,
            head=head,
            title=title,
            body_file=body_file,
            dry_run=dry_run,
            confirm_draft_pr=confirm,
            allow_non_waiting=allow_non_waiting,
        )

    def test_missing_task_returns_not_found_result(self) -> None:
        result = confirm_draft_pr(
            DraftPrConfirmRequest(
                task_key="AT-MISSING",
                repo="anderson930420/agent-taskflow",
                repo_path=self.repo,
                db_path=self.db_path,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "not_found")
        self.assertIn("Task not found", result.error or "")

    def test_task_not_waiting_is_rejected_by_default(self) -> None:
        self._seed_task(status="blocked")

        result = confirm_draft_pr(self._request(dry_run=True))

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("waiting_approval", result.error or "")

    def test_missing_confirm_refuses_actual_creation(self) -> None:
        self._seed_task()
        runner = FakeGhRunner()

        result = confirm_draft_pr(self._request(confirm=False), runner=runner)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("--confirm-draft-pr", result.error or "")
        self.assertGreaterEqual(len(runner.calls), 1)
        self.assertTrue(any(call["args"][:3] == ["gh", "pr", "list"] for call in runner.calls))
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))

    def test_dry_run_does_not_create_pr_or_record_draft_evidence(self) -> None:
        self._seed_task()
        before_artifacts = len(self.store.list_task_artifacts(self.task_key))
        before_events = len(self.store.list_task_events(self.task_key))
        runner = FakeGhRunner(list_stdout="[]\n")

        result = confirm_draft_pr(self._request(dry_run=True), runner=runner)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "dry_run")
        self.assertFalse(result.evidence["artifact_recorded"])
        self.assertFalse(result.evidence["event_recorded"])
        self.assertTrue(result.handoff["ready_for_draft_pr_review"])
        self.assertTrue(result.verification_preview["post_create_verification_required"])
        self.assertEqual(result.verification_preview["expected_files"], ["feature.txt"])
        self.assertEqual(result.verification_preview["expected_commits"], [self.head_sha])
        self.assertEqual(before_artifacts, len(self.store.list_task_artifacts(self.task_key)))
        self.assertEqual(before_events, len(self.store.list_task_events(self.task_key)))
        self.assertTrue(any(call["args"][:3] == ["gh", "pr", "list"] for call in runner.calls))
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))

    def test_missing_review_evidence_does_not_block_dry_run(self) -> None:
        self._seed_task(with_approval=False)
        runner = FakeGhRunner(list_stdout="[]\n")

        result = confirm_draft_pr(self._request(dry_run=True), runner=runner)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "dry_run")
        self.assertTrue(result.handoff["ready_for_draft_pr_review"])
        self.assertFalse(
            any("approval/review evidence" in warning for warning in result.warnings)
        )
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))

    def test_missing_phase_5b_readiness_blocks_creation(self) -> None:
        self._seed_task(with_validator=False)
        runner = FakeGhRunner()

        result = confirm_draft_pr(self._request(confirm=True), runner=runner)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("Validator evidence", " ".join(result.warnings))
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))

    def test_missing_review_evidence_does_not_block_actual_creation(self) -> None:
        self._seed_task(with_approval=False)
        runner = FakeGhRunner(
            list_stdout="[]\n",
            create_stdout="https://github.com/anderson930420/agent-taskflow/pull/125\n",
            view_stdout=self._gh_view_stdout(number=125),
        )

        result = confirm_draft_pr(self._request(confirm=True), runner=runner)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "draft_pr_created")
        self.assertTrue(result.draft_pr["created"])
        self.assertTrue(result.draft_pr["verified"])
        self.assertTrue(result.verification["passed"])
        self.assertTrue(result.summary["verified"])
        self.assertTrue(result.evidence["verification_recorded"])
        self.assertFalse(
            any("approval/review evidence" in warning for warning in result.warnings)
        )
        self.assertTrue(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))

    def test_missing_phase_5c_branch_push_evidence_blocks_creation(self) -> None:
        self._seed_task(with_branch_push=False)
        runner = FakeGhRunner()

        result = confirm_draft_pr(self._request(confirm=True), runner=runner)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("branch push evidence", " ".join(result.warnings))
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))

    def test_branch_push_push_ok_false_blocks_creation(self) -> None:
        self._seed_task(push_ok=False)
        runner = FakeGhRunner()

        result = confirm_draft_pr(self._request(confirm=True), runner=runner)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("push_ok must be True", " ".join(result.warnings))
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))

    def test_head_branch_main_blocks_creation(self) -> None:
        self._seed_task(branch="main")
        runner = FakeGhRunner()

        result = confirm_draft_pr(self._request(confirm=True, head="main", base="main"), runner=runner)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("Head branch", result.error or "")
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))

    def test_empty_title_or_body_blocks_creation(self) -> None:
        self._seed_task()
        empty_body = self.root / "empty-body.md"
        empty_body.write_text("", encoding="utf-8")
        runner = FakeGhRunner()

        result = confirm_draft_pr(
            self._request(confirm=True, title="", body_file=empty_body),
            runner=runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("title", result.error or "")
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))

    def test_existing_open_pr_blocks_duplicate_creation(self) -> None:
        self._seed_task()
        runner = FakeGhRunner(
            list_stdout=json.dumps(
                [
                    {
                        "number": 7,
                        "url": "https://github.com/anderson930420/agent-taskflow/pull/7",
                        "state": "OPEN",
                        "isDraft": True,
                        "title": "Existing draft PR",
                    }
                ]
            )
            + "\n",
            view_stdout=self._gh_view_stdout(
                number=7,
                url="https://github.com/anderson930420/agent-taskflow/pull/7",
                title="AT-DF-CONFIRM-001: Draft PR confirm task",
            ),
        )

        result = confirm_draft_pr(self._request(dry_run=True), runner=runner)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "already_exists_verified")
        self.assertTrue(result.existing_pr["exists"])
        self.assertEqual(result.existing_pr["number"], 7)
        self.assertTrue(result.verification["passed"])
        self.assertTrue(result.summary["verified"])
        self.assertEqual(result.verification["expected_files"], ["feature.txt"])
        self.assertEqual(result.verification["actual_files"], ["feature.txt"])
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))

    def test_ready_task_with_confirmation_creates_draft_pr(self) -> None:
        self._seed_task()
        before_artifacts = len(self.store.list_task_artifacts(self.task_key))
        before_events = len(self.store.list_task_events(self.task_key))
        runner = FakeGhRunner(
            list_stdout="[]\n",
            create_stdout="https://github.com/anderson930420/agent-taskflow/pull/123\n",
            view_stdout=self._gh_view_stdout(number=123),
        )

        result = confirm_draft_pr(self._request(confirm=True), runner=runner)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "draft_pr_created")
        self.assertTrue(result.draft_pr["created"])
        self.assertTrue(result.draft_pr["verified"])
        self.assertTrue(result.verification["passed"])
        self.assertTrue(result.summary["verified"])
        self.assertTrue(result.evidence["verification_recorded"])
        self.assertTrue(result.safety["pr_created"])
        self.assertTrue(result.safety["draft_pr"])
        self.assertTrue(result.safety["draft_pr_verified"])
        self.assertFalse(result.safety["merged"])
        self.assertFalse(result.safety["approved"])
        self.assertFalse(result.safety["cleanup_performed"])
        self.assertEqual(len(runner.calls), 3)
        self.assertTrue(any(call["args"][:3] == ["gh", "pr", "list"] for call in runner.calls))
        self.assertTrue(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))
        self.assertTrue(any(call["args"][:3] == ["gh", "pr", "view"] for call in runner.calls))
        self.assertGreater(len(self.store.list_task_artifacts(self.task_key)), before_artifacts)
        self.assertGreater(len(self.store.list_task_events(self.task_key)), before_events)
        self.assertTrue(
            any(
                artifact.artifact_type == "draft_pr"
                for artifact in self.store.list_task_artifacts(self.task_key)
            )
        )
        self.assertTrue(
            any(
                event.event_type == "draft_pr_created"
                for event in self.store.list_task_events(self.task_key)
            )
        )

    def test_created_pr_verification_failure_blocks_recording(self) -> None:
        self._seed_task()
        before_artifacts = len(self.store.list_task_artifacts(self.task_key))
        before_events = len(self.store.list_task_events(self.task_key))
        runner = FakeGhRunner(
            list_stdout="[]\n",
            create_stdout="https://github.com/anderson930420/agent-taskflow/pull/126\n",
            view_stdout=self._gh_view_stdout(
                number=126,
                files=[
                    "README.md",
                    "agent_taskflow/draft_pr_confirm.py",
                    "tests/test_draft_pr_confirm.py",
                ],
                commits=[
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    self.head_sha,
                ],
            ),
        )

        result = confirm_draft_pr(self._request(confirm=True), runner=runner)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "pr_created_verification_failed")
        self.assertFalse(result.evidence["artifact_recorded"])
        self.assertFalse(result.evidence["event_recorded"])
        self.assertFalse(result.evidence["verification_recorded"])
        self.assertTrue(result.safety["pr_created"])
        self.assertFalse(result.safety["draft_pr_verified"])
        self.assertFalse(result.summary["verified"])
        self.assertEqual(before_artifacts, len(self.store.list_task_artifacts(self.task_key)))
        self.assertEqual(before_events, len(self.store.list_task_events(self.task_key)))
        self.assertEqual(
            sorted(result.verification["unexpected_files"]),
            [
                "README.md",
                "agent_taskflow/draft_pr_confirm.py",
                "tests/test_draft_pr_confirm.py",
            ],
        )
        self.assertEqual(result.verification["missing_files"], ["feature.txt"])
        self.assertEqual(
            result.verification["unexpected_commits"],
            ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
        )
        self.assertFalse(result.summary["verified"])
        self.assertIn("GitHub PR files do not match handoff changed_files", result.warnings)
        self.assertIn("GitHub PR commits do not match expected branch diff", result.warnings)

    def test_existing_pr_verification_failure_blocks_duplicate_creation(self) -> None:
        self._seed_task()
        runner = FakeGhRunner(
            list_stdout=json.dumps(
                [
                    {
                        "number": 8,
                        "url": "https://github.com/anderson930420/agent-taskflow/pull/8",
                        "state": "OPEN",
                        "isDraft": True,
                        "title": "Existing stale PR",
                    }
                ]
            )
            + "\n",
            view_stdout=self._gh_view_stdout(
                number=8,
                url="https://github.com/anderson930420/agent-taskflow/pull/8",
                title="AT-DF-CONFIRM-001: Draft PR confirm task",
                files=[
                    "README.md",
                    "agent_taskflow/draft_pr_confirm.py",
                    "tests/test_draft_pr_confirm.py",
                ],
                commits=[
                    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    self.head_sha,
                ],
            ),
        )

        result = confirm_draft_pr(self._request(dry_run=True), runner=runner)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "existing_pr_verification_failed")
        self.assertTrue(result.existing_pr["exists"])
        self.assertFalse(result.evidence["artifact_recorded"])
        self.assertFalse(result.evidence["event_recorded"])
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))
        self.assertFalse(result.summary["verified"])
        self.assertIn("unexpected_files", result.verification)
        self.assertIn("unexpected_commits", result.verification)

    def test_failed_gh_pr_create_does_not_record_draft_pr_evidence(self) -> None:
        self._seed_task()
        before_artifacts = len(self.store.list_task_artifacts(self.task_key))
        before_events = len(self.store.list_task_events(self.task_key))
        runner = FakeGhRunner(
            list_stdout="[]\n",
            create_returncode=1,
            create_stderr="boom\n",
        )

        result = confirm_draft_pr(self._request(confirm=True), runner=runner)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("gh pr create failed", result.error or "")
        self.assertEqual(before_artifacts, len(self.store.list_task_artifacts(self.task_key)))
        self.assertEqual(before_events, len(self.store.list_task_events(self.task_key)))
        self.assertFalse(
            any(
                event.event_type == "draft_pr_created"
                for event in self.store.list_task_events(self.task_key)
            )
        )

    def test_successful_creation_records_draft_pr_artifact_and_event(self) -> None:
        self._seed_task()
        runner = FakeGhRunner(
            list_stdout="[]\n",
            create_stdout="https://github.com/anderson930420/agent-taskflow/pull/124\n",
            view_stdout=self._gh_view_stdout(number=124),
        )

        result = confirm_draft_pr(self._request(confirm=True), runner=runner)

        self.assertTrue(result.evidence["artifact_recorded"])
        self.assertTrue(result.evidence["event_recorded"])
        self.assertTrue(result.evidence["branch_push_verified"])
        self.assertTrue(result.evidence["verification_recorded"])
        self.assertTrue(result.draft_pr["created"])
        self.assertTrue(result.draft_pr["draft"])
        self.assertTrue(result.draft_pr["verified"])
        self.assertEqual(result.draft_pr["number"], 124)
        self.assertTrue(result.draft_pr["url"].endswith("/pull/124"))
        self.assertTrue(result.draft_pr["artifact_path"])
        artifact_path = Path(result.draft_pr["artifact_path"])
        self.assertTrue(artifact_path.is_file())
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["artifact_type"], "draft_pr")
        self.assertEqual(payload["kind"], "draft_pr_created")
        self.assertTrue(payload["branch_push_verified"])
        self.assertTrue(payload["verified"])
        self.assertTrue(payload["verification"]["passed"])
        self.assertFalse(payload["issue_closed"])

    def test_non_waiting_actual_creation_is_blocked_even_with_allow_non_waiting(self) -> None:
        self._seed_task(status="blocked")
        runner = FakeGhRunner()

        result = confirm_draft_pr(
            self._request(confirm=True, allow_non_waiting=True),
            runner=runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("waiting_approval", " ".join(result.warnings))
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))

    def _rewrite_issue_spec_repo(self, source_repo: str) -> None:
        issue_spec_path = self.artifact_root / self.task_key / "issue_spec.md"
        issue_spec_path.write_text(
            render_issue_spec(
                repo=source_repo,
                task_key=self.task_key,
                issue=self._issue_snapshot(),
                ingested_at="2026-05-03T00:00:00Z",
            ),
            encoding="utf-8",
        )

    def test_source_repo_mismatch_blocks_without_override(self) -> None:
        self._seed_task()
        self._rewrite_issue_spec_repo("agent-taskflow/dogfood")
        runner = FakeGhRunner()

        result = confirm_draft_pr(
            self._request(dry_run=True, repo="anderson930420/agent-taskflow"),
            runner=runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("does not match handoff repo", result.error or "")
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))
        self.assertEqual(result.evidence.get("source_repo"), "agent-taskflow/dogfood")
        self.assertEqual(result.evidence.get("target_repo"), "anderson930420/agent-taskflow")
        self.assertFalse(result.evidence.get("source_repo_overridden"))
        self.assertFalse(result.evidence.get("source_repo_mismatch_allowed"))
        self.assertFalse(result.safety.get("source_repo_overridden"))

    def test_source_repo_mismatch_override_dry_run_succeeds(self) -> None:
        self._seed_task()
        self._rewrite_issue_spec_repo("agent-taskflow/dogfood")
        runner = FakeGhRunner(list_stdout="[]\n")

        result = confirm_draft_pr(
            DraftPrConfirmRequest(
                task_key=self.task_key,
                repo="anderson930420/agent-taskflow",
                target_repo="anderson930420/agent-taskflow",
                allow_source_repo_mismatch=True,
                repo_path=self.repo,
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                dry_run=True,
            ),
            runner=runner,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "dry_run")
        self.assertEqual(result.evidence["source_repo"], "agent-taskflow/dogfood")
        self.assertEqual(result.evidence["target_repo"], "anderson930420/agent-taskflow")
        self.assertTrue(result.evidence["source_repo_overridden"])
        self.assertTrue(result.evidence["source_repo_mismatch_allowed"])
        self.assertTrue(result.safety["source_repo_overridden"])
        self.assertTrue(result.safety["source_repo_mismatch_allowed"])
        self.assertTrue(result.safety["human_confirmation_required"])
        self.assertFalse(result.safety["pr_created"])
        self.assertFalse(result.summary["pr_created"])
        self.assertFalse(result.summary["merged"])
        self.assertFalse(result.summary["approved"])
        self.assertFalse(result.summary["cleanup_performed"])
        self.assertIn(
            "Source repo differs from target repo; override explicitly allowed.",
            result.warnings,
        )
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))

    def test_override_without_target_repo_still_blocks(self) -> None:
        self._seed_task()
        self._rewrite_issue_spec_repo("agent-taskflow/dogfood")
        runner = FakeGhRunner()

        result = confirm_draft_pr(
            DraftPrConfirmRequest(
                task_key=self.task_key,
                repo="anderson930420/agent-taskflow",
                target_repo=None,
                allow_source_repo_mismatch=True,
                repo_path=self.repo,
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                dry_run=True,
            ),
            runner=runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("explicit --target-repo", result.error or "")
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))

    def test_override_dry_run_still_requires_confirm_for_creation(self) -> None:
        self._seed_task()
        self._rewrite_issue_spec_repo("agent-taskflow/dogfood")
        runner = FakeGhRunner()

        result = confirm_draft_pr(
            DraftPrConfirmRequest(
                task_key=self.task_key,
                repo="anderson930420/agent-taskflow",
                target_repo="anderson930420/agent-taskflow",
                allow_source_repo_mismatch=True,
                repo_path=self.repo,
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                dry_run=False,
                confirm_draft_pr=False,
            ),
            runner=runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("--confirm-draft-pr", result.error or "")
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))

    def test_target_repo_must_match_repo_when_both_provided(self) -> None:
        self._seed_task()
        runner = FakeGhRunner()

        result = confirm_draft_pr(
            DraftPrConfirmRequest(
                task_key=self.task_key,
                repo="anderson930420/agent-taskflow",
                target_repo="other/repo",
                allow_source_repo_mismatch=True,
                repo_path=self.repo,
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                dry_run=True,
            ),
            runner=runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("does not match repo", result.error or "")
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))

    def test_same_repo_path_still_works_without_override(self) -> None:
        self._seed_task()
        runner = FakeGhRunner(list_stdout="[]\n")

        result = confirm_draft_pr(self._request(dry_run=True), runner=runner)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "dry_run")
        self.assertEqual(result.evidence["target_repo"], "anderson930420/agent-taskflow")
        self.assertEqual(result.evidence["source_repo"], "anderson930420/agent-taskflow")
        self.assertFalse(result.evidence["source_repo_overridden"])
        self.assertFalse(result.evidence["source_repo_mismatch_allowed"])
        self.assertNotIn(
            "Source repo differs from target repo; override explicitly allowed.",
            result.warnings,
        )

    def test_proposed_pr_body_includes_governance_language(self) -> None:
        self._seed_task()
        runner = FakeGhRunner(list_stdout="[]\n")

        result = confirm_draft_pr(self._request(dry_run=True), runner=runner)

        self.assertTrue(result.ok)
        proposed_body = result.handoff.get("proposed_pr_body") or ""
        self.assertIn("no auto-merge", proposed_body)
        self.assertIn("human review required", proposed_body)

    def test_override_confirmed_creation_records_repo_block(self) -> None:
        self._seed_task()
        self._rewrite_issue_spec_repo("agent-taskflow/dogfood")
        runner = FakeGhRunner(
            list_stdout="[]\n",
            create_stdout="https://github.com/anderson930420/agent-taskflow/pull/777\n",
            view_stdout=self._gh_view_stdout(number=777),
        )

        result = confirm_draft_pr(
            DraftPrConfirmRequest(
                task_key=self.task_key,
                repo="anderson930420/agent-taskflow",
                target_repo="anderson930420/agent-taskflow",
                allow_source_repo_mismatch=True,
                repo_path=self.repo,
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                confirm_draft_pr=True,
            ),
            runner=runner,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "draft_pr_created")
        self.assertTrue(result.draft_pr["created"])
        self.assertTrue(result.draft_pr["draft"])
        self.assertFalse(result.summary["merged"])
        self.assertFalse(result.summary["approved"])
        self.assertFalse(result.summary["cleanup_performed"])
        self.assertTrue(result.evidence["source_repo_overridden"])
        self.assertEqual(result.evidence["source_repo"], "agent-taskflow/dogfood")
        self.assertEqual(result.evidence["target_repo"], "anderson930420/agent-taskflow")
        artifact_path = Path(result.draft_pr["artifact_path"])
        self.assertTrue(artifact_path.is_file())
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        self.assertTrue(payload["source_repo_overridden"])
        self.assertEqual(payload["source_repo"], "agent-taskflow/dogfood")
        self.assertEqual(payload["target_repo"], "anderson930420/agent-taskflow")
        self.assertTrue(payload["source_repo_mismatch_allowed"])
        self.assertFalse(payload["merged"])
        self.assertFalse(payload["approved"])
        self.assertFalse(payload["cleanup_performed"])

    def test_request_rejects_non_bool_allow_source_repo_mismatch(self) -> None:
        with self.assertRaises(TypeError):
            DraftPrConfirmRequest(
                task_key=self.task_key,
                repo="anderson930420/agent-taskflow",
                repo_path=self.repo,
                allow_source_repo_mismatch="yes",  # type: ignore[arg-type]
            )

    def test_request_rejects_empty_target_repo(self) -> None:
        with self.assertRaises(ValueError):
            DraftPrConfirmRequest(
                task_key=self.task_key,
                repo="anderson930420/agent-taskflow",
                repo_path=self.repo,
                target_repo="   ",
            )

    def test_static_forbidden_commands_are_absent(self) -> None:
        text = Path(draft_pr_confirm_module.__file__).read_text(encoding="utf-8")
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
            "shell=True",
            "prepare_worktree",
            "dispatch_executor",
            "while True",
            "webhook",
            "cron",
            "polling loop",
        ]
        for token in forbidden:
            self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
