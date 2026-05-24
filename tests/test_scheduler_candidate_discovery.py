"""Tests for the read-only scheduler candidate discovery module (Phase G)."""

from __future__ import annotations

import importlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_taskflow.models import TaskRecord
from agent_taskflow.scheduler_candidate_discovery import (
    ACTIONABLE_CANDIDATE_KINDS,
    CANDIDATE_SAFETY_FLAGS,
    DISCOVERY_NOTE,
    DISCOVERY_SAFETY_FLAGS,
    NOT_READY_KINDS,
    NO_ACTION_KINDS,
    SCHEMA_VERSION,
    SchedulerCandidateDiscoveryError,
    SchedulerCandidateDiscoveryRequest,
    discover_scheduler_candidates,
    list_scheduler_candidates,
)
from agent_taskflow.store import TaskMirrorStore


class _DiscoveryTestBase(unittest.TestCase):
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
        project: str = "agent-taskflow",
        title: str = "Discovery task",
    ) -> Path:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project=project,
                board=project,
                title=title,
                status=status,
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )
        return artifact_dir

    def seed_queued_with_package(
        self, task_key: str, *, project: str = "agent-taskflow"
    ) -> Path:
        artifact_dir = self.seed_task(task_key, status="queued", project=project)
        package_path = artifact_dir / "task_execution_package.json"
        package_path.write_text("{}\n", encoding="utf-8")
        self.store.record_task_artifact(
            task_key, "task_execution_package", package_path
        )
        return package_path

    def db_counts(self) -> dict[str, int]:
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

    def discover(self, **kwargs: object) -> dict[str, object]:
        request = SchedulerCandidateDiscoveryRequest(db_path=self.db_path, **kwargs)
        return discover_scheduler_candidates(request)


