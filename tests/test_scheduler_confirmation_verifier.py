from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_taskflow.models import TaskRecord
from agent_taskflow.scheduler_confirmation_verifier import (
    DEFAULT_EXPIRATION_MINUTES,
    STATUS_BLOCKED,
    STATUS_INVALID,
    STATUS_NOT_FOUND,
    STATUS_VALID,
    VERIFICATION_SCHEMA_VERSION,
    VERIFIER_SAFETY_FLAGS,
    SchedulerConfirmationVerificationRequest,
    SchedulerConfirmationVerifierError,
    verify_scheduler_confirmation_item,
)
from agent_taskflow.scheduler_confirmations import (
    SchedulerConfirmationRequest,
    create_scheduler_confirmation,
)
from agent_taskflow.scheduler_proposals import (
    SchedulerProposalRequest,
    compute_item_hash,
    compute_proposal_hash,
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
                title=f"verify task {task_key}",
                status="queued",
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )

    def _set_task_status(self, task_key: str, status: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_key = ?",
                (status, "2026-05-02T00:00:00Z", task_key),
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


class VerifierHappyPathTests(_Base):
    def test_valid_confirmation_marks_eligible_but_never_execution_allowed(
        self,
    ) -> None:
        proposal = self._proposal(["AT-VRF-OK-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))

        before = self._db_counts()

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )

        self.assertEqual(report["status"], STATUS_VALID)
        self.assertTrue(report["ok"])
        self.assertTrue(report["verification_passed"])
        self.assertTrue(report["eligible_for_command_specific_confirm"])
        # A verifier pass is NOT execution permission; these stay false.
        self.assertFalse(report["execution_allowed"])
        self.assertFalse(report["allowed_to_attempt"])
        self.assertFalse(report["execution_performed"])
        self.assertFalse(report["action_evidence_created"])
        self.assertEqual(report["schema_version"], VERIFICATION_SCHEMA_VERSION)
        self.assertEqual(report["proposal_item_id"], item_id)
        self.assertEqual(
            report["confirmation_id"], confirmation["confirmation_id"]
        )
        self.assertEqual(self._db_counts(), before)
        self.assertEqual(report["safety"], dict(VERIFIER_SAFETY_FLAGS))
        for check in report["checks"]:
            self.assertTrue(check["passed"], check)
        check_names = [check["name"] for check in report["checks"]]
        for name in (
            "bound_proposal_artifact_present",
            "bound_proposal_artifact_readable",
            "bound_proposal_schema_supported",
            "bound_proposal_id_matches",
            "bound_proposal_hash_matches",
            "bound_proposal_item_present",
            "bound_proposal_item_hash_matches",
        ):
            self.assertIn(name, check_names)
        self.assertTrue(report["revalidation"]["task_exists"])
        self.assertTrue(report["revalidation"]["current_item_hash_recomputed"])
        self.assertTrue(report["revalidation"]["current_item_hash_matches"])
        self.assertEqual(
            report["bound_proposal"]["recomputed_proposal_hash"],
            report["bound_proposal"]["artifact_proposal_hash"],
        )
        self.assertEqual(
            report["bound_proposal"]["artifact_item_hash"],
            report["bound_proposal"]["confirmation_item_hash"],
        )

    def test_safety_flags_always_mutation_false(self) -> None:
        proposal = self._proposal(["AT-VRF-SAFE-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )

        safety = report["safety"]
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
            "will_mutate_db",
            "will_mutate_github",
            "will_change_task_status",
            "will_start_background_worker",
        ):
            self.assertFalse(safety[key], key)
        self.assertTrue(safety["dry_run_only"])

    def test_emits_no_consumption_or_action_evidence(self) -> None:
        proposal = self._proposal(["AT-VRF-NOEV-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

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

        verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )

        with sqlite3.connect(self.db_path) as conn:
            new_artifact_types = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT artifact_type FROM task_artifacts"
                ).fetchall()
            }
            new_event_types = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT event_type FROM task_events"
                ).fetchall()
            }

        self.assertEqual(new_artifact_types, existing_artifact_types)
        self.assertEqual(new_event_types, existing_event_types)

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
        self.assertTrue(forbidden_artifacts.isdisjoint(new_artifact_types))
        self.assertTrue(forbidden_events.isdisjoint(new_event_types))


