from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_taskflow.github_issue_ingestion import GitHubIssueSnapshot, render_issue_spec
from agent_taskflow.mission_contract import build_mission_contract, write_mission_contract
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.pr_handoff_package import (
    PrHandoffPackageError,
    PrHandoffPackageRequest,
    create_pr_handoff_package,
)
from agent_taskflow.store import TaskMirrorStore


class PrHandoffPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.package_root = self.root / "handoff-package"
        self.worktree = self.root / "worktree"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.base_sha = self._init_repo()
        self._git(["worktree", "add", "-b", "task/AT-HANDOFF-PKG-001", str(self.worktree), "main"])
        (self.worktree / "z-change.txt").write_text("z\n", encoding="utf-8")
        (self.worktree / "a-change.txt").write_text("a\n", encoding="utf-8")
        self._git(["add", "a-change.txt", "z-change.txt"], cwd=self.worktree)
        self._git(["commit", "-m", "update handoff worktree"], cwd=self.worktree)
        self.head_sha = self._git(["rev-parse", "HEAD"], cwd=self.worktree).stdout.strip()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
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
        self.repo.mkdir()
        self._git(["init"])
        self._git(["config", "user.email", "agent-taskflow@example.invalid"])
        self._git(["config", "user.name", "Agent Taskflow"])
        (self.repo / "README.md").write_text("# handoff package test\n", encoding="utf-8")
        self._git(["add", "README.md"])
        self._git(["commit", "-m", "initial"])
        self._git(["branch", "-M", "main"])
        return self._git(["rev-parse", "main"]).stdout.strip()

    def _task_key(self) -> str:
        return "AT-HANDOFF-PKG-001"

    def _issue_snapshot(self) -> GitHubIssueSnapshot:
        return GitHubIssueSnapshot(
            number=1001,
            title="Waiting approval handoff package",
            body="Task body",
            state="open",
            labels=("ready", "review"),
            author="octocat",
            url="https://github.com/anderson930420/agent-taskflow/issues/1001",
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-02T00:00:00Z",
        )

    def _seed_task(
        self,
        *,
        status: str = "waiting_approval",
        with_issue_spec: bool = True,
        with_worktree: bool = True,
        with_executor: bool = True,
        executor_status: str = "completed",
        with_validator: bool = True,
        validator_status: str = "passed",
        with_mission_contract: bool = True,
        with_approval: bool = False,
    ) -> Path:
        task_key = self._task_key()
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Handoff package task",
                status=status,
                repo_path=self.repo,
                artifact_dir=artifact_dir,
            )
        )
        if with_worktree:
            self.store.upsert_task_worktree(
                TaskWorktreeRecord(
                    task_key=task_key,
                    repo_path=self.repo,
                    worktree_path=self.worktree,
                    branch=f"task/{task_key}",
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
        if with_mission_contract:
            contract = build_mission_contract(
                task_key=task_key,
                goal="Create a PR handoff package",
                repo_path=self.repo,
                worktree_path=self.worktree,
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
                status=executor_status,
                exit_code=0 if executor_status in {"completed", "passed"} else 1,
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
                status=validator_status,
                exit_code=0 if validator_status in {"passed", "completed"} else 1,
                summary="validator summary",
                log_path=validator_log,
                artifacts={"log": validator_log},
            )
            self.store.record_task_artifact(task_key, "review_log", validator_log)
        if with_approval:
            self.store.record_approval_decision(
                task_key,
                "accepted",
                decided_by="human",
                notes="Looks good",
            )
        return artifact_dir

    def _request(
        self,
        *,
        dry_run: bool = False,
        allow_non_waiting: bool = False,
        repo_path: Path | None = None,
        artifact_root: Path | None = None,
    ) -> PrHandoffPackageRequest:
        return PrHandoffPackageRequest(
            task_key=self._task_key(),
            repo_path=repo_path or self.repo,
            db_path=self.db_path,
            artifact_root=artifact_root or self.package_root,
            dry_run=dry_run,
            allow_non_waiting=allow_non_waiting,
        )

    def test_missing_task_returns_not_found_result(self) -> None:
        result = create_pr_handoff_package(
            PrHandoffPackageRequest(
                task_key="AT-MISSING",
                repo_path=self.repo,
                db_path=self.db_path,
                artifact_root=self.package_root,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "not_found")
        self.assertIn("Task not found", result.error or "")

    def test_task_not_waiting_is_rejected_by_default(self) -> None:
        self._seed_task(status="blocked")

        result = create_pr_handoff_package(self._request())

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("waiting_approval", result.error or "")

    def test_allow_non_waiting_does_not_mark_package_ready(self) -> None:
        self._seed_task(status="blocked")

        result = create_pr_handoff_package(self._request(allow_non_waiting=True))

        self.assertTrue(result.ok)
        self.assertFalse(result.summary["ready_for_branch_push_review"])
        self.assertFalse(result.summary["ready_for_draft_pr_review"])
        self.assertFalse(result.review_summary["ready_for_human_review"])

    def test_complete_waiting_task_creates_ready_handoff_package(self) -> None:
        self._seed_task(with_approval=True)

        result = create_pr_handoff_package(self._request())

        self.assertTrue(result.ok)
        self.assertTrue(result.summary["ready_for_branch_push_review"])
        self.assertTrue(result.summary["ready_for_draft_pr_review"])
        self.assertTrue(result.review_summary["ready_for_human_review"])
        self.assertTrue(result.git["available"])
        self.assertTrue(result.git["worktree_clean"])
        self.assertEqual(result.git["changed_files"], ["a-change.txt", "z-change.txt"])
        self.assertTrue(result.git["commit_summary"])
        self.assertIn("Handoff package task", result.handoff["proposed_pr_title"])
        self.assertIn("Task: AT-HANDOFF-PKG-001", result.handoff["proposed_pr_body"])

    def test_complete_waiting_task_without_review_evidence_is_still_ready(self) -> None:
        self._seed_task(with_approval=False)

        result = create_pr_handoff_package(self._request())

        self.assertTrue(result.ok)
        self.assertTrue(result.summary["ready_for_branch_push_review"])
        self.assertTrue(result.summary["ready_for_draft_pr_review"])
        self.assertTrue(result.review_summary["ready_for_human_review"])
        self.assertTrue(result.git["available"])
        self.assertTrue(result.git["worktree_clean"])
        self.assertEqual(result.git["changed_files"], ["a-change.txt", "z-change.txt"])
        self.assertFalse(
            any("approval/review evidence" in warning for warning in result.warnings)
        )

    def test_missing_issue_spec_creates_blocking_warning(self) -> None:
        self._seed_task(with_issue_spec=False)

        result = create_pr_handoff_package(self._request())

        self.assertFalse(result.summary["ready_for_branch_push_review"])
        self.assertIn("Issue/spec evidence is missing", " ".join(result.warnings))

    def test_missing_worktree_record_creates_blocking_warning(self) -> None:
        self._seed_task(with_worktree=False)

        result = create_pr_handoff_package(self._request())

        self.assertFalse(result.summary["ready_for_branch_push_review"])
        self.assertIn("Worktree record is missing", " ".join(result.warnings))

    def test_missing_worktree_path_creates_blocking_warning(self) -> None:
        self._seed_task()
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=self._task_key(),
                repo_path=self.repo,
                worktree_path=self.root / "missing-worktree",
                branch=f"task/{self._task_key()}",
                base_branch="main",
                base_sha=self.base_sha,
                status="active",
            )
        )

        result = create_pr_handoff_package(self._request())

        self.assertFalse(result.summary["ready_for_branch_push_review"])
        self.assertIn("Worktree path is missing on disk", " ".join(result.warnings))

    def test_missing_executor_evidence_creates_blocking_warning(self) -> None:
        self._seed_task(with_executor=False)

        result = create_pr_handoff_package(self._request())

        self.assertFalse(result.summary["ready_for_branch_push_review"])
        self.assertIn("No executor run evidence was found", " ".join(result.warnings))

    def test_missing_validator_evidence_creates_blocking_warning(self) -> None:
        self._seed_task(with_validator=False)

        result = create_pr_handoff_package(self._request())

        self.assertFalse(result.summary["ready_for_branch_push_review"])
        self.assertIn("No validator evidence was found", " ".join(result.warnings))

    def test_failed_validator_evidence_creates_blocking_warning(self) -> None:
        self._seed_task(validator_status="failed")

        result = create_pr_handoff_package(self._request())

        self.assertFalse(result.summary["ready_for_branch_push_review"])
        self.assertIn("At least one validator did not pass", " ".join(result.warnings))

    def test_changed_files_are_listed_deterministically(self) -> None:
        self._seed_task()

        result = create_pr_handoff_package(self._request())

        self.assertEqual(result.git["changed_files"], ["a-change.txt", "z-change.txt"])

    def test_clean_worktree_with_committed_readme_diff_reports_readme(self) -> None:
        clean_root = self.root / "clean-readme"
        repo = clean_root / "repo"
        worktree = clean_root / "worktree"
        db_path = clean_root / "state.db"
        artifact_root = clean_root / "artifacts"
        store = TaskMirrorStore(db_path)
        store.init_db()

        def git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
            completed = subprocess.run(
                ["git", *args],
                cwd=cwd or repo,
                shell=False,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if completed.returncode != 0:
                self.fail(f"git {' '.join(args)} failed: {completed.stderr}")
            return completed

        repo.mkdir(parents=True)
        git("init")
        git("config", "user.email", "agent-taskflow@example.invalid")
        git("config", "user.name", "Agent Taskflow")
        (repo / "README.md").write_text("# clean readme test\n", encoding="utf-8")
        git("add", "README.md")
        git("commit", "-m", "initial")
        git("branch", "-M", "main")
        base_sha = git("rev-parse", "main").stdout.strip()
        git("worktree", "add", "-b", "task/AT-HANDOFF-PKG-README", str(worktree), "main")

        task_key = "AT-HANDOFF-PKG-README"
        artifact_dir = artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Readme diff task",
                status="waiting_approval",
                repo_path=repo,
                artifact_dir=artifact_dir,
            )
        )
        store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=task_key,
                repo_path=repo,
                worktree_path=worktree,
                branch="task/AT-HANDOFF-PKG-README",
                base_branch="main",
                base_sha=base_sha,
                status="active",
            )
        )
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
        store.record_task_artifact(task_key, "issue_spec", issue_spec_path)
        contract = build_mission_contract(
            task_key=task_key,
            goal="Report committed README diff",
            repo_path=repo,
            worktree_path=worktree,
            artifact_dir=artifact_dir,
            executor="noop",
            required_validators=("pytest",),
        )
        write_mission_contract(contract, artifact_dir=artifact_dir)
        executor_log = artifact_dir / "executor.log"
        executor_log.write_text("executor log\n", encoding="utf-8")
        run_id = store.create_executor_run(task_key, "noop")
        store.finish_executor_run(
            task_key,
            run_id,
            executor="noop",
            status="completed",
            exit_code=0,
            summary="executor summary",
            log_path=executor_log,
            artifacts={"log": executor_log},
        )
        store.record_task_artifact(task_key, "worker_log", executor_log)
        validator_log = artifact_dir / "pytest.log"
        validator_log.write_text("validator log\n", encoding="utf-8")
        store.record_validation_result(
            task_key,
            "pytest",
            status="passed",
            exit_code=0,
            summary="validator summary",
            log_path=validator_log,
            artifacts={"log": validator_log},
        )
        store.record_task_artifact(task_key, "review_log", validator_log)

        (worktree / "README.md").write_text("# clean readme test\nupdated\n", encoding="utf-8")
        git("add", "README.md", cwd=worktree)
        git("commit", "-m", "update README", cwd=worktree)

        result = create_pr_handoff_package(
            PrHandoffPackageRequest(
                task_key=task_key,
                repo_path=repo,
                db_path=db_path,
                artifact_root=clean_root / "handoff-package",
            )
        )

        self.assertEqual(result.git["changed_files"], ["README.md"])
        self.assertTrue(result.git["worktree_clean"])
        self.assertNotEqual(result.git["diff_summary"], "(clean)")
        self.assertNotIn("EADME.md", result.git["changed_files"])

    def test_proposed_pr_title_and_body_are_deterministic(self) -> None:
        self._seed_task()

        first = create_pr_handoff_package(self._request())
        second = create_pr_handoff_package(self._request())

        self.assertEqual(first.handoff["proposed_pr_title"], second.handoff["proposed_pr_title"])
        self.assertEqual(first.handoff["proposed_pr_body"], second.handoff["proposed_pr_body"])

    def test_dry_run_does_not_write_artifact_or_event(self) -> None:
        self._seed_task()

        result = create_pr_handoff_package(self._request(dry_run=True))

        self.assertFalse(result.artifact_recorded)
        self.assertFalse(result.event_recorded)
        self.assertFalse(
            any(
                artifact.artifact_type == "pr_handoff_package"
                for artifact in self.store.list_task_artifacts(self._task_key())
            )
        )
        self.assertFalse(
            any(
                event.event_type == "pr_handoff_package_created"
                for event in self.store.list_task_events(self._task_key())
            )
        )

    def test_non_dry_run_writes_local_handoff_package_artifact_and_event(self) -> None:
        self._seed_task()

        result = create_pr_handoff_package(self._request())

        self.assertTrue(result.artifact_recorded)
        self.assertTrue(result.event_recorded)
        self.assertTrue(Path(result.package_json_path).is_file())
        self.assertTrue(Path(result.package_markdown_path).is_file())
        artifacts = self.store.list_task_artifacts(self._task_key())
        events = self.store.list_task_events(self._task_key())
        self.assertTrue(
            any(
                artifact.artifact_type == "pr_handoff_package"
                and artifact.path == Path(result.package_json_path)
                for artifact in artifacts
            )
        )
        self.assertTrue(any(event.event_type == "pr_handoff_package_created" for event in events))

    def test_safety_block_is_explicit(self) -> None:
        self._seed_task()

        result = create_pr_handoff_package(self._request())

        self.assertTrue(result.safety["human_review_required"])
        self.assertTrue(result.safety["read_only_git_remote"])
        self.assertFalse(result.safety["task_status_changed"])
        self.assertFalse(result.safety["workspace_prepared"])
        self.assertFalse(result.safety["executor_started"])
        self.assertFalse(result.safety["validators_started"])
        self.assertFalse(result.safety["branch_pushed"])
        self.assertFalse(result.safety["pr_created"])
        self.assertFalse(result.safety["merged"])
        self.assertFalse(result.safety["approved"])
        self.assertFalse(result.safety["cleanup_performed"])
        self.assertFalse(result.safety["branch_deleted"])
        self.assertFalse(result.safety["worktree_deleted"])
        self.assertFalse(result.safety["background_worker_started"])

    def test_no_push_or_pr_helpers_are_called(self) -> None:
        self._seed_task()

        import agent_taskflow.pr_handoff_package as module

        real_run = subprocess.run
        observed: list[list[str]] = []

        def guarded_run(*args, **kwargs):
            cmd = args[0]
            if isinstance(cmd, list):
                observed.append(cmd)
                self.assertEqual(cmd[0], "git")
                self.assertNotIn("push", cmd)
                self.assertNotIn("gh", cmd)
                self.assertNotIn("merge", cmd)
            return real_run(*args, **kwargs)

        with patch.object(module.subprocess, "run", side_effect=guarded_run):
            result = create_pr_handoff_package(self._request(dry_run=True))

        self.assertTrue(result.ok)
        self.assertTrue(all(cmd[0] == "git" for cmd in observed))


if __name__ == "__main__":
    unittest.main()
