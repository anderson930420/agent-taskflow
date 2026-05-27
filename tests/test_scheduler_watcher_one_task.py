"""Tests for agent_taskflow.scheduler_watcher_one_task."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.scheduler_watcher_one_task import (
    SchedulerWatcherOneTaskRequest,
    WATCHER_ONE_TASK_SAFETY_FLAGS,
    WATCHER_ONE_TASK_SCHEMA_VERSION,
    WATCHER_ONE_TASK_SOURCE,
    run_scheduler_watcher_one_task,
)
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "agent_taskflow" / "scheduler_watcher_one_task.py"
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "run_scheduler_watcher_one_task_smoke.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "run_scheduler_watcher_one_task_smoke_for_core_tests",
        SMOKE_SCRIPT,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        artifacts = conn.execute("SELECT COUNT(*) FROM task_artifacts").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    return {"artifacts": artifacts, "events": events}


class SchedulerWatcherOneTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.smoke = _load_smoke_module()
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        base_sha, branch = self.smoke._init_repo(
            self.repo_path, self.smoke.ELIGIBLE_TASK_KEY
        )
        self.smoke._seed_queued_task(
            store=self.store,
            task_key=self.smoke.ELIGIBLE_TASK_KEY,
            repo_path=self.repo_path,
            artifact_root=self.artifact_root,
            base_sha=base_sha,
            branch=branch,
        )
        self.smoke._seed_extra_task(
            store=self.store,
            task_key=self.smoke.BLOCKED_TASK_KEY,
            status="blocked",
            title="blocked",
            repo_path=self.repo_path,
            artifact_root=self.artifact_root,
            blocked_reason="waiting on human",
        )
        self.smoke._seed_extra_task(
            store=self.store,
            task_key=self.smoke.WAITING_TASK_KEY,
            status="waiting_approval",
            title="waiting",
            repo_path=self.repo_path,
            artifact_root=self.artifact_root,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _request(self, **overrides: Any) -> SchedulerWatcherOneTaskRequest:
        values: dict[str, Any] = {
            "db_path": self.db_path,
            "artifact_root": self.artifact_root,
        }
        values.update(overrides)
        return SchedulerWatcherOneTaskRequest(**values)

    def test_dry_run_preview_only_no_pipeline_calls(self) -> None:
        runner = self.smoke._FakeApprovedTaskRunner()
        branch = self.smoke._FakeBranchPush()
        draft = self.smoke._FakeDraftPR()
        before = _counts(self.db_path)

        result = run_scheduler_watcher_one_task(
            self._request(dry_run=True),
            approved_task_runner_fn=runner,
            branch_push_fn=branch,
            draft_pr_fn=draft,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(result["preview"]["candidate_count"], 1)
        self.assertEqual(runner.call_count, 0)
        self.assertEqual(branch.call_count, 0)
        self.assertEqual(draft.call_count, 0)
        self.assertEqual(before, _counts(self.db_path))
        self.assertTrue(result["safety"]["dry_run"])
        self.assertTrue(result["safety"]["preview_only"])
        self.assertFalse(result["safety"]["task_to_draft_pr_pipeline_called"])
        self.assertFalse(result["safety"]["approved_task_runner_called"])

    def test_confirmed_requires_selection(self) -> None:
        runner = self.smoke._FakeApprovedTaskRunner()
        branch = self.smoke._FakeBranchPush()
        draft = self.smoke._FakeDraftPR()

        result = run_scheduler_watcher_one_task(
            self._request(
                dry_run=False,
                confirm_run_watcher_one_task=True,
                confirm_run_one_shot_pipeline=True,
                confirm_prepare_pr=True,
                confirm_github_mutations=True,
                confirm_branch_push=True,
                confirm_draft_pr=True,
            ),
            approved_task_runner_fn=runner,
            branch_push_fn=branch,
            draft_pr_fn=draft,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_stage"], "selection")
        self.assertIn("selection_required", result["reasons"])
        self.assertEqual(runner.call_count, 0)
        self.assertEqual(branch.call_count, 0)
        self.assertEqual(draft.call_count, 0)

    def test_confirmed_rejects_ambiguous_selection_mode(self) -> None:
        runner = self.smoke._FakeApprovedTaskRunner()
        branch = self.smoke._FakeBranchPush()
        draft = self.smoke._FakeDraftPR()

        result = run_scheduler_watcher_one_task(
            self._request(
                dry_run=False,
                confirm_run_watcher_one_task=True,
                task_key=self.smoke.ELIGIBLE_TASK_KEY,
                select_first_candidate=True,
                confirm_select_first_candidate=True,
                confirm_run_one_shot_pipeline=True,
                confirm_prepare_pr=True,
                confirm_github_mutations=True,
                confirm_branch_push=True,
                confirm_draft_pr=True,
            ),
            approved_task_runner_fn=runner,
            branch_push_fn=branch,
            draft_pr_fn=draft,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_stage"], "selection")
        self.assertIn("ambiguous_selection_mode", result["reasons"])
        self.assertEqual(runner.call_count, 0)

    def test_first_candidate_requires_extra_confirmation(self) -> None:
        runner = self.smoke._FakeApprovedTaskRunner()
        branch = self.smoke._FakeBranchPush()
        draft = self.smoke._FakeDraftPR()

        result = run_scheduler_watcher_one_task(
            self._request(
                dry_run=False,
                confirm_run_watcher_one_task=True,
                select_first_candidate=True,
                confirm_select_first_candidate=False,
                confirm_run_one_shot_pipeline=True,
                confirm_prepare_pr=True,
                confirm_github_mutations=True,
                confirm_branch_push=True,
                confirm_draft_pr=True,
            ),
            approved_task_runner_fn=runner,
            branch_push_fn=branch,
            draft_pr_fn=draft,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_stage"], "selection")
        self.assertIn(
            "first_candidate_selection_not_confirmed", result["reasons"]
        )
        self.assertEqual(runner.call_count, 0)
        self.assertEqual(branch.call_count, 0)
        self.assertEqual(draft.call_count, 0)

    def test_confirmed_explicit_task_key_runs_one_task(self) -> None:
        runner = self.smoke._FakeApprovedTaskRunner()
        branch = self.smoke._FakeBranchPush()
        draft = self.smoke._FakeDraftPR()

        result = run_scheduler_watcher_one_task(
            self._request(
                dry_run=False,
                confirm_run_watcher_one_task=True,
                task_key=self.smoke.ELIGIBLE_TASK_KEY,
                confirm_run_one_shot_pipeline=True,
                confirm_prepare_pr=True,
                confirm_github_mutations=True,
                confirm_branch_push=True,
                confirm_draft_pr=True,
            ),
            approved_task_runner_fn=runner,
            branch_push_fn=branch,
            draft_pr_fn=draft,
        )

        self.assertTrue(result["ok"], msg=f"result: {result!r}")
        self.assertEqual(result["status"], "completed_one_task")
        self.assertEqual(result["selected_task_key"], self.smoke.ELIGIBLE_TASK_KEY)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(branch.call_count, 1)
        self.assertEqual(draft.call_count, 1)
        self.assertEqual(result["safety"]["processed_task_count"], 1)
        self.assertTrue(result["safety"]["one_task_only"])
        self.assertTrue(result["safety"]["operator_triggered"])
        self.assertTrue(result["safety"]["confirmed_watcher"])
        self.assertTrue(result["safety"]["task_to_draft_pr_pipeline_called"])
        self.assertFalse(result["safety"]["approved"])
        self.assertFalse(result["safety"]["merged"])
        self.assertFalse(result["safety"]["cleanup_performed"])
        self.assertFalse(result["safety"]["scheduler_loop_started"])
        self.assertFalse(result["safety"]["background_worker_started"])
        self.assertFalse(result["safety"]["automatic_task_picking_started"])
        self.assertFalse(result["safety"]["multi_task_batch_started"])

    def test_confirmed_first_candidate_runs_one_task(self) -> None:
        runner = self.smoke._FakeApprovedTaskRunner()
        branch = self.smoke._FakeBranchPush()
        draft = self.smoke._FakeDraftPR()

        result = run_scheduler_watcher_one_task(
            self._request(
                dry_run=False,
                confirm_run_watcher_one_task=True,
                select_first_candidate=True,
                confirm_select_first_candidate=True,
                confirm_run_one_shot_pipeline=True,
                confirm_prepare_pr=True,
                confirm_github_mutations=True,
                confirm_branch_push=True,
                confirm_draft_pr=True,
            ),
            approved_task_runner_fn=runner,
            branch_push_fn=branch,
            draft_pr_fn=draft,
        )

        self.assertTrue(result["ok"], msg=f"result: {result!r}")
        self.assertEqual(result["status"], "completed_one_task")
        self.assertEqual(result["selected_task_key"], self.smoke.ELIGIBLE_TASK_KEY)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(branch.call_count, 1)
        self.assertEqual(draft.call_count, 1)
        self.assertEqual(result["safety"]["processed_task_count"], 1)

    def test_confirmed_first_candidate_rerun_does_not_reselect_completed_task(self) -> None:
        runner = self.smoke._FakeApprovedTaskRunner()
        branch = self.smoke._FakeBranchPush()
        draft = self.smoke._FakeDraftPR()

        first = run_scheduler_watcher_one_task(
            self._request(
                dry_run=False,
                confirm_run_watcher_one_task=True,
                select_first_candidate=True,
                confirm_select_first_candidate=True,
                confirm_run_one_shot_pipeline=True,
                confirm_prepare_pr=True,
                confirm_github_mutations=True,
                confirm_branch_push=True,
                confirm_draft_pr=True,
            ),
            approved_task_runner_fn=runner,
            branch_push_fn=branch,
            draft_pr_fn=draft,
        )
        self.assertTrue(first["ok"], msg=f"first: {first!r}")
        before = _counts(self.db_path)

        second = run_scheduler_watcher_one_task(
            self._request(
                dry_run=False,
                confirm_run_watcher_one_task=True,
                select_first_candidate=True,
                confirm_select_first_candidate=True,
                confirm_run_one_shot_pipeline=True,
                confirm_prepare_pr=True,
                confirm_github_mutations=True,
                confirm_branch_push=True,
                confirm_draft_pr=True,
            ),
            approved_task_runner_fn=runner,
            branch_push_fn=branch,
            draft_pr_fn=draft,
        )

        self.assertFalse(second["ok"], msg=f"second: {second!r}")
        self.assertEqual(second["failed_stage"], "selection")
        self.assertIn("no_eligible_candidates", second["reasons"])
        self.assertIsNone(second["selected_candidate"])
        self.assertNotIn("selected_task_key", second)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(branch.call_count, 1)
        self.assertEqual(draft.call_count, 1)
        self.assertEqual(before, _counts(self.db_path))
        self.assertFalse(second["safety"]["task_to_draft_pr_pipeline_called"])
        self.assertFalse(second["safety"]["approved_task_runner_called"])
        self.assertFalse(second["safety"]["github_mutated"])
        self.assertFalse(second["safety"]["branch_pushed"])
        self.assertFalse(second["safety"]["draft_pr_created"])
        self.assertFalse(second["safety"]["automatic_task_picking_started"])
        self.assertFalse(second["safety"]["multi_task_batch_started"])

    def test_missing_downstream_confirm_flags_fail_before_execution(self) -> None:
        flag_sets = (
            {"confirm_run_one_shot_pipeline": False},
            {"confirm_prepare_pr": False},
            {"confirm_github_mutations": False},
            {"confirm_branch_push": False},
            {"confirm_draft_pr": False},
        )
        base = {
            "dry_run": False,
            "confirm_run_watcher_one_task": True,
            "task_key": self.smoke.ELIGIBLE_TASK_KEY,
            "confirm_run_one_shot_pipeline": True,
            "confirm_prepare_pr": True,
            "confirm_github_mutations": True,
            "confirm_branch_push": True,
            "confirm_draft_pr": True,
        }
        for override in flag_sets:
            with self.subTest(override=override):
                runner = self.smoke._FakeApprovedTaskRunner()
                branch = self.smoke._FakeBranchPush()
                draft = self.smoke._FakeDraftPR()
                kwargs = dict(base)
                kwargs.update(override)
                result = run_scheduler_watcher_one_task(
                    self._request(**kwargs),
                    approved_task_runner_fn=runner,
                    branch_push_fn=branch,
                    draft_pr_fn=draft,
                )
                self.assertFalse(result["ok"])
                self.assertEqual(result["failed_stage"], "confirmation_flags")
                self.assertEqual(runner.call_count, 0)
                self.assertEqual(branch.call_count, 0)
                self.assertEqual(draft.call_count, 0)

    def test_does_not_process_blocked_or_waiting_tasks(self) -> None:
        runner = self.smoke._FakeApprovedTaskRunner()
        branch = self.smoke._FakeBranchPush()
        draft = self.smoke._FakeDraftPR()

        for blocked_key in (
            self.smoke.BLOCKED_TASK_KEY,
            self.smoke.WAITING_TASK_KEY,
        ):
            with self.subTest(blocked_key=blocked_key):
                result = run_scheduler_watcher_one_task(
                    self._request(
                        dry_run=False,
                        confirm_run_watcher_one_task=True,
                        task_key=blocked_key,
                        confirm_run_one_shot_pipeline=True,
                        confirm_prepare_pr=True,
                        confirm_github_mutations=True,
                        confirm_branch_push=True,
                        confirm_draft_pr=True,
                    ),
                    approved_task_runner_fn=runner,
                    branch_push_fn=branch,
                    draft_pr_fn=draft,
                )
                self.assertFalse(result["ok"])
                self.assertEqual(result["failed_stage"], "selection")
                self.assertEqual(runner.call_count, 0)
                self.assertEqual(branch.call_count, 0)
                self.assertEqual(draft.call_count, 0)

    def test_resume_does_not_rerun_already_completed_task_to_draft_pr(self) -> None:
        runner = self.smoke._FakeApprovedTaskRunner()
        branch = self.smoke._FakeBranchPush()
        draft = self.smoke._FakeDraftPR()

        first = run_scheduler_watcher_one_task(
            self._request(
                dry_run=False,
                confirm_run_watcher_one_task=True,
                task_key=self.smoke.ELIGIBLE_TASK_KEY,
                confirm_run_one_shot_pipeline=True,
                confirm_prepare_pr=True,
                confirm_github_mutations=True,
                confirm_branch_push=True,
                confirm_draft_pr=True,
            ),
            approved_task_runner_fn=runner,
            branch_push_fn=branch,
            draft_pr_fn=draft,
        )
        self.assertTrue(first["ok"], msg=f"first: {first!r}")
        before = _counts(self.db_path)

        second = run_scheduler_watcher_one_task(
            self._request(
                dry_run=False,
                confirm_run_watcher_one_task=True,
                task_key=self.smoke.ELIGIBLE_TASK_KEY,
                resume_existing=True,
                resume_pr_preparation=True,
                confirm_run_one_shot_pipeline=True,
                confirm_prepare_pr=True,
                confirm_github_mutations=True,
                confirm_branch_push=True,
                confirm_draft_pr=True,
            ),
            approved_task_runner_fn=runner,
            branch_push_fn=branch,
            draft_pr_fn=draft,
        )

        self.assertTrue(second["ok"], msg=f"second: {second!r}")
        self.assertEqual(second["status"], "completed_one_task")
        self.assertTrue(second["single_use_enforced"])
        self.assertTrue(second["resume_already_processed"])
        self.assertTrue(second["duplicate_trigger_suppressed"])
        self.assertEqual(
            second["selected_candidate"]["reason"], "resume_already_processed"
        )
        self.assertTrue(second["selected_candidate"]["resume_via_skipped_preview"])
        self.assertEqual(
            second["task_to_draft_pr"]["status"], "draft_pr_already_created"
        )
        self.assertTrue(second["task_to_draft_pr"]["single_use_enforced"])
        self.assertTrue(second["task_to_draft_pr"]["resume_already_processed"])
        self.assertTrue(second["task_to_draft_pr"]["duplicate_trigger_suppressed"])
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(branch.call_count, 1)
        self.assertEqual(draft.call_count, 1)
        self.assertEqual(before, _counts(self.db_path))
        self.assertFalse(second["safety"]["approved_task_runner_called"])
        self.assertFalse(second["safety"]["github_mutated"])
        self.assertFalse(second["safety"]["branch_pushed"])
        self.assertFalse(second["safety"]["draft_pr_created"])
        self.assertTrue(second["safety"]["single_use_enforced"])
        self.assertTrue(second["safety"]["resume_already_processed"])
        self.assertTrue(second["safety"]["duplicate_trigger_suppressed"])

    def test_request_validates_and_normalizes(self) -> None:
        with self.assertRaises(ValueError):
            SchedulerWatcherOneTaskRequest(
                db_path=Path("relative.db"),
                artifact_root=Path("/tmp"),
            )
        with self.assertRaises(ValueError):
            SchedulerWatcherOneTaskRequest(
                db_path=Path("/tmp/x.db"),
                artifact_root=Path("relative"),
            )
        with self.assertRaises(ValueError):
            SchedulerWatcherOneTaskRequest(
                db_path=Path("/tmp/x.db"),
                artifact_root=Path("/tmp"),
                limit=-1,
            )
        with self.assertRaises(ValueError):
            SchedulerWatcherOneTaskRequest(
                db_path=Path("/tmp/x.db"),
                artifact_root=Path("/tmp"),
                proposal_max_items=0,
            )
        with self.assertRaises(ValueError):
            SchedulerWatcherOneTaskRequest(
                db_path=Path("/tmp/x.db"),
                artifact_root=Path("/tmp"),
                remote=" ",
            )
        request = SchedulerWatcherOneTaskRequest(
            db_path=Path("/tmp/x.db"),
            artifact_root=Path("/tmp"),
            project=" ",
            status=" queued ",
            recommended_command_kind=" ",
            operator="  op  ",
            operator_note=" ",
            task_key="  at-x  ",
            base_branch=" main ",
        )
        self.assertIsNone(request.project)
        self.assertEqual(request.status, "queued")
        self.assertIsNone(request.recommended_command_kind)
        self.assertEqual(request.operator, "op")
        self.assertIsNone(request.operator_note)
        self.assertEqual(request.task_key, "at-x")
        self.assertEqual(request.base_branch, "main")

    def test_constants_and_safety_defaults(self) -> None:
        self.assertEqual(
            WATCHER_ONE_TASK_SCHEMA_VERSION, "scheduler_watcher_one_task.v1"
        )
        self.assertEqual(WATCHER_ONE_TASK_SOURCE, "scheduler_watcher_one_task")
        self.assertTrue(WATCHER_ONE_TASK_SAFETY_FLAGS["one_task_only"])
        self.assertTrue(WATCHER_ONE_TASK_SAFETY_FLAGS["operator_triggered"])
        self.assertTrue(WATCHER_ONE_TASK_SAFETY_FLAGS["confirmed_watcher"])
        self.assertTrue(WATCHER_ONE_TASK_SAFETY_FLAGS["human_review_required"])
        for key in (
            "task_to_draft_pr_pipeline_called",
            "approved_task_runner_called",
            "github_mutated",
            "branch_pushed",
            "draft_pr_created",
            "approved",
            "merged",
            "cleanup_performed",
            "scheduler_loop_started",
            "background_worker_started",
            "automatic_task_picking_started",
            "multi_task_batch_started",
        ):
            self.assertFalse(WATCHER_ONE_TASK_SAFETY_FLAGS[key], key)

    def test_source_has_no_loop_batch_daemon_merge_cleanup(self) -> None:
        source_lines = [
            line
            for line in MODULE_PATH.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        ]
        code_text = "\n".join(
            line for line in source_lines if not _is_inside_docstring_marker(line)
        )
        source_text = MODULE_PATH.read_text(encoding="utf-8")
        forbidden_code = (
            "while True",
            "schedule.every",
            "asyncio.sleep",
            "threading.Thread",
            "Thread(",
            "subprocess.run",
            "from agent_taskflow.github_issue",
            "ingest_github",
            "discover_github",
            "from agent_taskflow.dispatcher",
            "from agent_taskflow.api",
            "from agent_taskflow.local_cleanup_confirm",
            "from agent_taskflow.remote_branch_cleanup_confirm",
            "from agent_taskflow.task_closeout_confirm",
            "merge_pull_request",
            "record_approval_decision(",
            "delete_worktree",
            "git push",
            "gh pr create",
            "batch_size",
            "for candidate in candidates:",
        )
        for needle in forbidden_code:
            self.assertNotIn(
                needle,
                source_text,
                msg=f"forbidden substring {needle!r} found in {MODULE_PATH}",
            )


def _is_inside_docstring_marker(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith('"""') or stripped.endswith('"""')


if __name__ == "__main__":
    unittest.main()