class VerifierBindingTests(_Base):
    def test_missing_confirmation_returns_not_found(self) -> None:
        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id="anything",
            )
        )
        self.assertEqual(report["status"], STATUS_NOT_FOUND)
        self.assertFalse(report["allowed_to_attempt"])
        self.assertFalse(report["ok"])

    def test_unsupported_schema_blocks_invalid(self) -> None:
        proposal = self._proposal(["AT-VRF-SCH-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))
        path = Path(confirmation["artifact_path"])  # type: ignore[index]
        on_disk = json.loads(path.read_text())
        on_disk["schema_version"] = "scheduler_confirmation.v999"
        path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_INVALID)
        self.assertFalse(report["allowed_to_attempt"])
        names = [c["name"] for c in report["checks"]]
        self.assertIn("confirmation_schema_supported", names)

    def test_unsafe_payload_blocks_invalid(self) -> None:
        proposal = self._proposal(["AT-VRF-UNSAFE-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))
        path = Path(confirmation["artifact_path"])  # type: ignore[index]
        on_disk = json.loads(path.read_text())
        on_disk["safety"]["execution_allowed"] = True
        path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_INVALID)
        failed_safety = next(
            c for c in report["checks"]
            if c["name"] == "confirmation_safety_payload_safe"
        )
        self.assertFalse(failed_safety["passed"])

    def test_missing_selected_item_blocks(self) -> None:
        proposal = self._proposal(["AT-VRF-NOITEM-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id="does-not-exist",
            )
        )
        self.assertEqual(report["status"], STATUS_BLOCKED)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "selected_proposal_item_present"
        )
        self.assertFalse(failed["passed"])

    def test_confirmation_item_hash_tamper_blocks_via_bound_proposal_check(
        self,
    ) -> None:
        proposal = self._proposal(["AT-VRF-HASH-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))
        path = Path(confirmation["artifact_path"])  # type: ignore[index]
        on_disk = json.loads(path.read_text())
        for item in on_disk["selected_items"]:
            if item["proposal_item_id"] == item_id:
                item["item_hash"] = "0" * 64
        path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_INVALID)
        names = [c["name"] for c in report["checks"]]
        self.assertIn("bound_proposal_item_hash_matches", names)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "bound_proposal_item_hash_matches"
        )
        self.assertFalse(failed["passed"])

    def test_task_key_mismatch_blocks(self) -> None:
        proposal = self._proposal(["AT-VRF-TK-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
                task_key="OTHER-TASK",
            )
        )
        self.assertEqual(report["status"], STATUS_BLOCKED)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "expected_task_key_matches"
        )
        self.assertFalse(failed["passed"])

    def test_expected_command_kind_mismatch_blocks(self) -> None:
        proposal = self._proposal(["AT-VRF-EK-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
                expected_command_kind="branch_push_review",
            )
        )
        self.assertEqual(report["status"], STATUS_BLOCKED)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "expected_command_kind_matches"
        )
        self.assertFalse(failed["passed"])


