"""Tests for agent_taskflow.integration_store (V1 Step 2 persistence)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_taskflow import integration_schema as schema
from agent_taskflow.integration_store import IntegrationStore, IntegrationStoreError
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


class IntegrationStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.integration = IntegrationStore(self.db_path)
        self.integration.init_db()
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-501",
                project="demo",
                status=schema.READY_FOR_INTEGRATION,
                repo_path=self.root / "repo",
            )
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()


class PrStateOwnershipTests(IntegrationStoreTestCase):
    def test_public_table_columns_match_the_spec_field_list(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(task_pr_state)")]
        self.assertEqual(columns[0], "task_key")
        self.assertEqual(tuple(columns[1:]), schema.TICKET_PR_FIELD_NAMES)

    def test_missing_row_reads_as_spec_defaults(self) -> None:
        state = self.integration.get_pr_state("AT-501")
        self.assertEqual(state, schema.default_pr_state())

    def test_update_persists_and_leaves_other_fields_untouched(self) -> None:
        self.integration.update_pr_state("AT-501", pr_number=42, pr_state="open")
        self.integration.update_pr_state("AT-501", pr_head_sha="abc123")
        state = self.integration.get_pr_state("AT-501")
        self.assertEqual(state["pr_number"], 42)
        self.assertEqual(state["pr_state"], "open")
        self.assertEqual(state["pr_head_sha"], "abc123")
        self.assertIs(state["pr_merged"], False)
        self.assertEqual(state["reintegration_count"], 0)

    def test_update_rejects_fields_outside_the_spec_list(self) -> None:
        with self.assertRaises(ValueError):
            self.integration.update_pr_state("AT-501", pr_squashed=True)

    def test_update_rejects_invalid_enum_values(self) -> None:
        with self.assertRaises(ValueError):
            self.integration.update_pr_state("AT-501", ci_status="green")

    def test_booleans_round_trip_as_python_bools(self) -> None:
        self.integration.update_pr_state("AT-501", pr_merged=True, reintegration_required=True)
        state = self.integration.get_pr_state("AT-501")
        self.assertIs(state["pr_merged"], True)
        self.assertIs(state["reintegration_required"], True)

    def test_increment_reintegration_count_is_explicit(self) -> None:
        self.assertEqual(self.integration.increment_reintegration_count("AT-501"), 1)
        self.assertEqual(self.integration.increment_reintegration_count("AT-501"), 2)
        self.assertEqual(self.integration.get_pr_state("AT-501")["reintegration_count"], 2)

    def test_unknown_task_is_rejected(self) -> None:
        with self.assertRaises(IntegrationStoreError):
            self.integration.update_pr_state("AT-NOPE", pr_number=1)


class PrivateIntegrationStateTests(IntegrationStoreTestCase):
    def test_private_fields_are_not_in_the_public_table(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            public = [row[1] for row in conn.execute("PRAGMA table_info(task_pr_state)")]
        self.assertNotIn("previous_integrated_base_sha", public)
        self.assertNotIn("new_target_sha", public)

    def test_private_state_round_trips(self) -> None:
        self.integration.update_integration_state(
            "AT-501",
            previous_integrated_base_sha="old",
            new_target_sha="new",
            behind_count=3,
        )
        state = self.integration.get_integration_state("AT-501")
        self.assertEqual(state["previous_integrated_base_sha"], "old")
        self.assertEqual(state["new_target_sha"], "new")
        self.assertEqual(state["behind_count"], 3)

    def test_private_state_rejects_unknown_fields(self) -> None:
        with self.assertRaises(ValueError):
            self.integration.update_integration_state("AT-501", not_a_field=1)


class EvidenceTests(IntegrationStoreTestCase):
    def test_validator_evidence_records_every_spec_field(self) -> None:
        self.integration.record_validator_evidence(
            "AT-501",
            integration_run_id="run-1",
            validator="pytest",
            command=("python", "-m", "pytest"),
            status="failed",
            exit_code=1,
            output="2 failed",
            branch_sha="branchsha",
            target_sha="targetsha",
            diff_context="M agent_taskflow/x.py",
        )
        rows = self.integration.list_validator_evidence("AT-501")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["validator"], "pytest")
        self.assertEqual(row["command"], ["python", "-m", "pytest"])
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["output"], "2 failed")
        self.assertEqual(row["branch_sha"], "branchsha")
        self.assertEqual(row["target_sha"], "targetsha")
        self.assertEqual(row["diff_context"], "M agent_taskflow/x.py")

    def test_review_evidence_records_retry_context(self) -> None:
        self.integration.record_review_evidence(
            "AT-501",
            pr_number=42,
            pr_url="https://github.com/owner/repo/pull/42",
            review_decision="changes_requested",
            reviewer="octocat",
            reviewed_at="2026-09-10T00:00:00Z",
            reviewed_head_sha="headsha",
            comments=[{"author": "octocat", "body": "please fix"}],
        )
        rows = self.integration.list_review_evidence("AT-501")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reviewer"], "octocat")
        self.assertEqual(rows[0]["reviewed_head_sha"], "headsha")
        self.assertEqual(rows[0]["comments"][0]["body"], "please fix")

    def test_conflict_evidence_persists_hunks_and_explanation(self) -> None:
        self.integration.record_conflict_evidence(
            "AT-501",
            integration_run_id="run-1",
            resolver="null",
            resolved=False,
            conflict_hunks=[{"path": "shared.txt", "hunk": "<<<<<<< HEAD"}],
            explanation="no resolver configured",
        )
        rows = self.integration.list_conflict_evidence("AT-501")
        self.assertEqual(rows[0]["conflict_hunks"][0]["path"], "shared.txt")
        self.assertEqual(rows[0]["explanation"], "no resolver configured")
        self.assertIs(rows[0]["resolved"], False)


class ConflictVerificationTests(IntegrationStoreTestCase):
    def test_verification_is_attached_to_its_conflict_run(self) -> None:
        self.integration.record_conflict_evidence(
            "AT-501", integration_run_id="run-1", resolver="ai", resolved=True,
            conflict_hunks=[{"path": "shared.txt", "hunk": "<<<<<<< HEAD"}],
            explanation="took both sides",
        )
        self.assertIsNone(self.integration.list_conflict_evidence("AT-501")[0]["verification"])
        checks = [{"name": "worktree_clean", "passed": False, "detail": "?? stray.orig"}]
        self.integration.record_conflict_verification(
            "AT-501", integration_run_id="run-1", checks=checks
        )
        row = self.integration.list_conflict_evidence("AT-501")[0]
        self.assertEqual(row["verification"], checks)
        # The resolver's claim is kept as-is next to the control plane's verdict.
        self.assertIs(row["resolved"], True)


class MigrationTests(IntegrationStoreTestCase):
    def test_init_db_is_idempotent(self) -> None:
        self.integration.init_db()
        self.integration.init_db()
        self.integration.update_pr_state("AT-501", pr_number=7)
        self.assertEqual(self.integration.get_pr_state("AT-501")["pr_number"], 7)

    def test_tables_are_created_by_the_base_store_init(self) -> None:
        other_db = self.root / "other.db"
        TaskMirrorStore(other_db).init_db()
        with sqlite3.connect(other_db) as conn:
            names = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        for table in (
            "task_pr_state",
            "task_integration_state",
            "integration_queue",
            "integration_locks",
            "integration_validator_evidence",
            "integration_review_evidence",
            "integration_conflict_evidence",
        ):
            self.assertIn(table, names)



class OpenPrPickupScopeTests(IntegrationStoreTestCase):
    """§32.0 — a watcher tick's pick-up set is its own repository's open PRs."""

    def _ticket(self, key: str, *, pr_url: str | None, pr_number: int | None = 1,
                pr_state: str | None = "open") -> None:
        self.store.upsert_task(
            TaskRecord(task_key=key, project="demo", status="waiting_for_review",
                       repo_path=self.root / "repo")
        )
        fields = {"pr_state": pr_state}
        if pr_number is not None:
            fields["pr_number"] = pr_number
        if pr_url is not None:
            fields["pr_url"] = pr_url
        self.integration.update_pr_state(key, **fields)

    def test_only_this_repositorys_open_prs_are_returned(self) -> None:
        self._ticket("AT-611", pr_url="https://github.com/owner/my_repo/pull/1")
        self._ticket("AT-612", pr_url="https://github.com/owner/myXrepo/pull/1")
        self._ticket("AT-613", pr_url="https://github.com/xowner/my_repo/pull/1")
        self._ticket("AT-614", pr_url="https://github.com/owner/my_repo-b/pull/1")
        self._ticket("AT-615", pr_url="https://github.com/owner/my_repo/pull/2", pr_state="closed")
        self._ticket("AT-616", pr_url=None, pr_number=None)
        self.assertEqual(
            [s["task_key"] for s in self.integration.list_open_pr_states("owner/my_repo")],
            ["AT-611"],
        )

    def test_the_same_pr_number_in_two_repositories_is_kept_apart(self) -> None:
        self._ticket("AT-621", pr_url="https://github.com/owner/repo-a/pull/42")
        self._ticket("AT-622", pr_url="https://github.com/owner/repo-b/pull/42")
        self.assertEqual(
            [s["task_key"] for s in self.integration.list_open_pr_states("owner/repo-a")],
            ["AT-621"],
        )
        self.assertEqual(
            [s["task_key"] for s in self.integration.list_open_pr_states("owner/repo-b")],
            ["AT-622"],
        )

    def test_repository_comparison_is_case_insensitive_like_github(self) -> None:
        self._ticket("AT-631", pr_url="https://github.com/Owner/My_Repo/pull/3")
        self.assertEqual(
            [s["task_key"] for s in self.integration.list_open_pr_states("owner/my_repo")],
            ["AT-631"],
        )

    def test_a_malformed_tick_repository_is_rejected(self) -> None:
        for repo in ("", "owner", "owner/", "/name", "owner/name/extra"):
            with self.subTest(repo=repo):
                with self.assertRaises(ValueError):
                    self.integration.list_open_pr_states(repo)


if __name__ == "__main__":
    unittest.main()
