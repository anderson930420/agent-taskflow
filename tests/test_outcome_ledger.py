"""Behavioural tests for the M2 §2.3 per-Attempt outcome ledger."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from agent_taskflow import outcome_ledger
from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.models import TaskRecord
from agent_taskflow.outcome_ledger import (
    ENRICHABLE_FIELDS,
    LEDGER_SCHEMA_VERSION,
    ledger_filename,
    record_attempt_outcome_observation,
    record_terminal_attempt_outcome,
)
from agent_taskflow.runtime_admission import RuntimeAdmissionStore
from agent_taskflow.runtime_admission_schema import migrate_runtime_admission
from agent_taskflow.store import TaskMirrorStore, connect


class OutcomeLedgerTestCase(unittest.TestCase):
    """Disposable fixtures only; every database is created under /tmp."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.repo_path.mkdir()
        self.artifact_base = self.root / "artifacts"
        self.artifact_base.mkdir()
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        migrate_runtime_admission(self.db_path)
        self.admission = RuntimeAdmissionStore(self.db_path)
        self.attempts = AttemptStore(self.db_path)

    # -- fixtures ---------------------------------------------------------

    def add_task(
        self,
        task_key: str = "AT-LEDGER-1",
        *,
        status: str = "queued",
        with_artifact_dir: bool = True,
    ) -> Path | None:
        artifact_dir: Path | None = None
        if with_artifact_dir:
            artifact_dir = self.artifact_base / task_key
            artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title=f"Outcome ledger {task_key}",
                status=status,
                repo_path=self.repo_path,
                artifact_dir=artifact_dir,
                executor="noop",
            )
        )
        return artifact_dir

    def claim(self, task_key: str = "AT-LEDGER-1", **kwargs):
        return self.admission.claim(
            task_key,
            owner_id=kwargs.pop("owner_id", "owner-1"),
            model=kwargs.pop("model", "configured-model-x"),
            policy_version=kwargs.pop("policy_version", "policy-7"),
            **kwargs,
        )

    def release(self, claim, *, attempt_status: str, task_status: str, reason_code: str, **kwargs):
        return self.admission.release(
            claim.attempt_id,
            owner_id=claim.owner_id,
            lease_token=claim.lease_token,
            attempt_status=attempt_status,
            task_status=task_status,
            reason_code=reason_code,
            **kwargs,
        )

    # -- readback helpers -------------------------------------------------

    def ledger_path(self, artifact_dir: Path, attempt_id: str) -> Path:
        return artifact_dir / ledger_filename(attempt_id)

    def read_ledger(self, artifact_dir: Path, attempt_id: str) -> dict:
        return json.loads(self.ledger_path(artifact_dir, attempt_id).read_text())

    def ledger_events(self, task_key: str) -> list[dict]:
        events = []
        for event in self.store.list_task_events(task_key):
            if event.source != "outcome_ledger" or not event.payload_json:
                continue
            events.append(json.loads(event.payload_json))
        return events

    def ledger_artifacts(self, task_key: str) -> list[str]:
        return [
            str(record.path)
            for record in self.store.list_task_artifacts(task_key)
            if "outcome-ledger-" in Path(record.path).name
        ]

    def set_attempt_column(self, attempt_id: str, column: str, value) -> None:
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute(
                f"UPDATE attempts SET {column} = ? WHERE attempt_id = ?",
                (value, attempt_id),
            )