class SchedulerCandidateDiscoveryTests(_DiscoveryTestBase):
    def test_queued_task_appears_as_actionable_candidate(self) -> None:
        self.seed_task("AT-DISC-001", status="queued")

        payload = self.discover()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "read_only")
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["candidate_count"], 1)
        candidate = payload["candidates"][0]
        self.assertEqual(candidate["task_key"], "AT-DISC-001")
        self.assertEqual(candidate["status"], "queued")
        self.assertEqual(
            candidate["recommended_command_kind"], "create_task_execution_package"
        )
        self.assertEqual(candidate["current_phase_label"], "queued_needs_package")
        self.assertTrue(candidate["candidate_ready"])
        self.assertEqual(candidate["required_next_gate"], "scheduler_proposal")
        self.assertEqual(
            candidate["required_operator_action"], "create_scheduler_proposal"
        )

    def test_queued_with_package_recommends_queued_task_handoff(self) -> None:
        self.seed_queued_with_package("AT-DISC-002")

        payload = self.discover()

        self.assertEqual(payload["candidate_count"], 1)
        candidate = payload["candidates"][0]
        self.assertEqual(candidate["recommended_command_kind"], "queued_task_handoff")
        self.assertEqual(
            candidate["required_next_gate"],
            "scheduler_proposal_then_confirmation_then_verifier_then_handoff",
        )
        self.assertTrue(candidate["candidate_ready"])

    def test_candidate_includes_required_fields(self) -> None:
        self.seed_task("AT-DISC-003", status="queued")

        payload = self.discover()
        candidate = payload["candidates"][0]

        for field in (
            "task_key",
            "project",
            "status",
            "current_phase_label",
            "recommended_command_kind",
            "candidate_ready",
            "required_next_gate",
            "required_operator_action",
            "missing_evidence",
            "consistency_warnings",
            "related_artifacts",
            "reason",
            "safety",
        ):
            self.assertIn(field, candidate, field)

    def test_candidate_safety_block_is_locked_down(self) -> None:
        self.seed_task("AT-DISC-004", status="queued")

        payload = self.discover()
        safety = payload["candidates"][0]["safety"]

        self.assertTrue(safety["read_only"])
        for flag in (
            "proposal_created",
            "confirmation_created",
            "handoff_created",
            "runtime_started",
            "approved_task_runner_called",
            "github_mutated",
            "approved",
            "merged",
            "cleanup_performed",
            "background_worker_started",
        ):
            self.assertFalse(safety[flag], flag)
        self.assertEqual(safety, dict(CANDIDATE_SAFETY_FLAGS))

    def test_top_level_safety_block_is_locked_down(self) -> None:
        self.seed_task("AT-DISC-005", status="queued")

        payload = self.discover()
        safety = payload["safety"]

        self.assertTrue(safety["read_only"])
        for flag in (
            "db_written",
            "artifact_written",
            "proposal_created",
            "confirmation_created",
            "handoff_created",
            "verifier_report_created",
            "runtime_started",
            "approved_task_runner_called",
            "github_mutated",
            "approved",
            "merged",
            "cleanup_performed",
            "background_worker_started",
            "task_status_changed",
            "scheduler_loop_started",
        ):
            self.assertFalse(safety[flag], flag)
        self.assertEqual(safety, dict(DISCOVERY_SAFETY_FLAGS))

    def test_discovery_note_states_not_execution_permission(self) -> None:
        self.seed_task("AT-DISC-NOTE", status="queued")

        payload = self.discover()

        self.assertEqual(payload["discovery_note"], DISCOVERY_NOTE)
        self.assertIn("not execution permission", payload["discovery_note"].lower())

    def test_no_db_writes_during_discovery(self) -> None:
        self.seed_task("AT-DISC-006", status="queued")
        before = self.db_counts()

        self.discover()

        self.assertEqual(self.db_counts(), before)

    def test_no_task_events_added_during_discovery(self) -> None:
        self.seed_task("AT-DISC-EVT", status="queued")
        with sqlite3.connect(self.db_path) as conn:
            before = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]

        self.discover()

        with sqlite3.connect(self.db_path) as conn:
            after = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        self.assertEqual(after, before)

    def test_no_task_artifacts_added_during_discovery(self) -> None:
        self.seed_queued_with_package("AT-DISC-ART")
        with sqlite3.connect(self.db_path) as conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM task_artifacts"
            ).fetchone()[0]

        self.discover()

        with sqlite3.connect(self.db_path) as conn:
            after = conn.execute(
                "SELECT COUNT(*) FROM task_artifacts"
            ).fetchone()[0]
        self.assertEqual(after, before)

    def test_task_status_is_not_mutated(self) -> None:
        self.seed_task("AT-DISC-007", status="queued")
        before_status = self.store.get_task("AT-DISC-007").status

        self.discover()

        self.assertEqual(self.store.get_task("AT-DISC-007").status, before_status)

    def test_module_does_not_import_approved_task_runner(self) -> None:
        module = importlib.import_module(
            "agent_taskflow.scheduler_candidate_discovery"
        )
        self.assertFalse(hasattr(module, "approved_task_runner"))
        self.assertFalse(hasattr(module, "queued_task_handoff"))
        self.assertFalse(hasattr(module, "intake_runner_handoff"))
        self.assertFalse(hasattr(module, "create_scheduler_proposal"))
        self.assertFalse(hasattr(module, "create_scheduler_confirmation"))

    def test_module_source_does_not_reference_runner_or_mutation_surfaces(
        self,
    ) -> None:
        source = Path(
            "agent_taskflow/scheduler_candidate_discovery.py"
        ).read_text(encoding="utf-8")
        forbidden_imports = (
            "from agent_taskflow.approved_task_runner",
            "from agent_taskflow.queued_task_handoff",
            "from agent_taskflow.intake_runner_handoff",
            "from agent_taskflow.scheduler_proposals",
            "from agent_taskflow.scheduler_confirmations",
            "from agent_taskflow.scheduler_confirmation_verifier",
            "from agent_taskflow.executors",
            "from agent_taskflow.dispatcher",
        )
        for line in forbidden_imports:
            self.assertNotIn(line, source, line)

    def test_no_runtime_audit_events_added(self) -> None:
        self.seed_queued_with_package("AT-DISC-RUNTIME")
        runtime_events = (
            "runtime_preflight_finished",
            "runtime_execution_started",
            "runtime_execution_finished",
        )
        with sqlite3.connect(self.db_path) as conn:
            placeholders = ",".join("?" for _ in runtime_events)
            before = conn.execute(
                f"SELECT COUNT(*) FROM task_events WHERE event_type IN ({placeholders})",
                runtime_events,
            ).fetchone()[0]

        self.discover()

        with sqlite3.connect(self.db_path) as conn:
            placeholders = ",".join("?" for _ in runtime_events)
            after = conn.execute(
                f"SELECT COUNT(*) FROM task_events WHERE event_type IN ({placeholders})",
                runtime_events,
            ).fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(after, 0)

    def test_no_scheduler_artifacts_added(self) -> None:
        self.seed_queued_with_package("AT-DISC-ARTIFACTS")
        guarded_types = (
            "scheduler_proposal",
            "scheduler_confirmation",
            "scheduler_confirmation_verifier_report",
            "intake_runner_handoff",
            "runtime_handoff_execution",
        )

        self.discover()

        with sqlite3.connect(self.db_path) as conn:
            placeholders = ",".join("?" for _ in guarded_types)
            count = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM task_artifacts
                WHERE artifact_type IN ({placeholders})
                """,
                guarded_types,
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_no_artifact_files_created_on_disk(self) -> None:
        self.seed_queued_with_package("AT-DISC-FS")
        scheduler_dir = self.artifact_root / "scheduler_proposals"

        self.discover()

        self.assertFalse(scheduler_dir.exists())

    def test_task_key_filter(self) -> None:
        self.seed_task("AT-DISC-FILTER-A", status="queued")
        self.seed_task("AT-DISC-FILTER-B", status="queued")

        payload = self.discover(task_key="AT-DISC-FILTER-A")

        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(
            payload["candidates"][0]["task_key"], "AT-DISC-FILTER-A"
        )

    def test_project_filter(self) -> None:
        self.seed_task("AT-DISC-PROJ-A", status="queued", project="agent-taskflow")
        self.seed_task("AT-DISC-PROJ-B", status="queued", project="other-project")

        payload = self.discover(project="other-project")

        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["candidates"][0]["task_key"], "AT-DISC-PROJ-B")

    def test_status_filter(self) -> None:
        self.seed_task("AT-DISC-S-QUEUED", status="queued")
        self.seed_task("AT-DISC-S-BLOCKED", status="blocked")

        payload = self.discover(status="blocked")

        keys = [candidate["task_key"] for candidate in payload["candidates"]]
        self.assertIn("AT-DISC-S-BLOCKED", keys)
        self.assertNotIn("AT-DISC-S-QUEUED", keys)

    def test_include_not_ready_default_excludes_human_pr_review(self) -> None:
        task_key = "AT-DISC-HUMAN"
        artifact_dir = self.seed_task(task_key, status="waiting_approval")
        self._record_executor_and_validators(task_key)
        self._record_pr_handoff(task_key, artifact_dir)
        self._record_branch_push(task_key, artifact_dir)
        self._record_draft_pr(task_key, artifact_dir, merged=False)

        default_payload = self.discover()
        included_payload = self.discover(include_not_ready=True)

        default_kinds = [
            candidate["recommended_command_kind"]
            for candidate in default_payload["candidates"]
        ]
        included_kinds = [
            candidate["recommended_command_kind"]
            for candidate in included_payload["candidates"]
        ]
        self.assertNotIn("human_pr_review", default_kinds)
        self.assertIn("human_pr_review", included_kinds)

        human = next(
            candidate
            for candidate in included_payload["candidates"]
            if candidate["recommended_command_kind"] == "human_pr_review"
        )
        self.assertFalse(human["candidate_ready"])
        self.assertEqual(human["required_next_gate"], "human_github_review")

    def test_include_no_action_includes_completed_tasks(self) -> None:
        task_key = "AT-DISC-DONE"
        artifact_dir = self.seed_task(task_key, status="completed")
        self._record_cleanup(task_key, artifact_dir)

        default_payload = self.discover()
        no_action_payload = self.discover(include_no_action=True)

        default_kinds = [
            candidate["recommended_command_kind"]
            for candidate in default_payload["candidates"]
        ]
        no_action_kinds = [
            candidate["recommended_command_kind"]
            for candidate in no_action_payload["candidates"]
        ]
        self.assertNotIn("no_action", default_kinds)
        self.assertIn("no_action", no_action_kinds)

        no_action = next(
            candidate
            for candidate in no_action_payload["candidates"]
            if candidate["recommended_command_kind"] == "no_action"
        )
        self.assertFalse(no_action["candidate_ready"])
        self.assertEqual(no_action["required_next_gate"], "none")
        self.assertEqual(no_action["required_operator_action"], "none")

    def test_include_not_ready_does_not_include_no_action(self) -> None:
        task_key = "AT-DISC-DONE-NOT-READY"
        artifact_dir = self.seed_task(task_key, status="completed")
        self._record_cleanup(task_key, artifact_dir)

        payload = self.discover(include_not_ready=True)

        kinds = [
            candidate["recommended_command_kind"]
            for candidate in payload["candidates"]
        ]
        self.assertNotIn("no_action", kinds)
        self.assertEqual(payload["candidate_count"], 0)

    def test_include_not_ready_and_no_action_include_both_categories(self) -> None:
        human_task_key = "AT-DISC-HUMAN-BOTH"
        human_artifact_dir = self.seed_task(
            human_task_key, status="waiting_approval"
        )
        self._record_executor_and_validators(human_task_key)
        self._record_pr_handoff(human_task_key, human_artifact_dir)
        self._record_branch_push(human_task_key, human_artifact_dir)
        self._record_draft_pr(human_task_key, human_artifact_dir, merged=False)

        done_task_key = "AT-DISC-DONE-BOTH"
        done_artifact_dir = self.seed_task(done_task_key, status="completed")
        self._record_cleanup(done_task_key, done_artifact_dir)

        payload = self.discover(include_not_ready=True, include_no_action=True)

        kinds_by_key = {
            candidate["task_key"]: candidate["recommended_command_kind"]
            for candidate in payload["candidates"]
        }
        self.assertEqual(kinds_by_key[human_task_key], "human_pr_review")
        self.assertEqual(kinds_by_key[done_task_key], "no_action")

    def test_candidate_ready_false_for_not_ready_kinds(self) -> None:
        for kind in NOT_READY_KINDS | NO_ACTION_KINDS:
            self.assertNotIn(kind, ACTIONABLE_CANDIDATE_KINDS)

    def test_limit_caps_candidate_count(self) -> None:
        self.seed_task("AT-DISC-LIMIT-A", status="queued")
        self.seed_task("AT-DISC-LIMIT-B", status="queued")
        self.seed_task("AT-DISC-LIMIT-C", status="queued")

        payload = self.discover(limit=2)

        self.assertEqual(payload["candidate_count"], 2)
        self.assertEqual(len(payload["candidates"]), 2)

    def test_returns_empty_when_no_tasks(self) -> None:
        payload = self.discover()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["candidate_count"], 0)
        self.assertEqual(payload["candidates"], [])
        self.assertTrue(payload["safety"]["read_only"])

    def test_missing_db_raises_discovery_error(self) -> None:
        missing = self.root / "no-such" / "state.db"
        with self.assertRaises(SchedulerCandidateDiscoveryError):
            discover_scheduler_candidates(
                SchedulerCandidateDiscoveryRequest(db_path=missing)
            )

    def test_request_normalizes_db_path_with_tilde(self) -> None:
        with patch.dict("os.environ", {"HOME": str(self.root)}):
            request = SchedulerCandidateDiscoveryRequest(db_path="~/state.db")

        self.assertEqual(request.db_path, self.root / "state.db")

    def test_discovery_uses_request_normalized_db_path(self) -> None:
        self.seed_task("AT-DISC-TILDE", status="queued")

        with patch.dict("os.environ", {"HOME": str(self.root)}):
            request = SchedulerCandidateDiscoveryRequest(db_path="~/state.db")

        payload = discover_scheduler_candidates(request)

        self.assertEqual(payload["db_path"], str(self.db_path))
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["candidates"][0]["task_key"], "AT-DISC-TILDE")

    def test_invalid_project_filter_raises(self) -> None:
        with self.assertRaises(ValueError):
            SchedulerCandidateDiscoveryRequest(db_path=self.db_path, project="   ")

    def test_invalid_status_filter_raises(self) -> None:
        with self.assertRaises(ValueError):
            SchedulerCandidateDiscoveryRequest(
                db_path=self.db_path, status="definitely-not-a-status"
            )

    def test_list_scheduler_candidates_wrapper(self) -> None:
        self.seed_task("AT-DISC-WRAP", status="queued")

        payload = list_scheduler_candidates(db_path=self.db_path)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(
            payload["candidates"][0]["task_key"], "AT-DISC-WRAP"
        )
        self.assertTrue(payload["safety"]["read_only"])

    # ------------------------------------------------------------------
    # Helpers to seed multi-phase evidence
    # ------------------------------------------------------------------

    def _record_artifact(
        self,
        task_key: str,
        artifact_dir: Path,
        artifact_type: str,
        filename: str,
        payload: dict[str, object] | None = None,
    ) -> Path:
        path = artifact_dir / filename
        if payload is None:
            path.write_text("{}\n", encoding="utf-8")
        else:
            path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
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

    def _record_pr_handoff(self, task_key: str, artifact_dir: Path) -> None:
        self._record_artifact(
            task_key, artifact_dir, "pr_handoff_package", "pr_handoff_package.json"
        )
        self.store.record_task_event(
            task_key,
            "pr_handoff_package_created",
            "pr_handoff_package",
            payload={"kind": "pr_handoff_package_created", "task_key": task_key},
        )

    def _record_branch_push(self, task_key: str, artifact_dir: Path) -> None:
        payload = {
            "kind": "branch_push_completed",
            "artifact_type": "branch_push",
            "task_key": task_key,
            "branch": f"task/{task_key}",
            "base_branch": "main",
            "head_sha": "head-sha",
            "push_ok": True,
        }
        self._record_artifact(
            task_key, artifact_dir, "branch_push", "branch_push.json", payload
        )
        self.store.record_task_event(
            task_key,
            "branch_push_completed",
            "branch_push_confirm",
            payload=payload,
        )

    def _record_draft_pr(
        self, task_key: str, artifact_dir: Path, *, merged: bool
    ) -> None:
        payload = {
            "kind": "draft_pr_created",
            "artifact_type": "draft_pr",
            "task_key": task_key,
            "verified": True,
            "verification": {"verified": True, "passed": True},
            "pr_number": 123,
            "pr_url": f"https://github.com/example/repo/pull/123",
            "current_state": "MERGED" if merged else "OPEN",
            "merged": merged,
            "recorded_post_merge": merged,
            "pr_created": True,
            "draft_pr_created": True,
        }
        self._record_artifact(
            task_key, artifact_dir, "draft_pr", "draft_pr.json", payload
        )
        self.store.record_task_event(
            task_key,
            "draft_pr_created",
            "draft_pr_confirm",
            payload=payload,
        )

    def _record_cleanup(self, task_key: str, artifact_dir: Path) -> None:
        for artifact_type, filename, event_type in (
            ("local_cleanup", "local_cleanup.json", "local_cleanup_completed"),
            (
                "remote_branch_cleanup",
                "remote_branch_cleanup.json",
                "remote_branch_cleanup_completed",
            ),
            ("task_closeout", "task_closeout.json", "task_closeout_completed"),
        ):
            payload = {
                "kind": event_type,
                "artifact_type": artifact_type,
                "task_key": task_key,
            }
            self._record_artifact(
                task_key, artifact_dir, artifact_type, filename, payload
            )
            self.store.record_task_event(
                task_key,
                event_type,
                f"{artifact_type}_confirm",
                payload=payload,
            )


if __name__ == "__main__":
    unittest.main()
