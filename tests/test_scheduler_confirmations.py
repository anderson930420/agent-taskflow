from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord
from agent_taskflow.scheduler_confirmations import (
    CONFIRMATION_ARTIFACT_TYPE,
    CONFIRMATION_EVENT_TYPE,
    CONFIRMATION_SAFETY_FLAGS,
    SCHEMA_VERSION,
    SchedulerConfirmationError,
    SchedulerConfirmationRequest,
    create_scheduler_confirmation,
)
from agent_taskflow.scheduler_proposals import (
    PROPOSAL_ARTIFACT_TYPE,
    SchedulerProposalRequest,
    create_scheduler_proposal,
)
from agent_taskflow.store import TaskMirrorStore


class _Base(unittest.TestCase):
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

    def _seed_queued(self, task_key: str) -> None:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title=f"confirm task {task_key}",
                status="queued",
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )

    def _record_proposal(self, task_keys: list[str]) -> dict[str, object]:
        for key in task_keys:
            self._seed_queued(key)
        return create_scheduler_proposal(
            SchedulerProposalRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                dry_run=False,
                confirm_create_proposal=True,
            )
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

    def _first_safe_item_id(self, proposal: dict[str, object]) -> str:
        for item in proposal["items"]:  # type: ignore[index]
            if (
                item["recommended_command_kind"] == "create_task_execution_package"
                and not item.get("consistency_warnings")
            ):
                return item["proposal_item_id"]
        raise AssertionError("no safe item available in seeded proposal")


class DryRunConfirmationTests(_Base):
    def test_dry_run_returns_payload_without_writing(self) -> None:
        proposal = self._record_proposal(["AT-CONF-DRY-001"])
        item_id = self._first_safe_item_id(proposal)
        before = self._db_counts()

        payload = create_scheduler_confirmation(
            SchedulerConfirmationRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                latest=True,
                selected_item_ids=(item_id,),
                dry_run=True,
            )
        )

        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["selected_items"][0]["proposal_item_id"], item_id)
        self.assertFalse(payload["selected_items"][0]["execution_allowed"])
        self.assertIsNone(payload["artifact_path"])
        self.assertFalse(payload["summary"]["execution_allowed"])
        self.assertEqual(self._db_counts(), before)
        # No confirmation directory was created
        self.assertFalse((self.artifact_root / "scheduler_confirmations").exists())

    def test_dry_run_safety_flags_all_false_for_execution(self) -> None:
        proposal = self._record_proposal(["AT-CONF-DRY-002"])
        item_id = self._first_safe_item_id(proposal)
        payload = create_scheduler_confirmation(
            SchedulerConfirmationRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                latest=True,
                selected_item_ids=(item_id,),
                dry_run=True,
            )
        )
        safety = payload["safety"]
        self.assertEqual(safety, CONFIRMATION_SAFETY_FLAGS)
        for key in (
            "execution_allowed",
            "will_execute",
            "will_push",
            "will_create_pr",
            "will_merge",
            "will_approve",
            "will_reject",
            "will_cleanup",
            "will_mutate_github",
            "will_change_task_status",
            "will_start_background_worker",
            "action_evidence_created",
            "workflow_action_performed",
        ):
            self.assertFalse(safety[key], key)
        self.assertTrue(safety["confirmation_only"])


