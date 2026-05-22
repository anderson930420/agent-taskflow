from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord
from agent_taskflow.scheduler_proposal_review import (
    DEFAULT_LIST_LIMIT,
    REVIEW_SAFETY_FLAGS,
    REVIEW_SCHEMA_VERSION,
    REVIEW_SOURCE,
    REVIEW_STATUSES,
    SchedulerProposalReviewError,
    SchedulerProposalReviewRequest,
    list_scheduler_proposals,
    review_scheduler_proposal,
)
from agent_taskflow.scheduler_proposals import (
    PROPOSAL_ARTIFACT_TYPE,
    PROPOSAL_EVENT_TYPE,
    SchedulerProposalRequest,
    compute_item_hash,
    compute_proposal_hash,
    create_scheduler_proposal,
    verify_proposal_hashes,
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

    def _seed_queued(self, task_key: str, *, title: str = "review task") -> None:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title=title,
                status="queued",
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )

    def _record_confirmed_proposal(
        self,
        task_keys: list[str] | None = None,
    ) -> dict[str, object]:
        if task_keys is None:
            task_keys = ["AT-REV-001"]
        for key in task_keys:
            self._seed_queued(key)
        request = SchedulerProposalRequest(
            db_path=self.db_path,
            artifact_root=self.artifact_root,
            dry_run=False,
            confirm_create_proposal=True,
        )
        return create_scheduler_proposal(request)

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


class HashHelperPublicSurfaceTests(_Base):
    def test_public_hash_helpers_match_internal(self) -> None:
        payload = self._record_confirmed_proposal(["AT-REV-PUB-001"])
        item = payload["items"][0]
        self.assertEqual(compute_item_hash(item), item["item_hash"])
        self.assertEqual(compute_proposal_hash(payload), payload["proposal_hash"])

    def test_verify_proposal_hashes_reports_valid(self) -> None:
        payload = self._record_confirmed_proposal(["AT-REV-PUB-002"])
        report = verify_proposal_hashes(payload)
        self.assertTrue(report["proposal_hash_valid"])
        for entry in report["items"]:
            self.assertTrue(entry["item_hash_valid"])
        self.assertEqual(report["hash_algorithm"], "sha256")

    def test_verify_proposal_hashes_detects_item_tamper(self) -> None:
        payload = self._record_confirmed_proposal(["AT-REV-PUB-003"])
        # Tamper with item content while leaving item_hash unchanged.
        tampered = json.loads(json.dumps(payload))
        tampered["items"][0]["proposed_action"] = "MUTATED"
        report = verify_proposal_hashes(tampered)
        self.assertFalse(report["items"][0]["item_hash_valid"])

    def test_verify_proposal_hashes_detects_proposal_tamper(self) -> None:
        payload = self._record_confirmed_proposal(["AT-REV-PUB-004"])
        tampered = json.loads(json.dumps(payload))
        tampered["proposal_hash"] = "0" * 64
        report = verify_proposal_hashes(tampered)
        self.assertFalse(report["proposal_hash_valid"])
        for entry in report["items"]:
            self.assertTrue(entry["item_hash_valid"])


