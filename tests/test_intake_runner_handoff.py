from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.intake_runner_handoff import (
    HANDOFF_ARTIFACT_TYPE,
    HANDOFF_EVENT_TYPE,
    HANDOFF_SAFETY_FLAGS,
    RUNNER_CONTRACT_FLAGS,
    SCHEMA_VERSION,
    STATUS_BLOCKED,
    STATUS_CREATED,
    STATUS_PREVIEW,
    IntakeRunnerHandoffError,
    IntakeRunnerHandoffRequest,
    create_intake_runner_handoff,
)
from agent_taskflow.models import TaskRecord
from agent_taskflow.scheduler_confirmation_verifier import STATUS_VALID
from agent_taskflow.scheduler_confirmations import (
    SchedulerConfirmationRequest,
    create_scheduler_confirmation,
)
from agent_taskflow.scheduler_proposals import (
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
                title=f"handoff task {task_key}",
                status="queued",
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )

    def _proposal(self, task_keys: list[str]) -> dict[str, object]:
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

    def _safe_item_id(self, proposal: dict[str, object]) -> str:
        for item in proposal["items"]:  # type: ignore[index]
            if (
                item["recommended_command_kind"] == "create_task_execution_package"
                and not item.get("consistency_warnings")
            ):
                return item["proposal_item_id"]
        raise AssertionError("no safe item available in seeded proposal")

    def _confirm(
        self,
        proposal: dict[str, object],
        item_ids: tuple[str, ...],
        *,
        acknowledge_warnings: bool = False,
    ) -> dict[str, object]:
        return create_scheduler_confirmation(
            SchedulerConfirmationRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                proposal_id=proposal["proposal_id"],  # type: ignore[index]
                selected_item_ids=item_ids,
                acknowledge_warnings=acknowledge_warnings,
                dry_run=False,
                confirm_create_confirmation=True,
            )
        )

    def _db_counts(self) -> dict[str, int]:
        with sqlite3.connect(self.db_path) as conn:
            return {
                "tasks": conn.execute(
                    "SELECT COUNT(*) FROM tasks"
                ).fetchone()[0],
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


class DryRunValidTests(_Base):
    def test_valid_verifier_returns_preview_payload_and_writes_nothing(
        self,
    ) -> None:
        proposal = self._proposal(["AT-IRH-OK-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))

        before = self._db_counts()

        payload = create_intake_runner_handoff(
            IntakeRunnerHandoffRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                proposal_item_id=item_id,
                latest=True,
            )
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], STATUS_PREVIEW)
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertIsNone(payload["artifact_path"])
        self.assertEqual(payload["task_key"], "AT-IRH-OK-001")
        self.assertEqual(
            payload["recommended_command_kind"], "create_task_execution_package"
        )
        self.assertEqual(
            payload["confirmation"]["confirmation_id"],
            confirmation["confirmation_id"],  # type: ignore[index]
        )
        self.assertEqual(
            payload["confirmation"]["verification_status"], STATUS_VALID
        )
        self.assertTrue(payload["confirmation"]["verification_passed"])
        self.assertTrue(
            payload["confirmation"]["eligible_for_command_specific_confirm"]
        )
        self.assertEqual(
            payload["proposal"]["proposal_id"],
            proposal["proposal_id"],  # type: ignore[index]
        )
        self.assertEqual(payload["safety"], dict(HANDOFF_SAFETY_FLAGS))
        self.assertEqual(
            payload["runner_contract"], dict(RUNNER_CONTRACT_FLAGS)
        )
        self.assertEqual(self._db_counts(), before)

    def test_dry_run_writes_no_artifact_or_event(self) -> None:
        proposal = self._proposal(["AT-IRH-NOWRITE-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        with sqlite3.connect(self.db_path) as conn:
            before_events = {
                row[0]
                for row in conn.execute(
                    "SELECT id FROM task_events"
                ).fetchall()
            }
            before_artifacts = {
                row[0]
                for row in conn.execute(
                    "SELECT id FROM task_artifacts"
                ).fetchall()
            }

        create_intake_runner_handoff(
            IntakeRunnerHandoffRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                proposal_item_id=item_id,
                latest=True,
            )
        )

        with sqlite3.connect(self.db_path) as conn:
            after_events = {
                row[0]
                for row in conn.execute(
                    "SELECT id FROM task_events"
                ).fetchall()
            }
            after_artifacts = {
                row[0]
                for row in conn.execute(
                    "SELECT id FROM task_artifacts"
                ).fetchall()
            }

        self.assertEqual(before_events, after_events)
        self.assertEqual(before_artifacts, after_artifacts)
        self.assertFalse(
            (self.artifact_root / "intake_runner_handoffs").exists()
        )


class ConfirmedModeTests(_Base):
    def test_confirmed_without_confirm_flag_raises(self) -> None:
        proposal = self._proposal(["AT-IRH-NOCONF-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        with self.assertRaises(IntakeRunnerHandoffError):
            create_intake_runner_handoff(
                IntakeRunnerHandoffRequest(
                    db_path=self.db_path,
                    artifact_root=self.artifact_root,
                    proposal_item_id=item_id,
                    latest=True,
                    dry_run=False,
                    confirm_create_handoff=False,
                )
            )

    def test_confirmed_writes_handoff_artifact_and_event(self) -> None:
        proposal = self._proposal(["AT-IRH-CONF-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        payload = create_intake_runner_handoff(
            IntakeRunnerHandoffRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                proposal_item_id=item_id,
                latest=True,
                dry_run=False,
                confirm_create_handoff=True,
            )
        )

        self.assertEqual(payload["status"], STATUS_CREATED)
        self.assertEqual(payload["mode"], "confirmed")
        artifact_path = Path(payload["artifact_path"])
        self.assertTrue(artifact_path.exists())
        on_disk = json.loads(artifact_path.read_text())
        self.assertEqual(on_disk["schema_version"], SCHEMA_VERSION)
        self.assertEqual(on_disk["handoff_id"], payload["handoff_id"])
        self.assertEqual(on_disk["status"], STATUS_CREATED)

        with sqlite3.connect(self.db_path) as conn:
            artifact_types_for_task = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT artifact_type FROM task_artifacts "
                    "WHERE task_key = ?",
                    ("AT-IRH-CONF-001",),
                ).fetchall()
            }
            event_types_for_task = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT event_type FROM task_events "
                    "WHERE task_key = ?",
                    ("AT-IRH-CONF-001",),
                ).fetchall()
            }

        self.assertIn(HANDOFF_ARTIFACT_TYPE, artifact_types_for_task)
        self.assertIn(HANDOFF_EVENT_TYPE, event_types_for_task)

    def test_confirmed_writes_only_handoff_artifact_and_event(self) -> None:
        proposal = self._proposal(["AT-IRH-CONF-ONLY-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        with sqlite3.connect(self.db_path) as conn:
            before_event_types = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT event_type FROM task_events"
                ).fetchall()
            }
            before_artifact_types = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT artifact_type FROM task_artifacts"
                ).fetchall()
            }

        create_intake_runner_handoff(
            IntakeRunnerHandoffRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                proposal_item_id=item_id,
                latest=True,
                dry_run=False,
                confirm_create_handoff=True,
            )
        )

        with sqlite3.connect(self.db_path) as conn:
            after_event_types = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT event_type FROM task_events"
                ).fetchall()
            }
            after_artifact_types = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT artifact_type FROM task_artifacts"
                ).fetchall()
            }

        new_artifact_types = after_artifact_types - before_artifact_types
        new_event_types = after_event_types - before_event_types
        self.assertEqual(new_artifact_types, {HANDOFF_ARTIFACT_TYPE})
        self.assertEqual(new_event_types, {HANDOFF_EVENT_TYPE})

        forbidden_artifacts = {
            "scheduler_confirmation_consumption",
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
            "scheduler_confirmation_consumed",
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
        self.assertTrue(forbidden_artifacts.isdisjoint(after_artifact_types))
        self.assertTrue(forbidden_events.isdisjoint(after_event_types))

    def test_confirmed_event_payload_disclaims_execution(self) -> None:
        proposal = self._proposal(["AT-IRH-EV-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        create_intake_runner_handoff(
            IntakeRunnerHandoffRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                proposal_item_id=item_id,
                latest=True,
                dry_run=False,
                confirm_create_handoff=True,
            )
        )

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT payload_json FROM task_events "
                "WHERE event_type = ? AND task_key = ?",
                (HANDOFF_EVENT_TYPE, "AT-IRH-EV-001"),
            ).fetchone()
        self.assertIsNotNone(row)
        event_payload = json.loads(row[0])

        self.assertEqual(event_payload["kind"], HANDOFF_EVENT_TYPE)
        self.assertTrue(event_payload["handoff_only"])
        self.assertFalse(event_payload["execution_allowed"])
        self.assertFalse(event_payload["execution_performed"])
        self.assertFalse(event_payload["executor_started"])
        self.assertFalse(event_payload["validators_started"])
        self.assertFalse(event_payload["action_evidence_created"])
        self.assertTrue(event_payload["requires_future_runtime_gate"])