class ConfirmedConfirmationTests(_Base):
    def test_confirmed_writes_artifact_and_event(self) -> None:
        proposal = self._record_proposal(["AT-CONF-CONF-001"])
        item_id = self._first_safe_item_id(proposal)

        payload = create_scheduler_confirmation(
            SchedulerConfirmationRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                latest=True,
                selected_item_ids=(item_id,),
                dry_run=False,
                confirm_create_confirmation=True,
            )
        )

        artifact_path = Path(payload["artifact_path"])
        self.assertTrue(artifact_path.exists())
        on_disk = json.loads(artifact_path.read_text())
        self.assertEqual(on_disk["confirmation_id"], payload["confirmation_id"])
        self.assertEqual(on_disk["mode"], "confirmed")
        self.assertEqual(on_disk["safety"]["execution_allowed"], False)

        with sqlite3.connect(self.db_path) as conn:
            artifact_rows = conn.execute(
                "SELECT task_key, path FROM task_artifacts "
                "WHERE artifact_type = ?",
                (CONFIRMATION_ARTIFACT_TYPE,),
            ).fetchall()
            event_rows = conn.execute(
                "SELECT task_key, payload_json FROM task_events "
                "WHERE event_type = ?",
                (CONFIRMATION_EVENT_TYPE,),
            ).fetchall()

        self.assertEqual(len(artifact_rows), 1)
        self.assertEqual(artifact_rows[0][0], "AT-CONF-CONF-001")
        self.assertEqual(artifact_rows[0][1], str(artifact_path))
        self.assertEqual(len(event_rows), 1)
        event_payload = json.loads(event_rows[0][1])
        self.assertEqual(event_payload["proposal_item_id"], item_id)
        self.assertEqual(event_payload["execution_allowed"], False)

    def test_confirmed_does_not_mutate_task_status(self) -> None:
        proposal = self._record_proposal(["AT-CONF-STAT-001"])
        item_id = self._first_safe_item_id(proposal)
        with sqlite3.connect(self.db_path) as conn:
            status_before = conn.execute(
                "SELECT status FROM tasks WHERE task_key = ?",
                ("AT-CONF-STAT-001",),
            ).fetchone()[0]

        create_scheduler_confirmation(
            SchedulerConfirmationRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                latest=True,
                selected_item_ids=(item_id,),
                dry_run=False,
                confirm_create_confirmation=True,
            )
        )

        with sqlite3.connect(self.db_path) as conn:
            status_after = conn.execute(
                "SELECT status FROM tasks WHERE task_key = ?",
                ("AT-CONF-STAT-001",),
            ).fetchone()[0]
        self.assertEqual(status_before, status_after)

    def test_confirmed_does_not_emit_action_evidence_types(self) -> None:
        proposal = self._record_proposal(["AT-CONF-NOEV-001"])
        item_id = self._first_safe_item_id(proposal)

        with sqlite3.connect(self.db_path) as conn:
            existing_artifact_types = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT artifact_type FROM task_artifacts"
                ).fetchall()
            }
            existing_event_types = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT event_type FROM task_events"
                ).fetchall()
            }

        create_scheduler_confirmation(
            SchedulerConfirmationRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                latest=True,
                selected_item_ids=(item_id,),
                dry_run=False,
                confirm_create_confirmation=True,
            )
        )

        forbidden_artifacts = {
            "task_execution_package",
            "pr_handoff",
            "pr_handoff_package",
            "draft_pr",
            "branch_push",
            "local_cleanup",
            "remote_branch_cleanup",
            "task_closeout",
        }
        forbidden_events = {
            "task_execution_package_created",
            "pr_handoff_created",
            "pr_handoff_package_created",
            "draft_pr_created",
            "branch_pushed",
            "branch_push_completed",
            "local_cleanup_completed",
            "remote_branch_cleanup_completed",
            "task_closeout_completed",
        }

        with sqlite3.connect(self.db_path) as conn:
            new_artifact_types = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT artifact_type FROM task_artifacts"
                ).fetchall()
            } - existing_artifact_types
            new_event_types = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT event_type FROM task_events"
                ).fetchall()
            } - existing_event_types

        self.assertTrue(forbidden_artifacts.isdisjoint(new_artifact_types))
        self.assertTrue(forbidden_events.isdisjoint(new_event_types))
        self.assertIn(CONFIRMATION_ARTIFACT_TYPE, new_artifact_types)
        self.assertIn(CONFIRMATION_EVENT_TYPE, new_event_types)


