from __future__ import annotations

import importlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_taskflow.models import TaskRecord
from agent_taskflow.scheduler_candidate_proposals import (
    SchedulerCandidateProposalRequest,
    candidate_proposal_safety,
    create_scheduler_proposal_from_candidate,
    propose_candidate_task,
)
from agent_taskflow.scheduler_proposals import (
    PROPOSAL_ARTIFACT_TYPE,
    PROPOSAL_EVENT_TYPE,
)
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_ARTIFACT_TYPES = (
    "scheduler_confirmation",
    "scheduler_confirmation_verifier_report",
    "verifier_report",
    "intake_runner_handoff",
    "runtime_handoff_execution",
    "validation_result",
    "approval_decision",
    "merge_recorded",
    "cleanup",
)

FORBIDDEN_EVENT_TYPES = (
    "scheduler_confirmation_created",
    "scheduler_confirmation_verifier_report",
    "verifier_report",
    "intake_runner_handoff_created",
    "runtime_preflight_finished",
    "runtime_execution_started",
    "runtime_execution_finished",
    "executor_run_started",
    "executor_run_finished",
    "validation_result",
    "approval_decision",
    "merge_recorded",
    "cleanup",
)

FORBIDDEN_PAYLOAD_MARKERS = (
    "executor_run_started",
    "executor_run_finished",
    "validation_result",
    "approved_task_runner",
)


