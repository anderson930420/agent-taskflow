"""Tests for agent_taskflow.pr_preparation_pipeline."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.github_issue_ingestion import GitHubIssueSnapshot, render_issue_spec
from agent_taskflow.mission_contract import build_mission_contract, write_mission_contract
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.pr_preparation_pipeline import (
    PRPreparationPipelineError,
    PRPreparationPipelineRequest,
    run_pr_preparation_pipeline,
)
import agent_taskflow.pr_preparation_pipeline as pr_preparation_pipeline_module
from agent_taskflow.runtime_handoff_execution_from_handoff import (
    RUNTIME_EXECUTION_ARTIFACT_TYPE,
    RUNTIME_EXECUTION_SCHEMA_VERSION,
    RUNTIME_EXECUTION_SOURCE,
    RUNTIME_FINISHED_EVENT_TYPE,
)
from agent_taskflow.store import TaskMirrorStore


class _FakeBranchPush:
    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        task_key = str(kwargs["task_key"])
        artifact_root = Path(kwargs["artifact_root"])
        branch = str(kwargs["branch"])
        remote = str(kwargs.get("remote") or "origin")
        artifact_path = artifact_root / "branch_push" / task_key / "branch_push.json"
        payload = {
            "kind": "branch_push_completed",
            "artifact_type": "branch_push",
            "task_key": task_key,
            "remote": remote,
            "branch": branch,
            "base_branch": "main",
            "branch_pushed": True,
            "push_ok": True,
            "pr_created": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "safety": {
                "branch_pushed": True,
                "pr_created": False,
                "merged": False,
                "approved": False,
                "cleanup_performed": False,
                "background_worker_started": False,
            },
        }
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        store = TaskMirrorStore(Path(kwargs["db_path"]))
        store.record_task_artifact(task_key, "branch_push", artifact_path)
        store.record_task_event(
            task_key,
            "branch_push_completed",
            "branch_push_confirm",
            message="Fake branch push completed",
            payload={**payload, "artifact_path": str(artifact_path)},
        )
        return {
            "ok": True,
            "status": "pushed",
            "remote": remote,
            "branch": branch,
            "branch_pushed": True,
            "push_ok": True,
            "branch_push_json_path": str(artifact_path),
            "summary": {"branch_pushed": True},
        }


class _FakeDraftPR:
    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        task_key = str(kwargs["task_key"])
        repo = str(kwargs["repo"])
        artifact_root = Path(kwargs["artifact_root"])
        artifact_path = artifact_root / "draft_pr" / task_key / "draft_pr.json"
        pr_url = f"https://github.com/{repo}/pull/123"
        payload = {
            "kind": "draft_pr_created",
            "artifact_type": "draft_pr",
            "task_key": task_key,
            "repo": repo,
            "draft": True,
            "pr_number": 123,
            "pr_url": pr_url,
            "pr_created": True,
            "draft_pr_created": True,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "safety": {
                "pr_created": True,
                "draft_pr": True,
                "merged": False,
                "approved": False,
                "cleanup_performed": False,
                "background_worker_started": False,
            },
        }
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        store = TaskMirrorStore(Path(kwargs["db_path"]))
        store.record_task_artifact(task_key, "draft_pr", artifact_path)
        store.record_task_event(
            task_key,
            "draft_pr_created",
            "draft_pr_confirm",
            message="Fake draft PR created",
            payload={**payload, "artifact_path": str(artifact_path)},
        )
        return {
            "ok": True,
            "status": "draft_pr_created",
            "draft_pr": {
                "created": True,
                "draft": True,
                "number": 123,
                "url": pr_url,
                "artifact_path": str(artifact_path),
            },
            "summary": {"draft_pr_created": True},
        }


class PRPreparationPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.task_key = "AT-L7C-PIPELINE-001"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.base_sha, self.branch = self._init_repo()
        self._seed_ready_task()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    def _init_repo(self) -> tuple[str, str]:
        self.repo.mkdir(parents=True)
        self._git("init", "-b", "main")
        self._git("config", "user.email", "agent-taskflow@example.invalid")
        self._git("config", "user.name", "Agent Taskflow")
        (self.repo / "README.md").write_text("# l7c pipeline\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")
        base_sha = self._git("rev-parse", "HEAD")
        branch = f"task/{self.task_key}"
        self._git("switch", "-c", branch)
        (self.repo / "feature.txt").write_text("feature\n", encoding="utf-8")
        self._git("add", "feature.txt")
        self._git("commit", "-m", "feature")
        return base_sha, branch

    def _seed_ready_task(self, *, runtime: bool = True, status: str = "waiting_approval") -> None:
        artifact_dir = self.artifact_root / self.task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=self.task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Level 7C pipeline task",
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
        issue = GitHubIssueSnapshot(
            number=701,
            title="Level 7C pipeline task",
            body="Task body",
            state="open",
            labels=("test",),
            author="octocat",
            url="https://github.com/anderson930420/agent-taskflow/issues/701",
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-02T00:00:00Z",
        )
        issue_spec_path = artifact_dir / "issue_spec.md"
        issue_spec_path.write_text(
            render_issue_spec(
                repo="anderson930420/agent-taskflow",
                task_key=self.task_key,
                issue=issue,
                ingested_at="2026-05-03T00:00:00Z",
            ),
            encoding="utf-8",
        )
        self.store.record_task_artifact(self.task_key, "issue_spec", issue_spec_path)
        contract = build_mission_contract(
            task_key=self.task_key,
            goal="Test Level 7C",
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
        if runtime:
            self._seed_runtime_evidence()

    def _seed_runtime_evidence(self) -> None:
        runtime_id = "runtime-test"
        runtime_path = (
            self.artifact_root
            / "runtime_handoff_executions"
            / runtime_id
            / "runtime_handoff_execution.json"
        )
        payload = {
            "schema_version": RUNTIME_EXECUTION_SCHEMA_VERSION,
            "runtime_execution_id": runtime_id,
            "source": RUNTIME_EXECUTION_SOURCE,
            "mode": "confirmed",
            "task_key": self.task_key,
            "artifact_path": str(runtime_path),
            "preflight_passed": True,
            "approved_task_runner_called": True,
            "runner_returned": True,
            "runner_ok": True,
            "runner_status": "waiting_approval",
            "runner_phase": "fake-runtime",
            "not_approval": True,
            "not_merge": True,
            "not_cleanup": True,
        }
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        self.store.record_task_artifact(self.task_key, RUNTIME_EXECUTION_ARTIFACT_TYPE, runtime_path)
        self.store.record_task_event(
            self.task_key,
            RUNTIME_FINISHED_EVENT_TYPE,
            RUNTIME_EXECUTION_SOURCE,
            message="Runtime execution finished",
            payload={
                "kind": RUNTIME_FINISHED_EVENT_TYPE,
                "task_key": self.task_key,
                "runtime_execution_id": runtime_id,
                "runner_ok": True,
                "runner_status": "waiting_approval",
                "runtime_execution_artifact_path": str(runtime_path),
                "approved": False,
                "merged": False,
                "cleanup_performed": False,
            },
        )

    def _request(self, **overrides: Any) -> PRPreparationPipelineRequest:
        values = {
            "db_path": self.db_path,
            "artifact_root": self.artifact_root,
            "task_key": self.task_key,
        }
        values.update(overrides)
        return PRPreparationPipelineRequest(**values)

    def test_dry_run_writes_nothing_and_no_github_mutation(self) -> None:
        branch = _FakeBranchPush()
        draft = _FakeDraftPR()
        before_artifacts = len(self.store.list_task_artifacts(self.task_key))
        before_events = len(self.store.list_task_events(self.task_key))

        result = run_pr_preparation_pipeline(
            self._request(dry_run=True),
            branch_push_fn=branch,
            draft_pr_fn=draft,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "dry_run")
        self.assertTrue(result["would_prepare_pr"])
        self.assertEqual(branch.call_count, 0)
        self.assertEqual(draft.call_count, 0)
        self.assertFalse(result["safety"]["github_mutated"])
        self.assertEqual(before_artifacts, len(self.store.list_task_artifacts(self.task_key)))
        self.assertEqual(before_events, len(self.store.list_task_events(self.task_key)))
        self.assertFalse(any(a.artifact_type == "pr_handoff" for a in self.store.list_task_artifacts(self.task_key)))

    def test_confirmed_requires_all_github_flags(self) -> None:
        flag_sets = (
            {"confirm_prepare_pr": False, "confirm_github_mutations": True, "confirm_branch_push": True, "confirm_draft_pr": True},
            {"confirm_prepare_pr": True, "confirm_github_mutations": False, "confirm_branch_push": True, "confirm_draft_pr": True},
            {"confirm_prepare_pr": True, "confirm_github_mutations": True, "confirm_branch_push": False, "confirm_draft_pr": True},
            {"confirm_prepare_pr": True, "confirm_github_mutations": True, "confirm_branch_push": True, "confirm_draft_pr": False},
        )
        for flags in flag_sets:
            branch = _FakeBranchPush()
            draft = _FakeDraftPR()
            with self.subTest(flags=flags):
                with self.assertRaises(PRPreparationPipelineError):
                    run_pr_preparation_pipeline(
                        self._request(dry_run=False, **flags),
                        branch_push_fn=branch,
                        draft_pr_fn=draft,
                    )
                self.assertEqual(branch.call_count, 0)
                self.assertEqual(draft.call_count, 0)

    def test_confirmed_pipeline_creates_handoff_push_draft_pr_with_fakes(self) -> None:
        branch = _FakeBranchPush()
        draft = _FakeDraftPR()

        result = run_pr_preparation_pipeline(
            self._request(
                dry_run=False,
                confirm_prepare_pr=True,
                confirm_github_mutations=True,
                confirm_branch_push=True,
                confirm_draft_pr=True,
            ),
            branch_push_fn=branch,
            draft_pr_fn=draft,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "draft_pr_created")
        self.assertEqual(branch.call_count, 1)
        self.assertEqual(draft.call_count, 1)
        self.assertEqual(result["stages"]["draft_pr"]["pr_number"], 123)
        self.assertTrue(result["stages"]["draft_pr"]["pr_url"].endswith("/pull/123"))
        artifacts = self.store.list_task_artifacts(self.task_key)
        events = self.store.list_task_events(self.task_key)
        self.assertTrue(any(a.artifact_type == "pr_handoff" for a in artifacts))
        self.assertTrue(any(a.artifact_type == "branch_push" for a in artifacts))
        self.assertTrue(any(a.artifact_type == "draft_pr" for a in artifacts))
        self.assertTrue(any(e.event_type == "pr_handoff_created" for e in events))
        self.assertTrue(any(e.event_type == "branch_push_completed" for e in events))
        self.assertTrue(any(e.event_type == "draft_pr_created" for e in events))

    def test_preflight_requires_waiting_approval(self) -> None:
        self._seed_ready_task(status="queued")
        branch = _FakeBranchPush()
        draft = _FakeDraftPR()

        result = run_pr_preparation_pipeline(
            self._request(dry_run=True),
            branch_push_fn=branch,
            draft_pr_fn=draft,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_stage"], "preflight")
        self.assertIn("task_status_not_waiting_approval: queued", result["reasons"])
        self.assertEqual(branch.call_count, 0)
        self.assertEqual(draft.call_count, 0)

    def test_preflight_requires_runtime_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            other = PRPreparationPipelineTests(methodName="runTest")
            other.tmp = tempfile.TemporaryDirectory(dir=tmp)
            other.root = Path(other.tmp.name)
            other.repo = other.root / "repo"
            other.db_path = other.root / "state.db"
            other.artifact_root = other.root / "artifacts"
            other.task_key = "AT-L7C-NO-RUNTIME"
            other.store = TaskMirrorStore(other.db_path)
            other.store.init_db()
            other.base_sha, other.branch = other._init_repo()
            other._seed_ready_task(runtime=False)
            self.addCleanup(other.tmp.cleanup)
            result = run_pr_preparation_pipeline(
                PRPreparationPipelineRequest(
                    db_path=other.db_path,
                    artifact_root=other.artifact_root,
                    task_key=other.task_key,
                    dry_run=True,
                ),
                branch_push_fn=_FakeBranchPush(),
                draft_pr_fn=_FakeDraftPR(),
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_stage"], "preflight")
        self.assertIn("runtime_handoff_execution_artifact_missing", result["reasons"])
        self.assertIn("runtime_execution_finished_event_missing", result["reasons"])

    def test_no_approval_merge_cleanup_side_effects(self) -> None:
        result = run_pr_preparation_pipeline(
            self._request(
                dry_run=False,
                confirm_prepare_pr=True,
                confirm_github_mutations=True,
                confirm_branch_push=True,
                confirm_draft_pr=True,
            ),
            branch_push_fn=_FakeBranchPush(),
            draft_pr_fn=_FakeDraftPR(),
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["safety"]["approved"])
        self.assertFalse(result["safety"]["merged"])
        self.assertFalse(result["safety"]["cleanup_performed"])
        with sqlite3.connect(self.db_path) as conn:
            payloads = [
                row[0]
                for row in conn.execute(
                    "SELECT payload_json FROM task_events WHERE payload_json IS NOT NULL"
                ).fetchall()
            ]
        text = "\n".join(payloads)
        self.assertNotIn('"approved": true', text)
        self.assertNotIn('"merged": true', text)
        self.assertNotIn('"cleanup_performed": true', text)

    def test_source_has_no_runtime_or_auto_loop_or_merge_cleanup(self) -> None:
        text = Path(pr_preparation_pipeline_module.__file__).read_text(encoding="utf-8")
        forbidden = (
            "from agent_taskflow.approved_task_runner",
            "run_approved_task_runner",
            "from agent_taskflow.executors",
            "from agent_taskflow.validators",
            "while True",
            "threading.Thread",
            "asyncio.sleep",
            "schedule.every",
            "local_cleanup_confirm",
            "remote_branch_cleanup_confirm",
            "task_closeout_confirm",
            "merge_pull_request",
            "record_approval_decision(",
            "update_task_status(",
        )
        for needle in forbidden:
            self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