class SelectorTests(_Base):
    def test_proposal_id_selector_loads_correct_proposal(self) -> None:
        first = self._record_proposal(["AT-CONF-SEL-A"])
        second = self._record_proposal(["AT-CONF-SEL-B"])
        item_id = self._first_safe_item_id(first)

        payload = create_scheduler_confirmation(
            SchedulerConfirmationRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                proposal_id=first["proposal_id"],
                selected_item_ids=(item_id,),
                dry_run=True,
            )
        )
        self.assertEqual(payload["proposal"]["proposal_id"], first["proposal_id"])
        self.assertNotEqual(
            payload["proposal"]["proposal_id"], second["proposal_id"]
        )

    def test_artifact_path_selector_loads_correct_proposal(self) -> None:
        first = self._record_proposal(["AT-CONF-PATH-A"])
        item_id = self._first_safe_item_id(first)
        payload = create_scheduler_confirmation(
            SchedulerConfirmationRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                proposal_artifact_path=Path(first["artifact_path"]),
                selected_item_ids=(item_id,),
                dry_run=True,
            )
        )
        self.assertEqual(payload["proposal"]["proposal_id"], first["proposal_id"])

    def test_latest_selector_loads_newest_proposal(self) -> None:
        self._record_proposal(["AT-CONF-LATE-A"])
        second = self._record_proposal(["AT-CONF-LATE-B"])
        item_id = self._first_safe_item_id(second)

        payload = create_scheduler_confirmation(
            SchedulerConfirmationRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                latest=True,
                selected_item_ids=(item_id,),
                dry_run=True,
            )
        )
        self.assertEqual(payload["proposal"]["proposal_id"], second["proposal_id"])

    def test_requires_exactly_one_selector(self) -> None:
        with self.assertRaises(SchedulerConfirmationError):
            create_scheduler_confirmation(
                SchedulerConfirmationRequest(
                    db_path=self.db_path,
                    artifact_root=self.artifact_root,
                    selected_item_ids=("x",),
                    dry_run=True,
                )
            )

    def test_relative_artifact_root_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SchedulerConfirmationRequest(
                db_path=self.db_path,
                artifact_root=Path("artifacts"),
                latest=True,
                selected_item_ids=("x",),
            )