class SchedulerCandidateProposalsTests(unittest.TestCase):
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

    def _seed_task(self, task_key: str, *, status: str) -> Path:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title=f"Candidate proposal {task_key}",
                status=status,
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )
        return artifact_dir

    def _seed_completed_no_action(self, task_key: str) -> None:
        artifact_dir = self._seed_task(task_key, status="completed")
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
            path = artifact_dir / filename
            path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            self.store.record_task_artifact(task_key, artifact_type, path)
            self.store.record_task_event(
                task_key,
                event_type,
                f"{artifact_type}_confirm",
                payload=payload,
            )

    def _request(self, task_key: str, **overrides: object) -> SchedulerCandidateProposalRequest:
        params: dict[str, object] = {
            "task_key": task_key,
            "db_path": self.db_path,
            "artifact_root": self.artifact_root,
        }
        params.update(overrides)
        return SchedulerCandidateProposalRequest(**params)

    def _create(self, task_key: str, **overrides: object) -> dict[str, object]:
        return create_scheduler_proposal_from_candidate(
            self._request(task_key, **overrides)
        )

    @staticmethod
    def _fake_proposal(
        task_key: str,
        *,
        recommended_command_kind: str,
        artifact_path: str | None = None,
    ) -> dict[str, object]:
        return {
            "proposal_id": f"proposal-{recommended_command_kind}",
            "artifact_path": artifact_path,
            "proposal_hash": "a" * 64,
            "items": [
                {
                    "task_key": task_key,
                    "recommended_command_kind": recommended_command_kind,
                    "proposal_item_id": f"{task_key}:{recommended_command_kind}",
                    "item_hash": "b" * 64,
                }
            ],
        }

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

    def _forbidden_side_effect_counts(self) -> dict[str, int]:
        with sqlite3.connect(self.db_path) as conn:
            artifact_placeholders = ",".join("?" for _ in FORBIDDEN_ARTIFACT_TYPES)
            event_placeholders = ",".join("?" for _ in FORBIDDEN_EVENT_TYPES)
            marker_clause = " OR ".join(
                "payload_json LIKE ?" for _ in FORBIDDEN_PAYLOAD_MARKERS
            )
            artifact_count = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM task_artifacts
                WHERE artifact_type IN ({artifact_placeholders})
                """,
                FORBIDDEN_ARTIFACT_TYPES,
            ).fetchone()[0]
            event_count = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM task_events
                WHERE event_type IN ({event_placeholders})
                """,
                FORBIDDEN_EVENT_TYPES,
            ).fetchone()[0]
            payload_count = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM task_events
                WHERE payload_json IS NOT NULL
                  AND ({marker_clause})
                """,
                tuple(f"%{marker}%" for marker in FORBIDDEN_PAYLOAD_MARKERS),
            ).fetchone()[0]
        return {
            "artifacts": artifact_count,
            "events": event_count,
            "payload_markers": payload_count,
        }

    def assert_safety_block(
        self,
        payload: dict[str, object],
        *,
        dry_run: bool,
        proposal_created: bool,
    ) -> None:
        self.assertEqual(
            payload["safety"],
            candidate_proposal_safety(
                dry_run=dry_run,
                proposal_created=proposal_created,
            ),
        )
        safety = payload["safety"]
        self.assertTrue(safety["explicit_operator_request"])
        self.assertEqual(safety["dry_run"], dry_run)
        self.assertEqual(safety["proposal_created"], proposal_created)
        self.assertFalse(safety["confirmation_created"])
        self.assertFalse(safety["verifier_report_created"])
        self.assertFalse(safety["handoff_created"])
        self.assertFalse(safety["runtime_started"])
        self.assertFalse(safety["approved_task_runner_called"])
        self.assertFalse(safety["executor_started"])
        self.assertFalse(safety["validators_started"])
        self.assertFalse(safety["github_mutated"])
        self.assertFalse(safety["approved"])
        self.assertFalse(safety["merged"])
        self.assertFalse(safety["cleanup_performed"])
        self.assertFalse(safety["background_worker_started"])
        self.assertTrue(safety["not_execution_permission"])

    def test_dry_run_candidate_ready_returns_preview(self) -> None:
        self._seed_task("AT-J1-001", status="queued")

        payload = self._create("AT-J1-001")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "preview")
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(payload["task_key"], "AT-J1-001")
        self.assertEqual(
            payload["candidate"]["recommended_command_kind"],
            "create_task_execution_package",
        )
        self.assertFalse(payload["proposal"]["created"])
        self.assert_safety_block(payload, dry_run=True, proposal_created=False)

    def test_dry_run_writes_no_db_event(self) -> None:
        self._seed_task("AT-J1-002", status="queued")
        before = self._db_counts()

        self._create("AT-J1-002")

        self.assertEqual(self._db_counts(), before)

    def test_dry_run_writes_no_artifact(self) -> None:
        self._seed_task("AT-J1-003", status="queued")

        payload = self._create("AT-J1-003")

        self.assertIsNone(payload["proposal"]["proposal_artifact_path"])
        self.assertFalse((self.artifact_root / "scheduler_proposals").exists())

    def test_confirmed_without_confirm_flag_blocks(self) -> None:
        self._seed_task("AT-J1-004", status="queued")
        before = self._db_counts()

        payload = self._create(
            "AT-J1-004",
            dry_run=False,
            confirm_create_proposal=False,
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["mode"], "confirmed")
        self.assertEqual(payload["block_reason"], "confirm_create_proposal_required")
        self.assertEqual(self._db_counts(), before)
        self.assert_safety_block(payload, dry_run=False, proposal_created=False)

    def test_confirmed_with_ready_candidate_writes_scheduler_proposal_artifact(self) -> None:
        self._seed_task("AT-J1-005", status="queued")

        payload = self._create(
            "AT-J1-005",
            dry_run=False,
            confirm_create_proposal=True,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "created")
        self.assertTrue(payload["proposal"]["created"])
        artifact_path = Path(payload["proposal"]["proposal_artifact_path"])
        self.assertTrue(artifact_path.exists())
        on_disk = json.loads(artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["proposal_hash"], payload["proposal"]["proposal_hash"])
        self.assertEqual(on_disk["items"][0]["task_key"], "AT-J1-005")
        self.assert_safety_block(payload, dry_run=False, proposal_created=True)

    def test_confirmed_writes_scheduler_proposal_event_and_artifact_row(self) -> None:
        self._seed_task("AT-J1-006", status="queued")
        before = self._db_counts()

        self._create(
            "AT-J1-006",
            dry_run=False,
            confirm_create_proposal=True,
        )

        artifacts = self.store.list_task_artifacts("AT-J1-006")
        events = self.store.list_task_events("AT-J1-006")
        self.assertEqual([a.artifact_type for a in artifacts], [PROPOSAL_ARTIFACT_TYPE])
        self.assertEqual([e.event_type for e in events], [PROPOSAL_EVENT_TYPE])
        after = self._db_counts()
        self.assertEqual(after["tasks"], before["tasks"])
        self.assertEqual(after["worktrees"], before["worktrees"])
        self.assertEqual(after["artifacts"], before["artifacts"] + 1)
        self.assertEqual(after["events"], before["events"] + 1)

    def test_proposal_hash_item_id_and_item_hash_are_present(self) -> None:
        self._seed_task("AT-J1-007", status="queued")

        payload = self._create("AT-J1-007")
        proposal = payload["proposal"]

        self.assertIsInstance(proposal["proposal_hash"], str)
        self.assertEqual(len(proposal["proposal_hash"]), 64)
        int(proposal["proposal_hash"], 16)
        self.assertEqual(
            proposal["proposal_item_id"],
            "AT-J1-007:create_task_execution_package",
        )
        self.assertIsInstance(proposal["item_hash"], str)
        self.assertEqual(len(proposal["item_hash"]), 64)
        int(proposal["item_hash"], 16)

    def test_proposal_recommended_command_kind_matches_candidate(self) -> None:
        self._seed_task("AT-J1-008", status="queued")

        payload = self._create("AT-J1-008")

        self.assertEqual(
            payload["proposal"]["recommended_command_kind"],
            payload["candidate"]["recommended_command_kind"],
        )

    def test_stale_expected_status_blocks(self) -> None:
        self._seed_task("AT-J1-009", status="queued")
        before = self._db_counts()

        payload = self._create("AT-J1-009", expected_status="blocked")

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["block_reason"], "stale_expected_status")
        self.assertEqual(self._db_counts(), before)
        self.assert_safety_block(payload, dry_run=True, proposal_created=False)

    def test_stale_expected_recommended_command_kind_blocks(self) -> None:
        self._seed_task("AT-J1-010", status="queued")
        before = self._db_counts()

        payload = self._create(
            "AT-J1-010",
            expected_recommended_command_kind="queued_task_handoff",
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(
            payload["block_reason"],
            "stale_expected_recommended_command_kind",
        )
        self.assertEqual(self._db_counts(), before)

    def test_candidate_not_ready_blocks(self) -> None:
        self._seed_completed_no_action("AT-J1-011")
        before = self._db_counts()

        payload = self._create("AT-J1-011", include_no_action=True)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["block_reason"], "candidate_not_ready")
        self.assertFalse(payload["candidate"]["candidate_ready"])
        self.assertEqual(self._db_counts(), before)

    def test_candidate_not_found_blocks(self) -> None:
        payload = self._create("AT-J1-MISSING")

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["block_reason"], "candidate_not_found")
        self.assertIsNone(payload["candidate"])
        self.assert_safety_block(payload, dry_run=True, proposal_created=False)

    def test_no_confirmation_verifier_handoff_runtime_runner_or_github_side_effects(self) -> None:
        self._seed_task("AT-J1-012", status="queued")

        payload = self._create(
            "AT-J1-012",
            dry_run=False,
            confirm_create_proposal=True,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(
            self._forbidden_side_effect_counts(),
            {"artifacts": 0, "events": 0, "payload_markers": 0},
        )
        self.assertEqual(self.store.list_executor_runs("AT-J1-012"), [])
        self.assertEqual(self.store.list_validation_results("AT-J1-012"), [])
        self.assertEqual(self.store.list_runtime_audit_events("AT-J1-012"), [])
        self.assertFalse((self.artifact_root / "scheduler_confirmations").exists())
        self.assertFalse((self.artifact_root / "intake_runner_handoff").exists())

    def test_confirmed_write_mismatch_returns_error_not_success(self) -> None:
        task_key = "AT-J1-MISMATCH"
        self._seed_task(task_key, status="queued")
        matching_preview = self._fake_proposal(
            task_key,
            recommended_command_kind="create_task_execution_package",
        )
        mismatched_created = self._fake_proposal(
            task_key,
            recommended_command_kind="queued_task_handoff",
            artifact_path=str(self.artifact_root / "scheduler_proposal.json"),
        )

        with patch(
            "agent_taskflow.scheduler_candidate_proposals.create_scheduler_proposal",
            side_effect=[matching_preview, mismatched_created],
        ):
            payload = self._create(
                task_key,
                dry_run=False,
                confirm_create_proposal=True,
            )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "error")
        self.assertIn("created_proposal_mismatch", payload["error"])
        self.assertFalse(payload["safety"]["proposal_created"])
        self.assertTrue(payload["safety"]["not_execution_permission"])

    def test_proposal_request_binds_live_candidate_status_and_kind(self) -> None:
        task_key = "AT-J1-BIND"
        self._seed_task(task_key, status="queued")
        matching_preview = self._fake_proposal(
            task_key,
            recommended_command_kind="create_task_execution_package",
        )

        with patch(
            "agent_taskflow.scheduler_candidate_proposals.create_scheduler_proposal",
            return_value=matching_preview,
        ) as create_proposal:
            payload = self._create(task_key)

        self.assertTrue(payload["ok"])
        proposal_request = create_proposal.call_args.args[0]
        self.assertEqual(proposal_request.status, "queued")
        self.assertEqual(
            proposal_request.include_command_kinds,
            ("create_task_execution_package",),
        )
        self.assertEqual(proposal_request.task_key, task_key)
        self.assertEqual(proposal_request.max_items, 1)

    def test_structured_block_safety_exists(self) -> None:
        self._seed_task("AT-J1-013", status="queued")

        payload = create_scheduler_proposal_from_candidate(
            SchedulerCandidateProposalRequest(
                task_key="AT-J1-013",
                db_path=self.db_path,
                artifact_root=None,
            )
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["block_reason"], "artifact_root_required")
        self.assert_safety_block(payload, dry_run=True, proposal_created=False)

    def test_module_does_not_import_forbidden_downstream_modules(self) -> None:
        module = importlib.import_module("agent_taskflow.scheduler_candidate_proposals")
        for attr in (
            "scheduler_confirmations",
            "scheduler_confirmation_verifier",
            "intake_runner_handoff",
            "queued_task_handoff",
            "approved_task_runner",
            "executors",
            "validators",
        ):
            self.assertFalse(hasattr(module, attr), attr)

        source = (
            REPO_ROOT / "agent_taskflow" / "scheduler_candidate_proposals.py"
        ).read_text(encoding="utf-8")
        forbidden_imports = (
            "from agent_taskflow.scheduler_confirmations",
            "from agent_taskflow.scheduler_confirmation_verifier",
            "from agent_taskflow.intake_runner_handoff",
            "from agent_taskflow.queued_task_handoff",
            "from agent_taskflow.approved_task_runner",
            "from agent_taskflow.executors",
            "from agent_taskflow.validators",
        )
        for forbidden in forbidden_imports:
            self.assertNotIn(forbidden, source, forbidden)

    def test_convenience_wrapper_returns_preview(self) -> None:
        self._seed_task("AT-J1-014", status="queued")

        payload = propose_candidate_task(
            "AT-J1-014",
            db_path=self.db_path,
            artifact_root=self.artifact_root,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "preview")


if __name__ == "__main__":
    unittest.main()
