from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from agent_taskflow.attempt_resources import AttemptResourceManager
from agent_taskflow.attempt_resources_schema import (
    ATTEMPT_RESOURCES_MIGRATION,
    migrate_attempt_resources,
)
from agent_taskflow.attempt_scoped_runtime_path import AttemptScopedRuntimeTaskStore
from agent_taskflow.models import TaskRecord
from agent_taskflow.runtime_admission import RuntimeAdmissionStore
from agent_taskflow.store import TaskMirrorStore, connect


class AttemptResourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.repo / "README.md").write_text("test\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")
        self.db_path = self.root / "state.db"
        self.artifact_base = self.root / "artifacts" / "AT-PR5-1"
        self.artifact_base.mkdir(parents=True)
        (self.artifact_base / "issue_spec.md").write_text("issue\n", encoding="utf-8")
        (self.artifact_base / "implementation_prompt.md").write_text(
            "prompt\n", encoding="utf-8"
        )
        self.base_store = TaskMirrorStore(self.db_path)
        self.base_store.init_db()
        self.base_store.upsert_task(
            TaskRecord(
                task_key="AT-PR5-1",
                project="agent-taskflow",
                board="agent-taskflow",
                title="Attempt resources",
                status="queued",
                repo_path=self.repo,
                artifact_dir=self.artifact_base,
                executor="noop",
                created_at="2026-07-11T00:00:00Z",
                updated_at="2026-07-11T00:00:00Z",
            )
        )

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _claim_and_prepare(self):
        store = AttemptScopedRuntimeTaskStore(self.db_path, heartbeat_interval_seconds=60)
        resource = store.preclaim_runtime(
            "AT-PR5-1",
            source="test-runtime",
            artifact_base_root=self.artifact_base,
            worktree_root=self.repo / ".worktrees",
            base_branch="main",
        )
        workspace = store.prepare_attempt_workspace("AT-PR5-1")
        self.assertTrue(workspace.ok, workspace.summary)
        return store, resource, workspace

    def test_migration_is_idempotent(self) -> None:
        migrate_attempt_resources(self.db_path)
        migrate_attempt_resources(self.db_path)
        with closing(connect(self.db_path)) as conn:
            count = conn.execute(
                "SELECT count(*) FROM schema_migrations WHERE name = ?",
                (ATTEMPT_RESOURCES_MIGRATION,),
            ).fetchone()[0]
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertEqual(count, 1)
        self.assertIn("attempt_resources", tables)

    def test_claim_allocates_attempt_scoped_paths_lock_pid_and_inputs(self) -> None:
        store, resource, workspace = self._claim_and_prepare()
        claim = store.runtime_claim("AT-PR5-1")
        assert claim is not None
        self.assertIn(claim.attempt_id, str(resource.worktree_path))
        self.assertIn(claim.attempt_id, str(resource.artifact_root))
        self.assertTrue(
            resource.branch_name.endswith(claim.attempt_id.removeprefix("attempt-")[:12])
        )
        self.assertEqual(resource.attempt_id, claim.attempt_id)
        self.assertEqual(workspace.worktree_path, resource.worktree_path)
        self.assertTrue(resource.lock_path.is_file())
        self.assertTrue(resource.pid_path.is_file())
        self.assertTrue((resource.artifact_root / "attempt-resources.json").is_file())
        self.assertEqual(
            (resource.artifact_root / "issue_spec.md").read_text(encoding="utf-8"),
            "issue\n",
        )
        self.assertEqual(
            (resource.artifact_root / "implementation_prompt.md").read_text(
                encoding="utf-8"
            ),
            "prompt\n",
        )
        pid_payload = json.loads(resource.pid_path.read_text(encoding="utf-8"))
        self.assertEqual(pid_payload["attempt_id"], claim.attempt_id)
        self.assertEqual(pid_payload["pid"], os.getpid())
        store.update_task_status("AT-PR5-1", "blocked", source="test-runtime")

    def test_normal_release_retains_history_and_removes_pid(self) -> None:
        store, resource, _ = self._claim_and_prepare()
        store.update_task_status(
            "AT-PR5-1",
            "blocked",
            source="test-runtime",
            blocked_reason="test complete",
        )
        persisted = AttemptResourceManager(self.db_path).get(resource.attempt_id)
        assert persisted is not None
        self.assertEqual(persisted.status, "released")
        self.assertFalse(persisted.pid_path.exists())
        self.assertTrue(persisted.lock_path.exists())
        self.assertTrue(persisted.worktree_path.exists())
        self.assertTrue(persisted.artifact_root.exists())

    def test_retry_gets_new_branch_worktree_and_artifact_root(self) -> None:
        first_store, first, _ = self._claim_and_prepare()
        first_store.update_task_status("AT-PR5-1", "blocked", source="test-runtime")
        self.base_store.update_task_status(
            "AT-PR5-1",
            "queued",
            source="test-reset",
            expected_current_status="blocked",
        )
        second_store, second, _ = self._claim_and_prepare()
        self.assertNotEqual(first.attempt_id, second.attempt_id)
        self.assertNotEqual(first.branch_name, second.branch_name)
        self.assertNotEqual(first.worktree_path, second.worktree_path)
        self.assertNotEqual(first.artifact_root, second.artifact_root)
        self.assertTrue(first.worktree_path.exists())
        self.assertTrue(first.artifact_root.exists())
        second_store.update_task_status("AT-PR5-1", "blocked", source="test-runtime")

    def test_same_attempt_workspace_prepare_is_idempotent_only_within_attempt(self) -> None:
        store, resource, first = self._claim_and_prepare()
        second = store.prepare_attempt_workspace("AT-PR5-1")
        self.assertTrue(second.ok)
        self.assertEqual(first.worktree_path, second.worktree_path)
        claim = store.runtime_claim("AT-PR5-1")
        assert claim is not None
        self.assertEqual(resource.attempt_id, claim.attempt_id)
        store.update_task_status("AT-PR5-1", "blocked", source="test-runtime")

    def test_reaper_clears_dead_process_markers_without_deleting_history(self) -> None:
        store, resource, _ = self._claim_and_prepare()
        state = store._attempt_resource_states["AT-PR5-1"]
        claim_state = store._state_for("AT-PR5-1")
        assert claim_state is not None
        store._stop_supervisor(claim_state)
        state.handle.lock.release()
        resource.pid_path.write_text(
            json.dumps({"attempt_id": resource.attempt_id, "pid": 99999999}),
            encoding="utf-8",
        )
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE runtime_leases SET expires_at = '2000-01-01T00:00:00Z' WHERE attempt_id = ?",
                (resource.attempt_id,),
            )
        RuntimeAdmissionStore(self.db_path).expire_stale_leases()
        result = AttemptResourceManager(self.db_path).reap_stale_resources()
        self.assertEqual(result["reaped_attempt_ids"], [resource.attempt_id])
        persisted = AttemptResourceManager(self.db_path).get(resource.attempt_id)
        assert persisted is not None
        self.assertEqual(persisted.status, "reaped")
        self.assertFalse(persisted.pid_path.exists())
        self.assertTrue(persisted.worktree_path.exists())
        self.assertTrue(persisted.artifact_root.exists())

    def test_reaper_refuses_live_pid(self) -> None:
        store, resource, _ = self._claim_and_prepare()
        state = store._attempt_resource_states["AT-PR5-1"]
        claim_state = store._state_for("AT-PR5-1")
        assert claim_state is not None
        store._stop_supervisor(claim_state)
        state.handle.lock.release()
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE runtime_leases SET expires_at = '2000-01-01T00:00:00Z' WHERE attempt_id = ?",
                (resource.attempt_id,),
            )
        RuntimeAdmissionStore(self.db_path).expire_stale_leases()
        result = AttemptResourceManager(self.db_path).reap_stale_resources()
        self.assertEqual(
            result["blocked_live_pid_attempt_ids"], [resource.attempt_id]
        )
        persisted = AttemptResourceManager(self.db_path).get(resource.attempt_id)
        assert persisted is not None
        self.assertEqual(persisted.status, "reap_blocked_live_pid")
        self.assertTrue(persisted.pid_path.exists())


if __name__ == "__main__":
    unittest.main()