class ValidationTests(_Base):
    def test_unknown_selected_item_blocks(self) -> None:
        self._record_proposal(["AT-CONF-UNK-001"])
        with self.assertRaises(SchedulerConfirmationError) as cm:
            create_scheduler_confirmation(
                SchedulerConfirmationRequest(
                    db_path=self.db_path,
                    artifact_root=self.artifact_root,
                    latest=True,
                    selected_item_ids=("DOES-NOT-EXIST",),
                    dry_run=True,
                )
            )
        self.assertIn("not found", str(cm.exception))

    def test_invalid_proposal_hash_blocks(self) -> None:
        proposal = self._record_proposal(["AT-CONF-HASH-P-001"])
        item_id = self._first_safe_item_id(proposal)
        artifact_path = Path(proposal["artifact_path"])
        on_disk = json.loads(artifact_path.read_text())
        on_disk["proposal_hash"] = "0" * 64
        artifact_path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))

        with self.assertRaises(SchedulerConfirmationError):
            create_scheduler_confirmation(
                SchedulerConfirmationRequest(
                    db_path=self.db_path,
                    artifact_root=self.artifact_root,
                    latest=True,
                    selected_item_ids=(item_id,),
                    dry_run=True,
                )
            )

    def test_invalid_item_hash_blocks(self) -> None:
        proposal = self._record_proposal(["AT-CONF-HASH-I-001"])
        item_id = self._first_safe_item_id(proposal)
        artifact_path = Path(proposal["artifact_path"])
        on_disk = json.loads(artifact_path.read_text())
        on_disk["items"][0]["proposed_action"] = "MUTATED"
        artifact_path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))

        with self.assertRaises(SchedulerConfirmationError):
            create_scheduler_confirmation(
                SchedulerConfirmationRequest(
                    db_path=self.db_path,
                    artifact_root=self.artifact_root,
                    latest=True,
                    selected_item_ids=(item_id,),
                    dry_run=True,
                )
            )

    def test_warnings_block_without_ack(self) -> None:
        proposal = self._record_proposal(["AT-CONF-WARN-001"])
        item_id = self._first_safe_item_id(proposal)
        artifact_path = Path(proposal["artifact_path"])
        on_disk = json.loads(artifact_path.read_text())
        # Inject a warning into the matching item while keeping its item_hash
        # consistent by recomputing via the public helper.
        from agent_taskflow.scheduler_proposals import (
            compute_item_hash,
            compute_proposal_hash,
        )

        for item in on_disk["items"]:
            if item["proposal_item_id"] == item_id:
                item["consistency_warnings"] = ["synthetic warning for tests"]
                item["item_hash"] = compute_item_hash(item)
        on_disk["proposal_hash"] = compute_proposal_hash(on_disk)
        artifact_path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))

        with self.assertRaises(SchedulerConfirmationError) as cm:
            create_scheduler_confirmation(
                SchedulerConfirmationRequest(
                    db_path=self.db_path,
                    artifact_root=self.artifact_root,
                    latest=True,
                    selected_item_ids=(item_id,),
                    acknowledge_warnings=False,
                    dry_run=True,
                )
            )
        self.assertIn("consistency_warnings", str(cm.exception))

    def test_warnings_acknowledged_records_flag(self) -> None:
        proposal = self._record_proposal(["AT-CONF-WARN-002"])
        item_id = self._first_safe_item_id(proposal)
        artifact_path = Path(proposal["artifact_path"])
        on_disk = json.loads(artifact_path.read_text())
        from agent_taskflow.scheduler_proposals import (
            compute_item_hash,
            compute_proposal_hash,
        )

        for item in on_disk["items"]:
            if item["proposal_item_id"] == item_id:
                item["consistency_warnings"] = ["synthetic warning"]
                item["item_hash"] = compute_item_hash(item)
        on_disk["proposal_hash"] = compute_proposal_hash(on_disk)
        artifact_path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))

        payload = create_scheduler_confirmation(
            SchedulerConfirmationRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                latest=True,
                selected_item_ids=(item_id,),
                acknowledge_warnings=True,
                dry_run=True,
            )
        )
        self.assertTrue(
            payload["selected_items"][0]["operator_acknowledged_warnings"]
        )
        self.assertFalse(payload["selected_items"][0]["execution_allowed"])

    def test_no_action_item_is_not_confirmable(self) -> None:
        proposal = self._record_proposal(["AT-CONF-NOACT-001"])
        # Force an item kind to no_action and rehash.
        artifact_path = Path(proposal["artifact_path"])
        on_disk = json.loads(artifact_path.read_text())
        from agent_taskflow.scheduler_proposals import (
            compute_item_hash,
            compute_proposal_hash,
        )

        target = on_disk["items"][0]
        target["recommended_command_kind"] = "no_action"
        target["proposal_item_id"] = (
            f"{target['task_key']}:no_action"
        )
        target["item_hash"] = compute_item_hash(target)
        on_disk["proposal_hash"] = compute_proposal_hash(on_disk)
        artifact_path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))

        with self.assertRaises(SchedulerConfirmationError) as cm:
            create_scheduler_confirmation(
                SchedulerConfirmationRequest(
                    db_path=self.db_path,
                    artifact_root=self.artifact_root,
                    latest=True,
                    selected_item_ids=(target["proposal_item_id"],),
                    dry_run=True,
                )
            )
        self.assertIn("not confirmable", str(cm.exception))

    def test_unknown_item_is_not_confirmable(self) -> None:
        proposal = self._record_proposal(["AT-CONF-UNKN-001"])
        artifact_path = Path(proposal["artifact_path"])
        on_disk = json.loads(artifact_path.read_text())
        from agent_taskflow.scheduler_proposals import (
            compute_item_hash,
            compute_proposal_hash,
        )

        target = on_disk["items"][0]
        target["recommended_command_kind"] = "unknown"
        target["proposal_item_id"] = f"{target['task_key']}:unknown"
        target["item_hash"] = compute_item_hash(target)
        on_disk["proposal_hash"] = compute_proposal_hash(on_disk)
        artifact_path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))

        with self.assertRaises(SchedulerConfirmationError):
            create_scheduler_confirmation(
                SchedulerConfirmationRequest(
                    db_path=self.db_path,
                    artifact_root=self.artifact_root,
                    latest=True,
                    selected_item_ids=(target["proposal_item_id"],),
                    dry_run=True,
                )
            )

    def test_non_dry_run_without_confirm_blocks(self) -> None:
        proposal = self._record_proposal(["AT-CONF-NODRY-001"])
        item_id = self._first_safe_item_id(proposal)
        before = self._db_counts()
        with self.assertRaises(SchedulerConfirmationError):
            create_scheduler_confirmation(
                SchedulerConfirmationRequest(
                    db_path=self.db_path,
                    artifact_root=self.artifact_root,
                    latest=True,
                    selected_item_ids=(item_id,),
                    dry_run=False,
                    confirm_create_confirmation=False,
                )
            )
        self.assertEqual(self._db_counts(), before)


if __name__ == "__main__":
    unittest.main()
