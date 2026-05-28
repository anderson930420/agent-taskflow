from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.github_issue_discovery import GitHubIssueDiscoveryIssue
from agent_taskflow.github_issue_ingestion import GitHubIssueSnapshot
from agent_taskflow.github_issue_one_task_automation import (
    GITHUB_ISSUE_ONE_TASK_AUTOMATION_SCHEMA_VERSION,
    GITHUB_ISSUE_ONE_TASK_AUTOMATION_SOURCE,
    GitHubIssueOneTaskAutomationRequest,
    run_github_issue_one_task_automation,
)
from agent_taskflow.mission_contract import build_mission_contract, write_mission_contract
from agent_taskflow.models import TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "run_scheduler_watcher_one_task_smoke.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "run_scheduler_watcher_one_task_smoke_for_github_issue_automation_tests",
        SMOKE_SCRIPT,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def discovery_issue(
    number: int,
    *,
    title: str | None = None,
    state: str = "open",
    labels: tuple[str, ...] = (),
) -> GitHubIssueDiscoveryIssue:
    return GitHubIssueDiscoveryIssue(
        number=number,
        title=title or f"Issue {number}",
        state=state,
        labels=labels,
        url=f"https://github.com/anderson930420/agent-taskflow/issues/{number}",
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-02T00:00:00Z",
    )


def issue_snapshot(
    number: int,
    *,
    title: str | None = None,
    state: str = "open",
    labels: tuple[str, ...] = ("ready",),
) -> GitHubIssueSnapshot:
    return GitHubIssueSnapshot(
        number=number,
        title=title or f"Issue {number}",
        body="Issue body for one-task automation.",
        state=state,
        labels=labels,
        author="octocat",
        url=f"https://github.com/anderson930420/agent-taskflow/issues/{number}",
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-02T00:00:00Z",
    )


class _FakeApprovedTaskRunnerWithWorktree:
    def __init__(
        self,
        *,
        repo_path: Path,
        branch: str,
        base_sha: str,
    ) -> None:
        self.repo_path = repo_path
        self.branch = branch
        self.base_sha = base_sha
        self.call_count = 0

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        db_path = Path(kwargs["db_path"])
        task_key = str(kwargs["task_key"])
        artifact_root = Path(kwargs["artifact_root"])
        store = TaskMirrorStore(db_path)
        task = store.get_task(task_key)
        artifact_dir = task.artifact_dir if task is not None else artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)

        executor_log = artifact_dir / "executor.log"
        executor_log.write_text("executor log\n", encoding="utf-8")
        run_id = store.create_executor_run(task_key, "noop")
        store.finish_executor_run(
            task_key,
            run_id,
            executor="noop",
            status="completed",
            exit_code=0,
            summary="fake executor completed for GitHub Issue automation test",
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
            summary="fake validator passed for GitHub Issue automation test",
            log_path=validator_log,
            artifacts={"log": validator_log},
        )
        store.record_task_artifact(task_key, "review_log", validator_log)
        store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=task_key,
                repo_path=self.repo_path,
                worktree_path=self.repo_path,
                branch=self.branch,
                base_branch="main",
                base_sha=self.base_sha,
                status="active",
            )
        )
        contract = build_mission_contract(
            task_key=task_key,
            goal=f"Run GitHub Issue automation test for {task_key}",
            repo_path=self.repo_path,
            worktree_path=self.repo_path,
            artifact_dir=artifact_dir,
            executor="noop",
            required_validators=("pytest",),
        )
        contract_path = write_mission_contract(contract, artifact_dir=artifact_dir)
        store.record_task_artifact(task_key, "manifest", contract_path)
        store.update_task_status(
            task_key,
            "waiting_approval",
            source="github-issue-one-task-automation-test",
            message="fake approved task runner completed",
        )
        return {
            "ok": True,
            "status": "waiting_approval",
            "phase": "waiting_approval",
            "task_key": task_key,
            "summary": {"final_task_status": "waiting_approval"},
            "safety": {
                "executor_started": False,
                "validators_started": False,
                "github_mutated": False,
                "approved": False,
                "merged": False,
                "cleanup_performed": False,
                "scheduler_loop_started": False,
                "background_worker_started": False,
                "automatic_task_picking_started": False,
            },
        }


class GitHubIssueOneTaskAutomationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.local_repo = self.root / "repo"
        self.local_repo.mkdir()
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()
        self.repo = "anderson930420/agent-taskflow"
        self.smoke = _load_smoke_module()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def request(self, **overrides: Any) -> GitHubIssueOneTaskAutomationRequest:
        values: dict[str, Any] = {
            "repo": self.repo,
            "db_path": self.db_path,
            "local_repo_path": self.local_repo,
            "artifact_root": self.artifact_root,
        }
        values.update(overrides)
        return GitHubIssueOneTaskAutomationRequest(**values)

    def confirmed_request(self, **overrides: Any) -> GitHubIssueOneTaskAutomationRequest:
        values: dict[str, Any] = {
            "dry_run": False,
            "select_first_issue": True,
            "confirm_select_first_issue": True,
            "confirm_ingest_issue": True,
            "confirm_run_watcher_one_task": True,
            "confirm_run_one_shot_pipeline": True,
            "confirm_prepare_pr": True,
            "confirm_github_mutations": True,
            "confirm_branch_push": True,
            "confirm_draft_pr": True,
        }
        values.update(overrides)
        return self.request(**values)

    def init_repo_for_task(self, task_key: str) -> tuple[str, str]:
        self.git("init", "-b", "main")
        self.git("config", "user.email", "agent-taskflow@example.invalid")
        self.git("config", "user.name", "Agent Taskflow")
        (self.local_repo / "README.md").write_text(
            "# GitHub Issue one-task automation\n", encoding="utf-8"
        )
        self.git("add", "README.md")
        self.git("commit", "-m", "initial")
        base_sha = self.git("rev-parse", "HEAD")
        branch = f"task/{task_key}"
        self.git("switch", "-c", branch)
        (self.local_repo / "feature.txt").write_text("feature\n", encoding="utf-8")
        self.git("add", "feature.txt")
        self.git("commit", "-m", "feature")
        return base_sha, branch

    def git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.local_repo,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} failed: {completed.stderr}"
            )
        return completed.stdout.strip()

    def test_first_confirmed_run_ingests_one_issue_and_calls_watcher_once(self) -> None:
        task_key = "AT-GH-501"
        base_sha, branch_name = self.init_repo_for_task(task_key)
        runner = _FakeApprovedTaskRunnerWithWorktree(
            repo_path=self.local_repo,
            branch=branch_name,
            base_sha=base_sha,
        )
        branch_push = self.smoke._FakeBranchPush()
        draft_pr = self.smoke._FakeDraftPR()
        ingestion_calls: list[int] = []

        result = run_github_issue_one_task_automation(
            self.confirmed_request(),
            discovery_fetcher=lambda request: [
                discovery_issue(501, title="Ready issue", labels=("ready",))
            ],
            ingestion_fetcher=lambda repo, issue_number: (
                ingestion_calls.append(issue_number)
                or issue_snapshot(501, title="Ready issue", labels=("ready",))
            ),
            approved_task_runner_fn=runner,
            branch_push_fn=branch_push,
            draft_pr_fn=draft_pr,
        )

        self.assertTrue(result["ok"], msg=f"result: {result!r}")
        self.assertEqual(result["schema_version"], GITHUB_ISSUE_ONE_TASK_AUTOMATION_SCHEMA_VERSION)
        self.assertEqual(result["source"], GITHUB_ISSUE_ONE_TASK_AUTOMATION_SOURCE)
        self.assertEqual(result["status"], "completed_one_task")
        self.assertEqual(result["mode"], "confirmed")
        self.assertEqual(result["selected_issue"]["number"], 501)
        self.assertEqual(result["selected_task_key"], task_key)
        self.assertEqual(result["ingestion"]["status"], "ingested")
        self.assertEqual(ingestion_calls, [501])
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(branch_push.call_count, 1)
        self.assertEqual(draft_pr.call_count, 1)
        safety = result["safety"]
        self.assertTrue(safety["one_issue_only"])
        self.assertTrue(safety["one_task_only"])
        self.assertTrue(safety["discovery_called"])
        self.assertTrue(safety["issue_ingested"])
        self.assertTrue(safety["watcher_called"])
        self.assertTrue(safety["approved_task_runner_called"])
        self.assertTrue(safety["github_mutated"])
        self.assertTrue(safety["branch_pushed"])
        self.assertTrue(safety["draft_pr_created"])
        self.assertFalse(safety["approved"])
        self.assertFalse(safety["merged"])
        self.assertFalse(safety["cleanup_performed"])
        self.assertFalse(safety["scheduler_loop_started"])
        self.assertFalse(safety["background_worker_started"])
        self.assertFalse(safety["multi_task_batch_started"])
        self.assertTrue(safety["human_review_required"])

    def test_second_confirmed_run_same_issue_is_noop_from_select_first_mode(self) -> None:
        task_key = "AT-GH-502"
        base_sha, branch_name = self.init_repo_for_task(task_key)
        runner = _FakeApprovedTaskRunnerWithWorktree(
            repo_path=self.local_repo,
            branch=branch_name,
            base_sha=base_sha,
        )
        branch_push = self.smoke._FakeBranchPush()
        draft_pr = self.smoke._FakeDraftPR()

        first = run_github_issue_one_task_automation(
            self.confirmed_request(),
            discovery_fetcher=lambda request: [
                discovery_issue(502, title="Duplicate issue", labels=("ready",))
            ],
            ingestion_fetcher=lambda repo, issue_number: issue_snapshot(
                502, title="Duplicate issue", labels=("ready",)
            ),
            approved_task_runner_fn=runner,
            branch_push_fn=branch_push,
            draft_pr_fn=draft_pr,
        )
        self.assertTrue(first["ok"], msg=f"first: {first!r}")
        before_counts = _db_counts(self.db_path)

        second = run_github_issue_one_task_automation(
            self.confirmed_request(),
            discovery_fetcher=lambda request: [
                discovery_issue(502, title="Duplicate issue", labels=("ready",))
            ],
            ingestion_fetcher=lambda repo, issue_number: issue_snapshot(502),
            approved_task_runner_fn=runner,
            branch_push_fn=branch_push,
            draft_pr_fn=draft_pr,
        )

        self.assertTrue(second["ok"], msg=f"second: {second!r}")
        self.assertEqual(second["status"], "no_eligible_issues")
        self.assertEqual(second["discovery"]["recommended_candidates"], [])
        self.assertEqual(second["discovery"]["already_ingested"][0]["number"], 502)
        self.assertEqual(second["discovery"]["already_ingested"][0]["task_key"], task_key)
        self.assertIsNone(second["ingestion"])
        self.assertIsNone(second["watcher"])
        self.assertIsNone(second["selected_task_key"])
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(branch_push.call_count, 1)
        self.assertEqual(draft_pr.call_count, 1)
        self.assertEqual(before_counts, _db_counts(self.db_path))
        self.assertFalse(second["safety"]["issue_ingested"])
        self.assertFalse(second["safety"]["watcher_called"])
        self.assertFalse(second["safety"]["approved_task_runner_called"])
        self.assertFalse(second["safety"]["branch_pushed"])
        self.assertFalse(second["safety"]["draft_pr_created"])

    def test_confirmed_no_candidate_path_writes_nothing_and_does_not_call_watcher(self) -> None:
        runner = _FakeApprovedTaskRunnerWithWorktree(
            repo_path=self.local_repo,
            branch="task/unused",
            base_sha="unused",
        )
        branch_push = self.smoke._FakeBranchPush()
        draft_pr = self.smoke._FakeDraftPR()

        result = run_github_issue_one_task_automation(
            self.confirmed_request(),
            discovery_fetcher=lambda request: [],
            ingestion_fetcher=lambda repo, issue_number: issue_snapshot(999),
            approved_task_runner_fn=runner,
            branch_push_fn=branch_push,
            draft_pr_fn=draft_pr,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "no_eligible_issues")
        self.assertIsNone(result["selected_issue"])
        self.assertIsNone(result["ingestion"])
        self.assertIsNone(result["watcher"])
        self.assertFalse(self.db_path.exists())
        self.assertEqual(runner.call_count, 0)
        self.assertEqual(branch_push.call_count, 0)
        self.assertEqual(draft_pr.call_count, 0)
        self.assertFalse(result["safety"]["issue_ingested"])
        self.assertFalse(result["safety"]["watcher_called"])

    def test_dry_run_discovers_and_selects_without_writes_or_watcher(self) -> None:
        def forbidden_ingestion(repo: str, issue_number: int) -> GitHubIssueSnapshot:
            raise AssertionError("dry-run must not call ingestion")

        result = run_github_issue_one_task_automation(
            self.request(
                dry_run=True,
                select_first_issue=True,
                confirm_select_first_issue=True,
            ),
            discovery_fetcher=lambda request: [
                discovery_issue(503, title="Dry run issue", labels=("ready",))
            ],
            ingestion_fetcher=forbidden_ingestion,
            approved_task_runner_fn=lambda **kwargs: {"ok": False},
            branch_push_fn=lambda **kwargs: {"ok": False},
            draft_pr_fn=lambda **kwargs: {"ok": False},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["selected_issue"]["number"], 503)
        self.assertTrue(result["selection"]["would_select_issue"])
        self.assertIsNone(result["ingestion"])
        self.assertIsNone(result["watcher"])
        self.assertIsNone(result["selected_task_key"])
        self.assertFalse(self.db_path.exists())
        safety = result["safety"]
        self.assertTrue(safety["dry_run"])
        self.assertTrue(safety["discovery_called"])
        self.assertFalse(safety["issue_ingested"])
        self.assertFalse(safety["watcher_called"])
        self.assertFalse(safety["approved_task_runner_called"])
        self.assertFalse(safety["github_mutated"])
        self.assertFalse(safety["branch_pushed"])
        self.assertFalse(safety["draft_pr_created"])

    def test_legacy_ingestion_failure_table_is_migrated(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE github_issue_ingestion_failures (
                    repo TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    failure_count INTEGER NOT NULL,
                    first_failed_at TEXT NOT NULL,
                    last_failed_at TEXT NOT NULL,
                    last_error_summary TEXT NOT NULL,
                    PRIMARY KEY(repo, issue_number)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO github_issue_ingestion_failures (
                    repo,
                    issue_number,
                    failure_count,
                    first_failed_at,
                    last_failed_at,
                    last_error_summary
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self.repo,
                    504,
                    1,
                    "2026-05-01T00:00:00Z",
                    "2026-05-01T00:00:00Z",
                    "legacy failure",
                ),
            )

        result = run_github_issue_one_task_automation(
            self.request(
                dry_run=True,
                select_first_issue=True,
                confirm_select_first_issue=True,
            ),
            discovery_fetcher=lambda request: [
                discovery_issue(504, title="Legacy failure issue", labels=("ready",))
            ],
            ingestion_fetcher=lambda repo, issue_number: issue_snapshot(issue_number),
            approved_task_runner_fn=lambda **kwargs: {"ok": False},
            branch_push_fn=lambda **kwargs: {"ok": False},
            draft_pr_fn=lambda **kwargs: {"ok": False},
        )

        self.assertTrue(result["ok"], msg=f"result: {result!r}")
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["selected_issue"]["number"], 504)
        self.assertEqual(
            result["ingestion_failure_registry"]["summary"]["ingestion_failure_count"],
            1,
        )
        self.assertEqual(
            result["ingestion_failure_registry"]["summary"][
                "quarantined_ingestion_failure_count"
            ],
            0,
        )

        with sqlite3.connect(self.db_path) as conn:
            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(github_issue_ingestion_failures)"
                ).fetchall()
            }
        self.assertIn("next_retry_after", columns)
        self.assertIn("quarantined", columns)

    def test_blocked_closed_and_excluded_issues_are_not_selected(self) -> None:
        result = run_github_issue_one_task_automation(
            self.confirmed_request(exclude_labels=("skip",)),
            discovery_fetcher=lambda request: [
                discovery_issue(601, title="Blocked issue", labels=("blocked",)),
                discovery_issue(602, title="Closed issue", state="closed"),
                discovery_issue(603, title="Excluded issue", labels=("skip",)),
            ],
            ingestion_fetcher=lambda repo, issue_number: issue_snapshot(issue_number),
            approved_task_runner_fn=lambda **kwargs: {"ok": False},
            branch_push_fn=lambda **kwargs: {"ok": False},
            draft_pr_fn=lambda **kwargs: {"ok": False},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "no_eligible_issues")
        self.assertEqual(result["discovery"]["recommended_candidates"], [])
        self.assertEqual(
            [item["number"] for item in result["discovery"]["closed_or_blocked"]],
            [601, 602],
        )
        self.assertEqual(
            [item["number"] for item in result["discovery"]["not_eligible"]],
            [603],
        )
        self.assertIsNone(result["selected_issue"])
        self.assertFalse(self.db_path.exists())
        self.assertFalse(result["safety"]["issue_ingested"])
        self.assertFalse(result["safety"]["watcher_called"])


def _db_counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        return {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "artifacts": conn.execute(
                "SELECT COUNT(*) FROM task_artifacts"
            ).fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "worktrees": conn.execute(
                "SELECT COUNT(*) FROM task_worktrees"
            ).fetchone()[0],
        }


if __name__ == "__main__":
    unittest.main()