class OutputSemanticTests(_Base):
    def test_all_outcomes_disclaim_execution(self) -> None:
        proposal = self._proposal(["AT-IRH-SEM-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        preview = create_intake_runner_handoff(
            IntakeRunnerHandoffRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                proposal_item_id=item_id,
                latest=True,
            )
        )
        blocked = create_intake_runner_handoff(
            IntakeRunnerHandoffRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                proposal_item_id="does-not-exist",
                latest=True,
            )
        )
        confirmed = create_intake_runner_handoff(
            IntakeRunnerHandoffRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                proposal_item_id=item_id,
                latest=True,
                dry_run=False,
                confirm_create_handoff=True,
            )
        )

        for payload in (preview, blocked, confirmed):
            runner = payload["runner_contract"]
            self.assertFalse(runner["execution_allowed"], payload)
            self.assertFalse(runner["execution_performed"], payload)
            self.assertFalse(runner["executor_started"], payload)
            self.assertFalse(runner["validators_started"], payload)
            self.assertFalse(runner["action_evidence_created"], payload)
            self.assertFalse(runner["runner_may_start"], payload)
            self.assertTrue(runner["requires_future_runtime_gate"], payload)
            safety = payload["safety"]
            self.assertTrue(safety["handoff_only"], payload)
            for key in (
                "will_execute",
                "will_push",
                "will_create_pr",
                "will_merge",
                "will_approve",
                "will_reject",
                "will_cleanup",
                "will_delete_branch",
                "will_delete_worktree",
                "will_mutate_github",
                "will_mutate_db_as_action",
                "will_start_background_worker",
            ):
                self.assertFalse(safety[key], (payload, key))