class VerifierNonConsumableKindTests(_Base):
    def _force_kind(
        self, confirmation_path: Path, item_id: str, kind: str
    ) -> None:
        on_disk = json.loads(confirmation_path.read_text())
        for item in on_disk["selected_items"]:
            if item["proposal_item_id"] == item_id:
                item["recommended_command_kind"] = kind
        confirmation_path.write_text(
            json.dumps(on_disk, indent=2, sort_keys=True)
        )

    def test_no_action_kind_blocks(self) -> None:
        proposal = self._proposal(["AT-VRF-NO-ACTION-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))
        self._force_kind(
            Path(confirmation["artifact_path"]),  # type: ignore[index]
            item_id,
            "no_action",
        )

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_BLOCKED)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "recommended_command_kind_is_consumable"
        )
        self.assertFalse(failed["passed"])

    def test_unknown_kind_blocks(self) -> None:
        proposal = self._proposal(["AT-VRF-UNK-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))
        self._force_kind(
            Path(confirmation["artifact_path"]),  # type: ignore[index]
            item_id,
            "unknown",
        )

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_BLOCKED)

    def test_human_pr_review_kind_blocks(self) -> None:
        proposal = self._proposal(["AT-VRF-HPRR-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))
        self._force_kind(
            Path(confirmation["artifact_path"]),  # type: ignore[index]
            item_id,
            "human_pr_review",
        )

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_BLOCKED)


class VerifierWarningTests(_Base):
    def _inject_warning(
        self, proposal_path: Path, confirmation_path: Path, item_id: str
    ) -> None:
        proposal_on_disk = json.loads(proposal_path.read_text())
        for item in proposal_on_disk["items"]:
            if item["proposal_item_id"] == item_id:
                item["consistency_warnings"] = ["synthetic"]
                item["item_hash"] = compute_item_hash(item)
        proposal_on_disk["proposal_hash"] = compute_proposal_hash(
            proposal_on_disk
        )
        proposal_path.write_text(
            json.dumps(proposal_on_disk, indent=2, sort_keys=True)
        )

        on_disk = json.loads(confirmation_path.read_text())
        on_disk["proposal"]["proposal_hash"] = proposal_on_disk["proposal_hash"]
        for item in on_disk["selected_items"]:
            if item["proposal_item_id"] == item_id:
                item["consistency_warnings"] = ["synthetic"]
                item["item_hash"] = next(
                    rec["item_hash"]
                    for rec in proposal_on_disk["items"]
                    if rec["proposal_item_id"] == item_id
                )
        confirmation_path.write_text(
            json.dumps(on_disk, indent=2, sort_keys=True)
        )

    def test_unacknowledged_warnings_block(self) -> None:
        proposal = self._proposal(["AT-VRF-WARN-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))
        self._inject_warning(
            Path(proposal["artifact_path"]),  # type: ignore[index]
            Path(confirmation["artifact_path"]),  # type: ignore[index]
            item_id,
        )

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_BLOCKED)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "confirmation_warnings_acknowledged"
        )
        self.assertFalse(failed["passed"])

    def test_acknowledged_warnings_pass_when_current_warnings_match(self) -> None:
        proposal = self._proposal(["AT-VRF-WARNACK-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(
            proposal, (item_id,), acknowledge_warnings=True
        )
        # operator_acknowledged_warnings stays false until item actually
        # has warnings; mutate the recorded artifact to mark it acked,
        # then inject a matching warning into the current recommendation.
        path = Path(confirmation["artifact_path"])  # type: ignore[index]
        on_disk = json.loads(path.read_text())
        for item in on_disk["selected_items"]:
            if item["proposal_item_id"] == item_id:
                item["consistency_warnings"] = ["fake-warning"]
                item["operator_acknowledged_warnings"] = True
        path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))

        # The current recommendation will not produce a matching warning
        # without seeding actual evidence, so this check should block on
        # warnings_match — but the acknowledgement check itself should
        # pass. Assert specifically.
        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        ack_check = next(
            c for c in report["checks"]
            if c["name"] == "confirmation_warnings_acknowledged"
        )
        self.assertTrue(ack_check["passed"])


class VerifierExpirationTests(_Base):
    def test_expired_confirmation_blocks(self) -> None:
        proposal = self._proposal(["AT-VRF-EXP-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))
        path = Path(confirmation["artifact_path"])  # type: ignore[index]
        on_disk = json.loads(path.read_text())
        on_disk["created_at"] = "2026-01-01T00:00:00Z"
        path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))

        # Use a very small override so even seconds-old confirmations
        # would not qualify — this isolates the expiration check.
        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
                max_age_minutes=1,
            )
        )
        self.assertEqual(report["status"], STATUS_BLOCKED)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "confirmation_not_expired"
        )
        self.assertFalse(failed["passed"])
        self.assertTrue(report["expiration"]["expired"])
        self.assertEqual(
            report["expiration"]["max_age_source"], "override_tightened"
        )
        self.assertEqual(report["expiration"]["max_age_minutes_override"], 1)
        self.assertEqual(report["expiration"]["effective_max_age_minutes"], 1)

    def test_unexpired_confirmation_passes_expiration(self) -> None:
        proposal = self._proposal(["AT-VRF-UNEXP-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        # Default expiration for create_task_execution_package is 30
        # minutes; verifier should treat a just-created confirmation as
        # fresh.
        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertFalse(report["expiration"]["expired"])
        self.assertEqual(
            report["expiration"]["max_age_minutes"],
            DEFAULT_EXPIRATION_MINUTES["create_task_execution_package"],
        )

    def test_explicit_now_override_marks_expired(self) -> None:
        proposal = self._proposal(["AT-VRF-NOW-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        future = datetime.now(timezone.utc) + timedelta(hours=2)
        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
                now=future,
            )
        )
        self.assertEqual(report["status"], STATUS_BLOCKED)
        self.assertTrue(report["expiration"]["expired"])


class VerifierDriftTests(_Base):
    def test_current_task_status_drift_blocks(self) -> None:
        proposal = self._proposal(["AT-VRF-STDR-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        # Drift the task status from "queued" to "waiting_approval"
        self._set_task_status("AT-VRF-STDR-001", "waiting_approval")

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_BLOCKED)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "task_status_matches_expected"
        )
        self.assertFalse(failed["passed"])

    def test_current_recommendation_kind_drift_blocks(self) -> None:
        proposal = self._proposal(["AT-VRF-KIND-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))

        # Force the confirmed item to claim the task is queued but
        # recommend a different kind than the recommendation will now
        # produce. We choose draft_pr_review which is not what the queued
        # recommendation would generate. Adjust the on-disk confirmation
        # to keep expected_status="queued" so the status check passes
        # first, then the kind check fails.
        path = Path(confirmation["artifact_path"])  # type: ignore[index]
        on_disk = json.loads(path.read_text())
        for item in on_disk["selected_items"]:
            if item["proposal_item_id"] == item_id:
                item["recommended_command_kind"] = "draft_pr_review"
        path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_BLOCKED)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "current_recommendation_kind_matches"
        )
        self.assertFalse(failed["passed"])

    def test_missing_task_blocks_revalidation(self) -> None:
        proposal = self._proposal(["AT-VRF-DEL-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        # Remove the task row to simulate "task no longer exists".
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM tasks WHERE task_key = ?", ("AT-VRF-DEL-001",)
            )

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_BLOCKED)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "task_exists_in_current_recommendations"
        )
        self.assertFalse(failed["passed"])


class VerifierSelectorTests(_Base):
    def test_requires_exactly_one_selector(self) -> None:
        with self.assertRaises(SchedulerConfirmationVerifierError):
            verify_scheduler_confirmation_item(
                SchedulerConfirmationVerificationRequest(
                    db_path=self.db_path,
                    proposal_item_id="foo",
                )
            )

    def test_more_than_one_selector_rejected(self) -> None:
        with self.assertRaises(SchedulerConfirmationVerifierError):
            verify_scheduler_confirmation_item(
                SchedulerConfirmationVerificationRequest(
                    db_path=self.db_path,
                    latest=True,
                    confirmation_id="confirmation-x",
                    proposal_item_id="foo",
                )
            )

    def test_lookup_by_confirmation_id(self) -> None:
        proposal = self._proposal(["AT-VRF-SEL-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                confirmation_id=confirmation["confirmation_id"],  # type: ignore[index]
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_VALID)

    def test_lookup_by_explicit_artifact_path(self) -> None:
        proposal = self._proposal(["AT-VRF-PATH-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                confirmation_artifact_path=Path(
                    confirmation["artifact_path"]  # type: ignore[index]
                ),
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_VALID)


class VerifierBoundProposalArtifactTests(_Base):
    def _mutate_confirmation(
        self,
        confirmation: dict[str, object],
        mutator,  # type: ignore[no-untyped-def]
    ) -> Path:
        path = Path(confirmation["artifact_path"])  # type: ignore[index]
        on_disk = json.loads(path.read_text())
        mutator(on_disk)
        path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))
        return path

    def _mutate_proposal(
        self,
        proposal: dict[str, object],
        mutator,  # type: ignore[no-untyped-def]
    ) -> Path:
        path = Path(proposal["artifact_path"])  # type: ignore[index]
        on_disk = json.loads(path.read_text())
        mutator(on_disk)
        path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))
        return path

    def test_missing_proposal_artifact_path_rejects(self) -> None:
        proposal = self._proposal(["AT-VRF-BIND-PATH-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))

        def drop_path(on_disk: dict[str, object]) -> None:
            del on_disk["proposal"]["proposal_artifact_path"]  # type: ignore[index]

        self._mutate_confirmation(confirmation, drop_path)

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_INVALID)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "bound_proposal_artifact_present"
        )
        self.assertFalse(failed["passed"])

    def test_proposal_artifact_file_missing_rejects(self) -> None:
        proposal = self._proposal(["AT-VRF-BIND-MISS-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))

        Path(proposal["artifact_path"]).unlink()  # type: ignore[index]

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_INVALID)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "bound_proposal_artifact_readable"
        )
        self.assertFalse(failed["passed"])

    def test_proposal_artifact_invalid_json_rejects(self) -> None:
        proposal = self._proposal(["AT-VRF-BIND-JSON-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        Path(proposal["artifact_path"]).write_text(  # type: ignore[index]
            "not valid json {",
            encoding="utf-8",
        )

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_INVALID)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "bound_proposal_artifact_readable"
        )
        self.assertFalse(failed["passed"])

    def test_proposal_schema_unsupported_rejects(self) -> None:
        proposal = self._proposal(["AT-VRF-BIND-SCH-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        def bad_schema(on_disk: dict[str, object]) -> None:
            on_disk["schema_version"] = "scheduler_proposal.v999"

        self._mutate_proposal(proposal, bad_schema)

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_INVALID)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "bound_proposal_schema_supported"
        )
        self.assertFalse(failed["passed"])

    def test_proposal_id_mismatch_rejects(self) -> None:
        proposal = self._proposal(["AT-VRF-BIND-PID-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))

        def swap_pid(on_disk: dict[str, object]) -> None:
            on_disk["proposal"]["proposal_id"] = (  # type: ignore[index]
                "proposal-tampered-deadbeef"
            )

        self._mutate_confirmation(confirmation, swap_pid)

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_INVALID)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "bound_proposal_id_matches"
        )
        self.assertFalse(failed["passed"])

    def test_proposal_hash_tamper_rejects(self) -> None:
        proposal = self._proposal(["AT-VRF-BIND-PHASH-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        def tamper_hash(on_disk: dict[str, object]) -> None:
            on_disk["proposal_hash"] = "f" * 64

        self._mutate_proposal(proposal, tamper_hash)

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_INVALID)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "bound_proposal_hash_matches"
        )
        self.assertFalse(failed["passed"])

    def test_proposal_item_id_absent_rejects(self) -> None:
        proposal = self._proposal(["AT-VRF-BIND-ABS-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        def drop_item(on_disk: dict[str, object]) -> None:
            on_disk["items"] = [
                entry
                for entry in on_disk["items"]  # type: ignore[index]
                if entry.get("proposal_item_id") != item_id  # type: ignore[union-attr]
            ]
            on_disk["proposal_hash"] = compute_proposal_hash(on_disk)

        self._mutate_proposal(proposal, drop_item)

        # Re-align confirmation.proposal.proposal_hash to match the
        # re-hashed proposal artifact so the absent-item check is what
        # short-circuits, not the proposal_hash check.
        confirmation_path = next(
            iter(
                (self.artifact_root / "scheduler_confirmations").iterdir()
            )
        ) / "scheduler_confirmation.json"
        on_disk_conf = json.loads(confirmation_path.read_text())
        on_disk_conf["proposal"]["proposal_hash"] = json.loads(
            Path(proposal["artifact_path"]).read_text()  # type: ignore[index]
        )["proposal_hash"]
        confirmation_path.write_text(
            json.dumps(on_disk_conf, indent=2, sort_keys=True)
        )

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_BLOCKED)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "bound_proposal_item_present"
        )
        self.assertFalse(failed["passed"])
        self.assertIn("not present", failed["detail"])

    def test_duplicate_proposal_item_id_rejects(self) -> None:
        proposal = self._proposal(["AT-VRF-BIND-DUP-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        def duplicate(on_disk: dict[str, object]) -> None:
            items = on_disk["items"]  # type: ignore[index]
            original = next(
                entry
                for entry in items  # type: ignore[union-attr]
                if entry.get("proposal_item_id") == item_id  # type: ignore[union-attr]
            )
            items.append(dict(original))  # type: ignore[union-attr]
            on_disk["proposal_hash"] = compute_proposal_hash(on_disk)

        self._mutate_proposal(proposal, duplicate)

        # Re-align confirmation.proposal.proposal_hash so the duplicate
        # check is what short-circuits, not the proposal_hash check.
        confirmation_path = next(
            iter(
                (self.artifact_root / "scheduler_confirmations").iterdir()
            )
        ) / "scheduler_confirmation.json"
        on_disk_conf = json.loads(confirmation_path.read_text())
        on_disk_conf["proposal"]["proposal_hash"] = json.loads(
            Path(proposal["artifact_path"]).read_text()  # type: ignore[index]
        )["proposal_hash"]
        confirmation_path.write_text(
            json.dumps(on_disk_conf, indent=2, sort_keys=True)
        )

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_BLOCKED)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "bound_proposal_item_present"
        )
        self.assertFalse(failed["passed"])
        self.assertIn("duplicated", failed["detail"])

    def test_proposal_item_hash_mismatch_rejects(self) -> None:
        proposal = self._proposal(["AT-VRF-BIND-ITHASH-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        def tamper_item_hash(on_disk: dict[str, object]) -> None:
            for entry in on_disk["items"]:  # type: ignore[index]
                if entry.get("proposal_item_id") == item_id:
                    entry["item_hash"] = "a" * 64
            on_disk["proposal_hash"] = compute_proposal_hash(on_disk)

        self._mutate_proposal(proposal, tamper_item_hash)

        # Re-align confirmation.proposal.proposal_hash so the
        # item_hash mismatch is what short-circuits.
        confirmation_path = next(
            iter(
                (self.artifact_root / "scheduler_confirmations").iterdir()
            )
        ) / "scheduler_confirmation.json"
        on_disk_conf = json.loads(confirmation_path.read_text())
        on_disk_conf["proposal"]["proposal_hash"] = json.loads(
            Path(proposal["artifact_path"]).read_text()  # type: ignore[index]
        )["proposal_hash"]
        confirmation_path.write_text(
            json.dumps(on_disk_conf, indent=2, sort_keys=True)
        )

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_INVALID)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "bound_proposal_item_hash_matches"
        )
        self.assertFalse(failed["passed"])


