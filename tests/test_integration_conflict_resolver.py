"""Tests for the bounded AI conflict resolver interface (spec §27, §27.2.1)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_taskflow import integration_schema as schema
from agent_taskflow.integration_conflict_resolver import (
    ConflictResolutionOutcome,
    ConflictResolutionRequest,
    NullConflictResolver,
    resolve_conflicts,
)
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


class RecordingResolver:
    name = "recording"

    def __init__(self, *, resolved: bool, explanation: str = "explained") -> None:
        self._resolved = resolved
        self._explanation = explanation
        self.requests: list[ConflictResolutionRequest] = []

    def resolve(self, request: ConflictResolutionRequest) -> ConflictResolutionOutcome:
        self.requests.append(request)
        return ConflictResolutionOutcome(
            resolver=self.name, resolved=self._resolved, explanation=self._explanation
        )


class ConflictResolverTestCase(unittest.TestCase):
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
                task_key="AT-801",
                project="demo",
                status=schema.INTEGRATING,
                repo_path=self.root / "repo",
            )
        )
        self.request = ConflictResolutionRequest(
            task_key="AT-801",
            prompt="Separate the ending page image",
            worktree_path=self.root,
            conflict_hunks=({"path": "shared.txt", "hunk": "<<<<<<< HEAD"},),
            task_diff="diff --git a/shared.txt",
            target_ref="origin/main",
            files=("shared.txt",),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()


class DefaultResolverTests(ConflictResolverTestCase):
    def test_the_default_resolver_resolves_nothing(self) -> None:
        outcome = NullConflictResolver().resolve(self.request)
        self.assertFalse(outcome.resolved)
        self.assertTrue(outcome.explanation)

    def test_no_resolver_configured_means_unresolved(self) -> None:
        outcome = resolve_conflicts(
            self.request,
            resolver=None,
            integration_store=self.integration,
            integration_run_id="run-1",
        )
        self.assertFalse(outcome.resolved)


class EvidenceTests(ConflictResolverTestCase):
    def test_hunks_and_explanation_are_persisted_on_failure(self) -> None:
        resolve_conflicts(
            self.request,
            resolver=RecordingResolver(resolved=False, explanation="could not reconcile"),
            integration_store=self.integration,
            integration_run_id="run-1",
        )
        rows = self.integration.list_conflict_evidence("AT-801")
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["resolved"], False)
        self.assertEqual(rows[0]["explanation"], "could not reconcile")
        self.assertEqual(rows[0]["conflict_hunks"][0]["path"], "shared.txt")

    def test_hunks_and_explanation_are_persisted_on_success(self) -> None:
        outcome = resolve_conflicts(
            self.request,
            resolver=RecordingResolver(resolved=True, explanation="took both sides"),
            integration_store=self.integration,
            integration_run_id="run-1",
        )
        self.assertTrue(outcome.resolved)
        rows = self.integration.list_conflict_evidence("AT-801")
        self.assertIs(rows[0]["resolved"], True)
        self.assertEqual(rows[0]["explanation"], "took both sides")

    def test_the_resolver_receives_the_bounded_input_set(self) -> None:
        resolver = RecordingResolver(resolved=True)
        resolve_conflicts(
            self.request,
            resolver=resolver,
            integration_store=self.integration,
            integration_run_id="run-1",
        )
        request = resolver.requests[0]
        self.assertEqual(request.prompt, "Separate the ending page image")
        self.assertEqual(request.conflict_hunks[0]["path"], "shared.txt")
        self.assertEqual(request.target_ref, "origin/main")
        self.assertEqual(request.files, ("shared.txt",))

    def test_a_resolver_cannot_report_an_approval(self) -> None:
        outcome = ConflictResolutionOutcome(resolver="x", resolved=True, explanation="e")
        self.assertFalse(hasattr(outcome, "approved"))
        self.assertFalse(hasattr(outcome, "merged"))


if __name__ == "__main__":
    unittest.main()
