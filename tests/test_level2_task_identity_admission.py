"""Level 2 Task identity is settled atomically at task admission.

Every ingestion path must persist a new task and its canonical Level 2 identity
in one transaction, so no observer and no crash can see the task classified
legacy. Historical tasks keep their existing identity.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.github_issue_ingestion import (
    GitHubIssueIngestionRequest,
    GitHubIssueSnapshot,
    ingest_github_issue,
)
from agent_taskflow.github_issue_intake import (
    GitHubIssueIntakeRequest as SelectedIssueIntakeRequest,
    intake_selected_github_issues as intake_selected_issues,
)
from agent_taskflow.github_issue_intake_gate import (
    GitHubIssueIntakeRequest as IntakeGateRequest,
    intake_selected_github_issues as intake_through_gate,
)
from agent_taskflow.level2_execution_authority import (
    Level2ExecutionAuthorityError,
    ensure_level2_task_identity,
    is_level2_task,
)
from agent_taskflow.store import TaskMirrorStore


ISSUE_NUMBER = 4242


def issue_snapshot(number: int = ISSUE_NUMBER) -> GitHubIssueSnapshot:
    return GitHubIssueSnapshot(
        number=number,
        title=f"Issue {number}",
        body="Issue body",
        state="open",
        labels=("ready",),
        author="octocat",
        url=f"https://github.com/anderson930420/agent-taskflow/issues/{number}",
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-02T00:00:00Z",
    )


class _AdmissionPathCase(unittest.TestCase):
    """Shared workspace for the three task-creating ingestion paths."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.artifact_root = self.root / "artifacts"
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # -- the three admission paths ----------------------------------------

    def ingest(self) -> Any:
        return ingest_github_issue(
            GitHubIssueIngestionRequest(
                repo="anderson930420/agent-taskflow",
                issue_number=ISSUE_NUMBER,
                local_repo_path=self.repo,
                artifact_root=self.artifact_root,
            ),
            store=self.store,
            fetcher=lambda repo, number: issue_snapshot(),
        )

    def intake_selected(self) -> Any:
        return intake_selected_issues(
            SelectedIssueIntakeRequest(
                repo="anderson930420/agent-taskflow",
                issue_numbers=(ISSUE_NUMBER,),
                db_path=self.db_path,
                local_repo_path=self.repo,
                artifact_root=self.artifact_root,
            ),
            store=self.store,
            fetcher=lambda repo, number: issue_snapshot(),
        )

    def intake_gate(self) -> Any:
        return intake_through_gate(
            IntakeGateRequest(
                repo="anderson930420/agent-taskflow",
                issue_numbers=(ISSUE_NUMBER,),
                repo_path=self.repo,
                artifact_root=self.artifact_root,
                db_path=self.db_path,
                dry_run=False,
            ),
            store=self.store,
            fetcher=lambda repo, number: issue_snapshot(),
        )

    def admission_paths(self) -> dict[str, Any]:
        return {
            "github_issue_ingestion": self.ingest,
            "github_issue_intake": self.intake_selected,
            "github_issue_intake_gate": self.intake_gate,
        }

    def task_keys(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            return [str(row[0]) for row in conn.execute("SELECT task_key FROM tasks")]


class FreshTaskIsLevel2BeforeExecutionTests(_AdmissionPathCase):
    def test_every_admission_path_creates_a_canonical_level2_task(self) -> None:
        for name, admit in self.admission_paths().items():
            with self.subTest(path=name):
                self.setUp()
                try:
                    admit()
                    keys = self.task_keys()
                    self.assertEqual(len(keys), 1, keys)
                    # Classification is settled by the time the task is
                    # readable, i.e. before any execution can be decided.
                    self.assertTrue(is_level2_task(self.db_path, keys[0]))
                    identity = AttemptStore(self.db_path).get_task_identity(keys[0])
                    assert identity is not None
                    self.assertFalse(identity.is_legacy)
                    self.assertEqual(identity.task_class, "canonical")
                finally:
                    self.tearDown()

    def test_reingestion_preserves_an_existing_legacy_identity(self) -> None:
        self.ingest()
        task_key = self.task_keys()[0]
        attempts = AttemptStore(self.db_path)
        identity = attempts.get_task_identity(task_key)
        assert identity is not None
        # Simulate a historical task that predates canonical admission.
        attempts.register_task_identity(
            task_key,
            task_class="legacy",
            task_id=identity.task_id,
            is_legacy=True,
        )

        result = self.ingest()

        self.assertEqual(result.status, "reused")
        self.assertFalse(is_level2_task(self.db_path, task_key))
        preserved = attempts.get_task_identity(task_key)
        assert preserved is not None
        self.assertEqual(preserved.task_id, identity.task_id)

    def test_reingestion_preserves_an_existing_canonical_identity(self) -> None:
        self.ingest()
        task_key = self.task_keys()[0]
        before = AttemptStore(self.db_path).get_task_identity(task_key)
        assert before is not None

        self.ingest()

        after = AttemptStore(self.db_path).get_task_identity(task_key)
        assert after is not None
        self.assertEqual(after.task_id, before.task_id)
        self.assertTrue(is_level2_task(self.db_path, task_key))


class AdmissionAtomicityTests(_AdmissionPathCase):
    def test_promotion_failure_persists_no_task_on_any_path(self) -> None:
        for name, admit in self.admission_paths().items():
            with self.subTest(path=name):
                self.setUp()
                try:
                    with mock.patch(
                        "agent_taskflow.level2_execution_authority."
                        "_promote_task_identity_in_connection",
                        side_effect=Level2ExecutionAuthorityError("promotion failed"),
                    ):
                        with self.assertRaises(Level2ExecutionAuthorityError):
                            admit()
                    # Neither half of the admission may survive.
                    self.assertEqual(self.task_keys(), [])
                    self.assertEqual(self.store.list_tasks(), [])
                finally:
                    self.tearDown()

    def test_concurrent_first_admission_yields_one_canonical_task(self) -> None:
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def admit() -> None:
            store = TaskMirrorStore(self.db_path)
            barrier.wait(timeout=10)
            try:
                ingest_github_issue(
                    GitHubIssueIngestionRequest(
                        repo="anderson930420/agent-taskflow",
                        issue_number=ISSUE_NUMBER,
                        local_repo_path=self.repo,
                        artifact_root=self.artifact_root,
                    ),
                    store=store,
                    fetcher=lambda repo, number: issue_snapshot(),
                )
            except BaseException as exc:  # noqa: BLE001 - recorded for assertion.
                errors.append(exc)

        threads = [threading.Thread(target=admit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        keys = self.task_keys()
        self.assertEqual(len(keys), 1, keys)
        # Whichever writer won, the surviving task is canonical: no interleaving
        # can leave a created task classified legacy.
        self.assertTrue(is_level2_task(self.db_path, keys[0]))


class EnsureLevel2TaskIdentityTests(_AdmissionPathCase):
    def test_is_idempotent_for_an_already_promoted_task(self) -> None:
        self.ingest()
        task_key = self.task_keys()[0]
        before = AttemptStore(self.db_path).get_task_identity(task_key)
        assert before is not None

        ensure_level2_task_identity(self.db_path, task_key)
        ensure_level2_task_identity(self.db_path, task_key)

        after = AttemptStore(self.db_path).get_task_identity(task_key)
        assert after is not None
        self.assertEqual(after.task_id, before.task_id)
        self.assertFalse(after.is_legacy)

    def test_in_transaction_promotion_matches_standalone_promotion(self) -> None:
        self.ingest()
        task_key = self.task_keys()[0]
        attempts = AttemptStore(self.db_path)
        identity = attempts.get_task_identity(task_key)
        assert identity is not None
        attempts.register_task_identity(
            task_key,
            task_class="legacy",
            task_id=identity.task_id,
            is_legacy=True,
        )

        from agent_taskflow.store import connect

        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            ensure_level2_task_identity(self.db_path, task_key, connection=conn)

        promoted = attempts.get_task_identity(task_key)
        assert promoted is not None
        self.assertFalse(promoted.is_legacy)
        self.assertEqual(promoted.task_class, "canonical")
        self.assertEqual(promoted.task_id, identity.task_id)

    def test_missing_task_fails_closed_in_a_caller_transaction(self) -> None:
        from agent_taskflow.store import connect

        AttemptStore(self.db_path).init_db()
        with self.assertRaises(Level2ExecutionAuthorityError):
            with connect(self.db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                ensure_level2_task_identity(self.db_path, "AT-GH-MISSING", connection=conn)


if __name__ == "__main__":
    unittest.main()
