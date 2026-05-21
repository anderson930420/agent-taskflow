from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.task_recommendations import (
    SAFETY_FLAGS,
    TaskRecommendationsRequest,
    list_task_recommendations,
)


class TaskRecommendationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def seed_task(
        self,
        task_key: str,
        *,
        status: str,
        title: str = "Recommendation task",
        blocked_reason: str | None = None,
        worktree: bool = False,
        worktree_physical: bool = True,
    ) -> Path:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title=title,
                status=status,
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                blocked_reason=blocked_reason,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )
        if worktree:
            worktree_path = self.repo / ".worktrees" / task_key
            if worktree_physical:
                worktree_path.mkdir(parents=True, exist_ok=True)
            self.store.upsert_task_worktree(
                TaskWorktreeRecord(
                    task_key=task_key,
                    repo_path=self.repo,
                    worktree_path=worktree_path,
                    branch=f"task/{task_key}",
                    base_branch="main",
                    base_sha="base-sha",
                    status="active",
                )
            )
        return artifact_dir

    def recommend_one(self, task_key: str) -> dict[str, object]:
        payload = list_task_recommendations(
            TaskRecommendationsRequest(db_path=self.db_path, task_key=task_key)
        )
        self.assertEqual(payload["count"], 1)
        return payload["items"][0]

    def record_artifact(
        self,
        task_key: str,
        artifact_type: str,
        filename: str,
        payload: dict[str, object] | None = None,
    ) -> Path:
        path = self.artifact_root / task_key / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if payload is not None:
            path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        else:
            path.write_text("{}\n", encoding="utf-8")
        self.store.record_task_artifact(task_key, artifact_type, path)
        return path

    def record_executor_and_validators(self, task_key: str) -> None:
        run_id = self.store.create_executor_run(task_key, "manual")
        self.store.finish_executor_run(
            task_key,
            run_id,
            executor="manual",
            status="completed",
            exit_code=0,
            summary="done",
        )
        self.store.record_validation_result(
            task_key,
            "pytest",
            status="passed",
            exit_code=0,
            summary="passed",
        )

    def record_pr_handoff(self, task_key: str) -> None:
        self.record_artifact(task_key, "pr_handoff_package", "pr_handoff_package.json")
        self.store.record_task_event(
            task_key,
            "pr_handoff_package_created",
            "pr_handoff_package",
            payload={"kind": "pr_handoff_package_created", "task_key": task_key},
        )

    def record_branch_push(self, task_key: str) -> None:
        payload = {
            "kind": "branch_push_completed",
            "artifact_type": "branch_push",
            "task_key": task_key,
            "branch": f"task/{task_key}",
            "base_branch": "main",
            "head_sha": "head-sha",
            "push_ok": True,
            "branch_pushed": True,
            "pr_created": False,
            "merged": False,
            "cleanup_performed": False,
        }
        self.record_artifact(task_key, "branch_push", "branch_push.json", payload)
        self.store.record_task_event(
            task_key,
            "branch_push_completed",
            "branch_push_confirm",
            payload=payload,
        )

    def record_draft_pr(self, task_key: str, *, merged: bool = False) -> None:
        payload = {
            "kind": "draft_pr_created",
            "artifact_type": "draft_pr",
            "task_key": task_key,
            "repo": "anderson930420/agent-taskflow",
            "base_branch": "main",
            "head_branch": f"task/{task_key}",
            "title": "Recommendation task",
            "draft": True,
            "pr_number": 123,
            "pr_url": "https://github.com/anderson930420/agent-taskflow/pull/123",
            "pr_created": True,
            "draft_pr_created": True,
            "verified": True,
            "verification": {"passed": True, "verified": True},
            "merged": False,
            "recorded_post_merge": merged,
            "current_state": "MERGED" if merged else "OPEN",
            "requires_human_confirmation": True,
        }
        self.record_artifact(task_key, "draft_pr", "draft_pr.json", payload)
        self.store.record_task_event(
            task_key,
            "draft_pr_created",
            "draft_pr_confirm",
            payload=payload,
        )

    def record_cleanup(self, task_key: str, *, closeout: bool = False) -> None:
        local_payload = {
            "kind": "local_cleanup_completed",
            "artifact_type": "local_cleanup",
            "task_key": task_key,
            "cleanup_scope": "local",
            "worktree_removed": True,
        }
        self.record_artifact(task_key, "local_cleanup", "local_cleanup.json", local_payload)
        self.store.record_task_event(
            task_key,
            "local_cleanup_completed",
            "local_cleanup_confirm",
            payload=local_payload,
        )
        remote_payload = {
            "kind": "remote_branch_cleanup_completed",
            "artifact_type": "remote_branch_cleanup",
            "task_key": task_key,
            "cleanup_scope": "remote_branch",
            "remote_branch_deleted": True,
        }
        self.record_artifact(
            task_key,
            "remote_branch_cleanup",
            "remote_branch_cleanup.json",
            remote_payload,
        )
        self.store.record_task_event(
            task_key,
            "remote_branch_cleanup_completed",
            "remote_branch_cleanup_confirm",
            payload=remote_payload,
        )
        if closeout:
            closeout_payload = {
                "kind": "task_closeout_completed",
                "artifact_type": "task_closeout",
                "task_key": task_key,
                "cleanup_scope": "task_closeout",
                "task_closeout_performed": True,
            }
            self.record_artifact(task_key, "task_closeout", "task_closeout.json", closeout_payload)
            self.store.record_task_event(
                task_key,
                "task_closeout_completed",
                "task_closeout_confirm",
                payload=closeout_payload,
            )

    def seed_waiting_ready(self, task_key: str) -> None:
        self.seed_task(task_key, status="waiting_approval", worktree=True)
        self.record_executor_and_validators(task_key)

    def test_queued_without_package_recommends_create_task_execution_package(self) -> None:
        self.seed_task("AT-REC-001", status="queued")

        item = self.recommend_one("AT-REC-001")

        self.assertEqual(item["recommended_command_kind"], "create_task_execution_package")
        self.assertEqual(item["current_phase_label"], "queued_needs_package")

    def test_queued_with_package_recommends_queued_task_handoff(self) -> None:
        self.seed_task("AT-REC-002", status="queued")
        self.record_artifact("AT-REC-002", "task_execution_package", "task_execution_package.json")

        item = self.recommend_one("AT-REC-002")

        self.assertEqual(item["recommended_command_kind"], "queued_task_handoff")

    def test_waiting_approval_passed_without_pr_handoff_recommends_pr_handoff_package(self) -> None:
        self.seed_waiting_ready("AT-REC-003")

        item = self.recommend_one("AT-REC-003")

        self.assertEqual(item["recommended_command_kind"], "pr_handoff_package")

    def test_waiting_approval_with_pr_handoff_without_branch_push_recommends_branch_push(self) -> None:
        self.seed_waiting_ready("AT-REC-004")
        self.record_pr_handoff("AT-REC-004")

        item = self.recommend_one("AT-REC-004")

        self.assertEqual(item["recommended_command_kind"], "branch_push_review")

    def test_waiting_approval_with_branch_push_without_draft_pr_recommends_draft_pr(self) -> None:
        self.seed_waiting_ready("AT-REC-005")
        self.record_pr_handoff("AT-REC-005")
        self.record_branch_push("AT-REC-005")

        item = self.recommend_one("AT-REC-005")

        self.assertEqual(item["recommended_command_kind"], "draft_pr_review")

    def test_waiting_approval_verified_draft_pr_not_merged_recommends_human_review(self) -> None:
        self.seed_waiting_ready("AT-REC-006")
        self.record_pr_handoff("AT-REC-006")
        self.record_branch_push("AT-REC-006")
        self.record_draft_pr("AT-REC-006", merged=False)

        item = self.recommend_one("AT-REC-006")

        self.assertEqual(item["recommended_command_kind"], "human_pr_review")
        self.assertFalse(item["pr_status"]["merged"])

    def test_waiting_approval_merged_pr_without_cleanup_recommends_post_merge_cleanup(self) -> None:
        self.seed_waiting_ready("AT-REC-007")
        self.record_pr_handoff("AT-REC-007")
        self.record_branch_push("AT-REC-007")
        self.record_draft_pr("AT-REC-007", merged=True)

        item = self.recommend_one("AT-REC-007")

        self.assertEqual(item["recommended_command_kind"], "post_merge_cleanup_review")
        self.assertTrue(item["pr_status"]["merged"])

    def test_completed_with_cleanup_evidence_recommends_no_action(self) -> None:
        self.seed_task("AT-REC-008", status="completed")
        self.record_cleanup("AT-REC-008", closeout=True)

        item = self.recommend_one("AT-REC-008")

        self.assertEqual(item["recommended_command_kind"], "no_action")
        self.assertFalse(item["required_human_confirmation"])

    def test_blocked_task_recommends_inspect_blocker_and_includes_reason(self) -> None:
        self.seed_task(
            "AT-REC-009",
            status="blocked",
            blocked_reason="validator failed",
        )

        item = self.recommend_one("AT-REC-009")

        self.assertEqual(item["recommended_command_kind"], "inspect_blocker")
        self.assertEqual(item["blocked_reason"], "validator failed")
        self.assertIn("validator failed", item["reason"])

    def test_inconsistent_evidence_recommends_inspect_evidence_not_action(self) -> None:
        self.seed_waiting_ready("AT-REC-010")
        self.record_branch_push("AT-REC-010")

        item = self.recommend_one("AT-REC-010")

        self.assertEqual(item["recommended_command_kind"], "inspect_evidence")
        self.assertIn("out of the expected workflow sequence", item["reason"])
        self.assertFalse(item["required_human_confirmation"])

    def test_safety_flags_are_read_only_and_mutation_flags_false(self) -> None:
        self.seed_task("AT-REC-011", status="queued")

        item = self.recommend_one("AT-REC-011")

        self.assertEqual(item["safety_flags"], SAFETY_FLAGS)
        self.assertTrue(item["safety_flags"]["read_only"])
        for key, value in item["safety_flags"].items():
            if key != "read_only":
                self.assertFalse(value, key)

    def test_output_is_json_serializable(self) -> None:
        self.seed_task("AT-REC-012", status="queued")

        payload = list_task_recommendations(
            TaskRecommendationsRequest(db_path=self.db_path, task_key="AT-REC-012")
        )

        json.dumps(payload, sort_keys=True)

    def test_consistency_warnings_default_empty_when_no_worktree(self) -> None:
        self.seed_task("AT-REC-013", status="queued")

        item = self.recommend_one("AT-REC-013")

        self.assertEqual(item["consistency_warnings"], [])

    def test_completed_stale_active_row_missing_path_keeps_no_action(self) -> None:
        self.seed_task(
            "AT-REC-014",
            status="completed",
            worktree=True,
            worktree_physical=False,
        )
        self.record_cleanup("AT-REC-014", closeout=True)

        before_counts = self._db_counts()
        item = self.recommend_one("AT-REC-014")
        after_counts = self._db_counts()

        self.assertEqual(item["recommended_command_kind"], "no_action")
        self.assertEqual(item["current_phase_label"], "closed_out")
        self.assertEqual(item["safety_flags"], SAFETY_FLAGS)
        self.assertTrue(item["safety_flags"]["read_only"])
        for key, value in item["safety_flags"].items():
            if key != "read_only":
                self.assertFalse(value, key)

        warnings = item["consistency_warnings"]
        self.assertIsInstance(warnings, list)
        self.assertTrue(
            any("physical worktree path is missing" in w for w in warnings),
            warnings,
        )
        self.assertTrue(
            any("still reports active" in w for w in warnings),
            warnings,
        )

        self.assertFalse(item["worktree_status"]["path_exists"])
        self.assertEqual(item["worktree_status"]["status"], "active")
        self.assertIsNone(item["worktree_status"]["cleaned_at"])
        self.assertEqual(before_counts, after_counts)

    def test_waiting_approval_missing_path_no_cleanup_overrides_to_inspect(self) -> None:
        self.seed_task(
            "AT-REC-015",
            status="waiting_approval",
            worktree=True,
            worktree_physical=False,
        )
        self.record_executor_and_validators("AT-REC-015")

        item = self.recommend_one("AT-REC-015")

        self.assertEqual(item["recommended_command_kind"], "inspect_evidence")
        self.assertEqual(item["current_phase_label"], "missing_physical_worktree")
        self.assertIn(
            "physical worktree path is missing",
            item["reason"],
        )
        warnings = item["consistency_warnings"]
        self.assertTrue(
            any(
                "no cleanup evidence is present yet" in w for w in warnings
            ),
            warnings,
        )
        self.assertEqual(item["safety_flags"], SAFETY_FLAGS)

    def test_completed_with_cleanup_and_active_row_present_path_emits_row_warning(
        self,
    ) -> None:
        self.seed_task(
            "AT-REC-016",
            status="completed",
            worktree=True,
            worktree_physical=True,
        )
        self.record_cleanup("AT-REC-016", closeout=True)

        item = self.recommend_one("AT-REC-016")

        self.assertEqual(item["recommended_command_kind"], "no_action")
        warnings = item["consistency_warnings"]
        self.assertTrue(
            any("still reports active" in w for w in warnings),
            warnings,
        )
        self.assertFalse(
            any("physical worktree path is missing" in w for w in warnings),
            warnings,
        )

    def test_warning_count_sums_per_item_warnings(self) -> None:
        self.seed_task(
            "AT-REC-017",
            status="completed",
            worktree=True,
            worktree_physical=False,
        )
        self.record_cleanup("AT-REC-017", closeout=True)

        payload = list_task_recommendations(
            TaskRecommendationsRequest(db_path=self.db_path, task_key="AT-REC-017")
        )

        item = payload["items"][0]
        self.assertEqual(
            payload["summary"]["warning_count"],
            len(item["consistency_warnings"]),
        )
        self.assertGreater(payload["summary"]["warning_count"], 0)

    def test_db_counts_unchanged_after_warning_path(self) -> None:
        self.seed_task(
            "AT-REC-018",
            status="completed",
            worktree=True,
            worktree_physical=False,
        )
        self.record_cleanup("AT-REC-018", closeout=True)

        before = self._db_counts()
        list_task_recommendations(
            TaskRecommendationsRequest(db_path=self.db_path, task_key="AT-REC-018")
        )
        after = self._db_counts()

        self.assertEqual(before, after)

    def _db_counts(self) -> dict[str, int]:
        import sqlite3

        with sqlite3.connect(self.db_path) as conn:
            return {
                "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                "events": conn.execute(
                    "SELECT COUNT(*) FROM task_events"
                ).fetchone()[0],
                "artifacts": conn.execute(
                    "SELECT COUNT(*) FROM task_artifacts"
                ).fetchone()[0],
                "worktrees": conn.execute(
                    "SELECT COUNT(*) FROM task_worktrees"
                ).fetchone()[0],
            }


if __name__ == "__main__":
    unittest.main()