class BlockedDryRunTests(_Base):
    def test_blocked_dry_run_when_verifier_not_valid(self) -> None:
        proposal = self._proposal(["AT-IRH-BLK-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        payload = create_intake_runner_handoff(
            IntakeRunnerHandoffRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                proposal_item_id="nope",
                latest=True,
            )
        )
        self.assertEqual(payload["status"], STATUS_BLOCKED)
        self.assertFalse(payload["ok"])
        self.assertIsNone(payload["artifact_path"])
        self.assertIn("error", payload)

    def test_blocked_confirmed_mode_refuses_persistence(self) -> None:
        proposal = self._proposal(["AT-IRH-RFS-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        before = self._db_counts()
        with self.assertRaises(IntakeRunnerHandoffError):
            create_intake_runner_handoff(
                IntakeRunnerHandoffRequest(
                    db_path=self.db_path,
                    artifact_root=self.artifact_root,
                    proposal_item_id="nope-not-here",
                    latest=True,
                    dry_run=False,
                    confirm_create_handoff=True,
                )
            )
        self.assertEqual(self._db_counts(), before)
        self.assertFalse(
            (self.artifact_root / "intake_runner_handoffs").exists()
        )

    def test_invalid_confirmation_artifact_refuses_persistence(self) -> None:
        proposal = self._proposal(["AT-IRH-INV-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))

        # Corrupt the schema_version so verifier yields STATUS_INVALID.
        path = Path(confirmation["artifact_path"])  # type: ignore[index]
        on_disk = json.loads(path.read_text())
        on_disk["schema_version"] = "scheduler_confirmation.v999"
        path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))

        with self.assertRaises(IntakeRunnerHandoffError):
            create_intake_runner_handoff(
                IntakeRunnerHandoffRequest(
                    db_path=self.db_path,
                    artifact_root=self.artifact_root,
                    proposal_item_id=item_id,
                    latest=True,
                    dry_run=False,
                    confirm_create_handoff=True,
                )
            )
        self.assertFalse(
            (self.artifact_root / "intake_runner_handoffs").exists()
        )

    def test_expected_task_key_mismatch_blocks_dry_run(self) -> None:
        proposal = self._proposal(["AT-IRH-TK-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        payload = create_intake_runner_handoff(
            IntakeRunnerHandoffRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                proposal_item_id=item_id,
                latest=True,
                task_key="SOME-OTHER-TASK",
            )
        )
        self.assertEqual(payload["status"], STATUS_BLOCKED)
        self.assertIn(
            "expected_task_key_matches",
            payload["verifier_report_summary"]["failed_check_names"],
        )

    def test_expected_task_key_mismatch_refuses_confirmed(self) -> None:
        proposal = self._proposal(["AT-IRH-TK-RFS-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        with self.assertRaises(IntakeRunnerHandoffError):
            create_intake_runner_handoff(
                IntakeRunnerHandoffRequest(
                    db_path=self.db_path,
                    artifact_root=self.artifact_root,
                    proposal_item_id=item_id,
                    latest=True,
                    task_key="OTHER",
                    dry_run=False,
                    confirm_create_handoff=True,
                )
            )

    def test_expected_command_kind_mismatch_blocks_dry_run(self) -> None:
        proposal = self._proposal(["AT-IRH-EK-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        payload = create_intake_runner_handoff(
            IntakeRunnerHandoffRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                proposal_item_id=item_id,
                latest=True,
                expected_command_kind="branch_push_review",
            )
        )
        self.assertEqual(payload["status"], STATUS_BLOCKED)
        self.assertIn(
            "expected_command_kind_matches",
            payload["verifier_report_summary"]["failed_check_names"],
        )

    def test_expected_command_kind_mismatch_refuses_confirmed(self) -> None:
        proposal = self._proposal(["AT-IRH-EK-RFS-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        with self.assertRaises(IntakeRunnerHandoffError):
            create_intake_runner_handoff(
                IntakeRunnerHandoffRequest(
                    db_path=self.db_path,
                    artifact_root=self.artifact_root,
                    proposal_item_id=item_id,
                    latest=True,
                    expected_command_kind="draft_pr_review",
                    dry_run=False,
                    confirm_create_handoff=True,
                )
            )


class SelectorValidationTests(_Base):
    def test_missing_selector_rejected(self) -> None:
        with self.assertRaises(IntakeRunnerHandoffError):
            create_intake_runner_handoff(
                IntakeRunnerHandoffRequest(
                    db_path=self.db_path,
                    artifact_root=self.artifact_root,
                    proposal_item_id="something",
                )
            )

    def test_multiple_selectors_rejected(self) -> None:
        with self.assertRaises(IntakeRunnerHandoffError):
            create_intake_runner_handoff(
                IntakeRunnerHandoffRequest(
                    db_path=self.db_path,
                    artifact_root=self.artifact_root,
                    proposal_item_id="something",
                    latest=True,
                    confirmation_id="confirmation-x",
                )
            )

    def test_lookup_by_confirmation_id(self) -> None:
        proposal = self._proposal(["AT-IRH-SEL-CID-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))

        payload = create_intake_runner_handoff(
            IntakeRunnerHandoffRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                proposal_item_id=item_id,
                confirmation_id=confirmation["confirmation_id"],  # type: ignore[index]
            )
        )
        self.assertEqual(payload["status"], STATUS_PREVIEW)
        self.assertEqual(
            payload["confirmation"]["confirmation_id"],
            confirmation["confirmation_id"],  # type: ignore[index]
        )

    def test_lookup_by_explicit_artifact_path(self) -> None:
        proposal = self._proposal(["AT-IRH-SEL-PATH-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))

        payload = create_intake_runner_handoff(
            IntakeRunnerHandoffRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                proposal_item_id=item_id,
                confirmation_artifact_path=Path(
                    confirmation["artifact_path"]  # type: ignore[index]
                ),
            )
        )
        self.assertEqual(payload["status"], STATUS_PREVIEW)


class ArtifactEventTypeDisjointTests(_Base):
    def test_artifact_and_event_types_distinct_from_action_evidence(
        self,
    ) -> None:
        action_evidence_artifact_types = {
            "scheduler_confirmation_consumption",
            "task_execution_package",
            "pr_handoff",
            "pr_handoff_package",
            "draft_pr",
            "branch_push",
            "local_cleanup",
            "remote_branch_cleanup",
            "task_closeout",
        }
        action_evidence_event_types = {
            "scheduler_confirmation_consumed",
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
        self.assertNotIn(HANDOFF_ARTIFACT_TYPE, action_evidence_artifact_types)
        self.assertNotIn(HANDOFF_EVENT_TYPE, action_evidence_event_types)


if __name__ == "__main__":
    unittest.main()