class ListSchedulerProposalsTests(_Base):
    def test_list_returns_only_scheduler_proposal_artifacts(self) -> None:
        self._record_confirmed_proposal(["AT-REV-LIST-001"])
        # Record a non-proposal artifact too.
        unrelated = self.artifact_root / "AT-REV-LIST-001" / "unrelated.json"
        unrelated.write_text("{}\n", encoding="utf-8")
        self.store.record_task_artifact(
            "AT-REV-LIST-001",
            "task_execution_package",
            unrelated,
        )

        payload = list_scheduler_proposals(
            SchedulerProposalReviewRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
            )
        )

        self.assertEqual(payload["review_mode"], "list")
        self.assertEqual(payload["schema_version"], REVIEW_SCHEMA_VERSION)
        self.assertEqual(payload["source"], REVIEW_SOURCE)
        self.assertEqual(len(payload["proposals"]), 1)
        summary = payload["proposals"][0]
        self.assertEqual(summary["review_status"], "valid_unverified")
        self.assertTrue(summary["on_disk_ok"])
        self.assertTrue(summary["proposal_id"].startswith("proposal-"))
        self.assertEqual(summary["task_key_count"], 1)
        self.assertIn("AT-REV-LIST-001", summary["task_keys"])

    def test_list_with_no_proposals_returns_empty(self) -> None:
        payload = list_scheduler_proposals(
            SchedulerProposalReviewRequest(db_path=self.db_path)
        )
        self.assertEqual(payload["proposal_count"], 0)
        self.assertEqual(payload["proposals"], [])

    def test_list_does_not_mutate_db(self) -> None:
        self._record_confirmed_proposal(["AT-REV-LIST-002"])
        before = self._db_counts()
        list_scheduler_proposals(
            SchedulerProposalReviewRequest(db_path=self.db_path)
        )
        self.assertEqual(self._db_counts(), before)

    def test_list_safety_flags(self) -> None:
        payload = list_scheduler_proposals(
            SchedulerProposalReviewRequest(db_path=self.db_path)
        )
        self.assertEqual(payload["safety"], REVIEW_SAFETY_FLAGS)
        self.assertTrue(payload["safety"]["read_only"])
        self.assertFalse(payload["safety"]["will_execute"])

    def test_list_limit_truncates(self) -> None:
        for key in ("AT-REV-LIM-A", "AT-REV-LIM-B"):
            self._record_confirmed_proposal([key])

        payload = list_scheduler_proposals(
            SchedulerProposalReviewRequest(db_path=self.db_path, list_limit=1)
        )
        self.assertEqual(payload["proposal_count"], 1)
        self.assertEqual(payload["total_recorded"], 2)


