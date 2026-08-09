from __future__ import annotations

from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.execution_engine_approved_task_adapter import (
    ApprovedTaskRunnerExecutionEngineAdapter,
)
from agent_taskflow.execution_engine_contract import (
    ExecutionEngineExecutorProfile,
    ExecutionEngineRequest,
    ExecutionEngineValidatorProfile,
    ExecutionEngineWorkspaceProfile,
)
from agent_taskflow.lifecycle_control import (
    RuntimeControlStore,
    RuntimeKillRequested,
    RuntimePausedError,
)
from agent_taskflow.lifecycle_runtime_path import LifecycleRuntimeAdmissionStore
from agent_taskflow.models import TaskRecord
from agent_taskflow.project_class_control_schema import migrate_project_class_controls
from agent_taskflow.runtime_admission import RuntimeAdmissionStore
from agent_taskflow.store import TaskMirrorStore, connect


class ProjectClassControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        mirror = TaskMirrorStore(self.db_path)
        mirror.init_db()
        fixtures = (
            ("AT-A1", "project-a", "docs-only"),
            ("AT-A2", "project-a", "test-hardening"),
            ("AT-B1", "project-b", "docs-only"),
        )
        for task_key, project, _task_class in fixtures:
            mirror.upsert_task(
                TaskRecord(
                    task_key=task_key,
                    project=project,
                    status="queued",
                    repo_path=self.repo,
                    artifact_dir=self.artifacts / task_key,
                    executor="manual",
                )
            )
        attempts = AttemptStore(self.db_path)
        attempts.init_db()
        for task_key, _project, task_class in fixtures:
            attempts.register_task_identity(
                task_key,
                task_class=task_class,
                is_legacy=False,
            )
        self.attempts = attempts
        self.controls = RuntimeControlStore(self.db_path)
        self.controls.init_db()
        migrate_project_class_controls(self.db_path)
        self.admission = LifecycleRuntimeAdmissionStore(self.db_path)

    def test_scope_normalizers_keep_project_class_and_task_identities_distinct(self) -> None:
        project = self.controls.pause(
            scope_kind="project", scope_id="foo", actor="operator-a"
        )
        task_class = self.controls.disable_task_class_governance(
            "foo", actor="operator-b"
        )
        task = self.controls.pause(
            scope_kind="task", scope_id="foo", actor="operator-c"
        )

        self.assertEqual((project.scope_kind, project.scope_id), ("project", "foo"))
        self.assertEqual(
            (task_class.scope_kind, task_class.scope_id), ("task_class", "foo")
        )
        self.assertEqual((task.scope_kind, task.scope_id), ("task", "foo"))
        self.assertEqual(
            {(project.scope_kind, project.scope_id),
             (task_class.scope_kind, task_class.scope_id),
             (task.scope_kind, task.scope_id)},
            {("project", "foo"), ("task_class", "foo"), ("task", "foo")},
        )
        with self.assertRaisesRegex(ValueError, "not process kills"):
            self.controls.request_kill(
                scope_kind="project", scope_id="foo", actor="operator"
            )
        with self.assertRaisesRegex(ValueError, "not process kills"):
            self.controls.set_control(
                "kill_requested",
                scope_kind="project",
                scope_id="foo",
                actor="operator",
                reason_code="operator_kill_requested",
            )
        with self.assertRaisesRegex(ValueError, "governance"):
            self.controls.pause(
                scope_kind="task_class", scope_id="foo", actor="operator"
            )
        with self.assertRaisesRegex(ValueError, "not process kill"):
            self.controls.request_kill(
                scope_kind="task_class", scope_id="foo", actor="operator"
            )
        with self.assertRaisesRegex(ValueError, "actor"):
            self.controls.pause(
                scope_kind="project", scope_id="project-a", actor="  "
            )

    def test_project_pause_isolated_and_existing_attempt_remains_active(self) -> None:
        active_a2 = self.admission.claim("AT-A2", owner_id="runner-a2")
        self.controls.pause(
            scope_kind="project",
            scope_id="project-a",
            actor="project-operator",
            metadata={"ticket": "M1-D"},
        )

        with self.assertRaises(RuntimePausedError):
            self.admission.claim("AT-A1", owner_id="runner-a1")
        unaffected_b1 = self.admission.claim("AT-B1", owner_id="runner-b1")
        still_active = self.attempts.get_attempt(active_a2.attempt_id)
        self.assertIsNotNone(still_active)
        assert still_active is not None
        self.assertTrue(still_active.is_active)
        self.assertEqual(still_active.status, "preparing")
        self.assertIsNotNone(unaffected_b1)

        self.controls.clear(
            scope_kind="project",
            scope_id="project-a",
            actor="project-operator",
        )
        resumed_a1 = self.admission.claim("AT-A1", owner_id="runner-a1")
        self.assertIsNotNone(resumed_a1)

    def test_raw_alternate_claim_cannot_bypass_project_pause(self) -> None:
        self.controls.pause(
            scope_kind="project", scope_id="project-a", actor="operator"
        )
        raw_admission = RuntimeAdmissionStore(self.db_path)

        with self.assertRaises(RuntimePausedError):
            raw_admission.claim("AT-A1", owner_id="alternate-entry")

        self.assertEqual(self.attempts.list_attempts("AT-A1"), [])

    def test_direct_level2_preparing_transition_cannot_bypass_project_pause(self) -> None:
        self.controls.pause(
            scope_kind="project", scope_id="project-a", actor="operator"
        )

        with closing(connect(self.db_path)) as conn:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "admission denied by persisted control"
            ):
                with conn:
                    conn.execute(
                        "UPDATE tasks SET status = 'preparing' WHERE task_key = 'AT-A1'"
                    )
            status = conn.execute(
                "SELECT status FROM tasks WHERE task_key = 'AT-A1'"
            ).fetchone()[0]
        self.assertEqual(status, "queued")
        self.assertEqual(self.attempts.list_attempts("AT-A1"), [])

    def test_execution_engine_rejects_paused_project_before_injected_runner(self) -> None:
        self.controls.pause(
            scope_kind="project", scope_id="project-a", actor="operator"
        )
        calls = 0

        def injected_runner(_request):
            nonlocal calls
            calls += 1
            return {"ok": True, "status": "waiting_approval"}

        request = ExecutionEngineRequest(
            task_key="AT-A1",
            project="untrusted-project-b",
            dry_run=False,
            preflight=False,
            executor_profile=ExecutionEngineExecutorProfile(executor="manual"),
            validator_profile=ExecutionEngineValidatorProfile(),
            workspace=ExecutionEngineWorkspaceProfile(
                repo_path=self.repo,
                artifact_dir=self.artifacts / "AT-A1",
                worktree_root=self.root / "worktrees",
            ),
            lifecycle_db_path=self.db_path,
            runtime_handoff_path=self.root / "handoff.json",
            metadata={
                "level2_execution": True,
                "execution_authority": "execution_engine",
                "legacy_fallback_allowed": False,
                "confirmed": True,
            },
        )
        result = ApprovedTaskRunnerExecutionEngineAdapter(
            approved_task_runner=injected_runner
        ).execute(request)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("paused", (result.summary or "").lower())
        self.assertEqual(calls, 0)
        self.assertEqual(self.attempts.list_attempts("AT-A1"), [])

    def test_task_class_disable_is_global_immediate_and_execution_neutral(self) -> None:
        active_a1 = self.admission.claim("AT-A1", owner_id="runner-a1")
        initial_a1 = self.controls.class_control_allows_auto_merge("AT-A1")
        initial_b1 = self.controls.class_control_allows_auto_merge("AT-B1")
        self.assertTrue(initial_a1.class_control_allows_auto_merge)
        self.assertTrue(initial_b1.class_control_allows_auto_merge)
        self.assertFalse(initial_a1.actual_auto_merge_enabled)

        disabled = self.controls.disable_task_class_governance(
            "docs-only",
            actor="governance-operator",
            metadata={"reason": "immediate M1-D drill"},
        )
        self.assertEqual(disabled.reason_code, "operator_task_class_governance_disabled")
        self.assertFalse(
            self.controls.class_control_allows_auto_merge(
                "AT-A1"
            ).class_control_allows_auto_merge
        )
        self.assertFalse(
            self.controls.class_control_allows_auto_merge(
                "AT-B1"
            ).class_control_allows_auto_merge
        )
        self.assertTrue(
            self.controls.class_control_allows_auto_merge(
                "AT-A2"
            ).class_control_allows_auto_merge
        )
        self.controls.assert_not_killed("AT-A1", active_a1.attempt_id)
        active = self.attempts.get_attempt(active_a1.attempt_id)
        assert active is not None
        self.assertTrue(active.is_active)
        self.assertEqual(active.status, "preparing")

        cleared = self.controls.clear(
            scope_kind="task_class",
            scope_id="docs-only",
            actor="governance-operator",
        )
        restored = self.controls.class_control_allows_auto_merge("AT-A1")
        self.assertEqual(
            cleared.reason_code, "operator_task_class_governance_cleared"
        )
        self.assertTrue(restored.class_control_allows_auto_merge)
        self.assertFalse(restored.actual_auto_merge_enabled)

    def test_task_bound_governance_uses_persisted_class_not_caller_input(self) -> None:
        self.controls.disable_task_class_governance(
            "docs-only", actor="governance-operator"
        )

        # The Task-bound governance API deliberately accepts no caller class.
        # AT-A1 remains denied because its persisted tasks.task_class is
        # docs-only; querying another class cannot alter that decision.
        self.assertFalse(
            self.controls.class_control_allows_auto_merge(
                "AT-A1"
            ).class_control_allows_auto_merge
        )
        self.assertTrue(
            self.controls.task_class_governance_permitted("test-hardening")
        )

    def test_legacy_class_disable_does_not_remove_human_gated_level1_execution(self) -> None:
        mirror = TaskMirrorStore(self.db_path)
        mirror.upsert_task(
            TaskRecord(
                task_key="AT-LEGACY",
                project="project-a",
                status="queued",
                repo_path=self.repo,
                artifact_dir=self.artifacts / "AT-LEGACY",
                executor="manual",
            )
        )
        self.controls.disable_task_class_governance(
            "legacy", actor="governance-operator"
        )

        claim = RuntimeAdmissionStore(self.db_path).claim(
            "AT-LEGACY", owner_id="human-gated-level1"
        )

        self.assertIsNotNone(claim)
        self.assertEqual(
            self.controls.effective_control(task_key="AT-LEGACY").mode, "running"
        )
        self.assertFalse(
            self.controls.class_control_allows_auto_merge(
                "AT-LEGACY"
            ).class_control_allows_auto_merge
        )

    def test_execution_precedence_is_explicit_and_class_governance_is_separate(self) -> None:
        self.controls.set_control(
            "running",
            scope_kind="global",
            actor="operator",
            reason_code="operator_pause_cleared",
        )
        self.controls.pause(
            scope_kind="project", scope_id="project-a", actor="operator"
        )
        self.controls.set_control(
            "running",
            scope_kind="task",
            scope_id="AT-A1",
            actor="operator",
            reason_code="operator_pause_cleared",
        )
        self.assertEqual(
            self.controls.effective_control(task_key="AT-A1").mode, "paused"
        )

        self.controls.clear(
            scope_kind="project", scope_id="project-a", actor="operator"
        )
        self.controls.pause(
            scope_kind="task", scope_id="AT-A1", actor="operator"
        )
        self.assertEqual(
            self.controls.effective_control(task_key="AT-A1").mode, "paused"
        )

        self.controls.request_kill(scope_kind="global", actor="operator")
        self.controls.pause(
            scope_kind="project", scope_id="project-a", actor="operator"
        )
        with self.assertRaises(RuntimeKillRequested):
            self.controls.assert_admission_allowed("AT-A1")

        self.controls.clear(scope_kind="global", actor="operator")
        self.controls.clear(
            scope_kind="project", scope_id="project-a", actor="operator"
        )
        self.controls.clear(
            scope_kind="task", scope_id="AT-A1", actor="operator"
        )
        self.controls.disable_task_class_governance(
            "docs-only", actor="operator"
        )
        self.assertEqual(
            self.controls.effective_control(task_key="AT-A1").mode, "running"
        )

    def test_project_and_class_events_are_attributed_and_append_only(self) -> None:
        self.controls.pause(
            scope_kind="project",
            scope_id="project-a",
            actor="alice",
            metadata={"ticket": "OPS-1"},
        )
        self.controls.clear(
            scope_kind="project", scope_id="project-a", actor="bob"
        )
        self.controls.disable_task_class_governance(
            "docs-only", actor="carol", metadata={"ticket": "OPS-2"}
        )
        events = self.controls.list_control_events(
            scope_kind="project", scope_id="project-a"
        )
        class_events = self.controls.list_control_events(
            scope_kind="task_class", scope_id="docs-only"
        )

        self.assertEqual([event.actor for event in events], ["alice", "bob"])
        self.assertEqual([event.generation for event in events], [1, 2])
        self.assertEqual(events[0].metadata, {"ticket": "OPS-1"})
        self.assertEqual(class_events[0].actor, "carol")
        self.assertTrue(all(event.timestamp for event in events + class_events))
        with closing(connect(self.db_path)) as conn:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                with conn:
                    conn.execute(
                        "UPDATE runtime_control_events SET actor = 'mallory' WHERE event_id = ?",
                        (events[0].event_id,),
                    )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                with conn:
                    conn.execute(
                        "DELETE FROM runtime_control_events WHERE event_id = ?",
                        (class_events[0].event_id,),
                    )


if __name__ == "__main__":
    unittest.main()
