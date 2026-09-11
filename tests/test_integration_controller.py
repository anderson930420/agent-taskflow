"""Tests for agent_taskflow.integration_controller (spec §22-§27, §29, §31)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_taskflow import integration_schema as schema
from agent_taskflow import integration_git as git_ops
from agent_taskflow.integration_conflict_resolver import (
    ConflictResolutionOutcome,
    ConflictResolutionRequest,
)
from agent_taskflow.integration_controller import (
    IntegrationRequest,
    integrate_task,
)
from agent_taskflow.integration_queue import IntegrationLock, enqueue_for_integration
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.integration_validators import IntegrationValidatorSpec
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.github_pr_adapter import GitHubPrAdapter
from v1_step2_fixtures import FakeGhRunner, GitFixture  # noqa: E402


GREEN = (IntegrationValidatorSpec(name="unit", command=("true",)),)
RED = (IntegrationValidatorSpec(name="unit", command=("false",)),)


class TakeBothSidesResolver:
    """A stand-in bounded resolver: keeps the task side of every conflict."""

    name = "test-resolver"

    def __init__(self, *, succeed: bool = True) -> None:
        self.succeed = succeed

    def resolve(self, request: ConflictResolutionRequest) -> ConflictResolutionOutcome:
        if not self.succeed:
            return ConflictResolutionOutcome(
                resolver=self.name, resolved=False, explanation="cannot reconcile"
            )
        worktree = Path(request.worktree_path)
        for entry in request.conflict_hunks:
            path = worktree / entry["path"]
            merged = "\n".join(
                line
                for line in path.read_text(encoding="utf-8").splitlines()
                if not line.startswith(("<<<<<<<", "=======", ">>>>>>>"))
            )
            path.write_text(merged + "\n", encoding="utf-8")
        git_ops.stage_all(worktree)
        git_ops.commit_conflict_resolution(worktree, "resolve integration conflict")
        return ConflictResolutionOutcome(
            resolver=self.name, resolved=True, explanation="kept both sides"
        )


class ControllerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = GitFixture(self.root)
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.integration = IntegrationStore(self.db_path)
        self.integration.init_db()
        self.gh_runner = FakeGhRunner()
        self.github = GitHubPrAdapter("owner/repo", runner=self.gh_runner)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_task(self, task_key: str, *, status: str = schema.READY_FOR_INTEGRATION) -> Path:
        worktree = self.fixture.create_task_worktree(task_key)
        artifact_dir = self.artifacts / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="demo",
                status=status,
                repo_path=self.fixture.repo,
                title=f"{task_key} title",
                artifact_dir=artifact_dir,
            )
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=task_key,
                repo_path=self.fixture.repo,
                worktree_path=worktree,
                branch=f"task/{task_key}",
                base_branch="main",
                base_sha=self.fixture.target_sha(),
                status="active",
            )
        )
        return worktree

    def request(self, task_key: str, **overrides) -> IntegrationRequest:
        kwargs = dict(
            task_key=task_key,
            repo="owner/repo",
            db_path=self.db_path,
            target_branch="main",
            remote="origin",
            validator_specs=GREEN,
            dry_run=False,
            confirm_integration=True,
            owner="test-runtime",
        )
        kwargs.update(overrides)
        return IntegrationRequest(**kwargs)

    def integrate(self, task_key: str, **overrides):
        return integrate_task(
            self.request(task_key, **overrides),
            store=self.store,
            integration_store=self.integration,
            github=self.github,
        )

    def status_of(self, task_key: str) -> str:
        return self.store.get_task(task_key).status


class EntryGuardTests(ControllerTestCase):
    def test_only_ready_for_integration_tickets_are_integrated(self) -> None:
        self.make_task("AT-101", status=schema.NEEDS_REVIEW)
        result = self.integrate("AT-101")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_REVIEW)

    def test_dry_run_mutates_nothing(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        result = self.integrate("AT-101", dry_run=True, confirm_integration=False)
        self.assertEqual(result.status, "dry_run")
        self.assertTrue(result.confirmation_required)
        self.assertEqual(self.status_of("AT-101"), schema.READY_FOR_INTEGRATION)
        self.assertEqual(self.gh_runner.calls, [])
        self.assertIsNone(self.integration.get_pr_state("AT-101")["pr_number"])

    def test_confirmation_is_required_even_when_dry_run_is_off(self) -> None:
        self.make_task("AT-101")
        result = self.integrate("AT-101", dry_run=False, confirm_integration=False)
        self.assertEqual(result.status, "dry_run")
        self.assertEqual(self.gh_runner.calls, [])


class InitialIntegrationTests(ControllerTestCase):
    def test_initial_integration_reaches_needs_review_with_a_pr(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        result = self.integrate("AT-101")
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(result.mode, "initial")
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_REVIEW)
        self.assertEqual(result.pr_number, 42)
        self.assertEqual(self.integration.get_pr_state("AT-101")["pr_number"], 42)
        self.assertEqual(self.integration.get_pr_state("AT-101")["pr_state"], "open")

    def test_initial_integration_uses_the_latest_target(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        advanced = self.fixture.advance_target()
        result = self.integrate("AT-101")
        self.assertEqual(result.integrated_base_sha, advanced)
        self.assertEqual(self.integration.get_pr_state("AT-101")["integrated_base_sha"], advanced)
        self.assertEqual(git_ops.behind_count(worktree, "HEAD", "origin/main"), 0)

    def test_initial_integration_rebases(self) -> None:
        worktree = self.make_task("AT-101")
        original = self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        self.fixture.advance_target()
        self.integrate("AT-101")
        # A rebase replays the commit, so the pre-rebase SHA is gone.
        self.assertNotIn(original, git_ops.rev_list(worktree, "HEAD"))

    def test_task_branch_is_published_to_the_remote(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        self.integrate("AT-101")
        from v1_step2_fixtures import git as raw_git

        self.assertIn("task/AT-101", raw_git(self.fixture.origin, "branch", "--list", "task/AT-101"))

    def test_evidence_artifact_is_written_and_recorded(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        result = self.integrate("AT-101")
        self.assertTrue(result.integration_json_path.is_file())
        payload = json.loads(result.integration_json_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["task_key"], "AT-101")
        self.assertIs(payload["safety"]["force_pushed"], False)
        self.assertIs(payload["safety"]["merged"], False)
        types = {a.artifact_type for a in self.store.list_task_artifacts("AT-101")}
        self.assertIn("integration_result", types)

    def test_lifecycle_transitions_are_auditable(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        self.integrate("AT-101")
        statuses = [
            json.loads(event.payload_json)["status"]
            for event in self.store.list_task_events("AT-101")
            if event.event_type == "status_changed"
        ]
        self.assertEqual(statuses[-2:], [schema.INTEGRATING, schema.NEEDS_REVIEW])


class ValidatorGateTests(ControllerTestCase):
    def test_red_validators_never_reach_needs_review(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        result = self.integrate("AT-101", validator_specs=RED)
        self.assertEqual(result.status, "needs_decision")
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_DECISION)
        self.assertFalse(result.validators_passed)

    def test_red_validators_do_not_push_or_create_a_pr(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        result = self.integrate("AT-101", validator_specs=RED)
        self.assertEqual(self.gh_runner.calls, [])
        self.assertIsNone(result.pr_number)
        self.assertFalse([cmd for cmd in result.git_commands if cmd[:2] == ("git", "push")])

    def test_validator_failure_does_not_auto_retry(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        self.integrate("AT-101", validator_specs=RED)
        runs = {
            row["integration_run_id"] for row in self.integration.list_validator_evidence("AT-101")
        }
        self.assertEqual(len(runs), 1)

    def test_validator_failure_persists_full_evidence(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        self.integrate("AT-101", validator_specs=RED)
        row = self.integration.list_validator_evidence("AT-101")[0]
        self.assertEqual(row["validator"], "unit")
        self.assertEqual(row["command"], ["false"])
        self.assertTrue(row["branch_sha"])
        self.assertTrue(row["target_sha"])
        self.assertIsNotNone(row["diff_context"])


class LockAndQueueTests(ControllerTestCase):
    def test_the_lock_is_released_before_review(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        result = self.integrate("AT-101")
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_REVIEW)
        self.assertIsNone(self.integration.get_integration_lock("owner/repo"))
        self.assertIs(result.lock_held_after_return, False)

    def test_multiple_same_repo_prs_can_wait_in_needs_review(self) -> None:
        for key in ("AT-101", "AT-102"):
            worktree = self.make_task(key)
            self.fixture.commit_in(worktree, f"{key}.txt", "f\n", "feature")
            self.integrate(key)
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_REVIEW)
        self.assertEqual(self.status_of("AT-102"), schema.NEEDS_REVIEW)
        self.assertNotEqual(
            self.integration.get_pr_state("AT-101")["pr_number"],
            self.integration.get_pr_state("AT-102")["pr_number"],
        )

    def test_a_held_repo_lock_serializes_integration(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        enqueue_for_integration(self.integration, "AT-101", repo="owner/repo")
        with IntegrationLock(self.integration, "owner/repo", owner="other-runtime"):
            result = self.integrate("AT-101")
        self.assertEqual(result.status, "lock_unavailable")
        self.assertEqual(self.status_of("AT-101"), schema.READY_FOR_INTEGRATION)
        self.assertEqual(self.gh_runner.calls, [])

    def test_a_different_repo_lock_does_not_block(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        with IntegrationLock(self.integration, "owner/other", owner="other-runtime"):
            result = self.integrate("AT-101")
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_REVIEW)

    def test_the_lock_is_released_when_integration_fails(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        self.integrate("AT-101", validator_specs=RED)
        self.assertIsNone(self.integration.get_integration_lock("owner/repo"))

    def test_integration_removes_the_ticket_from_the_queue(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        enqueue_for_integration(self.integration, "AT-101", repo="owner/repo")
        self.integrate("AT-101")
        from agent_taskflow.integration_queue import queue_for_repo

        self.assertEqual(queue_for_repo(self.integration, "owner/repo"), [])


class ReIntegrationTests(ControllerTestCase):
    def _published(self, task_key: str = "AT-101") -> Path:
        worktree = self.make_task(task_key)
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        self.integrate(task_key)
        return worktree

    def _requeue(self, task_key: str = "AT-101") -> None:
        self.store.update_task_status(task_key, schema.READY_FOR_INTEGRATION, source="test")
        self.integration.update_pr_state(task_key, reintegration_required=True)

    def test_reintegration_updates_the_same_pr_number(self) -> None:
        worktree = self._published()
        first_pr = self.integration.get_pr_state("AT-101")["pr_number"]
        self.fixture.advance_target()
        self._requeue()
        result = self.integrate("AT-101")
        self.assertEqual(result.mode, "reintegration")
        self.assertEqual(result.pr_number, first_pr)
        self.assertEqual(self.integration.get_pr_state("AT-101")["pr_number"], first_pr)
        self.assertEqual(
            len([c for c in self.gh_runner.calls if c[:3] == ["gh", "pr", "create"]]), 1
        )
        # Ruling 35: the same PR is updated through the REST PATCH, never
        # `gh pr edit` (which fails on gh 2.45.0), and the new body landed.
        self.assertEqual([c for c in self.gh_runner.calls if c[:3] == ["gh", "pr", "edit"]], [])
        patches = [
            c for c in self.gh_runner.calls
            if c[:5] == ["gh", "api", "-X", "PATCH", f"repos/owner/repo/pulls/{first_pr}"]
        ]
        self.assertEqual(len(patches), 1)
        self.assertIn(result.integrated_base_sha, self.gh_runner.pulls[first_pr]["body"])

    def test_reintegration_merges_rather_than_rebasing(self) -> None:
        worktree = self._published()
        published_sha = git_ops.head_sha(worktree)
        self.fixture.advance_target()
        self._requeue()
        self.integrate("AT-101")
        self.assertIn(published_sha, git_ops.rev_list(worktree, "HEAD"))

    def test_a_reintegration_pushes_only_the_allowlisted_form(self) -> None:
        self._published()
        self.fixture.advance_target()
        self._requeue()
        result = self.integrate("AT-101")
        self.assertIs(result.force_pushed, False)
        pushes = [cmd for cmd in result.git_commands if cmd[:2] == ("git", "push")]
        self.assertEqual(pushes, [("git", "push", "origin", "task/AT-101")])

    def test_reintegration_increments_the_counter(self) -> None:
        self._published()
        for expected in (1, 2):
            self.fixture.advance_target(f"advance-{expected}.txt")
            self._requeue()
            result = self.integrate("AT-101")
            self.assertEqual(result.reintegration_count, expected)
            self.assertEqual(
                self.integration.get_pr_state("AT-101")["reintegration_count"], expected
            )

    def test_reintegration_reruns_validators(self) -> None:
        self._published()
        self.fixture.advance_target()
        self._requeue()
        self.integrate("AT-101")
        runs = [
            row["integration_run_id"] for row in self.integration.list_validator_evidence("AT-101")
        ]
        self.assertEqual(len(set(runs)), 2)

    def test_reintegration_with_red_validators_stops_for_decision(self) -> None:
        self._published()
        self.fixture.advance_target()
        self._requeue()
        result = self.integrate("AT-101", validator_specs=RED)
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_DECISION)
        self.assertFalse(result.validators_passed)

    def test_reintegration_records_the_base_shas_and_clears_the_flag(self) -> None:
        self._published()
        first_base = self.integration.get_pr_state("AT-101")["integrated_base_sha"]
        advanced = self.fixture.advance_target()
        self._requeue()
        result = self.integrate("AT-101")
        self.assertEqual(result.previous_integrated_base_sha, first_base)
        self.assertEqual(result.integrated_base_sha, advanced)
        self.assertIs(self.integration.get_pr_state("AT-101")["reintegration_required"], False)
        private = self.integration.get_integration_state("AT-101")
        self.assertEqual(private["previous_integrated_base_sha"], first_base)
        self.assertEqual(private["new_target_sha"], advanced)

    def test_reintegration_publishes_the_spec_hint_block(self) -> None:
        self._published()
        self.fixture.advance_target()
        self._requeue()
        self.integrate("AT-101", trigger="AT-123 merged before this PR was reviewed.")
        body = self.gh_runner.pulls[42]["body"]
        self.assertIn("Re-integrated after target branch advanced.", body)
        self.assertIn("Previous base:", body)
        self.assertIn("AT-123", body)

    def test_a_no_op_reintegration_still_reruns_validators(self) -> None:
        """§44 — every re-integration reruns validators, even a no-op one."""
        self._published()
        self._requeue()
        result = self.integrate("AT-101")
        self.assertTrue(result.already_up_to_date)
        self.assertTrue(result.validators_passed)
        runs = {
            row["integration_run_id"] for row in self.integration.list_validator_evidence("AT-101")
        }
        self.assertEqual(len(runs), 2)
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_REVIEW)

    def test_a_no_op_reintegration_does_not_push_by_default(self) -> None:
        self._published()
        self._requeue()
        result = self.integrate("AT-101")
        self.assertFalse([cmd for cmd in result.git_commands if cmd[:2] == ("git", "push")])

    def test_a_no_op_reintegration_pushes_when_explicitly_configured(self) -> None:
        self._published()
        self._requeue()
        result = self.integrate("AT-101", push_no_op_reintegration=True)
        self.assertTrue([cmd for cmd in result.git_commands if cmd[:2] == ("git", "push")])


class ConflictTests(ControllerTestCase):
    def _conflicting(self) -> Path:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "shared.txt", "task-side\n", "task edits shared")
        self.fixture.advance_target("shared.txt", "target-side\n")
        return worktree

    def test_unresolvable_conflict_stops_for_decision(self) -> None:
        self._conflicting()
        result = self.integrate("AT-101", conflict_resolver=None)
        self.assertEqual(result.status, "needs_decision")
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_DECISION)
        self.assertEqual(self.gh_runner.calls, [])

    def test_unresolvable_conflict_persists_hunks_and_explanation(self) -> None:
        self._conflicting()
        self.integrate("AT-101", conflict_resolver=None)
        rows = self.integration.list_conflict_evidence("AT-101")
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["resolved"], False)
        self.assertTrue(rows[0]["explanation"])
        self.assertEqual(rows[0]["conflict_hunks"][0]["path"], "shared.txt")

    def test_unresolvable_conflict_leaves_a_clean_worktree(self) -> None:
        worktree = self._conflicting()
        self.integrate("AT-101", conflict_resolver=None)
        self.assertFalse(git_ops.in_progress_operation(worktree))

    def test_unresolvable_conflict_does_not_auto_retry(self) -> None:
        self._conflicting()
        self.integrate("AT-101", conflict_resolver=None)
        self.assertEqual(len(self.integration.list_conflict_evidence("AT-101")), 1)

    def test_a_resolved_conflict_continues_to_validators_and_pr(self) -> None:
        self._conflicting()
        result = self.integrate("AT-101", conflict_resolver=TakeBothSidesResolver())
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_REVIEW)
        self.assertTrue(result.conflict_resolved)
        self.assertTrue(result.validators_passed)

    def test_a_resolved_conflict_adds_the_ai_hint(self) -> None:
        self._conflicting()
        result = self.integrate("AT-101", conflict_resolver=TakeBothSidesResolver())
        self.assertIn("ai_conflict_resolution", [hint.code for hint in result.hints])
        self.assertIn("AI-assisted conflict resolution", self.gh_runner.pulls[42]["body"])

    def test_a_resolved_conflict_with_red_validators_still_stops(self) -> None:
        self._conflicting()
        result = self.integrate(
            "AT-101", conflict_resolver=TakeBothSidesResolver(), validator_specs=RED
        )
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_DECISION)
        self.assertTrue(result.conflict_resolved)
        self.assertFalse(result.validators_passed)


class NegativeScopeTests(ControllerTestCase):
    def test_an_initial_integration_pushes_only_the_allowlisted_form(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        result = self.integrate("AT-101")
        pushes = [cmd for cmd in result.git_commands if cmd[:2] == ("git", "push")]
        self.assertEqual(pushes, [("git", "push", "origin", "task/AT-101")])

    def test_an_initial_integration_does_not_push_the_target_branch(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        result = self.integrate("AT-101")
        for cmd in result.git_commands:
            if cmd[:2] == ("git", "push"):
                self.assertNotIn("main", cmd)
                self.assertFalse([part for part in cmd if part.endswith(":main")])

    def test_an_initial_integration_issues_no_gh_merge(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        self.integrate("AT-101")
        for call in self.gh_runner.calls:
            self.assertNotIn("merge", call)

    def test_the_result_reports_that_nothing_was_merged_or_cleaned_up(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        result = self.integrate("AT-101")
        self.assertIs(result.merged, False)
        self.assertIs(result.cleanup_performed, False)
        self.assertIs(result.force_pushed, False)

    def test_integration_never_reaches_completed(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        result = self.integrate("AT-101")
        self.assertNotEqual(result.final_task_status, schema.COMPLETED)
        self.assertNotEqual(self.status_of("AT-101"), schema.COMPLETED)

    def test_the_worktree_survives_integration(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        self.integrate("AT-101")
        self.assertTrue(worktree.is_dir())



class AlreadyMergedTests(ControllerTestCase):
    def test_a_pr_that_is_already_merged_is_never_reintegrated(self) -> None:
        """The §32 watcher can record a human merge on a Ticket queued for
        re-integration; integrating it would only push to a dead branch."""
        self.make_task("AT-101")
        self.integration.update_pr_state(
            "AT-101",
            pr_number=42,
            pr_state="closed",
            pr_merged=True,
            merge_commit_sha="abc123",
        )
        result = self.integrate("AT-101")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("already merged", result.summary)
        self.assertEqual(self.gh_runner.calls, [])
        self.assertEqual(result.git_commands, ())
        self.assertEqual(self.status_of("AT-101"), schema.READY_FOR_INTEGRATION)



class CrossRepoGuardTests(ControllerTestCase):
    def test_a_pr_from_another_repository_is_never_updated(self) -> None:
        """§32.0 — a PR belongs to exactly one repository; the same number
        open elsewhere must never be edited from this request."""
        self.make_task("AT-101")
        self.integration.update_pr_state(
            "AT-101", pr_number=42, pr_url="https://github.com/other/repo/pull/42",
            pr_state="open",
        )
        result = self.integrate("AT-101")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertIn("other/repo", result.summary)
        self.assertEqual(self.gh_runner.calls, [])
        self.assertEqual(result.git_commands, ())
        self.assertEqual(self.status_of("AT-101"), schema.READY_FOR_INTEGRATION)



class StageOnlyResolver:
    """Resolves and stages the conflict but never commits or continues."""

    name = "stage-only"

    def resolve(self, request: ConflictResolutionRequest) -> ConflictResolutionOutcome:
        worktree = Path(request.worktree_path)
        for entry in request.conflict_hunks:
            (worktree / entry["path"]).write_text("resolved\n", encoding="utf-8")
        git_ops.stage_all(worktree)
        return ConflictResolutionOutcome(
            resolver=self.name, resolved=True, explanation="staged but not committed"
        )


class StrayFileResolver(TakeBothSidesResolver):
    """Resolves and commits correctly, then leaves an untracked backup file."""

    name = "stray-file"

    def resolve(self, request: ConflictResolutionRequest) -> ConflictResolutionOutcome:
        super().resolve(request)
        (Path(request.worktree_path) / "resolver-notes.orig").write_text(
            "left behind\n", encoding="utf-8"
        )
        return ConflictResolutionOutcome(
            resolver=self.name, resolved=True, explanation="resolved, left a backup file"
        )


class MarkerKeepingResolver:
    """Commits the conflicted files exactly as they are, markers included."""

    name = "marker-keeping"

    def resolve(self, request: ConflictResolutionRequest) -> ConflictResolutionOutcome:
        worktree = Path(request.worktree_path)
        git_ops.stage_all(worktree)
        git_ops.commit_conflict_resolution(worktree, "resolve conflict")
        return ConflictResolutionOutcome(
            resolver=self.name, resolved=True, explanation="committed as-is"
        )


class AbandoningResolver:
    """Claims success but aborts the update instead of resolving it."""

    name = "abandoning"

    def resolve(self, request: ConflictResolutionRequest) -> ConflictResolutionOutcome:
        worktree = Path(request.worktree_path)
        if git_ops.in_progress_operation(worktree) == "merge":
            git_ops.abort_merge(worktree)
        else:
            git_ops.abort_rebase(worktree)
        return ConflictResolutionOutcome(
            resolver=self.name, resolved=True, explanation="abandoned the update"
        )


class ClaimOnlyResolver:
    """Claims success but never touches the tree: the rebase stays stopped."""

    name = "claim-only"

    def __init__(self) -> None:
        self.rebase_dir_present: bool | None = None

    def resolve(self, request: ConflictResolutionRequest) -> ConflictResolutionOutcome:
        directory = git_ops.git_dir(Path(request.worktree_path))
        self.rebase_dir_present = (directory / "rebase-merge").exists() or (
            directory / "rebase-apply"
        ).exists()
        return ConflictResolutionOutcome(
            resolver=self.name, resolved=True, explanation="claimed resolved, did nothing"
        )


class ResolutionVerificationTests(ControllerTestCase):
    """Review blocker B2 — after ANY AI conflict resolution, and before
    validators run, the control plane verifies five deterministic checks. Any
    failure stops at needs_decision (§27.2.1): the conflict hunks, the AI's
    explanation and the failed checks are persisted, nothing is pushed, and
    integrated_base_sha is not recorded. No auto retry.
    """

    def _conflicting(self) -> Path:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "shared.txt", "task-side\n", "task edits shared")
        self.fixture.advance_target("shared.txt", "target-side\n")
        return worktree

    def assert_stopped_by(self, result, check: str, *, gh_calls_before: int = 0,
                          base_before: str | None = None) -> None:
        self.assertEqual(result.status, "needs_decision", result.summary)
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_DECISION)
        self.assertIn(check, result.resolution_checks_failed)
        self.assertTrue(result.conflict_detected)
        self.assertFalse(result.validators_passed)
        # Nothing pushed, no PR touched, integrated_base_sha not recorded.
        self.assertEqual([c for c in result.git_commands if c[:2] == ("git", "push")], [])
        self.assertEqual(len(self.gh_runner.calls), gh_calls_before)
        self.assertEqual(
            self.integration.get_pr_state("AT-101")["integrated_base_sha"], base_before
        )
        # Validators never ran for this run.
        self.assertEqual(
            [r for r in self.integration.list_validator_evidence("AT-101")
             if r["integration_run_id"] == result.integration_run_id],
            [],
        )
        # Hunks, the AI's explanation and the failed check are all persisted.
        evidence = self.integration.list_conflict_evidence("AT-101")[-1]
        self.assertEqual(evidence["integration_run_id"], result.integration_run_id)
        self.assertTrue(evidence["conflict_hunks"])
        self.assertTrue(evidence["explanation"])
        self.assertIn(check, [c["name"] for c in evidence["verification"] if not c["passed"]])
        self.assertNotIn("ai_conflict_resolution", [hint.code for hint in result.hints])

    def test_a_resolver_that_stages_but_does_not_commit_ends_in_needs_decision(self) -> None:
        worktree = self._conflicting()
        result = self.integrate("AT-101", conflict_resolver=StageOnlyResolver())
        self.assertNotEqual(self.status_of("AT-101"), schema.NEEDS_REVIEW)
        self.assert_stopped_by(result, git_ops.CHECK_NO_OPERATION_IN_PROGRESS)
        # The half-finished rebase is aborted so the worktree is left usable.
        self.assertIsNone(git_ops.in_progress_operation(worktree))

    def test_check_b_a_stray_file_left_by_the_resolver_stops_integration(self) -> None:
        self._conflicting()
        result = self.integrate("AT-101", conflict_resolver=StrayFileResolver())
        self.assert_stopped_by(result, git_ops.CHECK_WORKTREE_CLEAN)
        self.assertEqual(result.resolution_checks_failed, (git_ops.CHECK_WORKTREE_CLEAN,))

    def test_check_c_committed_conflict_markers_stop_integration(self) -> None:
        self._conflicting()
        result = self.integrate("AT-101", conflict_resolver=MarkerKeepingResolver())
        self.assert_stopped_by(result, git_ops.CHECK_NO_CONFLICT_MARKERS)
        self.assertEqual(result.resolution_checks_failed, (git_ops.CHECK_NO_CONFLICT_MARKERS,))

    def test_check_d_a_resolution_that_leaves_head_unchanged_stops_integration(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "shared.txt", "task-side\n", "task edits shared")
        self.integrate("AT-101")
        base = self.integration.get_pr_state("AT-101")["integrated_base_sha"]
        self.fixture.advance_target("shared.txt", "target-side\n")
        self.store.update_task_status("AT-101", schema.READY_FOR_INTEGRATION, source="test")
        self.integration.update_pr_state("AT-101", reintegration_required=True)
        calls_before = len(self.gh_runner.calls)
        # Re-integration merges; aborting that merge leaves HEAD where it was.
        result = self.integrate("AT-101", conflict_resolver=AbandoningResolver())
        self.assert_stopped_by(
            result, git_ops.CHECK_HEAD_IS_NEW_COMMIT,
            gh_calls_before=calls_before, base_before=base,
        )

    def test_check_e_a_resolution_that_drops_the_target_stops_integration(self) -> None:
        self._conflicting()
        # Aborting the initial rebase restores the old tip: HEAD is a different
        # commit from the conflict point, but the latest target is not in it.
        result = self.integrate("AT-101", conflict_resolver=AbandoningResolver())
        self.assert_stopped_by(result, git_ops.CHECK_TARGET_IS_ANCESTOR)
        self.assertEqual(result.resolution_checks_failed, (git_ops.CHECK_TARGET_IS_ANCESTOR,))


    def test_a_stopped_rebase_still_fails_check_a_under_the_amended_rule(self) -> None:
        """Amended check (a): REBASE_HEAD alone no longer counts, but a rebase
        that stopped on a conflict and was never continued still has
        rebase-merge/, so it is still caught — needs_decision, nothing pushed,
        no integrated_base_sha."""
        worktree = self._conflicting()
        resolver = ClaimOnlyResolver()
        result = self.integrate("AT-101", conflict_resolver=resolver)
        self.assertTrue(resolver.rebase_dir_present)
        self.assert_stopped_by(result, git_ops.CHECK_NO_OPERATION_IN_PROGRESS)
        self.assertIsNone(self.integration.get_pr_state("AT-101")["integrated_base_sha"])
        self.assertIsNone(git_ops.in_progress_operation(worktree))



class ProtectedBranchNormalizationTests(ControllerTestCase):
    """Review blocking item / Ruling 18 — a task branch recorded as
    ``refs/heads/main`` (or any other spelling of main or the base branch) is
    refused as soon as integration reads it, before any git command runs:
    nothing is pushed, no integrated_base_sha is recorded, and the Ticket ends
    in needs_decision via §27.2.1."""

    def _integrate_with_recorded_branch(self, branch: str, *, target_branch: str = "main"):
        from v1_step2_fixtures import git as raw_git

        self.make_task("AT-101")
        # As in the reviewer's reproduction: the shared clone's local main
        # holds a commit origin lacks, so any push to main would be visible.
        (self.fixture.repo / "local.txt").write_text("unpushed\n", encoding="utf-8")
        raw_git(self.fixture.repo, "add", "-A")
        raw_git(self.fixture.repo, "commit", "-m", "unpushed local commit on main")
        recorded = self.store.get_task_worktree("AT-101")
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key="AT-101", repo_path=recorded.repo_path,
                worktree_path=recorded.worktree_path, branch=branch,
                base_branch=target_branch, base_sha=recorded.base_sha, status="active",
            )
        )
        origin_main_before = self.fixture.target_sha()
        result = self.integrate("AT-101", target_branch=target_branch)
        return result, origin_main_before

    def assert_refused_before_any_git(self, result, origin_main_before: str) -> None:
        self.assertEqual(result.status, "needs_decision", result.summary)
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_DECISION)
        self.assertEqual(result.git_commands, ())
        self.assertEqual(self.gh_runner.calls, [])
        self.assertEqual(self.fixture.target_sha(), origin_main_before)
        self.assertIsNone(self.integration.get_pr_state("AT-101")["integrated_base_sha"])
        self.assertIsNone(self.integration.get_integration_lock("owner/repo"))
        blocked = [e for e in self.store.list_task_events("AT-101") if e.event_type == "integration_blocked"]
        self.assertEqual(len(blocked), 1)

    def test_the_reviewers_refs_heads_main_scenario_pushes_nothing(self) -> None:
        result, before = self._integrate_with_recorded_branch("refs/heads/main")
        self.assert_refused_before_any_git(result, before)
        self.assertIn("refs/heads/main", result.summary)

    def test_each_listed_spelling_of_main_or_the_base_branch_pushes_nothing(self) -> None:
        for branch, target_branch in (
            ("heads/main", "main"),
            ("refs/heads/develop", "develop"),
            ("heads/develop", "develop"),
            ("  refs/heads/main  ", "main"),
        ):
            with self.subTest(branch=branch, target_branch=target_branch):
                self.tearDown()
                self.setUp()
                result, before = self._integrate_with_recorded_branch(
                    branch, target_branch=target_branch
                )
                self.assert_refused_before_any_git(result, before)


class GitFailureGuardTests(ControllerTestCase):
    """Review Ruling 19 — no git failure may leave a Ticket in `integrating`.
    A failure ends in needs_decision via §27.2.1 with an audited
    integration_blocked event. (Remapping to §29.2 `failed` is Step 5's.)"""

    def assert_stopped_not_stuck(self, result, message: str) -> None:
        self.assertEqual(result.status, "needs_decision", result.summary)
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_DECISION)
        self.assertNotEqual(self.status_of("AT-101"), schema.INTEGRATING)
        self.assertIsNone(self.integration.get_integration_lock("owner/repo"))
        self.assertEqual([c for c in result.git_commands if c[:2] == ("git", "push")], [])
        self.assertEqual(self.gh_runner.calls, [])
        self.assertIsNone(self.integration.get_pr_state("AT-101")["integrated_base_sha"])
        blocked = [e for e in self.store.list_task_events("AT-101") if e.event_type == "integration_blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertIn(message, blocked[0].message)

    def test_a_behind_count_failure_ends_in_needs_decision_not_integrating(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        with mock.patch.object(
            git_ops, "behind_count", side_effect=git_ops.IntegrationGitError("simulated rev-list failure")
        ):
            result = self.integrate("AT-101")
        self.assert_stopped_not_stuck(result, "simulated rev-list failure")

    def test_a_git_failure_mid_conflict_aborts_the_rebase_and_ends_in_needs_decision(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "shared.txt", "task-side\n", "task edits shared")
        self.fixture.advance_target("shared.txt", "target-side\n")
        with mock.patch.object(
            git_ops, "conflict_hunks", side_effect=git_ops.IntegrationGitError("simulated grep failure")
        ):
            result = self.integrate("AT-101")
        self.assert_stopped_not_stuck(result, "simulated grep failure")
        # The half-finished rebase is aborted so the worktree is left usable.
        self.assertIsNone(git_ops.in_progress_operation(worktree))



def _origin_branch_sha(fixture: GitFixture, branch: str) -> str | None:
    from v1_step2_fixtures import git as raw_git

    listed = raw_git(fixture.origin, "branch", "--list", branch).strip()
    return raw_git(fixture.origin, "rev-parse", branch).strip() if listed else None


class UnvalidatedBranchRefusalTests(ControllerTestCase):
    """Review round 4, Ruling 30 — never publish a branch that is not what
    was validated. The push publishes the branch by name, so HEAD must be on
    the Ticket's branch and the branch must be the validated commit, both
    before validators and before the push. A refusal pushes nothing, records
    no integrated_base_sha, and ends in needs_decision with an audited event
    naming the mismatch."""

    def assert_refused(self, result, *, stage: str, mismatch: str) -> None:
        self.assertEqual(result.status, "needs_decision", result.summary)
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_DECISION)
        self.assertNotEqual(self.status_of("AT-101"), schema.NEEDS_REVIEW)
        self.assertEqual([c for c in result.git_commands if c[:2] == ("git", "push")], [])
        self.assertIsNone(_origin_branch_sha(self.fixture, "task/AT-101"))
        self.assertEqual(self.gh_runner.calls, [])
        pr_state = self.integration.get_pr_state("AT-101")
        self.assertIsNone(pr_state["integrated_base_sha"])
        self.assertIsNone(pr_state["pr_number"])
        self.assertIsNone(self.integration.get_integration_lock("owner/repo"))
        blocked = [
            e for e in self.store.list_task_events("AT-101")
            if e.event_type == "integration_blocked"
        ]
        self.assertEqual(len(blocked), 1)
        payload = json.loads(blocked[0].payload_json)
        self.assertEqual(payload["check"], "head_on_task_branch")
        self.assertEqual(payload["stage"], stage)
        self.assertEqual(payload["task_branch"], "task/AT-101")
        self.assertIn(mismatch, payload["mismatch"])

    def test_a_detached_head_without_a_conflict_never_reaches_review(self) -> None:
        # The reviewer's scenario: the rebase succeeds on a detached HEAD, so
        # HEAD holds the target but the branch the push would publish does not.
        from v1_step2_fixtures import git as raw_git

        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        target = self.fixture.advance_target()
        raw_git(worktree, "checkout", "-q", "--detach")
        result = self.integrate("AT-101")
        self.assert_refused(result, stage="before_validators", mismatch="detached")
        # Validators never ran against the unpublishable tree.
        self.assertEqual(self.integration.list_validator_evidence("AT-101"), [])
        # And the branch that would have been published lacks the target.
        self.assertFalse(
            git_ops.commit_in_history(worktree, target, "refs/heads/task/AT-101")
        )

    def test_head_on_another_branch_is_refused(self) -> None:
        from v1_step2_fixtures import git as raw_git

        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        raw_git(worktree, "checkout", "-q", "-b", "task/other")
        result = self.integrate("AT-101")
        self.assert_refused(
            result, stage="before_validators", mismatch="refs/heads/task/other"
        )

    def test_a_validator_that_moves_the_branch_is_refused_before_the_push(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        mover = IntegrationValidatorSpec(
            name="mover",
            command=(
                "git", "-c", "user.name=v", "-c", "user.email=v@example.invalid",
                "commit", "-q", "--allow-empty", "-m", "moved by a validator",
            ),
        )
        result = self.integrate("AT-101", validator_specs=GREEN + (mover,))
        self.assertTrue(result.validators_passed)
        self.assert_refused(result, stage="before_push", mismatch="moved")

    def test_a_validator_that_detaches_head_is_refused_before_the_push(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        detacher = IntegrationValidatorSpec(
            name="detacher", command=("git", "checkout", "-q", "--detach")
        )
        result = self.integrate("AT-101", validator_specs=GREEN + (detacher,))
        self.assert_refused(result, stage="before_push", mismatch="detached")

    def test_a_detached_head_on_reintegration_is_refused(self) -> None:
        from v1_step2_fixtures import git as raw_git

        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        self.integrate("AT-101")
        published = _origin_branch_sha(self.fixture, "task/AT-101")
        first_base = self.integration.get_pr_state("AT-101")["integrated_base_sha"]
        self.store.update_task_status("AT-101", schema.READY_FOR_INTEGRATION, source="test")
        self.integration.update_pr_state("AT-101", reintegration_required=True)
        self.fixture.advance_target()
        raw_git(worktree, "checkout", "-q", "--detach")
        calls_before = len(self.gh_runner.calls)

        result = self.integrate("AT-101")
        self.assertEqual(result.status, "needs_decision", result.summary)
        self.assertEqual([c for c in result.git_commands if c[:2] == ("git", "push")], [])
        self.assertEqual(_origin_branch_sha(self.fixture, "task/AT-101"), published)
        self.assertEqual(len(self.gh_runner.calls), calls_before)
        # integrated_base_sha still names what the published branch contains.
        self.assertEqual(
            self.integration.get_pr_state("AT-101")["integrated_base_sha"], first_base
        )

    def test_the_published_branch_always_contains_the_recorded_target(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        self.fixture.advance_target()
        result = self.integrate("AT-101")
        self.assertTrue(result.ok, result.summary)
        pr_state = self.integration.get_pr_state("AT-101")
        published = _origin_branch_sha(self.fixture, "task/AT-101")
        self.assertEqual(published, pr_state["pr_head_sha"])
        self.assertTrue(
            git_ops.commit_in_history(worktree, pr_state["integrated_base_sha"], published)
        )


class PrIdentityTests(ControllerTestCase):
    """Review round 4, Ruling 29d — the PR's identity is recorded the moment
    `gh pr create` returns, before any poll, so no real PR is ever orphaned."""

    def test_initial_integration_never_polls_after_create(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        result = self.integrate("AT-101")
        self.assertTrue(result.ok, result.summary)
        self.assertEqual([c[:3] for c in self.gh_runner.calls], [["gh", "pr", "create"]])

    def test_the_identity_survives_a_failure_right_after_create(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        real_update = self.integration.update_pr_state

        def failing_update(task_key, **fields):
            if "integrated_base_sha" in fields:
                raise RuntimeError("simulated failure after gh pr create")
            return real_update(task_key, **fields)

        with mock.patch.object(self.integration, "update_pr_state", side_effect=failing_update):
            result = self.integrate("AT-101")

        self.assertEqual(result.status, "needs_decision", result.summary)
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_DECISION)
        pr_state = self.integration.get_pr_state("AT-101")
        self.assertEqual(pr_state["pr_number"], 42)
        self.assertEqual(pr_state["pr_url"], "https://github.com/owner/repo/pull/42")
        self.assertEqual(pr_state["pr_state"], "open")
        self.assertEqual(pr_state["pr_head_sha"], _origin_branch_sha(self.fixture, "task/AT-101"))
        self.assertIsNone(pr_state["integrated_base_sha"])


class IntegrationBoundaryTests(ControllerTestCase):
    """Review round 4, Ruling 31 — the integration boundary catches every
    exception, not just IntegrationGitError. No Ticket stays `integrating`;
    the audited event records the original exception type and message."""

    def assert_stopped(self, result, exception_type: str, message: str) -> dict:
        self.assertEqual(result.status, "needs_decision", result.summary)
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_DECISION)
        self.assertIsNone(self.integration.get_integration_lock("owner/repo"))
        self.assertIsNone(self.integration.get_pr_state("AT-101")["integrated_base_sha"])
        blocked = [
            e for e in self.store.list_task_events("AT-101")
            if e.event_type == "integration_blocked"
        ]
        self.assertEqual(len(blocked), 1)
        payload = json.loads(blocked[0].payload_json)
        self.assertEqual(payload["exception_type"], exception_type)
        self.assertIn(message, payload["exception_message"])
        self.assertIn(exception_type, blocked[0].message)
        return payload

    def _ready(self) -> Path:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        return worktree

    def test_a_unicode_decode_error_ends_in_needs_decision(self) -> None:
        self._ready()
        error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        with mock.patch.object(git_ops, "diff_context", side_effect=error):
            result = self.integrate("AT-101")
        self.assert_stopped(result, "UnicodeDecodeError", "invalid start byte")
        self.assertEqual([c for c in result.git_commands if c[:2] == ("git", "push")], [])
        self.assertEqual(self.gh_runner.calls, [])

    def test_a_missing_git_binary_ends_in_needs_decision(self) -> None:
        self._ready()
        error = FileNotFoundError(2, "No such file or directory", "git")
        with mock.patch.object(git_ops, "fetch", side_effect=error):
            result = self.integrate("AT-101")
        self.assert_stopped(result, "FileNotFoundError", "No such file or directory")

    def test_a_runtime_error_ends_in_needs_decision(self) -> None:
        self._ready()
        with mock.patch(
            "agent_taskflow.integration_controller.build_reviewer_hints",
            side_effect=RuntimeError("simulated hint failure"),
        ):
            result = self.integrate("AT-101")
        self.assert_stopped(result, "RuntimeError", "simulated hint failure")
        self.assertEqual(self.gh_runner.calls, [])

    def test_a_missing_gh_after_the_push_ends_in_needs_decision(self) -> None:
        self._ready()

        def no_gh(args, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", "gh")

        self.github = GitHubPrAdapter("owner/repo", runner=no_gh)
        result = self.integrate("AT-101")
        self.assert_stopped(result, "FileNotFoundError", "No such file or directory")
        # The branch was already pushed; no PR exists and nothing claims one.
        self.assertIsNotNone(_origin_branch_sha(self.fixture, "task/AT-101"))
        self.assertIsNone(self.integration.get_pr_state("AT-101")["pr_number"])

    def test_an_in_progress_rebase_is_aborted_on_a_non_git_failure(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "shared.txt", "task-side\n", "task edits shared")
        self.fixture.advance_target("shared.txt", "target-side\n")
        with mock.patch.object(
            git_ops, "conflict_hunks", side_effect=RuntimeError("simulated failure mid-rebase")
        ):
            result = self.integrate("AT-101")
        self.assert_stopped(result, "RuntimeError", "simulated failure mid-rebase")
        self.assertIsNone(git_ops.in_progress_operation(worktree))

    def test_the_reviewers_latin1_filename_conflict_never_leaves_integrating(self) -> None:
        # The round-4 repro, unmocked: a rebase conflict on a file whose name
        # is Latin-1, not UTF-8. git prints the raw name, which used to raise
        # UnicodeDecodeError past the IntegrationGitError-only guard.
        import os

        from v1_step2_fixtures import git as raw_git

        name = os.fsdecode(b"caf\xe9.txt")
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, name, "task-side\n", "task edits a latin-1 file")
        # Advance the target by hand: the fixture helper names its staging
        # clone and commit after the file, and decodes git's output strictly.
        staging = self.root / "staging-latin1"
        raw_git(self.root, "clone", "-q", str(self.fixture.origin), str(staging))
        (staging / name).write_text("target-side\n", encoding="utf-8")
        raw_git(staging, "add", "-A")
        raw_git(staging, "commit", "-q", "-m", "target edits the latin-1 file")
        raw_git(staging, "push", "-q", "origin", "main")
        result = self.integrate("AT-101")
        self.assertEqual(result.status, "needs_decision", result.summary)
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_DECISION)
        self.assertIsNone(self.integration.get_integration_lock("owner/repo"))
        self.assertIsNone(self.integration.get_pr_state("AT-101")["integrated_base_sha"])
        self.assertEqual([c for c in result.git_commands if c[:2] == ("git", "push")], [])
        self.assertIsNone(git_ops.in_progress_operation(worktree))

    def test_a_failure_after_the_review_hand_off_is_raised_not_rewritten(self) -> None:
        # The Ticket already left `integrating`, so it is not stuck; the guard
        # surfaces the error rather than guessing a new status.
        self._ready()
        real_record = self.store.record_task_event

        def failing_record(task_key, event_type, *args, **kwargs):
            if event_type == "integration_completed":
                raise RuntimeError("simulated failure after hand-off")
            return real_record(task_key, event_type, *args, **kwargs)

        with mock.patch.object(self.store, "record_task_event", side_effect=failing_record):
            with self.assertRaises(RuntimeError):
                self.integrate("AT-101")
        self.assertEqual(self.status_of("AT-101"), schema.NEEDS_REVIEW)
        self.assertIsNone(self.integration.get_integration_lock("owner/repo"))


if __name__ == "__main__":
    unittest.main()