class VerifierGenericSafetyTests(_Base):
    def _force_safety(
        self, confirmation: dict[str, object], key: str, value: object
    ) -> Path:
        path = Path(confirmation["artifact_path"])  # type: ignore[index]
        on_disk = json.loads(path.read_text())
        on_disk["safety"][key] = value
        path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))
        return path

    def test_unknown_will_flag_true_blocks(self) -> None:
        proposal = self._proposal(["AT-VRF-SAF-WILL-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))
        self._force_safety(confirmation, "will_foo", True)

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_INVALID)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "confirmation_safety_payload_safe"
        )
        self.assertFalse(failed["passed"])
        self.assertIn("will_foo", failed["detail"])

    def test_unknown_performed_flag_true_blocks(self) -> None:
        proposal = self._proposal(["AT-VRF-SAF-PERF-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))
        self._force_safety(confirmation, "some_action_performed", True)

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_INVALID)
        failed = next(
            c for c in report["checks"]
            if c["name"] == "confirmation_safety_payload_safe"
        )
        self.assertFalse(failed["passed"])
        self.assertIn("some_action_performed", failed["detail"])

    def test_truthy_non_bool_will_flag_blocks(self) -> None:
        proposal = self._proposal(["AT-VRF-SAF-NB-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))
        # ``1`` is truthy but ``is False`` short-circuits the legacy
        # truthy checks above by happening on a key those don't list.
        self._force_safety(confirmation, "will_smuggle", 1)

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_INVALID)


class VerifierExpirationHardeningTests(_Base):
    def test_override_greater_than_default_does_not_loosen_ttl(self) -> None:
        proposal = self._proposal(["AT-VRF-EXP-CAP-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))

        # Set created_at to default + 5 minutes in the past. With the
        # default TTL of 30 minutes the confirmation is fresh; an
        # attempt to loosen TTL via override=1440 (24h) must NOT make
        # it last longer than the default.
        path = Path(confirmation["artifact_path"])  # type: ignore[index]
        on_disk = json.loads(path.read_text())
        old = datetime.now(timezone.utc) - timedelta(
            minutes=DEFAULT_EXPIRATION_MINUTES["create_task_execution_package"]
            + 5
        )
        on_disk["created_at"] = (
            old.isoformat().replace("+00:00", "Z")
        )
        path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
                max_age_minutes=24 * 60,  # absurdly long override
            )
        )
        self.assertEqual(report["status"], STATUS_BLOCKED)
        self.assertTrue(report["expiration"]["expired"])
        self.assertEqual(
            report["expiration"]["max_age_source"], "default_capped_override"
        )
        self.assertEqual(
            report["expiration"]["effective_max_age_minutes"],
            DEFAULT_EXPIRATION_MINUTES["create_task_execution_package"],
        )
        self.assertEqual(
            report["expiration"]["default_max_age_minutes"],
            DEFAULT_EXPIRATION_MINUTES["create_task_execution_package"],
        )
        self.assertEqual(
            report["expiration"]["max_age_minutes_override"], 24 * 60
        )

    def test_override_less_than_default_tightens_ttl(self) -> None:
        proposal = self._proposal(["AT-VRF-EXP-TIGHT-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))

        # created_at is 10 minutes ago; default TTL=30 → fresh.
        # Override TTL to 5 minutes → expired.
        path = Path(confirmation["artifact_path"])  # type: ignore[index]
        on_disk = json.loads(path.read_text())
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        on_disk["created_at"] = old.isoformat().replace("+00:00", "Z")
        path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
                max_age_minutes=5,
            )
        )
        self.assertEqual(report["status"], STATUS_BLOCKED)
        self.assertTrue(report["expiration"]["expired"])
        self.assertEqual(
            report["expiration"]["max_age_source"], "override_tightened"
        )
        self.assertEqual(
            report["expiration"]["effective_max_age_minutes"], 5
        )

    def test_future_created_at_rejects(self) -> None:
        proposal = self._proposal(["AT-VRF-EXP-FUT-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))

        future = datetime.now(timezone.utc) + timedelta(hours=1)
        path = Path(confirmation["artifact_path"])  # type: ignore[index]
        on_disk = json.loads(path.read_text())
        on_disk["created_at"] = future.isoformat().replace("+00:00", "Z")
        path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_BLOCKED)
        self.assertTrue(report["expiration"]["expired"])
        self.assertEqual(
            report["expiration"]["detail"],
            "confirmation.created_at is in the future",
        )


class VerifierOutputSemanticTests(_Base):
    def test_all_outcomes_always_disclaim_execution(self) -> None:
        # Cover three statuses: VALID, BLOCKED, NOT_FOUND.
        proposal = self._proposal(["AT-VRF-OUT-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        valid = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        blocked = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id="nonexistent",
            )
        )
        # Build a fresh DB to force NOT_FOUND.
        empty_db = self.root / "empty.db"
        TaskMirrorStore(empty_db).init_db()
        not_found = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=empty_db,
                latest=True,
                proposal_item_id=item_id,
            )
        )

        for report in (valid, blocked, not_found):
            self.assertFalse(report["execution_allowed"], report)
            self.assertFalse(report["execution_performed"], report)
            self.assertFalse(report["action_evidence_created"], report)
            self.assertFalse(report["allowed_to_attempt"], report)

        self.assertEqual(valid["status"], STATUS_VALID)
        self.assertTrue(valid["verification_passed"])
        self.assertTrue(valid["eligible_for_command_specific_confirm"])
        self.assertTrue(valid["ok"])

        self.assertEqual(blocked["status"], STATUS_BLOCKED)
        self.assertFalse(blocked["verification_passed"])
        self.assertFalse(blocked["eligible_for_command_specific_confirm"])
        self.assertFalse(blocked["ok"])

        self.assertEqual(not_found["status"], STATUS_NOT_FOUND)
        self.assertFalse(not_found["verification_passed"])
        self.assertFalse(not_found["eligible_for_command_specific_confirm"])
        self.assertFalse(not_found["ok"])

    def test_ok_is_false_for_invalid_status(self) -> None:
        proposal = self._proposal(["AT-VRF-OUT-OKINV-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))

        # Corrupt the schema_version so the confirmation is INVALID.
        path = Path(confirmation["artifact_path"])  # type: ignore[index]
        on_disk = json.loads(path.read_text())
        on_disk["schema_version"] = "scheduler_confirmation.v999"
        path.write_text(json.dumps(on_disk, indent=2, sort_keys=True))

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_INVALID)
        self.assertFalse(report["ok"])
        self.assertFalse(report["verification_passed"])
        self.assertFalse(report["eligible_for_command_specific_confirm"])

    def test_verifier_writes_no_db_events_or_artifacts(self) -> None:
        proposal = self._proposal(["AT-VRF-OUT-NOWRITE-001"])
        item_id = self._safe_item_id(proposal)
        self._confirm(proposal, (item_id,))

        before_counts = self._db_counts()
        with sqlite3.connect(self.db_path) as conn:
            before_event_ids = {
                row[0]
                for row in conn.execute(
                    "SELECT id FROM task_events"
                ).fetchall()
            }
            before_artifact_ids = {
                row[0]
                for row in conn.execute(
                    "SELECT id FROM task_artifacts"
                ).fetchall()
            }

        verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                latest=True,
                proposal_item_id=item_id,
            )
        )

        with sqlite3.connect(self.db_path) as conn:
            after_event_ids = {
                row[0]
                for row in conn.execute(
                    "SELECT id FROM task_events"
                ).fetchall()
            }
            after_artifact_ids = {
                row[0]
                for row in conn.execute(
                    "SELECT id FROM task_artifacts"
                ).fetchall()
            }

        self.assertEqual(self._db_counts(), before_counts)
        self.assertEqual(before_event_ids, after_event_ids)
        self.assertEqual(before_artifact_ids, after_artifact_ids)


