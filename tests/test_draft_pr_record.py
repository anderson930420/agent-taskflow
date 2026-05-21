"""Tests for agent_taskflow.draft_pr_record."""

from __future__ import annotations

from dataclasses import dataclass
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.draft_pr_record import (
    DraftPrConfirmError,
    DraftPrRecordRequest,
    record_existing_draft_pr,
)
import agent_taskflow.draft_pr_record as draft_pr_record_module
from agent_taskflow.github_issue_ingestion import GitHubIssueSnapshot, render_issue_spec
from agent_taskflow.mission_contract import build_mission_contract, write_mission_contract
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore


REPO = "anderson930420/agent-taskflow"


@dataclass
class FakeCompletedProcess:
    returncode: int
    stdout: str
    stderr: str = ""


def _compare_stdout_from_view(view_stdout: str) -> str:
    """Build a default ``gh api .../compare/...`` stdout from a view stdout."""

    try:
        view = json.loads(view_stdout)
    except json.JSONDecodeError:
        return json.dumps({"commits": [], "files": [], "status": "identical"})
    files = []
    for item in view.get("files") or []:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            files.append({"filename": item["path"], "status": "modified"})
    commits = []
    for item in view.get("commits") or []:
        if isinstance(item, dict) and isinstance(item.get("oid"), str):
            commits.append({"sha": item["oid"]})
    return json.dumps({"commits": commits, "files": files, "status": "ahead"})


class FakeGhRunner:
    def __init__(
        self,
        *,
        view_stdout: str = "",
        view_returncode: int = 0,
        view_stderr: str = "",
        compare_stdout: str | None = None,
        compare_returncode: int = 0,
        compare_stderr: str = "",
    ) -> None:
        self.view_stdout = view_stdout
        self.view_returncode = view_returncode
        self.view_stderr = view_stderr
        self.compare_stdout = (
            compare_stdout
            if compare_stdout is not None
            else _compare_stdout_from_view(view_stdout)
        )
        self.compare_returncode = compare_returncode
        self.compare_stderr = compare_stderr
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> FakeCompletedProcess:
        self.calls.append({"args": args, "kwargs": kwargs})
        if args[:3] == ["gh", "pr", "view"]:
            return FakeCompletedProcess(
                returncode=self.view_returncode,
                stdout=self.view_stdout,
                stderr=self.view_stderr,
            )
        if (
            len(args) >= 3
            and args[:2] == ["gh", "api"]
            and isinstance(args[2], str)
            and "/compare/" in args[2]
        ):
            return FakeCompletedProcess(
                returncode=self.compare_returncode,
                stdout=self.compare_stdout,
                stderr=self.compare_stderr,
            )
        raise AssertionError(f"unexpected command: {args}")


class DraftPrRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.task_key = "AT-RECORD-001"
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
        (self.repo / "README.md").write_text("# record draft pr test\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")
        base_sha = self._git("rev-parse", "main").stdout.strip()
        self._git("switch", "-c", self.branch)
        (self.repo / "feature.txt").write_text("feature\n", encoding="utf-8")
        self._git("add", "feature.txt")
        self._git("commit", "-m", "feature")
        return base_sha

    def _issue_snapshot(self) -> GitHubIssueSnapshot:
        return GitHubIssueSnapshot(
            number=2001,
            title="Record existing PR task",
            body="Body",
            state="open",
            labels=("ready",),
            author="octocat",
            url="https://github.com/anderson930420/agent-taskflow/issues/2001",
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-02T00:00:00Z",
        )

    def _seed_task(self, *, status: str = "waiting_approval", source_repo: str | None = None) -> Path:
        artifact_dir = self.artifact_root / self.task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=self.task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Record existing PR task",
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
                repo=source_repo or REPO,
                task_key=self.task_key,
                issue=self._issue_snapshot(),
                ingested_at="2026-05-03T00:00:00Z",
            ),
            encoding="utf-8",
        )
        self.store.record_task_artifact(self.task_key, "issue_spec", issue_spec_path)
        contract = build_mission_contract(
            task_key=self.task_key,
            goal="Record existing PR",
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
        # branch push artifact (required by handoff/cleanup chain)
        branch_push_dir = self.artifact_root / "branch_push" / self.task_key
        branch_push_dir.mkdir(parents=True, exist_ok=True)
        branch_push_path = branch_push_dir / "branch_push.json"
        payload = {
            "kind": "branch_push_completed",
            "artifact_type": "branch_push",
            "task_key": self.task_key,
            "branch": self.branch,
            "base_branch": "main",
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "push_performed": True,
            "push_ok": True,
            "branch_pushed": True,
            "pr_created": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "safety": {"branch_pushed": True, "pr_created": False, "merged": False, "force_push": False},
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
        return artifact_dir

    def _gh_view_stdout(
        self,
        *,
        number: int = 29,
        state: str = "MERGED",
        is_draft: bool = False,
        title: str | None = None,
        base: str = "main",
        head: str | None = None,
        head_ref_oid: str | None = None,
        files: list[str] | None = None,
        commits: list[str] | None = None,
        url: str | None = None,
    ) -> str:
        return json.dumps(
            {
                "url": url or f"https://github.com/anderson930420/agent-taskflow/pull/{number}",
                "number": number,
                "headRefName": head or self.branch,
                "headRefOid": head_ref_oid or self.head_sha,
                "baseRefName": base,
                "isDraft": is_draft,
                "title": title or f"{self.task_key}: Record existing PR task",
                "body": "Task: " + self.task_key,
                "state": state,
                "commits": [{"oid": oid} for oid in (commits or [self.head_sha])],
                "files": [{"path": p} for p in (files or ["feature.txt"])],
            }
        )

    def _request(
        self,
        *,
        dry_run: bool = False,
        confirm: bool = False,
        target_repo: str | None = None,
        allow_source_repo_mismatch: bool = False,
        pr_number: int = 29,
    ) -> DraftPrRecordRequest:
        return DraftPrRecordRequest(
            task_key=self.task_key,
            repo=REPO,
            target_repo=target_repo,
            allow_source_repo_mismatch=allow_source_repo_mismatch,
            pr_number=pr_number,
            repo_path=self.repo,
            db_path=self.db_path,
            artifact_root=self.artifact_root,
            dry_run=dry_run,
            confirm_record_existing_pr=confirm,
        )

    def test_dry_run_does_not_write_evidence(self) -> None:
        self._seed_task()
        before_artifacts = len(self.store.list_task_artifacts(self.task_key))
        before_events = len(self.store.list_task_events(self.task_key))
        runner = FakeGhRunner(view_stdout=self._gh_view_stdout())

        result = record_existing_draft_pr(self._request(dry_run=True), runner=runner)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "dry_run")
        self.assertFalse(result.artifact_recorded)
        self.assertFalse(result.event_recorded)
        self.assertTrue(result.verification["passed"])
        self.assertEqual(before_artifacts, len(self.store.list_task_artifacts(self.task_key)))
        self.assertEqual(before_events, len(self.store.list_task_events(self.task_key)))

    def test_missing_confirm_blocks_write(self) -> None:
        self._seed_task()
        runner = FakeGhRunner(view_stdout=self._gh_view_stdout())

        result = record_existing_draft_pr(self._request(confirm=False), runner=runner)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("--confirm-record-existing-pr", result.error or "")

    def test_confirmed_run_writes_artifact_and_event(self) -> None:
        self._seed_task()
        before_artifacts = self.store.list_task_artifacts(self.task_key)
        before_events = self.store.list_task_events(self.task_key)
        runner = FakeGhRunner(view_stdout=self._gh_view_stdout())

        result = record_existing_draft_pr(self._request(confirm=True), runner=runner)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "recorded")
        self.assertTrue(result.artifact_recorded)
        self.assertTrue(result.event_recorded)
        self.assertTrue(result.merged)
        artifact_path = Path(result.artifact_path or "")
        self.assertTrue(artifact_path.is_file())
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["artifact_type"], "draft_pr")
        self.assertEqual(payload["kind"], "draft_pr_created")
        self.assertEqual(payload["pr_number"], 29)
        self.assertEqual(payload["head_sha"], self.head_sha)
        # The on-disk artifact records the draft-creation snapshot
        # (pr_created=True, merged=False, draft=True) so the closeout chain
        # can read it the same way regardless of when it was recorded. The
        # observed live merge state lives in current_state/
        # recorded_post_merge/human_review_external.
        self.assertTrue(payload["pr_created"])
        self.assertFalse(payload["merged"])
        self.assertTrue(payload["draft"])
        self.assertTrue(payload["recorded_post_merge"])
        self.assertEqual(payload["current_state"], "MERGED")
        self.assertTrue(payload["human_review_external"])
        self.assertFalse(payload["approved"])
        self.assertFalse(payload["cleanup_performed"])
        new_artifacts = [
            a for a in self.store.list_task_artifacts(self.task_key) if a not in before_artifacts
        ]
        new_events = [
            e for e in self.store.list_task_events(self.task_key) if e not in before_events
        ]
        self.assertTrue(any(a.artifact_type == "draft_pr" for a in new_artifacts))
        self.assertTrue(any(e.event_type == "draft_pr_created" for e in new_events))

    def test_refuses_closed_not_merged_pr(self) -> None:
        self._seed_task()
        runner = FakeGhRunner(
            view_stdout=self._gh_view_stdout(state="CLOSED", is_draft=False),
        )

        result = record_existing_draft_pr(self._request(dry_run=True), runner=runner)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("CLOSED", result.error or "")

    def test_refuses_base_mismatch(self) -> None:
        self._seed_task()
        runner = FakeGhRunner(
            view_stdout=self._gh_view_stdout(base="develop"),
        )

        result = record_existing_draft_pr(self._request(dry_run=True), runner=runner)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("baseRefName", result.error or "")

    def test_refuses_head_mismatch(self) -> None:
        self._seed_task()
        runner = FakeGhRunner(
            view_stdout=self._gh_view_stdout(head="task/other"),
        )

        result = record_existing_draft_pr(self._request(dry_run=True), runner=runner)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("headRefName", result.error or "")

    def test_refuses_head_oid_mismatch(self) -> None:
        self._seed_task()
        runner = FakeGhRunner(
            view_stdout=self._gh_view_stdout(head_ref_oid="0" * 40),
        )

        result = record_existing_draft_pr(self._request(dry_run=True), runner=runner)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("headRefOid", result.error or "")

    def test_refuses_title_mismatch(self) -> None:
        self._seed_task()
        runner = FakeGhRunner(
            view_stdout=self._gh_view_stdout(title="Different title"),
        )

        result = record_existing_draft_pr(self._request(dry_run=True), runner=runner)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("title", result.error or "")

    def test_refuses_unexpected_compare_files(self) -> None:
        self._seed_task()
        runner = FakeGhRunner(
            view_stdout=self._gh_view_stdout(),
            compare_stdout=json.dumps(
                {
                    "commits": [{"sha": self.head_sha}],
                    "files": [
                        {"filename": "feature.txt", "status": "modified"},
                        {"filename": "unexpected.md", "status": "added"},
                    ],
                }
            ),
        )

        result = record_existing_draft_pr(self._request(dry_run=True), runner=runner)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "verification_failed")
        self.assertIn("files do not match", result.error or "")
        self.assertIn(
            "unexpected.md",
            result.verification.get("unexpected_files", []),
        )

    def test_source_repo_mismatch_blocks_without_override(self) -> None:
        self._seed_task(source_repo="agent-taskflow/dogfood")
        runner = FakeGhRunner(view_stdout=self._gh_view_stdout())

        result = record_existing_draft_pr(self._request(dry_run=True), runner=runner)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("does not match handoff repo", result.error or "")

    def test_source_repo_mismatch_override_dry_run_succeeds(self) -> None:
        self._seed_task(source_repo="agent-taskflow/dogfood")
        runner = FakeGhRunner(view_stdout=self._gh_view_stdout())

        result = record_existing_draft_pr(
            DraftPrRecordRequest(
                task_key=self.task_key,
                repo=REPO,
                target_repo=REPO,
                allow_source_repo_mismatch=True,
                pr_number=29,
                repo_path=self.repo,
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                dry_run=True,
            ),
            runner=runner,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "dry_run")
        self.assertTrue(result.safety["source_repo_overridden"])
        self.assertIn(
            "Source repo differs from target repo; override explicitly allowed.",
            result.warnings,
        )

    def test_does_not_call_gh_pr_create(self) -> None:
        self._seed_task()
        runner = FakeGhRunner(view_stdout=self._gh_view_stdout())

        record_existing_draft_pr(self._request(confirm=True), runner=runner)

        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "create"] for call in runner.calls))
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "merge"] for call in runner.calls))
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "close"] for call in runner.calls))
        self.assertFalse(any(call["args"][:3] == ["gh", "pr", "review"] for call in runner.calls))

    def test_open_draft_pr_is_also_recordable(self) -> None:
        self._seed_task()
        runner = FakeGhRunner(
            view_stdout=self._gh_view_stdout(state="OPEN", is_draft=True),
        )

        result = record_existing_draft_pr(self._request(confirm=True), runner=runner)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "recorded")
        self.assertFalse(result.merged)
        self.assertTrue(result.is_draft)
        payload = json.loads(Path(result.artifact_path or "").read_text(encoding="utf-8"))
        self.assertTrue(payload["pr_created"])
        self.assertFalse(payload["merged"])
        self.assertTrue(payload["draft"])
        self.assertFalse(payload["recorded_post_merge"])
        self.assertEqual(payload["current_state"], "OPEN")
        self.assertTrue(payload["current_is_draft"])

    def test_invalid_pr_number_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DraftPrRecordRequest(
                task_key=self.task_key,
                repo=REPO,
                pr_number=0,
                repo_path=self.repo,
            )

    def test_static_forbidden_commands_are_absent(self) -> None:
        text = Path(draft_pr_record_module.__file__).read_text(encoding="utf-8")
        forbidden = [
            "gh pr create",
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
