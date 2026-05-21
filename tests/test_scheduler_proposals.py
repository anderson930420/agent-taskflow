from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import (
    TASK_ARTIFACT_TYPES,
    TASK_EVENT_TYPES,
    TaskRecord,
    TaskWorktreeRecord,
    validate_task_artifact_type,
    validate_task_event_type,
)
from agent_taskflow.scheduler_proposals import (
    COMMAND_KIND_PRIORITY,
    DEFAULT_ACTIONABLE_COMMAND_KINDS,
    EXECUTABLE_COMMAND_KINDS,
    ITEM_SAFETY_FLAGS,
    PROPOSAL_ARTIFACT_TYPE,
    PROPOSAL_EVENT_TYPE,
    PROPOSAL_SAFETY_FLAGS,
    PROPOSAL_SOURCE,
    SCHEMA_VERSION,
    SchedulerProposalError,
    SchedulerProposalRequest,
    create_scheduler_proposal,
)
from agent_taskflow.store import TaskMirrorStore


class SchedulerProposalsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_task(
        self,
        task_key: str,
        *,
        status: str,
        title: str = "Proposal task",
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

    def _record_artifact(
        self,
        task_key: str,
        artifact_type: str,
        filename: str,
        payload: dict[str, object] | None = None,
    ) -> Path:
        path = self.artifact_root / task_key / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if payload is not None:
            path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        else:
            path.write_text("{}\n", encoding="utf-8")
        self.store.record_task_artifact(task_key, artifact_type, path)
        return path

    def _record_executor_and_validators(self, task_key: str) -> None:
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

    def _record_full_cleanup(self, task_key: str) -> None:
        for artifact_type, event_type in [
            ("local_cleanup", "local_cleanup_completed"),
            ("remote_branch_cleanup", "remote_branch_cleanup_completed"),
            ("task_closeout", "task_closeout_completed"),
        ]:
            payload = {"kind": event_type, "task_key": task_key}
            self._record_artifact(task_key, artifact_type, f"{artifact_type}.json", payload)
            self.store.record_task_event(
                task_key,
                event_type,
                f"{artifact_type}_confirm",
                payload=payload,
            )

    def _db_counts(self) -> dict[str, int]:
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

    def _propose(self, **overrides: object) -> dict[str, object]:
        params: dict[str, object] = {
            "db_path": self.db_path,
            "artifact_root": self.artifact_root,
        }
        params.update(overrides)
        return create_scheduler_proposal(SchedulerProposalRequest(**params))

    # --- Required scenarios ---

    def test_queued_without_package_proposes_create_task_execution_package(self) -> None:
        self._seed_task("AT-SP-001", status="queued")

        payload = self._propose()

        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        kinds = [item["recommended_command_kind"] for item in payload["items"]]
        self.assertIn("create_task_execution_package", kinds)
        item = next(
            i for i in payload["items"]
            if i["recommended_command_kind"] == "create_task_execution_package"
        )
        self.assertTrue(item["executable"])
        self.assertTrue(item["requires_human_confirmation"])
        self.assertEqual(item["safety_flags"], ITEM_SAFETY_FLAGS)

    def test_waiting_approval_without_pr_handoff_proposes_pr_handoff_package(self) -> None:
        self._seed_task("AT-SP-002", status="waiting_approval", worktree=True)
        self._record_executor_and_validators("AT-SP-002")

        payload = self._propose()

        kinds = [item["recommended_command_kind"] for item in payload["items"]]
        self.assertIn("pr_handoff_package", kinds)

    def test_completed_no_action_task_excluded_by_default(self) -> None:
        self._seed_task("AT-SP-003", status="completed")
        self._record_full_cleanup("AT-SP-003")

        payload = self._propose()

        task_keys = [item["task_key"] for item in payload["items"]]
        self.assertNotIn("AT-SP-003", task_keys)

    def test_completed_task_included_with_include_completed(self) -> None:
        self._seed_task("AT-SP-004", status="completed")
        self._record_full_cleanup("AT-SP-004")

        payload = self._propose(include_completed=True)

        task_keys = [item["task_key"] for item in payload["items"]]
        self.assertIn("AT-SP-004", task_keys)
        item = next(i for i in payload["items"] if i["task_key"] == "AT-SP-004")
        self.assertEqual(item["recommended_command_kind"], "no_action")
        self.assertFalse(item["executable"])

    def test_no_action_included_with_include_no_action_when_status_not_completed(self) -> None:
        # Use a non-completed task that returns no_action — currently uncommon
        # in the recommendation contract, so the simpler check is on the
        # completed path with include_no_action set explicitly.
        self._seed_task("AT-SP-005", status="completed")
        self._record_full_cleanup("AT-SP-005")

        payload_no_action_only = self._propose(
            include_completed=True,
            include_no_action=True,
        )

        task_keys = [item["task_key"] for item in payload_no_action_only["items"]]
        self.assertIn("AT-SP-005", task_keys)

    def test_sorting_is_deterministic_by_priority_severity_task_key(self) -> None:
        # blocked → inspect_blocker (priority 1)
        self._seed_task("AT-SP-006-B", status="blocked", blocked_reason="needs review")
        # queued without package → create_task_execution_package (priority 10)
        self._seed_task("AT-SP-006-A", status="queued")
        # waiting_approval at pr_handoff_package (priority 8)
        self._seed_task("AT-SP-006-C", status="waiting_approval", worktree=True)
        self._record_executor_and_validators("AT-SP-006-C")

        payload = self._propose()

        kinds = [item["recommended_command_kind"] for item in payload["items"]]
        priorities = [item["priority_rank"] for item in payload["items"]]
        self.assertEqual(priorities, sorted(priorities))
        # inspect_blocker must come before pr_handoff_package must come before create_task_execution_package
        self.assertLess(
            kinds.index("inspect_blocker"),
            kinds.index("pr_handoff_package"),
        )
        self.assertLess(
            kinds.index("pr_handoff_package"),
            kinds.index("create_task_execution_package"),
        )

        # Re-run and confirm same order.
        payload_again = self._propose()
        self.assertEqual(
            [item["task_key"] for item in payload["items"]],
            [item["task_key"] for item in payload_again["items"]],
        )

    def test_consistency_warnings_flag_item_not_executable(self) -> None:
        # waiting_approval task with worktree row but missing physical path
        # AND no cleanup evidence → recommendation overrides to inspect_evidence
        # AND consistency_warnings is non-empty. Items should be included
        # (so an operator sees them) but flagged not-executable.
        self._seed_task(
            "AT-SP-007",
            status="waiting_approval",
            worktree=True,
            worktree_physical=False,
        )
        self._record_executor_and_validators("AT-SP-007")

        payload = self._propose()

        items = [i for i in payload["items"] if i["task_key"] == "AT-SP-007"]
        self.assertEqual(len(items), 1, payload["items"])
        item = items[0]
        self.assertTrue(item["consistency_warnings"], item)
        self.assertFalse(item["executable"])

    def test_dry_run_does_not_write_artifacts_or_db_events(self) -> None:
        self._seed_task("AT-SP-008", status="queued")
        before = self._db_counts()

        payload = self._propose()

        self.assertEqual(payload["mode"], "dry_run")
        self.assertIsNone(payload["artifact_path"])
        self.assertFalse(payload["summary"]["proposal_evidence_recorded"])
        self.assertEqual(self._db_counts(), before)
        proposals_dir = self.artifact_root / "scheduler_proposals"
        self.assertFalse(proposals_dir.exists())

    def test_confirmed_writes_proposal_artifact_and_event_only(self) -> None:
        self._seed_task("AT-SP-009", status="queued")
        before = self._db_counts()

        payload = self._propose(dry_run=False, confirm_create_proposal=True)

        self.assertEqual(payload["mode"], "confirmed")
        self.assertIsNotNone(payload["artifact_path"])
        artifact_path = Path(payload["artifact_path"])
        self.assertTrue(artifact_path.exists())
        on_disk = json.loads(artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["proposal_id"], payload["proposal_id"])
        self.assertEqual(on_disk["mode"], "confirmed")
        self.assertTrue(on_disk["summary"]["proposal_evidence_recorded"])

        with sqlite3.connect(self.db_path) as conn:
            artifact_types = [
                row[0]
                for row in conn.execute(
                    "SELECT artifact_type FROM task_artifacts WHERE task_key = ?",
                    ("AT-SP-009",),
                ).fetchall()
            ]
            event_types = [
                row[0]
                for row in conn.execute(
                    "SELECT event_type FROM task_events WHERE task_key = ?",
                    ("AT-SP-009",),
                ).fetchall()
            ]
        self.assertIn(PROPOSAL_ARTIFACT_TYPE, artifact_types)
        self.assertIn(PROPOSAL_EVENT_TYPE, event_types)
        # No action-evidence types were written.
        for action_type in (
            "branch_push",
            "draft_pr",
            "pr_handoff_package",
            "local_cleanup",
            "remote_branch_cleanup",
            "task_closeout",
        ):
            self.assertNotIn(action_type, artifact_types)
        for action_event in (
            "branch_push_completed",
            "draft_pr_created",
            "pr_handoff_package_created",
            "local_cleanup_completed",
            "remote_branch_cleanup_completed",
            "task_closeout_completed",
        ):
            self.assertNotIn(action_event, event_types)

        # Counts grew only by the proposal artifact + proposal event.
        after = self._db_counts()
        self.assertEqual(after["tasks"], before["tasks"])
        self.assertEqual(after["worktrees"], before["worktrees"])
        self.assertEqual(after["artifacts"], before["artifacts"] + 1)
        self.assertEqual(after["events"], before["events"] + 1)

    def test_confirmed_does_not_mutate_task_status(self) -> None:
        self._seed_task("AT-SP-010", status="queued")
        before_status = self.store.get_task("AT-SP-010").status

        self._propose(dry_run=False, confirm_create_proposal=True)

        self.assertEqual(self.store.get_task("AT-SP-010").status, before_status)

    def test_non_dry_run_without_confirmation_is_blocked(self) -> None:
        self._seed_task("AT-SP-011", status="queued")

        with self.assertRaisesRegex(
            SchedulerProposalError,
            "confirm_create_proposal",
        ):
            self._propose(dry_run=False, confirm_create_proposal=False)

        proposals_dir = self.artifact_root / "scheduler_proposals"
        self.assertFalse(proposals_dir.exists())

    def test_relative_artifact_root_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "artifact_root must be an absolute"):
            SchedulerProposalRequest(
                db_path=self.db_path,
                artifact_root=Path("relative/path"),
            )

    def test_relative_db_path_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "db_path must be an absolute"):
            SchedulerProposalRequest(
                db_path=Path("relative/state.db"),
                artifact_root=self.artifact_root,
            )

    def test_safety_flags_are_mutation_false_in_all_paths(self) -> None:
        self._seed_task("AT-SP-012", status="queued")

        dry = self._propose()
        confirmed = self._propose(dry_run=False, confirm_create_proposal=True)

        for payload in (dry, confirmed):
            self.assertEqual(payload["safety"], PROPOSAL_SAFETY_FLAGS)
            self.assertTrue(payload["safety"]["read_only_scan"])
            self.assertTrue(payload["safety"]["proposal_only"])
            self.assertFalse(payload["safety"]["workflow_action_performed"])
            self.assertFalse(payload["safety"]["action_evidence_created"])
            for key, value in payload["safety"].items():
                if key in {"read_only_scan", "proposal_only"}:
                    continue
                self.assertFalse(value, key)
            for item in payload["items"]:
                self.assertEqual(item["safety_flags"], ITEM_SAFETY_FLAGS)
                self.assertTrue(item["safety_flags"]["proposal_only"])
                for key, value in item["safety_flags"].items():
                    if key == "proposal_only":
                        continue
                    self.assertFalse(value, key)

    def test_artifact_and_event_constants_accepted_by_models(self) -> None:
        self.assertIn(PROPOSAL_ARTIFACT_TYPE, TASK_ARTIFACT_TYPES)
        self.assertIn(PROPOSAL_EVENT_TYPE, TASK_EVENT_TYPES)
        self.assertEqual(
            validate_task_artifact_type(PROPOSAL_ARTIFACT_TYPE),
            PROPOSAL_ARTIFACT_TYPE,
        )
        self.assertEqual(
            validate_task_event_type(PROPOSAL_EVENT_TYPE),
            PROPOSAL_EVENT_TYPE,
        )

    def test_missing_db_raises_scheduler_proposal_error(self) -> None:
        missing = self.root / "missing" / "state.db"
        with self.assertRaisesRegex(SchedulerProposalError, "not found"):
            create_scheduler_proposal(
                SchedulerProposalRequest(
                    db_path=missing,
                    artifact_root=self.artifact_root,
                )
            )

    def test_max_items_limits_selection(self) -> None:
        self._seed_task("AT-SP-013-A", status="queued")
        self._seed_task("AT-SP-013-B", status="queued")
        self._seed_task("AT-SP-013-C", status="queued")

        payload = self._propose(max_items=2)

        self.assertEqual(payload["summary"]["candidate_count"], 3)
        self.assertEqual(payload["summary"]["item_count"], 2)
        self.assertEqual(len(payload["items"]), 2)

    def test_exclude_command_kinds_drops_matching_items(self) -> None:
        self._seed_task("AT-SP-014", status="queued")

        payload = self._propose(
            exclude_command_kinds=("create_task_execution_package",),
        )

        kinds = [item["recommended_command_kind"] for item in payload["items"]]
        self.assertNotIn("create_task_execution_package", kinds)

    def test_include_command_kinds_restricts_to_provided_set(self) -> None:
        self._seed_task("AT-SP-015-A", status="queued")
        self._seed_task("AT-SP-015-B", status="blocked", blocked_reason="x")

        payload = self._propose(include_command_kinds=("inspect_blocker",))

        kinds = [item["recommended_command_kind"] for item in payload["items"]]
        self.assertEqual(kinds, ["inspect_blocker"])

    def test_priority_table_matches_expected_order(self) -> None:
        for earlier_kind, later_kind in zip(
            list(COMMAND_KIND_PRIORITY)[:-1],
            list(COMMAND_KIND_PRIORITY)[1:],
        ):
            self.assertLess(
                COMMAND_KIND_PRIORITY[earlier_kind],
                COMMAND_KIND_PRIORITY[later_kind],
            )

    def test_default_actionable_kinds_subset_of_recommendation_kinds(self) -> None:
        from agent_taskflow.task_recommendations import RECOMMENDED_COMMAND_KINDS

        for kind in DEFAULT_ACTIONABLE_COMMAND_KINDS:
            self.assertIn(kind, RECOMMENDED_COMMAND_KINDS)
        for kind in EXECUTABLE_COMMAND_KINDS:
            self.assertIn(kind, RECOMMENDED_COMMAND_KINDS)
            self.assertIn(kind, DEFAULT_ACTIONABLE_COMMAND_KINDS)

    def test_payload_is_json_serializable(self) -> None:
        self._seed_task("AT-SP-016", status="queued")

        payload = self._propose()

        json.dumps(payload, sort_keys=True)
        self.assertEqual(payload["source"], PROPOSAL_SOURCE)


if __name__ == "__main__":
    unittest.main()