class TerminalRouteCoverageTests(OutcomeLedgerTestCase):
    def test_admission_release_publishes_one_immutable_snapshot(self) -> None:
        artifact_dir = self.add_task()
        claim = self.claim()
        self.release(
            claim,
            attempt_status="waiting_approval",
            task_status="waiting_approval",
            reason_code="runtime_waiting_approval",
            execution_result="completed",
            validation_result="passed",
        )

        payload = self.read_ledger(artifact_dir, claim.attempt_id)
        self.assertEqual(payload["schema_version"], LEDGER_SCHEMA_VERSION)
        self.assertEqual(payload["record_type"], "base_closeout_snapshot")
        self.assertEqual(payload["attempt_id"], claim.attempt_id)
        self.assertEqual(payload["task_key"], "AT-LEDGER-1")
        self.assertEqual(payload["closeout_route"], "runtime_admission_release")
        self.assertEqual(payload["fields"]["final_status"]["value"], "waiting_approval")
        self.assertEqual(payload["fields"]["final_status"]["provenance"], "observed")
        self.assertFalse(payload["attempt_terminal"]["is_active"])
        self.assertIsNotNone(payload["attempt_terminal"]["ended_at"])
        self.assertEqual(
            payload["terminal_lifecycle_event"]["reason_code"], "runtime_waiting_approval"
        )
        # The Attempt outcome and the Task status are separate facts.
        self.assertEqual(payload["task_status_at_closeout"]["value"], "waiting_approval")
        self.assertEqual(self.ledger_artifacts("AT-LEDGER-1"), [str(self.ledger_path(artifact_dir, claim.attempt_id))])

        published = [event for event in self.ledger_events("AT-LEDGER-1") if event["status"] == "published"]
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["attempt_id"], claim.attempt_id)

    def test_all_release_terminal_outcomes_publish_exactly_one_ledger(self) -> None:
        cases = [
            ("executor_failure", "failed", "failed", "executor_failed", "failed", None),
            ("validation_failure", "validation_failed", "needs_decision", "validator_failed", "completed", "failed"),
            ("executor_timeout", "execution_timeout", "failed", "executor_timeout", "timeout", None),
            ("operator_kill", "execution_aborted", "canceled", "operator_kill_requested", "aborted", None),
            ("governance_blocked", "blocked", "blocked", "runtime_governance_blocked", None, None),
            ("success", "completed", "completed", "runtime_completed", "completed", "passed"),
        ]
        for name, attempt_status, task_status, reason, execution, validation in cases:
            with self.subTest(case=name):
                task_key = f"AT-LEDGER-{name}"
                artifact_dir = self.add_task(task_key)
                claim = self.claim(task_key)
                self.release(
                    claim,
                    attempt_status=attempt_status,
                    task_status=task_status,
                    reason_code=reason,
                    execution_result=execution,
                    validation_result=validation,
                )
                payload = self.read_ledger(artifact_dir, claim.attempt_id)
                self.assertEqual(payload["fields"]["final_status"]["value"], attempt_status)
                self.assertEqual(
                    payload["fields"]["final_status"]["execution_result"], execution
                )
                self.assertEqual(payload["terminal_lifecycle_event"]["reason_code"], reason)
                published = [
                    event
                    for event in self.ledger_events(task_key)
                    if event["status"] == "published"
                ]
                self.assertEqual(len(published), 1)

    def test_lease_expiry_publishes_ledger_for_each_expired_attempt(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-EXPIRY")
        claim = self.claim("AT-LEDGER-EXPIRY")
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE runtime_leases SET expires_at = '2000-01-01T00:00:00Z' WHERE lease_id = ?",
                (claim.lease_id,),
            )

        expired = self.admission.expire_stale_leases()

        self.assertEqual(expired, [claim.attempt_id])
        payload = self.read_ledger(artifact_dir, claim.attempt_id)
        self.assertEqual(payload["closeout_route"], "runtime_lease_expiry")
        self.assertEqual(payload["fields"]["final_status"]["value"], "execution_aborted")
        self.assertEqual(
            payload["terminal_lifecycle_event"]["reason_code"], "runtime_lease_expired"
        )

    def test_direct_close_attempt_publishes_ledger(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-DIRECT")
        attempt = self.attempts.create_attempt(
            "AT-LEDGER-DIRECT",
            artifact_root=artifact_dir,
            model="configured-model-x",
        )
        self.attempts.close_attempt(
            attempt.attempt_id,
            status="failed",
            reason_code="executor_failed",
            actor="tester",
            execution_result="failed",
        )

        payload = self.read_ledger(artifact_dir, attempt.attempt_id)
        self.assertEqual(payload["closeout_route"], "attempt_store_close")
        self.assertEqual(payload["fields"]["final_status"]["value"], "failed")
        self.assertEqual(
            payload["fields"]["canonical_execution_path"]["value"], "attempt_store_direct"
        )

    def test_compatibility_status_trigger_closure_publishes_ledger(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-TRIGGER")
        claim = self.claim("AT-LEDGER-TRIGGER")

        self.store.update_task_status("AT-LEDGER-TRIGGER", "completed")

        payload = self.read_ledger(artifact_dir, claim.attempt_id)
        self.assertEqual(payload["closeout_route"], "task_status_trigger")
        self.assertEqual(payload["attempt_id"], claim.attempt_id)
        self.assertEqual(payload["fields"]["final_status"]["value"], "completed")
        self.assertEqual(
            payload["terminal_lifecycle_event"]["reason_code"],
            "runtime_attempt_closed_by_task_status",
        )

    def test_released_attempt_is_not_published_twice_by_a_later_status_write(self) -> None:
        """`LifecycleRuntimeTaskStore._release` delegates to admission release.

        The admission seam is the single hook, and the audit status write it
        performs afterwards must not produce a second ledger.
        """
        self.add_task("AT-LEDGER-ONCE")
        claim = self.claim("AT-LEDGER-ONCE")
        self.release(
            claim,
            attempt_status="blocked",
            task_status="blocked",
            reason_code="runtime_governance_blocked",
        )
        self.store.update_task_status(
            "AT-LEDGER-ONCE", "blocked", message="governance", source="owner-1"
        )

        references = self.ledger_events("AT-LEDGER-ONCE")
        self.assertEqual([event["status"] for event in references], ["published"])
        self.assertEqual(len(self.ledger_artifacts("AT-LEDGER-ONCE")), 1)

    def test_status_write_without_active_attempt_publishes_nothing(self) -> None:
        self.add_task("AT-LEDGER-NOATTEMPT")
        self.store.update_task_status("AT-LEDGER-NOATTEMPT", "completed")
        self.assertEqual(self.ledger_events("AT-LEDGER-NOATTEMPT"), [])

    def test_status_write_on_a_store_without_attempt_schema_is_unaffected(self) -> None:
        plain_db = self.root / "plain.db"
        plain = TaskMirrorStore(plain_db)
        plain.init_db()
        plain.upsert_task(
            TaskRecord(
                task_key="AT-PLAIN-1",
                project="agent-taskflow",
                board="agent-taskflow",
                title="Plain",
                status="queued",
                repo_path=self.repo_path,
            )
        )
        plain.update_task_status("AT-PLAIN-1", "completed")
        self.assertEqual(plain.get_task("AT-PLAIN-1").status, "completed")


class RetryHistoryTests(OutcomeLedgerTestCase):
    def _two_attempts(self) -> tuple[Path, str, str]:
        artifact_dir = self.add_task("AT-LEDGER-RETRY")
        first = self.claim("AT-LEDGER-RETRY")
        self.release(
            first,
            attempt_status="failed",
            task_status="failed",
            reason_code="executor_failed",
            execution_result="failed",
        )
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE tasks SET status = 'queued' WHERE task_key = ?",
                ("AT-LEDGER-RETRY",),
            )
        second = self.claim("AT-LEDGER-RETRY", owner_id="owner-2")
        self.release(
            second,
            attempt_status="completed",
            task_status="completed",
            reason_code="runtime_completed",
            execution_result="completed",
            validation_result="passed",
        )
        return artifact_dir, first.attempt_id, second.attempt_id

    def test_each_attempt_gets_its_own_immutable_artifact(self) -> None:
        artifact_dir, first_id, second_id = self._two_attempts()

        first = self.read_ledger(artifact_dir, first_id)
        second = self.read_ledger(artifact_dir, second_id)
        self.assertNotEqual(first["artifact_name"], second["artifact_name"])
        self.assertEqual(first["attempt_number"], 1)
        self.assertEqual(second["attempt_number"], 2)
        self.assertEqual(first["fields"]["retry_count"]["value"], 0)
        self.assertEqual(second["fields"]["retry_count"]["value"], 1)
        self.assertEqual(
            second["fields"]["retry_count"]["preceding_attempt_ids"], [first_id]
        )
        self.assertFalse(first["fields"]["first_pass_success"]["value"])
        self.assertFalse(second["fields"]["first_pass_success"]["value"])
        self.assertEqual(
            second["fields"]["first_pass_success"]["first_attempt_id"], first_id
        )

    def test_first_pass_success_is_true_only_for_a_successful_first_attempt(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-FIRSTPASS")
        claim = self.claim("AT-LEDGER-FIRSTPASS")
        self.release(
            claim,
            attempt_status="completed",
            task_status="completed",
            reason_code="runtime_completed",
            execution_result="completed",
            validation_result="passed",
        )
        payload = self.read_ledger(artifact_dir, claim.attempt_id)
        self.assertTrue(payload["fields"]["first_pass_success"]["value"])
        self.assertEqual(payload["fields"]["first_pass_success"]["provenance"], "observed")

    def test_replaying_an_older_attempt_after_a_retry_keeps_stable_history(self) -> None:
        artifact_dir, first_id, second_id = self._two_attempts()
        original = self.ledger_path(artifact_dir, first_id).read_bytes()

        replay = record_terminal_attempt_outcome(
            db_path=self.db_path,
            attempt_id=first_id,
            closeout_route="attempt_store_close",
        )

        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(self.ledger_path(artifact_dir, first_id).read_bytes(), original)

        # The derivation itself, not only the published file, excludes the
        # later Attempt: a freshly derived snapshot still reports retry_count 0.
        snapshot = outcome_ledger._read_snapshot(self.db_path, first_id)
        fields = outcome_ledger._build_fields(snapshot, artifact_dir)
        self.assertEqual(fields["retry_count"]["value"], 0)
        self.assertEqual(fields["retry_count"]["preceding_attempt_ids"], [])
        self.assertEqual(
            [row["attempt_id"] for row in snapshot["history"]], [first_id]
        )


class ProvenanceFieldTests(OutcomeLedgerTestCase):
    def _published(self, **claim_kwargs) -> tuple[Path, dict, str]:
        artifact_dir = self.add_task("AT-LEDGER-FIELDS")
        claim = self.claim("AT-LEDGER-FIELDS", **claim_kwargs)
        self.release(
            claim,
            attempt_status="completed",
            task_status="completed",
            reason_code="runtime_completed",
            execution_result="completed",
            validation_result="passed",
            merge_recommendation="ready",
        )
        return artifact_dir, self.read_ledger(artifact_dir, claim.attempt_id), claim.attempt_id

    def test_every_required_field_is_present_with_provenance(self) -> None:
        _, payload, _ = self._published()
        for name in outcome_ledger.LEDGER_FIELDS:
            with self.subTest(field=name):
                record = payload["fields"][name]
                self.assertIn("value", record)
                self.assertIn(
                    record["provenance"], {"observed", "unknown", "not_applicable"}
                )
                if record["provenance"] == "observed":
                    self.assertIsNotNone(record["source"])
                else:
                    self.assertIsNotNone(record["reason"])

    def test_future_outcomes_are_null_unknown_and_never_invented(self) -> None:
        _, payload, _ = self._published()
        for name in ENRICHABLE_FIELDS:
            with self.subTest(field=name):
                self.assertIsNone(payload["fields"][name]["value"])
                self.assertEqual(payload["fields"][name]["provenance"], "unknown")
                self.assertEqual(
                    payload["fields"][name]["reason"], "not_observed_at_attempt_closeout"
                )

    def test_model_snapshot_separates_configured_from_observed_backend_model(self) -> None:
        _, payload, _ = self._published()
        model = payload["fields"]["model_snapshot"]
        self.assertEqual(model["value"]["configured_model"], "configured-model-x")
        self.assertIsNone(model["value"]["observed_backend_model"])
        self.assertEqual(model["observed_backend_model_provenance"], "unknown")

    def test_policy_version_and_merge_recommendation_use_recorded_values(self) -> None:
        _, payload, _ = self._published()
        self.assertEqual(payload["fields"]["policy_version"]["value"], "policy-7")
        self.assertEqual(payload["fields"]["merge_recommendation"]["value"], "ready")

    def test_missing_optional_source_is_unknown_not_zero(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-SPARSE")
        claim = self.claim("AT-LEDGER-SPARSE", model=None, policy_version=None)
        self.release(
            claim,
            attempt_status="failed",
            task_status="failed",
            reason_code="runtime_failed",
        )
        payload = self.read_ledger(artifact_dir, claim.attempt_id)
        self.assertIsNone(payload["fields"]["policy_version"]["value"])
        self.assertEqual(payload["fields"]["policy_version"]["provenance"], "unknown")
        self.assertIsNone(payload["fields"]["merge_recommendation"]["value"])
        self.assertEqual(payload["fields"]["merge_recommendation"]["provenance"], "unknown")

    def test_phase_durations_come_from_this_attempts_own_transitions(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-PHASES")
        claim = self.claim("AT-LEDGER-PHASES")
        for previous, phase, reason in (
            ("preparing", "implementing", "runtime_implementing"),
            ("implementing", "implementing", "runtime_lease_heartbeat"),
            ("implementing", "validating", "runtime_validating"),
        ):
            self.attempts.append_lifecycle_event(
                "AT-LEDGER-PHASES",
                attempt_id=claim.attempt_id,
                from_status=previous,
                to_status=phase,
                reason_code=reason,
                actor=claim.owner_id,
            )
        self.release(
            claim,
            attempt_status="completed",
            task_status="completed",
            reason_code="runtime_completed",
        )
        payload = self.read_ledger(artifact_dir, claim.attempt_id)
        phases = payload["fields"]["phase_durations"]
        self.assertEqual(phases["provenance"], "observed")
        self.assertEqual(
            [segment["phase"] for segment in phases["value"]],
            ["preparing", "implementing", "validating", "completed"],
        )
        for segment in phases["value"]:
            self.assertIsNotNone(segment["duration_seconds"])
            self.assertGreaterEqual(segment["duration_seconds"], 0)

    def test_canonical_execution_path_requires_recorded_producer_proof(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-PATH")
        claim = self.claim("AT-LEDGER-PATH", reason_code="canonical_runtime_pickup_claimed")
        self.release(
            claim,
            attempt_status="completed",
            task_status="completed",
            reason_code="runtime_completed",
        )
        payload = self.read_ledger(artifact_dir, claim.attempt_id)
        path_field = payload["fields"]["canonical_execution_path"]
        self.assertEqual(path_field["value"], "canonical_runtime_path")
        self.assertEqual(path_field["provenance"], "observed")
        self.assertEqual(
            path_field["producer_reason_code"], "canonical_runtime_pickup_claimed"
        )

    def test_unrecognized_producer_reason_code_stays_unknown(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-PATH2")
        attempt = self.attempts.create_attempt(
            "AT-LEDGER-PATH2",
            artifact_root=artifact_dir,
            reason_code="some_unmapped_producer",
        )
        self.attempts.close_attempt(
            attempt.attempt_id,
            status="completed",
            reason_code="runtime_completed",
            actor="tester",
        )
        payload = self.read_ledger(artifact_dir, attempt.attempt_id)
        path_field = payload["fields"]["canonical_execution_path"]
        self.assertIsNone(path_field["value"])
        self.assertEqual(path_field["reason"], "unrecognized_producer_reason_code")
        self.assertEqual(path_field["producer_reason_code"], "some_unmapped_producer")

    def test_human_intervention_counts_only_attempt_bound_documented_events(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-HUMAN")
        claim = self.claim("AT-LEDGER-HUMAN")
        self.attempts.append_lifecycle_event(
            "AT-LEDGER-HUMAN",
            attempt_id=claim.attempt_id,
            from_status="implementing",
            to_status="implementing",
            reason_code="operator_kill_requested",
            actor="anderson",
        )
        # A task-scoped approval carries no Attempt binding and must not count.
        self.store.record_approval_decision(
            "AT-LEDGER-HUMAN", "accepted", decided_by="anderson"
        )
        self.release(
            claim,
            attempt_status="execution_aborted",
            task_status="canceled",
            reason_code="operator_kill_requested",
        )
        payload = self.read_ledger(artifact_dir, claim.attempt_id)
        human = payload["fields"]["human_intervention_count"]
        # The operator event plus the operator-attributed terminal release.
        self.assertEqual(human["value"], 2)
        self.assertEqual(len(human["matched_event_ids"]), 2)
        self.assertIn("operator_kill_requested", human["counted_reason_codes"])

    def test_diff_size_reads_the_attempt_bound_changed_files_audit(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-DIFF")
        (artifact_dir / "changed-files-audit.json").write_text(
            json.dumps(
                {
                    "changed_files": [
                        {"path": "a.py", "status": "M"},
                        {"path": "b.py", "status": "A"},
                    ],
                    "violations": [],
                    "collection_error": None,
                }
            )
        )
        claim = self.claim("AT-LEDGER-DIFF")
        self.release(
            claim,
            attempt_status="completed",
            task_status="completed",
            reason_code="runtime_completed",
        )
        diff = self.read_ledger(artifact_dir, claim.attempt_id)["fields"]["diff_size"]
        self.assertEqual(diff["provenance"], "observed")
        self.assertEqual(diff["value"]["changed_file_count"], 2)

    def test_corrupt_changed_files_audit_is_unknown_not_zero(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-DIFF2")
        (artifact_dir / "changed-files-audit.json").write_text("{not json")
        claim = self.claim("AT-LEDGER-DIFF2")
        self.release(
            claim,
            attempt_status="completed",
            task_status="completed",
            reason_code="runtime_completed",
        )
        diff = self.read_ledger(artifact_dir, claim.attempt_id)["fields"]["diff_size"]
        self.assertIsNone(diff["value"])
        self.assertEqual(diff["provenance"], "unknown")
        self.assertTrue(diff["reason"].startswith("changed_files_audit_corrupt_evidence"))

    def test_symlinked_changed_files_audit_is_refused(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-DIFF3")
        outside = self.root / "outside.json"
        outside.write_text(json.dumps({"changed_files": [{"path": "x", "status": "M"}]}))
        os.symlink(outside, artifact_dir / "changed-files-audit.json")
        claim = self.claim("AT-LEDGER-DIFF3")
        self.release(
            claim,
            attempt_status="completed",
            task_status="completed",
            reason_code="runtime_completed",
        )
        diff = self.read_ledger(artifact_dir, claim.attempt_id)["fields"]["diff_size"]
        self.assertIsNone(diff["value"])
        self.assertTrue(diff["reason"].startswith("changed_files_audit_unreadable"))


class IdempotenceAndConflictTests(OutcomeLedgerTestCase):
    def _closed_attempt(self) -> tuple[Path, str]:
        artifact_dir = self.add_task("AT-LEDGER-IDEM")
        claim = self.claim("AT-LEDGER-IDEM")
        self.release(
            claim,
            attempt_status="completed",
            task_status="completed",
            reason_code="runtime_completed",
        )
        return artifact_dir, claim.attempt_id

    def test_repeated_publication_is_idempotent(self) -> None:
        artifact_dir, attempt_id = self._closed_attempt()
        original = self.ledger_path(artifact_dir, attempt_id).read_bytes()

        again = record_terminal_attempt_outcome(
            db_path=self.db_path,
            attempt_id=attempt_id,
            closeout_route="runtime_admission_release",
        )

        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["reason"], "already_published_for_this_attempt")
        self.assertEqual(self.ledger_path(artifact_dir, attempt_id).read_bytes(), original)
        self.assertEqual(len(self.ledger_artifacts("AT-LEDGER-IDEM")), 1)

    def test_concurrent_publication_produces_one_artifact_and_one_winner(self) -> None:
        artifact_dir, attempt_id = self._closed_attempt()
        first_bytes = self.ledger_path(artifact_dir, attempt_id).read_bytes()

        def publish(route: str) -> dict:
            return record_terminal_attempt_outcome(
                db_path=self.db_path,
                attempt_id=attempt_id,
                closeout_route=route,
            )

        routes = ["runtime_admission_release", "attempt_store_close", "task_status_trigger"] * 3
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(publish, routes))

        self.assertEqual({result["status"] for result in results}, {"duplicate"})
        self.assertEqual(self.ledger_path(artifact_dir, attempt_id).read_bytes(), first_bytes)
        self.assertEqual(len(self.ledger_artifacts("AT-LEDGER-IDEM")), 1)

    def test_first_concurrent_writer_wins_and_the_rest_are_duplicates(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-RACE")
        attempt = self.attempts.create_attempt("AT-LEDGER-RACE", artifact_root=artifact_dir)
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE attempts
                SET is_active = 0, ended_at = '2026-09-19T10:00:00Z', status = 'completed'
                WHERE attempt_id = ?
                """,
                (attempt.attempt_id,),
            )
            conn.execute(
                "UPDATE tasks SET active_attempt_id = NULL WHERE task_key = ?",
                ("AT-LEDGER-RACE",),
            )

        def publish(_: int) -> dict:
            return record_terminal_attempt_outcome(
                db_path=self.db_path,
                attempt_id=attempt.attempt_id,
                closeout_route="attempt_store_close",
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(publish, range(8)))

        statuses = [result["status"] for result in results]
        self.assertEqual(statuses.count("published"), 1)
        self.assertEqual(statuses.count("duplicate"), 7)
        self.assertEqual(len(self.ledger_artifacts("AT-LEDGER-RACE")), 1)

    def test_conflicting_existing_artifact_is_never_overwritten(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-CONFLICT")
        attempt = self.attempts.create_attempt(
            "AT-LEDGER-CONFLICT", artifact_root=artifact_dir
        )
        foreign = {"schema_version": LEDGER_SCHEMA_VERSION, "attempt_id": "attempt-other"}
        target = artifact_dir / ledger_filename(attempt.attempt_id)
        target.write_text(json.dumps(foreign))

        self.attempts.close_attempt(
            attempt.attempt_id,
            status="completed",
            reason_code="runtime_completed",
            actor="tester",
        )

        self.assertEqual(json.loads(target.read_text()), foreign)
        references = self.ledger_events("AT-LEDGER-CONFLICT")
        self.assertEqual([event["status"] for event in references], ["conflict"])
        self.assertEqual(references[0]["reason"], "existing_ledger_identity_conflict")
        # The lifecycle result is unchanged by the evidence conflict.
        self.assertFalse(self.attempts.get_attempt(attempt.attempt_id).is_active)

    def test_unreadable_existing_artifact_is_reported_not_replaced(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-CORRUPT")
        attempt = self.attempts.create_attempt(
            "AT-LEDGER-CORRUPT", artifact_root=artifact_dir
        )
        target = artifact_dir / ledger_filename(attempt.attempt_id)
        target.write_text("}} not json")

        self.attempts.close_attempt(
            attempt.attempt_id,
            status="completed",
            reason_code="runtime_completed",
            actor="tester",
        )

        self.assertEqual(target.read_text(), "}} not json")
        references = self.ledger_events("AT-LEDGER-CORRUPT")
        self.assertEqual([event["status"] for event in references], ["incomplete"])
        self.assertTrue(references[0]["reason"].startswith("existing_ledger_corrupt_evidence"))


class BindingAndSafetyTests(OutcomeLedgerTestCase):
    def test_unknown_attempt_records_incomplete_without_fabricating_a_ledger(self) -> None:
        self.add_task("AT-LEDGER-MISSING")
        reference = record_terminal_attempt_outcome(
            db_path=self.db_path,
            attempt_id="attempt-does-not-exist",
            closeout_route="task_status_trigger",
            task_key_hint="AT-LEDGER-MISSING",
        )
        self.assertEqual(reference["status"], "incomplete")
        self.assertEqual(reference["reason"], "attempt_row_not_found")
        self.assertIsNone(reference["path"])
        self.assertEqual(
            [event["reason"] for event in self.ledger_events("AT-LEDGER-MISSING")],
            ["attempt_row_not_found"],
        )
        self.assertEqual(list(self.artifact_base.glob("**/outcome-ledger-*.json")), [])

    def test_integration_run_id_is_not_accepted_as_an_attempt_id(self) -> None:
        self.add_task("AT-LEDGER-INTEGRATION")
        reference = record_terminal_attempt_outcome(
            db_path=self.db_path,
            attempt_id="integration-run-2026-09-19-01",
            closeout_route="attempt_store_close",
            task_key_hint="AT-LEDGER-INTEGRATION",
        )
        self.assertEqual(reference["status"], "incomplete")
        self.assertEqual(reference["reason"], "attempt_row_not_found")

    def test_wrong_task_identity_is_refused(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-IDENTITY")
        self.add_task("AT-LEDGER-OTHER")
        claim = self.claim("AT-LEDGER-IDENTITY")
        self.release(
            claim,
            attempt_status="completed",
            task_status="completed",
            reason_code="runtime_completed",
        )
        self.ledger_path(artifact_dir, claim.attempt_id).unlink()

        reference = record_terminal_attempt_outcome(
            db_path=self.db_path,
            attempt_id=claim.attempt_id,
            closeout_route="attempt_store_close",
            expected_task_key="AT-LEDGER-OTHER",
        )
        self.assertEqual(reference["status"], "incomplete")
        self.assertEqual(reference["reason"], "task_identity_mismatch")
        self.assertFalse(self.ledger_path(artifact_dir, claim.attempt_id).exists())

    def test_still_active_attempt_is_never_published(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-ACTIVE")
        claim = self.claim("AT-LEDGER-ACTIVE")
        reference = record_terminal_attempt_outcome(
            db_path=self.db_path,
            attempt_id=claim.attempt_id,
            closeout_route="attempt_store_close",
        )
        self.assertEqual(reference["reason"], "attempt_is_not_terminal")
        self.assertFalse(self.ledger_path(artifact_dir, claim.attempt_id).exists())

    def test_missing_artifact_root_is_explicit_and_keeps_the_lifecycle_result(self) -> None:
        self.add_task("AT-LEDGER-NOROOT", with_artifact_dir=False)
        attempt = self.attempts.create_attempt("AT-LEDGER-NOROOT")
        closed = self.attempts.close_attempt(
            attempt.attempt_id,
            status="failed",
            reason_code="executor_failed",
            actor="tester",
        )

        self.assertFalse(closed.is_active)
        self.assertEqual(closed.status, "failed")
        references = self.ledger_events("AT-LEDGER-NOROOT")
        self.assertEqual(
            [(event["status"], event["reason"]) for event in references],
            [("incomplete", "attempt_artifact_root_missing")],
        )
        self.assertEqual(list(self.artifact_base.glob("**/outcome-ledger-*.json")), [])

    def test_artifact_root_with_parent_traversal_is_refused(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-TRAVERSAL")
        attempt = self.attempts.create_attempt(
            "AT-LEDGER-TRAVERSAL", artifact_root=artifact_dir
        )
        self.set_attempt_column(
            attempt.attempt_id, "artifact_root", f"{artifact_dir}/../escaped"
        )
        self.attempts.close_attempt(
            attempt.attempt_id,
            status="completed",
            reason_code="runtime_completed",
            actor="tester",
        )
        references = self.ledger_events("AT-LEDGER-TRAVERSAL")
        self.assertEqual(
            [(event["status"], event["reason"]) for event in references],
            [("incomplete", "attempt_artifact_root_unsafe")],
        )
        self.assertFalse((self.root / "escaped").exists())

    def test_symlinked_artifact_root_cannot_publish_outside(self) -> None:
        self.add_task("AT-LEDGER-SYMLINK", with_artifact_dir=False)
        real = self.root / "real-target"
        real.mkdir()
        link = self.artifact_base / "AT-LEDGER-SYMLINK"
        os.symlink(real, link)
        attempt = self.attempts.create_attempt("AT-LEDGER-SYMLINK", artifact_root=link)

        closed = self.attempts.close_attempt(
            attempt.attempt_id,
            status="completed",
            reason_code="runtime_completed",
            actor="tester",
        )

        self.assertFalse(closed.is_active)
        self.assertEqual(list(real.iterdir()), [])
        references = self.ledger_events("AT-LEDGER-SYMLINK")
        self.assertEqual([event["status"] for event in references], ["incomplete"])
        self.assertEqual(references[0]["reason"], "attempt_artifact_root_unavailable")
        # O_DIRECTORY|O_NOFOLLOW on a symlinked component reports ENOTDIR on
        # Linux rather than ELOOP; either way the link is never traversed.
        self.assertIn(references[0]["error"]["type"], {"NotADirectoryError", "OSError"})

    def test_unmaterialized_recorded_root_is_created_not_replaced(self) -> None:
        self.add_task("AT-LEDGER-LAZYROOT", with_artifact_dir=False)
        recorded = self.artifact_base / "lazy" / "AT-LEDGER-LAZYROOT"
        self.assertFalse(recorded.exists())
        attempt = self.attempts.create_attempt(
            "AT-LEDGER-LAZYROOT", artifact_root=recorded
        )

        self.attempts.close_attempt(
            attempt.attempt_id,
            status="completed",
            reason_code="runtime_completed",
            actor="tester",
        )

        payload = self.read_ledger(recorded, attempt.attempt_id)
        self.assertEqual(payload["attempt_id"], attempt.attempt_id)
        self.assertEqual(
            [event["status"] for event in self.ledger_events("AT-LEDGER-LAZYROOT")],
            ["published"],
        )

    def test_publication_failure_does_not_alter_the_terminal_lifecycle(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-WRITEFAIL")
        claim = self.claim("AT-LEDGER-WRITEFAIL")

        with mock.patch.object(
            outcome_ledger, "publish_once", side_effect=OSError("disk full")
        ):
            lease = self.release(
                claim,
                attempt_status="completed",
                task_status="completed",
                reason_code="runtime_completed",
            )

        self.assertFalse(lease.is_active)
        attempt = self.attempts.get_attempt(claim.attempt_id)
        self.assertFalse(attempt.is_active)
        self.assertEqual(attempt.status, "completed")
        self.assertEqual(self.store.get_task("AT-LEDGER-WRITEFAIL").status, "completed")
        self.assertFalse(self.ledger_path(artifact_dir, claim.attempt_id).exists())
        references = self.ledger_events("AT-LEDGER-WRITEFAIL")
        self.assertEqual(
            [(event["status"], event["reason"]) for event in references],
            [("incomplete", "ledger_publication_failed")],
        )
        self.assertEqual(references[0]["error"]["type"], "OSError")

    def test_artifact_index_failure_is_reported_as_incomplete_evidence(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-INDEXFAIL")
        claim = self.claim("AT-LEDGER-INDEXFAIL")

        with mock.patch.object(
            TaskMirrorStore, "record_task_artifact", side_effect=sqlite3.OperationalError("locked")
        ):
            self.release(
                claim,
                attempt_status="completed",
                task_status="completed",
                reason_code="runtime_completed",
            )

        self.assertTrue(self.ledger_path(artifact_dir, claim.attempt_id).exists())
        self.assertEqual(self.attempts.get_attempt(claim.attempt_id).status, "completed")
        references = self.ledger_events("AT-LEDGER-INDEXFAIL")
        self.assertEqual([event["status"] for event in references], ["incomplete"])
        self.assertFalse(references[0]["artifact_indexed"])

    def test_event_index_failure_never_escalates_into_the_caller(self) -> None:
        self.add_task("AT-LEDGER-EVENTFAIL")
        claim = self.claim("AT-LEDGER-EVENTFAIL")
        with mock.patch.object(
            TaskMirrorStore, "record_task_event", side_effect=sqlite3.OperationalError("locked")
        ):
            lease = self.release(
                claim,
                attempt_status="completed",
                task_status="completed",
                reason_code="runtime_completed",
            )
        self.assertFalse(lease.is_active)
        self.assertEqual(self.attempts.get_attempt(claim.attempt_id).status, "completed")

    def test_unknown_closeout_route_is_a_programming_error(self) -> None:
        with self.assertRaises(ValueError):
            record_terminal_attempt_outcome(
                db_path=self.db_path,
                attempt_id="attempt-x",
                closeout_route="made_up_route",
            )

    def test_unsafe_attempt_id_still_yields_a_single_path_component(self) -> None:
        self.assertEqual(
            ledger_filename("attempt-abc123"), "outcome-ledger-attempt-abc123.json"
        )
        name = ledger_filename("../../etc/passwd")
        self.assertEqual(Path(name).name, name)
        self.assertTrue(name.startswith("outcome-ledger-sha256-"))
        self.assertEqual(name, ledger_filename("../../etc/passwd"))


class LaterObservationTests(OutcomeLedgerTestCase):
    def _published(self) -> tuple[Path, str]:
        artifact_dir = self.add_task("AT-LEDGER-OBS")
        claim = self.claim("AT-LEDGER-OBS")
        self.release(
            claim,
            attempt_status="waiting_approval",
            task_status="waiting_approval",
            reason_code="runtime_waiting_approval",
        )
        return artifact_dir, claim.attempt_id

    def test_observation_is_appended_without_rewriting_the_base_ledger(self) -> None:
        artifact_dir, attempt_id = self._published()
        base_bytes = self.ledger_path(artifact_dir, attempt_id).read_bytes()

        reference = record_attempt_outcome_observation(
            db_path=self.db_path,
            attempt_id=attempt_id,
            observation_type="task_closeout_confirmed",
            observed_fields={"human_decision": {"decision": "merged_on_github"}},
            source_reference="/tmp/evidence/task_closeout.json",
            actor="task_closeout_confirm",
            expected_task_key="AT-LEDGER-OBS",
        )

        self.assertEqual(reference["status"], "published")
        self.assertEqual(self.ledger_path(artifact_dir, attempt_id).read_bytes(), base_bytes)
        observation = json.loads(Path(reference["path"]).read_text())
        self.assertEqual(observation["record_type"], "later_observation")
        self.assertEqual(observation["attempt_id"], attempt_id)
        self.assertFalse(observation["overwrites_base_ledger"])
        self.assertEqual(
            observation["base_ledger"]["path"],
            str(self.ledger_path(artifact_dir, attempt_id)),
        )
        self.assertEqual(
            observation["fields"]["human_decision"]["provenance"], "observed"
        )
        # Base ledger still reports the future fact as unknown.
        base = self.read_ledger(artifact_dir, attempt_id)
        self.assertEqual(base["fields"]["human_decision"]["provenance"], "unknown")

    def test_two_observations_are_separate_uniquely_identified_artifacts(self) -> None:
        artifact_dir, attempt_id = self._published()
        first = record_attempt_outcome_observation(
            db_path=self.db_path,
            attempt_id=attempt_id,
            observation_type="task_closeout_confirmed",
            observed_fields={"human_decision": {"decision": "merged_on_github"}},
            source_reference="/tmp/one.json",
            actor="operator",
        )
        second = record_attempt_outcome_observation(
            db_path=self.db_path,
            attempt_id=attempt_id,
            observation_type="rollback_observed",
            observed_fields={"rollback_result": {"reverted": True, "commit": "abc"}},
            source_reference="/tmp/two.json",
            actor="operator",
        )
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(first["status"], "published")
        self.assertEqual(second["status"], "published")
        self.assertEqual(len(list(artifact_dir.glob("*-observation-*.json"))), 2)

    def test_observation_refuses_fields_that_exist_at_closeout(self) -> None:
        _, attempt_id = self._published()
        with self.assertRaises(ValueError):
            record_attempt_outcome_observation(
                db_path=self.db_path,
                attempt_id=attempt_id,
                observation_type="bad",
                observed_fields={"final_status": "completed"},
                source_reference="/tmp/x.json",
                actor="operator",
            )

    def test_observation_cannot_manufacture_a_ledger_for_an_unterminalized_attempt(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-NOBASE")
        claim = self.claim("AT-LEDGER-NOBASE")
        reference = record_attempt_outcome_observation(
            db_path=self.db_path,
            attempt_id=claim.attempt_id,
            observation_type="task_closeout_confirmed",
            observed_fields={"human_decision": {"decision": "merged_on_github"}},
            source_reference="/tmp/x.json",
            actor="operator",
        )
        self.assertEqual(reference["status"], "incomplete")
        self.assertTrue(reference["reason"].startswith("base_ledger_"))
        self.assertEqual(list(artifact_dir.glob("*-observation-*.json")), [])

    def test_observation_refuses_a_mismatched_task_binding(self) -> None:
        _, attempt_id = self._published()
        self.add_task("AT-LEDGER-OBS-OTHER")
        reference = record_attempt_outcome_observation(
            db_path=self.db_path,
            attempt_id=attempt_id,
            observation_type="task_closeout_confirmed",
            observed_fields={"post_merge_result": {"task_status": "completed"}},
            source_reference="/tmp/x.json",
            actor="operator",
            expected_task_key="AT-LEDGER-OBS-OTHER",
        )
        self.assertEqual(reference["status"], "incomplete")
        self.assertEqual(reference["reason"], "task_identity_mismatch")


class ReadbackTests(OutcomeLedgerTestCase):
    def test_existing_indexes_expose_the_ledger_and_its_provenance(self) -> None:
        artifact_dir = self.add_task("AT-LEDGER-READBACK")
        claim = self.claim("AT-LEDGER-READBACK")
        self.release(
            claim,
            attempt_status="completed",
            task_status="completed",
            reason_code="runtime_completed",
        )

        artifacts = [
            record
            for record in self.store.list_task_artifacts("AT-LEDGER-READBACK")
            if record.artifact_type == "other"
        ]
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(
            artifacts[0].path, self.ledger_path(artifact_dir, claim.attempt_id)
        )

        events = [
            event
            for event in self.store.list_task_events("AT-LEDGER-READBACK")
            if event.source == "outcome_ledger"
        ]
        self.assertEqual(len(events), 1)
        reference = json.loads(events[0].payload_json)
        self.assertEqual(reference["attempt_id"], claim.attempt_id)
        self.assertEqual(reference["status"], "published")
        self.assertFalse(reference["lifecycle_authority"])

        payload = json.loads(Path(reference["path"]).read_text())
        self.assertEqual(sorted(payload["fields"]), sorted(outcome_ledger.LEDGER_FIELDS))
        self.assertFalse(payload["merge_authority"])


if __name__ == "__main__":  # pragma: no cover - direct invocation helper
    unittest.main()