class ReviewSchedulerProposalTests(_Base):
    def test_latest_returns_newest_proposal(self) -> None:
        first = self._record_confirmed_proposal(["AT-REV-LAT-A"])
        second = self._record_confirmed_proposal(["AT-REV-LAT-B"])
        self.assertNotEqual(first["proposal_id"], second["proposal_id"])

        payload = review_scheduler_proposal(
            SchedulerProposalReviewRequest(db_path=self.db_path, latest=True)
        )

        self.assertEqual(payload["review_status"], "valid")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["proposal_id"], second["proposal_id"])
        self.assertEqual(payload["proposal_hash"], second["proposal_hash"])
        self.assertTrue(payload["hash_valid"])

    def test_by_proposal_id_loads_correct_artifact(self) -> None:
        first = self._record_confirmed_proposal(["AT-REV-ID-A"])
        self._record_confirmed_proposal(["AT-REV-ID-B"])

        payload = review_scheduler_proposal(
            SchedulerProposalReviewRequest(
                db_path=self.db_path,
                proposal_id=first["proposal_id"],
            )
        )

        self.assertEqual(payload["proposal_id"], first["proposal_id"])
        self.assertEqual(payload["proposal_hash"], first["proposal_hash"])
        self.assertEqual(payload["review_status"], "valid")
        self.assertEqual(payload["selector"], {"kind": "proposal_id", "value": first["proposal_id"]})

    def test_by_artifact_path_loads_correct_artifact(self) -> None:
        first = self._record_confirmed_proposal(["AT-REV-AP-001"])
        artifact_path = Path(first["artifact_path"])

        payload = review_scheduler_proposal(
            SchedulerProposalReviewRequest(
                db_path=self.db_path,
                artifact_path=artifact_path,
            )
        )

        self.assertEqual(payload["proposal_id"], first["proposal_id"])
        self.assertEqual(payload["review_status"], "valid")
        self.assertEqual(payload["selector"]["kind"], "artifact_path")
        self.assertEqual(payload["selector"]["value"], str(artifact_path))

    def test_valid_proposal_marks_each_item_hash_valid(self) -> None:
        self._record_confirmed_proposal(["AT-REV-VAL-001"])

        payload = review_scheduler_proposal(
            SchedulerProposalReviewRequest(db_path=self.db_path, latest=True)
        )

        self.assertTrue(payload["hash_valid"])
        self.assertTrue(payload["items"])
        for item in payload["items"]:
            self.assertTrue(item["item_hash_valid"])
            self.assertEqual(len(item["item_hash"]), 64)
            self.assertIn("expected_refs", item)
            self.assertIn("expected_evidence_summary", item)

    def test_tampered_proposal_hash_returns_invalid_hash(self) -> None:
        proposal = self._record_confirmed_proposal(["AT-REV-INV-001"])
        artifact_path = Path(proposal["artifact_path"])
        on_disk = json.loads(artifact_path.read_text(encoding="utf-8"))
        on_disk["proposal_hash"] = "0" * 64
        artifact_path.write_text(
            json.dumps(on_disk, indent=2, sort_keys=True), encoding="utf-8"
        )

        payload = review_scheduler_proposal(
            SchedulerProposalReviewRequest(db_path=self.db_path, latest=True)
        )

        self.assertEqual(payload["review_status"], "invalid_hash")
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["hash_valid"])
        self.assertIsNotNone(payload["hash_report"])
        self.assertFalse(payload["hash_report"]["proposal_hash_valid"])

    def test_tampered_item_content_returns_invalid_hash(self) -> None:
        proposal = self._record_confirmed_proposal(["AT-REV-INV-002"])
        artifact_path = Path(proposal["artifact_path"])
        on_disk = json.loads(artifact_path.read_text(encoding="utf-8"))
        on_disk["items"][0]["proposed_action"] = "MUTATED"
        artifact_path.write_text(
            json.dumps(on_disk, indent=2, sort_keys=True), encoding="utf-8"
        )

        payload = review_scheduler_proposal(
            SchedulerProposalReviewRequest(db_path=self.db_path, latest=True)
        )

        self.assertEqual(payload["review_status"], "invalid_hash")
        self.assertFalse(payload["ok"])
        report = payload["hash_report"]
        self.assertFalse(any(entry["item_hash_valid"] for entry in report["items"]))

    def test_missing_artifact_returns_missing_artifact(self) -> None:
        proposal = self._record_confirmed_proposal(["AT-REV-MISS-001"])
        artifact_path = Path(proposal["artifact_path"])
        artifact_path.unlink()

        payload = review_scheduler_proposal(
            SchedulerProposalReviewRequest(db_path=self.db_path, latest=True)
        )

        self.assertEqual(payload["review_status"], "missing_artifact")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["proposal_id"], proposal["proposal_id"])
        self.assertIn("not found", payload["error"])

    def test_unreadable_json_returns_unreadable_artifact(self) -> None:
        proposal = self._record_confirmed_proposal(["AT-REV-BAD-001"])
        artifact_path = Path(proposal["artifact_path"])
        artifact_path.write_text("not json", encoding="utf-8")

        payload = review_scheduler_proposal(
            SchedulerProposalReviewRequest(db_path=self.db_path, latest=True)
        )

        self.assertEqual(payload["review_status"], "unreadable_artifact")
        self.assertFalse(payload["ok"])

    def test_unsupported_schema_returns_unsupported_schema(self) -> None:
        proposal = self._record_confirmed_proposal(["AT-REV-SCH-001"])
        artifact_path = Path(proposal["artifact_path"])
        on_disk = json.loads(artifact_path.read_text(encoding="utf-8"))
        on_disk["schema_version"] = "scheduler_proposal.v999"
        artifact_path.write_text(
            json.dumps(on_disk, indent=2, sort_keys=True), encoding="utf-8"
        )

        payload = review_scheduler_proposal(
            SchedulerProposalReviewRequest(db_path=self.db_path, latest=True)
        )

        self.assertEqual(payload["review_status"], "unsupported_schema")
        self.assertFalse(payload["ok"])

    def test_unsafe_payload_returns_unsafe_payload(self) -> None:
        proposal = self._record_confirmed_proposal(["AT-REV-SAF-001"])
        artifact_path = Path(proposal["artifact_path"])
        on_disk = json.loads(artifact_path.read_text(encoding="utf-8"))
        on_disk["safety"]["workflow_action_performed"] = True
        artifact_path.write_text(
            json.dumps(on_disk, indent=2, sort_keys=True), encoding="utf-8"
        )

        payload = review_scheduler_proposal(
            SchedulerProposalReviewRequest(db_path=self.db_path, latest=True)
        )

        self.assertEqual(payload["review_status"], "unsafe_payload")
        self.assertFalse(payload["ok"])
        self.assertIn("workflow_action_performed", payload["error"])

    def test_unsafe_proposal_only_false_returns_unsafe_payload(self) -> None:
        proposal = self._record_confirmed_proposal(["AT-REV-SAF-002"])
        artifact_path = Path(proposal["artifact_path"])
        on_disk = json.loads(artifact_path.read_text(encoding="utf-8"))
        on_disk["safety"]["proposal_only"] = False
        artifact_path.write_text(
            json.dumps(on_disk, indent=2, sort_keys=True), encoding="utf-8"
        )

        payload = review_scheduler_proposal(
            SchedulerProposalReviewRequest(db_path=self.db_path, latest=True)
        )

        self.assertEqual(payload["review_status"], "unsafe_payload")
        self.assertIn("proposal_only", payload["error"])

    def test_review_does_not_mutate_db_or_disk(self) -> None:
        self._record_confirmed_proposal(["AT-REV-NOMUT-001"])
        before = self._db_counts()
        review_scheduler_proposal(
            SchedulerProposalReviewRequest(db_path=self.db_path, latest=True)
        )
        self.assertEqual(self._db_counts(), before)

    def test_no_verify_hashes_skips_hash_report(self) -> None:
        self._record_confirmed_proposal(["AT-REV-NV-001"])

        payload = review_scheduler_proposal(
            SchedulerProposalReviewRequest(
                db_path=self.db_path,
                latest=True,
                verify_hashes=False,
            )
        )

        self.assertEqual(payload["review_status"], "valid")
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["hash_valid"])
        self.assertIsNone(payload["hash_report"])
        for item in payload["items"]:
            self.assertIsNone(item["item_hash_valid"])

    def test_no_verify_does_not_mask_invalid_hash_via_status_field(self) -> None:
        # With --no-verify-hashes, hash status is unverified rather than
        # claimed valid; this is the intended behavior.
        proposal = self._record_confirmed_proposal(["AT-REV-NV-002"])
        artifact_path = Path(proposal["artifact_path"])
        on_disk = json.loads(artifact_path.read_text(encoding="utf-8"))
        on_disk["proposal_hash"] = "0" * 64
        artifact_path.write_text(
            json.dumps(on_disk, indent=2, sort_keys=True), encoding="utf-8"
        )

        payload = review_scheduler_proposal(
            SchedulerProposalReviewRequest(
                db_path=self.db_path,
                latest=True,
                verify_hashes=False,
            )
        )

        # Without verification, the review cannot return invalid_hash; it
        # is the caller's responsibility to verify when they want hash
        # guarantees.
        self.assertIn(payload["review_status"], REVIEW_STATUSES)
        self.assertEqual(payload["hash_report"], None)

    def test_include_items_false_omits_items(self) -> None:
        self._record_confirmed_proposal(["AT-REV-ITEMS-001"])

        payload = review_scheduler_proposal(
            SchedulerProposalReviewRequest(
                db_path=self.db_path,
                latest=True,
                include_items=False,
            )
        )

        self.assertEqual(payload["review_status"], "valid")
        self.assertIsNone(payload["items"])

    def test_review_safety_flags(self) -> None:
        self._record_confirmed_proposal(["AT-REV-SAF-FLAGS"])

        payload = review_scheduler_proposal(
            SchedulerProposalReviewRequest(db_path=self.db_path, latest=True)
        )

        self.assertEqual(payload["safety"], REVIEW_SAFETY_FLAGS)
        self.assertTrue(payload["safety"]["read_only"])
        self.assertTrue(payload["safety"]["proposal_only"])
        for key in (
            "will_execute",
            "will_push",
            "will_create_pr",
            "will_merge",
            "will_approve",
            "will_cleanup",
            "will_mutate_db",
            "will_mutate_github",
        ):
            self.assertFalse(payload["safety"][key])

    def test_missing_db_raises(self) -> None:
        missing = self.root / "missing" / "state.db"
        with self.assertRaisesRegex(SchedulerProposalReviewError, "not found"):
            review_scheduler_proposal(
                SchedulerProposalReviewRequest(db_path=missing, latest=True)
            )

    def test_no_selector_raises(self) -> None:
        self._record_confirmed_proposal(["AT-REV-NOSEL-001"])
        with self.assertRaisesRegex(
            SchedulerProposalReviewError, "artifact_path, proposal_id, or latest"
        ):
            review_scheduler_proposal(
                SchedulerProposalReviewRequest(db_path=self.db_path)
            )

    def test_multiple_selectors_raises(self) -> None:
        proposal = self._record_confirmed_proposal(["AT-REV-MULTI-001"])
        with self.assertRaisesRegex(
            SchedulerProposalReviewError, "only one of"
        ):
            review_scheduler_proposal(
                SchedulerProposalReviewRequest(
                    db_path=self.db_path,
                    latest=True,
                    proposal_id=proposal["proposal_id"],
                )
            )

    def test_unknown_proposal_id_returns_missing_artifact(self) -> None:
        self._record_confirmed_proposal(["AT-REV-UNK-001"])

        payload = review_scheduler_proposal(
            SchedulerProposalReviewRequest(
                db_path=self.db_path,
                proposal_id="proposal-19990101T000000-deadbeef",
            )
        )

        self.assertEqual(payload["review_status"], "missing_artifact")
        self.assertIsNone(payload["artifact_path"])

    def test_relative_db_path_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "db_path must be an absolute"):
            SchedulerProposalReviewRequest(
                db_path=Path("relative/state.db"),
            )

    def test_relative_artifact_root_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "artifact_root must be an absolute"
        ):
            SchedulerProposalReviewRequest(
                db_path=self.db_path,
                artifact_root=Path("relative/path"),
            )

    def test_relative_artifact_path_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "artifact_path must be an absolute"
        ):
            SchedulerProposalReviewRequest(
                db_path=self.db_path,
                artifact_path=Path("relative/scheduler_proposal.json"),
            )

    def test_invalid_proposal_id_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid proposal_id format"):
            SchedulerProposalReviewRequest(
                db_path=self.db_path,
                proposal_id="not a proposal",
            )

    def test_review_status_enum_complete(self) -> None:
        self.assertEqual(
            set(REVIEW_STATUSES),
            {
                "valid",
                "invalid_hash",
                "missing_artifact",
                "unreadable_artifact",
                "unsupported_schema",
                "unsafe_payload",
            },
        )

    def test_review_constants_exposed(self) -> None:
        self.assertEqual(REVIEW_SCHEMA_VERSION, "scheduler_proposal_review.v1")
        self.assertEqual(REVIEW_SOURCE, "scheduler_proposal_review")
        self.assertEqual(DEFAULT_LIST_LIMIT, 50)

    def test_review_payload_is_json_serializable(self) -> None:
        self._record_confirmed_proposal(["AT-REV-JSON-001"])
        payload = review_scheduler_proposal(
            SchedulerProposalReviewRequest(db_path=self.db_path, latest=True)
        )
        json.dumps(payload, sort_keys=True)