class VerifierConfirmationLookupTests(_Base):
    def test_confirmation_id_lookup_ignores_unrelated_artifacts(self) -> None:
        proposal_a = self._proposal(["AT-VRF-LK-A-001"])
        item_a = self._safe_item_id(proposal_a)
        confirmation_a = self._confirm(proposal_a, (item_a,))

        proposal_b = self._proposal(["AT-VRF-LK-B-001"])
        item_b = self._safe_item_id(proposal_b)
        confirmation_b = self._confirm(proposal_b, (item_b,))

        # Use the confirmation_id of A while querying — verifier must
        # pick A's artifact even though B has been recorded later.
        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                confirmation_id=confirmation_a["confirmation_id"],  # type: ignore[index]
                proposal_item_id=item_a,
            )
        )
        self.assertEqual(report["status"], STATUS_VALID)
        self.assertEqual(
            report["confirmation_id"],
            confirmation_a["confirmation_id"],  # type: ignore[index]
        )
        self.assertNotEqual(
            report["confirmation_id"],
            confirmation_b["confirmation_id"],  # type: ignore[index]
        )

    def test_confirmation_id_lookup_skips_unreadable_candidate(self) -> None:
        proposal = self._proposal(["AT-VRF-LK-SKIP-001"])
        item_id = self._safe_item_id(proposal)
        confirmation = self._confirm(proposal, (item_id,))

        # Insert a stale artifact row that points to a missing file
        # under the confirmation artifact type so the verifier sees
        # both the unreadable candidate and the real one.
        bogus = self.artifact_root / "scheduler_confirmations" / "missing.json"
        self.store.record_task_artifact(
            "AT-VRF-LK-SKIP-001",
            "scheduler_confirmation",
            bogus,
        )

        report = verify_scheduler_confirmation_item(
            SchedulerConfirmationVerificationRequest(
                db_path=self.db_path,
                confirmation_id=confirmation["confirmation_id"],  # type: ignore[index]
                proposal_item_id=item_id,
            )
        )
        self.assertEqual(report["status"], STATUS_VALID)


if __name__ == "__main__":
    unittest.main()
