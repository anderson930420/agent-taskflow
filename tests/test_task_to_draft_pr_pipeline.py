"""Tests for agent_taskflow.task_to_draft_pr_pipeline."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.task_to_draft_pr_pipeline import (
    TASK_TO_DRAFT_PR_PIPELINE_SAFETY_FLAGS,
    TASK_TO_DRAFT_PR_PIPELINE_SCHEMA_VERSION,
    TASK_TO_DRAFT_PR_PIPELINE_SOURCE,
    TaskToDraftPRPipelineRequest,
    run_task_to_draft_pr_pipeline,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "agent_taskflow" / "task_to_draft_pr_pipeline.py"
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "run_task_to_draft_pr_pipeline_smoke.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "run_task_to_draft_pr_pipeline_smoke_for_core_tests",
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


class _FailingBranchPush:
    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self, **_kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        return {
            "ok": False,
            "status": "failed",
            "reasons": ["fake_branch_push_failed"],
            "summary": {"branch_pushed": False},
        }


class _FailingDraftPR:
    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self, **_kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        return {
            "ok": False,
            "status": "failed",
            "reasons": ["fake_draft_pr_failed"],
            "summary": {"draft_pr_created": False},
        }


class TaskToDraftPRPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.artifact_root = self.root / "artifacts"
        self.task_key = "AT-L7D-CORE-TEST"
        self.smoke = _load_smoke_module()
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        base_sha, branch = self.smoke._init_repo(self.repo_path, self.task_key)
        self.smoke._seed_queued_task(
            store=self.store,
            task_key=self.task_key,
            repo_path=self.repo_path,
            artifact_root=self.artifact_root,
            base_sha=base_sha,
            branch=branch,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _request(self, **overrides: Any) -> TaskToDraftPRPipelineRequest:
        values: dict[str, Any] = {
            "db_path": self.db_path,
            "artifact_root": self.artifact_root,
            "task_key": self.task_key,
        }
        values.update(overrides)
        return TaskToDraftPRPipelineRequest(**values)

    def test_dry_run_writes_nothing_and_no_calls(self) -> None:
        runner = self.smoke._FakeApprovedTaskRunner()
        branch = self.smoke._FakeBranchPush()
        draft = self.smoke._FakeDraftPR()
        before = _counts(self.db_path)

        result = run_task_to_draft_pr_pipeline(
            self._request(dry_run=True),
            approved_task_runner_fn=runner,
            branch_push_fn=branch,
            draft_pr_fn=draft,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "dry_run")
        self.assertTrue(result["would_run_task_to_draft_pr"])
        self.assertEqual(runner.call_count, 0)
        self.assertEqual(branch.call_count, 0)
        self.assertEqual(draft.call_count, 0)
        self.assertEqual(before, _counts(self.db_path))
        self.assertTrue(result["safety"]["dry_run"])
        self.assertFalse(result["safety"]["approved_task_runner_called"])
        self.assertFalse(result["safety"]["github_mutated"])
        self.assertFalse(result["safety"]["branch_pushed"])
        self.assertFalse(result["safety"]["draft_pr_created"])

    def test_confirmed_requires_all_flags(self) -> None:
        flag_sets = (
            {
                "confirm_run_one_shot_pipeline": False,
                "confirm_prepare_pr": True,
                "confirm_github_mutations": True,
                "confirm_branch_push": True,
                "confirm_draft_pr": True,
            },
            {
                "confirm_run_one_shot_pipeline": True,
                "confirm_prepare_pr": False,
                "confirm_github_mutations": True,
                "confirm_branch_push": True,
                "confirm_draft_pr": True,
            },
            {
                "confirm_run_one_shot_pipeline": True,
                "confirm_prepare_pr": True,
                "confirm_github_mutations": False,
                "confirm_branch_push": True,
                "confirm_draft_pr": True,
            },
            {
                "confirm_run_one_shot_pipeline": True,
                "confirm_prepare_pr": True,
                "confirm_github_mutations": True,
                "confirm_branch_push": False,
                "confirm_draft_pr": True,
            },
            {
                "confirm_run_one_shot_pipeline": True,
                "confirm_prepare_pr": True,
                "confirm_github_mutations": True,
                "confirm_branch_push": True,
                "confirm_draft_pr": False,
            },
        )
        for flags in flag_sets:
            with self.subTest(flags=flags):
                runner = self.smoke._FakeApprovedTaskRunner()
                branch = self.smoke._FakeBranchPush()
                draft = self.smoke._FakeDraftPR()
                before = _counts(self.db_path)
                result = run_task_to_draft_pr_pipeline(
                    self._request(dry_run=False, **flags),
                    approved_task_runner_fn=runner,
                    branch_push_fn=branch,
                    draft_pr_fn=draft,
                )
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "failed")
                self.assertIn("requires all confirmations", result["reasons"][0])
                self.assertEqual(runner.call_count, 0)
                self.assertEqual(branch.call_count, 0)
                self.assertEqual(draft.call_count, 0)
                self.assertEqual(before, _counts(self.db_path))

    def test_confirmed_pipeline_runs_one_shot_then_pr_preparation(self) -> None:
        runner = self.smoke._FakeApprovedTaskRunner()
        branch = self.smoke._FakeBranchPush()
        draft = self.smoke._FakeDraftPR()

        result = run_task_to_draft_pr_pipeline(
            self._request(
                dry_run=False,
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
        self.assertEqual(result["status"], "draft_pr_created")
        self.assertEqual(result["final_task_status"], "waiting_approval")
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(branch.call_count, 1)
        self.assertEqual(draft.call_count, 1)
        self.assertEqual(result["stages"]["one_shot"]["status"], "completed")
        self.assertEqual(
            result["stages"]["one_shot"]["runner_status"], "waiting_approval"
        )
        self.assertTrue(result["stages"]["one_shot"]["approved_task_runner_called"])
        self.assertTrue(result["stages"]["pr_preparation"]["branch_pushed"])
        self.assertTrue(result["stages"]["pr_preparation"]["draft_pr_created"])
        self.assertEqual(result["stages"]["pr_preparation"]["pr_number"], 1)
        self.assertTrue(result["safety"]["github_mutated"])
        self.assertFalse(result["safety"]["approved"])
        self.assertFalse(result["safety"]["merged"])
        self.assertFalse(result["safety"]["cleanup_performed"])

    def test_one_shot_failure_stops_before_pr_preparation(self) -> None:
        runner = self.smoke._FakeApprovedTaskRunner()
        branch = self.smoke._FakeBranchPush()
        draft = self.smoke._FakeDraftPR()

        result = run_task_to_draft_pr_pipeline(
            self._request(
                task_key="AT-DOES-NOT-EXIST",
                dry_run=False,
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
        self.assertEqual(result["failed_stage"], "one_shot")
        self.assertIn("task_missing", result["reasons"])
        self.assertEqual(runner.call_count, 0)
        self.assertEqual(branch.call_count, 0)
        self.assertEqual(draft.call_count, 0)
        self.assertFalse(result["safety"]["github_mutated"])

    def test_pr_preparation_failure_returns_failed_stage(self) -> None:
        runner = self.smoke._FakeApprovedTaskRunner()
        branch = self.smoke._FakeBranchPush()
        draft = _FailingDraftPR()

        result = run_task_to_draft_pr_pipeline(
            self._request(
                dry_run=False,
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
        self.assertEqual(result["failed_stage"], "pr_preparation")
        self.assertIn("fake_draft_pr_failed", result["reasons"])
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(branch.call_count, 1)
        self.assertEqual(draft.call_count, 1)
        self.assertTrue(result["safety"]["branch_pushed"])
        self.assertFalse(result["safety"]["draft_pr_created"])
        self.assertFalse(result["safety"]["approved"])
        self.assertFalse(result["safety"]["merged"])
        self.assertFalse(result["safety"]["cleanup_performed"])

    def test_resume_existing_does_not_rerun_runtime_before_pr_preparation(self) -> None:
        first = run_task_to_draft_pr_pipeline(
            self._request(
                dry_run=False,
                confirm_run_one_shot_pipeline=True,
                confirm_prepare_pr=True,
                confirm_github_mutations=True,
                confirm_branch_push=True,
                confirm_draft_pr=True,
            ),
            approved_task_runner_fn=self.smoke._FakeApprovedTaskRunner(),
            branch_push_fn=self.smoke._FakeBranchPush(),
            draft_pr_fn=self.smoke._FakeDraftPR(),
        )
        self.assertTrue(first["ok"], msg=f"first: {first!r}")

        second_runner = self.smoke._FakeApprovedTaskRunner()
        second = run_task_to_draft_pr_pipeline(
            self._request(
                dry_run=False,
                resume_existing=True,
                confirm_run_one_shot_pipeline=True,
                confirm_prepare_pr=True,
                confirm_github_mutations=True,
                confirm_branch_push=True,
                confirm_draft_pr=True,
            ),
            approved_task_runner_fn=second_runner,
            branch_push_fn=_FailingBranchPush(),
            draft_pr_fn=_FailingDraftPR(),
        )

        self.assertFalse(second["ok"])
        self.assertEqual(second["failed_stage"], "pr_preparation")
        self.assertEqual(second_runner.call_count, 0)
        runtime_artifacts = [
            artifact
            for artifact in TaskMirrorStore(self.db_path).list_task_artifacts(
                self.task_key
            )
            if artifact.artifact_type == "runtime_handoff_execution"
        ]
        self.assertEqual(len(runtime_artifacts), 1)

    def test_constants_and_safety_defaults(self) -> None:
        self.assertEqual(
            TASK_TO_DRAFT_PR_PIPELINE_SCHEMA_VERSION,
            "task_to_draft_pr_pipeline.v1",
        )
        self.assertEqual(
            TASK_TO_DRAFT_PR_PIPELINE_SOURCE, "task_to_draft_pr_pipeline"
        )
        self.assertTrue(TASK_TO_DRAFT_PR_PIPELINE_SAFETY_FLAGS["one_task_only"])
        self.assertTrue(TASK_TO_DRAFT_PR_PIPELINE_SAFETY_FLAGS["operator_triggered"])
        self.assertFalse(TASK_TO_DRAFT_PR_PIPELINE_SAFETY_FLAGS["github_mutated"])
        self.assertFalse(TASK_TO_DRAFT_PR_PIPELINE_SAFETY_FLAGS["branch_pushed"])
        self.assertFalse(TASK_TO_DRAFT_PR_PIPELINE_SAFETY_FLAGS["draft_pr_created"])
        self.assertFalse(TASK_TO_DRAFT_PR_PIPELINE_SAFETY_FLAGS["approved"])
        self.assertFalse(TASK_TO_DRAFT_PR_PIPELINE_SAFETY_FLAGS["merged"])
        self.assertFalse(TASK_TO_DRAFT_PR_PIPELINE_SAFETY_FLAGS["cleanup_performed"])
        self.assertTrue(
            TASK_TO_DRAFT_PR_PIPELINE_SAFETY_FLAGS["human_review_required"]
        )

    def test_request_validates_and_normalizes(self) -> None:
        with self.assertRaises(ValueError):
            TaskToDraftPRPipelineRequest(
                db_path=Path("relative.db"),
                artifact_root=Path("/tmp"),
                task_key="AT-X",
            )
        with self.assertRaises(ValueError):
            TaskToDraftPRPipelineRequest(
                db_path=Path("/tmp/x.db"),
                artifact_root=Path("relative"),
                task_key="AT-X",
            )
        with self.assertRaises(ValueError):
            TaskToDraftPRPipelineRequest(
                db_path=Path("/tmp/x.db"),
                artifact_root=Path("/tmp"),
                task_key="AT-X",
                proposal_max_items=0,
            )
        with self.assertRaises(ValueError):
            TaskToDraftPRPipelineRequest(
                db_path=Path("/tmp/x.db"),
                artifact_root=Path("/tmp"),
                task_key="AT-X",
                remote=" ",
            )
        request = TaskToDraftPRPipelineRequest(
            db_path=Path("/tmp/x.db"),
            artifact_root=Path("/tmp"),
            task_key=" at-x ",
            operator=" ",
            operator_note=" note ",
            recommended_command_kind=" ",
            base_branch=" main ",
        )
        self.assertEqual(request.task_key, "at-x")
        self.assertIsNone(request.operator)
        self.assertEqual(request.operator_note, "note")
        self.assertIsNone(request.recommended_command_kind)
        self.assertEqual(request.base_branch, "main")
        self.assertTrue(request.draft)

    def test_source_has_no_loop_auto_pick_ingest_merge_cleanup(self) -> None:
        source_text = MODULE_PATH.read_text(encoding="utf-8")
        forbidden_substrings = (
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
        )
        for needle in forbidden_substrings:
            self.assertNotIn(
                needle,
                source_text,
                msg=f"forbidden substring {needle!r} found in {MODULE_PATH}",
            )


if __name__ == "__main__":
    unittest.main()