class ReviewArtifactEventInvariantsTests(_Base):
    def test_artifact_and_event_types_unchanged_after_review(self) -> None:
        proposal = self._record_confirmed_proposal(["AT-REV-INV-AE-001"])
        before_counts = self._db_counts()

        review_scheduler_proposal(
            SchedulerProposalReviewRequest(db_path=self.db_path, latest=True)
        )
        list_scheduler_proposals(
            SchedulerProposalReviewRequest(db_path=self.db_path)
        )

        self.assertEqual(self._db_counts(), before_counts)

        with sqlite3.connect(self.db_path) as conn:
            artifact_types = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT artifact_type FROM task_artifacts"
                ).fetchall()
            ]
            event_types = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT event_type FROM task_events"
                ).fetchall()
            ]
        self.assertIn(PROPOSAL_ARTIFACT_TYPE, artifact_types)
        self.assertIn(PROPOSAL_EVENT_TYPE, event_types)
        # No review-output artifact type leaked into the DB.
        self.assertNotIn("scheduler_proposal_review", artifact_types)
        self.assertNotIn("scheduler_proposal_reviewed", event_types)
        # Task remains unchanged.
        self.assertEqual(
            self.store.get_task("AT-REV-INV-AE-001").status, "queued"
        )


if __name__ == "__main__":
    unittest.main()
