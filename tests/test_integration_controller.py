"""Tests for agent_taskflow.integration_controller (spec §22-§27, §29, §31)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
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
        self.assertTrue([c for c in self.gh_runner.calls if c[:3] == ["gh", "pr", "edit"]])

    def test_reintegration_merges_rather_than_rebasing(self) -> None:
        worktree = self._published()
        published_sha = git_ops.head_sha(worktree)
        self.fixture.advance_target()
        self._requeue()
        self.integrate("AT-101")
        self.assertIn(published_sha, git_ops.rev_list(worktree, "HEAD"))

    def test_reintegration_never_force_pushes(self) -> None:
        self._published()
        self.fixture.advance_target()
        self._requeue()
        result = self.integrate("AT-101")
        self.assertIs(result.force_pushed, False)
        for command in result.git_commands:
            self.assertNotIn("--force", command)
            self.assertNotIn("-f", command)
            self.assertNotIn("--force-with-lease", command)

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
    def test_no_git_command_is_ever_a_force_push(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        result = self.integrate("AT-101")
        pushes = [cmd for cmd in result.git_commands if cmd[:2] == ("git", "push")]
        self.assertTrue(pushes)
        for cmd in pushes:
            self.assertFalse({"--force", "-f", "--force-with-lease"} & set(cmd))
            self.assertFalse([part for part in cmd if part.startswith("+")])

    def test_no_git_command_pushes_the_target_branch(self) -> None:
        worktree = self.make_task("AT-101")
        self.fixture.commit_in(worktree, "feature.txt", "f\n", "feature")
        result = self.integrate("AT-101")
        for cmd in result.git_commands:
            if cmd[:2] == ("git", "push"):
                self.assertNotIn("main", cmd)
                self.assertFalse([part for part in cmd if part.endswith(":main")])

    def test_no_gh_command_merges(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
