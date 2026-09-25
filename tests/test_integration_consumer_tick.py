"""V1-F10: one integration-tick pass runs every consumer phase (SPEC §47.2).

Real disposable git repositories and the Step 2 fake ``gh``, in F9's style.
The pass order is PR outcomes → verified-merge cleanup → target freshness →
FIFO drain, once each, scoped to one repository.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_integration_tick import TickFixture
from v1_step2_fixtures import git

from agent_taskflow import integration_schema as schema
from agent_taskflow.integration_cleanup import IntegrationCleanupRequest, run_integration_cleanup
from agent_taskflow.integration_controller import integrate_task
from agent_taskflow.integration_queue import enqueue_for_integration, queue_for_repo
from agent_taskflow.integration_tick import (
    PHASE_ORDER,
    IntegrationTickRequest,
    run_integration_tick,
)
from agent_taskflow.integration_validators import IntegrationValidatorSpec
from agent_taskflow.integration_watcher import poll_pr_outcomes, poll_target_freshness
from agent_taskflow.models import TaskWorktreeRecord
from agent_taskflow.ticket_models import TicketRecord


VALIDATORS = (IntegrationValidatorSpec(
    "seeded", (sys.executable, "-c", "from pathlib import Path; assert Path('README.md').is_file()")
),)
REVIEW = [{"author": {"login": "octocat"}, "state": "CHANGES_REQUESTED",
           "body": "please change", "submittedAt": "2026-09-25T00:00:00Z"}]


class ConsumerFixture(TickFixture):
    def ticket(self, *, repo="owner/repo", priority="normal", enqueue=True,
               status=schema.READY_FOR_INTEGRATION):
        ticket = self.tickets.create_ticket(
            actor="f10-test",
            build=lambda key: TicketRecord(
                task_key=key, repository="fixture", prompt="F10 consumer tick",
                title=key, priority=priority, status=status, repo_path=self.fixture.repo,
                base_branch="main", branch=f"task/{key}",
                worktree_path=self.fixture.repo / ".worktrees" / key,
                artifact_dir=self.root / "artifacts" / key, github_repo=repo,
            ),
        )
        path = self.fixture.create_task_worktree(ticket.task_key)
        ticket.artifact_dir.mkdir(parents=True)
        self.fixture.commit_in(path, f"{ticket.task_key}.txt", "work\n", "work")
        self.store.upsert_task_worktree(TaskWorktreeRecord(
            task_key=ticket.task_key, repo_path=ticket.repo_path, worktree_path=path,
            branch=ticket.branch, base_branch="main", base_sha=self.fixture.target_sha(),
            status="active",
        ))
        if enqueue:
            enqueue_for_integration(self.integration, ticket.task_key, repo=repo, priority=priority)
        return ticket

    def request(self, **overrides):
        values = dict(repo="owner/repo", repo_path=self.fixture.repo, db_path=self.db_path,
                      validator_specs=VALIDATORS, dry_run=False, confirm_integration=True,
                      consumer_phases=True, confirm_pr_poll=True, confirm_cleanup=True,
                      confirm_freshness=True)
        values.update(overrides)
        return IntegrationTickRequest(**values)

    def preview(self):
        return self.tick(dry_run=True, confirm_integration=False, confirm_pr_poll=False,
                         confirm_cleanup=False, confirm_freshness=False)

    def integrated(self, count):
        made = [self.ticket() for _ in range(count)]
        result = self.tick()
        self.assertTrue(result["ok"], result)
        self.assertEqual([self.status(t) for t in made], [schema.NEEDS_REVIEW] * count)
        return made

    def pr(self, ticket):
        return self.integration.get_pr_state(ticket.task_key)["pr_number"]

    def head(self, ticket):
        return git(ticket.worktree_path, "rev-parse", "HEAD").strip()

    def github_merge(self, ticket, method="squash"):
        """What a human merge on GitHub leaves behind: the target and the PR."""
        sha = self.fixture.merge_branch_into_target(ticket.branch, method=method)
        self.gh.set_pr(self.pr(ticket), state="MERGED", mergedAt="2026-09-25T00:00:00Z",
                       mergeCommit={"oid": sha}, headRefOid=self.head(ticket))
        return sha

    def events(self, ticket=None):
        with closing(sqlite3.connect(self.db_path)) as conn:
            query = "SELECT task_key, event_type, source FROM task_events"
            rows = conn.execute(query + (" WHERE task_key = ?" if ticket else "") + " ORDER BY id",
                                (ticket.task_key,) if ticket else ()).fetchall()
        return rows

    def db_digest(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dump = list(conn.iterdump())
        return hashlib.sha256(self.db_path.read_bytes()).hexdigest(), dump

    def refs(self, path, *patterns):
        return git(path, "for-each-ref", "--format=%(refname) %(objectname)", *patterns)

    def branch_exists(self, path, branch):
        return bool(git(path, "branch", "--list", branch).strip())

    def phase(self, result, name):
        return result["phases"][name]


class PhaseOrderAndScopeTests(ConsumerFixture):
    def test_one_pass_runs_each_phase_once_in_the_pinned_order(self):
        merged, stale = self.integrated(2)
        queued = self.ticket()
        self.github_merge(merged)
        calls = []

        def spy(name, real):
            def wrapper(*args, **kwargs):
                calls.append(name)
                return real(*args, **kwargs)
            return wrapper

        with patch("agent_taskflow.integration_tick.poll_pr_outcomes", spy("pr", poll_pr_outcomes)), \
                patch("agent_taskflow.integration_tick.run_integration_cleanup",
                      spy("cleanup", run_integration_cleanup)), \
                patch("agent_taskflow.integration_tick.poll_target_freshness",
                      spy("freshness", poll_target_freshness)), \
                patch("agent_taskflow.integration_tick.integrate_task", spy("drain", integrate_task)):
            result = self.tick()
        self.assertEqual(calls, ["pr", "cleanup", "freshness", "drain", "drain"])
        self.assertEqual(result["phase_order"], list(PHASE_ORDER))
        self.assertEqual(list(PHASE_ORDER),
                         ["pr_outcomes", "verified_merge_cleanup", "target_freshness", "queue_drain"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["tick_status"], "ok")
        # The merge completes, then the stale sibling is re-queued behind the
        # already-queued Ticket and re-integrated in the same pass (FIFO).
        self.assertEqual(self.status(merged), schema.COMPLETED)
        self.assertEqual([o["task_key"] for o in result["outcomes"]], [queued.task_key, stale.task_key])
        self.assertEqual(self.phase(result, "target_freshness")["requeued"], [stale.task_key])
        self.assertEqual([self.status(t) for t in (stale, queued)], [schema.NEEDS_REVIEW] * 2)
        self.assertEqual(self.integration.get_pr_state(stale.task_key)["reintegration_count"], 1)
        self.assertEqual(result["remaining_task_keys"], [])

    def test_another_repository_is_never_polled_cleaned_requeued_or_dequeued(self):
        mine = self.integrated(1)[0]
        mine_queued = self.ticket()
        others = {}
        for name, status, enqueue in (("open", schema.NEEDS_REVIEW, False),
                                      ("merged", schema.NEEDS_REVIEW, False),
                                      ("queued", schema.READY_FOR_INTEGRATION, True),
                                      ("orphan", schema.READY_FOR_INTEGRATION, False)):
            others[name] = self.ticket(repo="owner/other", status=status, enqueue=enqueue)
        # The same PR number open in two repositories (§32.0).
        self.integration.update_pr_state(others["open"].task_key, pr_number=self.pr(mine),
                                         pr_url=f"https://github.com/owner/other/pull/{self.pr(mine)}",
                                         pr_state="open")
        # A merge commit that really is in the target, so only the repository
        # filter keeps this Ticket out of the cleanup pick-up.
        other_sha = self.fixture.advance_target("other.txt")
        self.integration.update_pr_state(others["merged"].task_key, pr_number=7, pr_state="closed",
                                         pr_url="https://github.com/owner/other/pull/7",
                                         pr_merged=True, merge_commit_sha=other_sha)
        before = {key: (self.status(t), self.integration.get_pr_state(t.task_key),
                        self.events(t), t.worktree_path.is_dir())
                  for key, t in others.items()}
        other_queue = queue_for_repo(self.integration, "owner/other")
        self.github_merge(mine)
        result = self.tick()
        after = {key: (self.status(t), self.integration.get_pr_state(t.task_key),
                       self.events(t), t.worktree_path.is_dir())
                 for key, t in others.items()}
        self.assertEqual(after, before)
        self.assertEqual(queue_for_repo(self.integration, "owner/other"), other_queue)
        self.assertFalse(any("owner/other" in " ".join(call) for call in self.gh.calls))
        other_keys = {t.task_key for t in others.values()}
        seen = set(self.phase(result, "verified_merge_cleanup")["candidates"])
        for name in ("pr_outcomes", "target_freshness"):
            seen |= {o["task_key"] for o in self.phase(result, name)["outcomes"]}
        seen |= {o["task_key"] for o in result["outcomes"]}
        seen |= {o["task_key"] for o in result["ready_for_integration_unqueued"]}
        seen |= {o["task_key"] for o in result["merge_verified_not_completed"]}
        self.assertFalse(seen & other_keys, seen)
        self.assertEqual(self.status(mine), schema.COMPLETED)
        self.assertEqual(self.status(mine_queued), schema.NEEDS_REVIEW)


class DryRunTests(ConsumerFixture):
    def test_without_confirmations_nothing_is_written_to_the_db_git_or_github(self):
        merged, closed, reviewed, stale, unverifiable, verifiable = self.integrated(6)
        queued = self.ticket()
        self.github_merge(merged, method="merge")
        self.gh.set_pr(self.pr(closed), state="CLOSED")
        self.gh.set_pr(self.pr(reviewed), reviewDecision="CHANGES_REQUESTED", reviews=REVIEW)
        # Recorded by an earlier confirmed poll: one merge that verifies, one that cannot.
        self.integration.update_pr_state(unverifiable.task_key, pr_state="closed", pr_merged=True,
                                         merge_commit_sha="0" * 40)
        sha = self.fixture.merge_branch_into_target(verifiable.branch, method="squash")
        self.integration.update_pr_state(verifiable.task_key, pr_state="closed", pr_merged=True,
                                         merge_commit_sha=sha)
        repo, origin = self.fixture.repo, self.fixture.origin
        # Tags (review N3): origin has tags a fetch can follow, one clashing
        # with a local tag of the same name, and one on a commit no branch
        # reaches; the clone has a local-only tag. fetch.pruneTags keeps its
        # default, so a global setting cannot change what this test sees.
        git(repo, "config", "fetch.pruneTags", "false")
        git(repo, "tag", "shared", "main")
        git(repo, "tag", "local-only", "main")
        git(origin, "tag", "remote-light", "main")
        git(origin, "tag", "-a", "remote-annotated", "-m", "release", "main~1")
        git(origin, "tag", "shared", "main")
        side = self.root / "side"
        git(self.root, "clone", "-q", str(origin), str(side))
        self.fixture.commit_in(side, "side.txt", "side\n", "on no branch")
        git(side, "tag", "remote-unreachable")
        git(side, "push", "-q", "origin", "remote-unreachable")

        def tags(path):
            return dict(line.split() for line in self.refs(path, "refs/tags").splitlines())

        worktrees = git(repo, "worktree", "list", "--porcelain")
        heads = self.refs(repo, "refs/heads")
        local_tags = tags(repo)
        remote_refs = self.refs(origin)
        artifacts = sorted(p for p in (self.root / "artifacts").rglob("*"))
        digest = self.db_digest()
        calls = len(self.gh.calls)

        result = self.preview()

        self.assertEqual(self.db_digest(), digest)
        self.assertEqual(git(repo, "worktree", "list", "--porcelain"), worktrees)
        self.assertEqual(self.refs(repo, "refs/heads"), heads)
        # The preview's `git fetch origin --prune` (inside the freshness poll
        # and merge verification) auto-follows origin's tags that point into
        # fetched history and are missing locally. It moves and deletes no
        # local tag, and fetches no tag that no fetched branch reaches.
        followed = {name: sha for name, sha in tags(origin).items()
                    if name in {"refs/tags/remote-light", "refs/tags/remote-annotated"}}
        self.assertEqual(len(followed), 2)
        self.assertEqual(tags(repo), {**local_tags, **followed})
        self.assertNotEqual(tags(repo)["refs/tags/shared"], tags(origin)["refs/tags/shared"])
        self.assertEqual(self.refs(origin), remote_refs)
        self.assertEqual(sorted(p for p in (self.root / "artifacts").rglob("*")), artifacts)
        new_calls = self.gh.calls[calls:]
        self.assertTrue(new_calls)
        self.assertTrue(all(call[:3] == ["gh", "pr", "view"] for call in new_calls), new_calls)
        self.assertEqual(result["confirmations"], dict.fromkeys(PHASE_ORDER, False))
        self.assertTrue(all(not self.phase(result, name)["confirmed"] for name in PHASE_ORDER))
        # What each phase would do is still reported.
        proposed = {o["task_key"]: o for o in self.phase(result, "pr_outcomes")["outcomes"]}
        self.assertTrue(proposed[merged.task_key]["merged"])
        self.assertEqual(proposed[closed.task_key]["proposed_transition"], schema.CANCELLED)
        self.assertEqual(proposed[reviewed.task_key]["proposed_transition"], schema.NEEDS_DECISION)
        self.assertTrue(all(o["applied_transition"] is None for o in proposed.values()))
        cleanup = {o["task_key"]: o for o in self.phase(result, "verified_merge_cleanup")["outcomes"]}
        self.assertEqual(cleanup[verifiable.task_key]["status"], "dry_run")
        self.assertTrue(cleanup[verifiable.task_key]["merge_verified"])
        self.assertEqual(cleanup[unverifiable.task_key]["status"], "merge_not_verified")
        self.assertIn(stale.task_key, self.phase(result, "target_freshness")["would_requeue"])
        self.assertEqual(self.phase(result, "target_freshness")["requeued"], [])
        self.assertEqual([o["status"] for o in result["outcomes"]], ["dry_run"])
        self.assertEqual(result["outcomes"][0]["task_key"], queued.task_key)
        self.assertFalse(result["ok"])  # the unverifiable merge needs attention


class FreshnessTests(ConsumerFixture):
    def test_stale_needs_review_is_requeued_and_reintegrated_in_the_same_pass(self):
        first, second = self.integrated(2)
        prs = {t.task_key: self.pr(t) for t in (first, second)}
        heads = {t.task_key: self.head(t) for t in (first, second)}
        target = self.fixture.advance_target()
        result = self.tick()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.phase(result, "target_freshness")["requeued"],
                         [first.task_key, second.task_key])
        self.assertEqual([o["task_key"] for o in result["outcomes"]], [first.task_key, second.task_key])
        for ticket in (first, second):
            state = self.integration.get_pr_state(ticket.task_key)
            self.assertEqual(self.status(ticket), schema.NEEDS_REVIEW)
            self.assertEqual(state["pr_number"], prs[ticket.task_key])
            self.assertEqual(state["integrated_base_sha"], target)
            self.assertEqual(state["reintegration_count"], 1)
            # Same PR, normal push: the old head stays in the new head's history.
            git(ticket.worktree_path, "merge-base", "--is-ancestor", heads[ticket.task_key], "HEAD")
            kinds = [row[1] for row in self.events(ticket)]
            self.assertIn("reintegration_required", kinds)
        creates = [c for c in self.gh.calls if c[:3] == ["gh", "pr", "create"]]
        self.assertEqual(len(creates), 2)

    def test_integrating_is_deferred_and_paused_or_needs_decision_is_never_requeued(self):
        integrating, paused, deciding = self.integrated(3)
        self.store.update_task_status(integrating.task_key, schema.INTEGRATING, source="test")
        self.store.update_task_status(paused.task_key, "paused", source="test")
        self.store.update_task_status(deciding.task_key, schema.NEEDS_DECISION, source="test")
        self.fixture.advance_target()
        result = self.tick()
        outcomes = {o["task_key"]: o for o in self.phase(result, "target_freshness")["outcomes"]}
        self.assertTrue(outcomes[integrating.task_key]["deferred"])
        self.assertIsNone(outcomes[integrating.task_key]["behind_count"])
        for ticket in (paused, deciding):
            self.assertTrue(outcomes[ticket.task_key]["stale"])
            self.assertFalse(outcomes[ticket.task_key]["requeued"])
        self.assertEqual(self.phase(result, "target_freshness")["requeued"], [])
        self.assertEqual([self.status(t) for t in (integrating, paused, deciding)],
                         [schema.INTEGRATING, "paused", schema.NEEDS_DECISION])
        self.assertEqual(queue_for_repo(self.integration, "owner/repo"), [])


class PrOutcomeTests(ConsumerFixture):
    def test_merge_is_recorded_without_completion_when_cleanup_is_unconfirmed(self):
        ticket = self.integrated(1)[0]
        sha = self.github_merge(ticket)
        result = self.tick(confirm_cleanup=False)
        state = self.integration.get_pr_state(ticket.task_key)
        self.assertTrue(state["pr_merged"])
        self.assertEqual(state["merge_commit_sha"], sha)
        self.assertEqual(self.status(ticket), schema.NEEDS_REVIEW)
        self.assertTrue(ticket.worktree_path.is_dir())
        self.assertEqual([row[1] for row in self.events(ticket)].count("merge_detected"), 1)
        (cleanup,) = self.phase(result, "verified_merge_cleanup")["outcomes"]
        self.assertEqual((cleanup["status"], cleanup["merge_verified"]), ("dry_run", True))
        self.assertTrue(result["ok"], result)
        events = self.events()
        again = self.tick(confirm_cleanup=False)
        self.assertEqual(self.events(), events)
        self.assertEqual(self.phase(again, "verified_merge_cleanup")["candidates"], [ticket.task_key])

    def test_changes_requested_moves_to_needs_decision_once_per_reviewed_head(self):
        ticket = self.integrated(1)[0]
        self.gh.set_pr(self.pr(ticket), reviewDecision="CHANGES_REQUESTED", reviews=REVIEW)
        result = self.tick()
        (outcome,) = self.phase(result, "pr_outcomes")["outcomes"]
        self.assertEqual(outcome["applied_transition"], schema.NEEDS_DECISION)
        self.assertEqual(self.status(ticket), schema.NEEDS_DECISION)
        self.assertEqual(len(self.integration.list_review_evidence(ticket.task_key)), 1)
        self.tick()
        self.assertEqual(len(self.integration.list_review_evidence(ticket.task_key)), 1)
        kinds = [row[1] for row in self.events(ticket)]
        self.assertEqual(kinds.count("pr_review_changes_requested"), 1)
        self.assertEqual(self.status(ticket), schema.NEEDS_DECISION)

    def test_closed_unmerged_is_cancelled_dequeued_and_never_cleaned(self):
        ticket = self.integrated(1)[0]
        # A Ticket already re-queued for re-integration when its PR is closed.
        self.store.update_task_status(ticket.task_key, schema.READY_FOR_INTEGRATION, source="test",
                                      expected_current_status=schema.NEEDS_REVIEW)
        enqueue_for_integration(self.integration, ticket.task_key, repo="owner/repo")
        self.gh.set_pr(self.pr(ticket), state="CLOSED")
        with patch("agent_taskflow.integration_tick.run_integration_cleanup",
                   wraps=run_integration_cleanup) as cleanup, \
                patch("agent_taskflow.integration_tick.integrate_task", wraps=integrate_task) as drain:
            for _ in range(3):
                result = self.tick()
                self.assertEqual(self.status(ticket), schema.CANCELLED)
        cleanup.assert_not_called()
        drain.assert_not_called()
        self.assertFalse(self.integration.is_queued(ticket.task_key))
        self.assertTrue(ticket.worktree_path.is_dir())
        self.assertTrue(self.branch_exists(self.fixture.repo, ticket.branch))
        self.assertTrue(self.branch_exists(self.fixture.origin, ticket.branch))
        self.assertIsNotNone(self.integration.get_integration_state(ticket.task_key)["closed_unmerged_at"])
        self.assertEqual(self.phase(result, "verified_merge_cleanup")["candidates"], [])

    def test_a_poll_failure_is_audited_and_reported_never_swallowed(self):
        ticket = self.integrated(1)[0]
        del self.gh.pulls[self.pr(ticket)]
        result = self.tick()
        (outcome,) = self.phase(result, "pr_outcomes")["outcomes"]
        self.assertIn("no such PR", outcome["poll_error"])
        self.assertFalse(self.phase(result, "pr_outcomes")["ok"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["tick_status"], "not_ok")
        self.assertIn("pr_poll_failed", [row[1] for row in self.events(ticket)])
        self.assertEqual(self.status(ticket), schema.NEEDS_DECISION)


class CleanupPhaseTests(ConsumerFixture):
    def test_verified_merge_completes_for_merge_squash_and_rebase(self):
        with patch("agent_taskflow.integration_tick.run_integration_cleanup",
                   wraps=run_integration_cleanup) as cleanup:
            for method in ("merge", "squash", "rebase"):
                with self.subTest(method=method):
                    ticket = self.integrated(1)[0]
                    head = self.head(ticket)
                    sha = self.github_merge(ticket, method=method)
                    self.assertNotEqual(sha, head)
                    result = self.tick()
                    self.assertTrue(result["ok"], result)
                    (outcome,) = self.phase(result, "verified_merge_cleanup")["outcomes"]
                    self.assertEqual(outcome["status"], "cleaned")
                    self.assertEqual(outcome["merge_commit_sha"], sha)
                    self.assertEqual(self.status(ticket), schema.COMPLETED)
                    self.assertFalse(ticket.worktree_path.exists())
                    self.assertFalse(self.branch_exists(self.fixture.repo, ticket.branch))
                    self.assertTrue(self.branch_exists(self.fixture.origin, ticket.branch))
                    self.assertFalse(outcome["remote_branch_deleted"])
                    self.assertEqual(self.store.get_task_worktree(ticket.task_key).status, "cleaned")
                    self.assertIsNotNone(
                        self.integration.get_integration_state(ticket.task_key)["merge_verified_at"])
        requests = [call.args[0] for call in cleanup.call_args_list]
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(not r.confirm_cancelled_cleanup and not r.delete_remote_branch
                            for r in requests))

    def test_failed_verification_leaves_the_ticket_uncompleted_and_a_candidate(self):
        ticket = self.integrated(1)[0]
        # GitHub claims a merge whose commit never reached the target.
        self.gh.set_pr(self.pr(ticket), state="MERGED", mergedAt="2026-09-25T00:00:00Z",
                       mergeCommit={"oid": self.head(ticket)})
        for _ in range(2):
            result = self.tick()
            (outcome,) = self.phase(result, "verified_merge_cleanup")["outcomes"]
            self.assertEqual(outcome["status"], "merge_not_verified")
            self.assertFalse(result["ok"])
            self.assertEqual(self.status(ticket), schema.NEEDS_REVIEW)
            self.assertTrue(ticket.worktree_path.is_dir())
            self.assertIsNone(self.integration.get_integration_state(ticket.task_key)["merge_verified_at"])

    def test_integrating_is_refused_and_stays_a_candidate_until_it_finishes(self):
        ticket = self.integrated(1)[0]
        sha = self.fixture.merge_branch_into_target(ticket.branch, method="merge")
        self.integration.update_pr_state(ticket.task_key, pr_state="closed", pr_merged=True,
                                         merge_commit_sha=sha)
        self.store.update_task_status(ticket.task_key, schema.INTEGRATING, source="test")
        for _ in range(2):
            result = self.tick()
            (outcome,) = self.phase(result, "verified_merge_cleanup")["outcomes"]
            self.assertEqual(outcome["status"], "blocked")
            self.assertEqual(self.status(ticket), schema.INTEGRATING)
            self.assertTrue(ticket.worktree_path.is_dir())
        self.store.update_task_status(ticket.task_key, schema.NEEDS_REVIEW, source="test")
        self.tick()
        self.assertEqual(self.status(ticket), schema.COMPLETED)

    def test_an_unsafe_target_is_refused_retained_and_reported_on_every_tick(self):
        ticket = self.integrated(1)[0]
        self.github_merge(ticket)
        sentinel = ticket.worktree_path / "operator-notes.txt"
        sentinel.write_text("keep me\n", encoding="utf-8")
        for _ in range(2):
            result = self.tick()
            (outcome,) = self.phase(result, "verified_merge_cleanup")["outcomes"]
            self.assertEqual(outcome["status"], "cleanup_refused")
            self.assertTrue(sentinel.is_file())
            self.assertEqual(self.status(ticket), schema.NEEDS_REVIEW)
            self.assertIsNone(self.integration.get_integration_state(ticket.task_key)["merge_verified_at"])
        sentinel.unlink()
        self.tick()
        self.assertEqual(self.status(ticket), schema.COMPLETED)

    def test_cleanup_incomplete_after_verification_is_reported_on_every_tick(self):
        """Review N4: a verified merge that git could not fully clean up never goes quiet."""
        ticket = self.integrated(1)[0]
        sha = self.github_merge(ticket)
        git(self.fixture.repo, "worktree", "lock", str(ticket.worktree_path))
        # Another repository's stalled Ticket is not this tick's to report.
        other = self.ticket(repo="owner/other", status=schema.NEEDS_REVIEW, enqueue=False)
        self.integration.update_pr_state(other.task_key, pr_number=9, pr_state="closed",
                                         pr_url="https://github.com/owner/other/pull/9",
                                         pr_merged=True, merge_commit_sha=sha)
        self.integration.update_integration_state(other.task_key,
                                                  merge_verified_at="2026-09-25T00:00:00+00:00")
        first = self.tick()
        (outcome,) = self.phase(first, "verified_merge_cleanup")["outcomes"]
        self.assertEqual(outcome["status"], "cleanup_incomplete")
        later = [self.tick(), self.preview()]
        for result in (first, *later):
            (report,) = result["merge_verified_not_completed"]
            self.assertEqual((report["task_key"], report["status"], report["merge_commit_sha"]),
                             (ticket.task_key, schema.NEEDS_REVIEW, sha))
            self.assertIsNotNone(report["merge_verified_at"])
            self.assertIsNone(report["cleanup_confirmed_at"])
            self.assertEqual(report["worktree_path"], str(ticket.worktree_path))
            self.assertFalse(result["ok"])
            self.assertEqual(result["tick_status"], "not_ok")
        for result in later:
            # It has left every pick-up; only the report keeps it visible.
            self.assertEqual(self.phase(result, "verified_merge_cleanup")["candidates"], [])
            self.assertTrue(all(phase["ok"] for phase in result["phases"].values()), result)
        self.assertEqual(self.status(ticket), schema.NEEDS_REVIEW)
        self.assertTrue(ticket.worktree_path.is_dir())
        # A human-confirmed retry once the cause is fixed finishes it; the report clears.
        git(self.fixture.repo, "worktree", "unlock", str(ticket.worktree_path))
        retry = run_integration_cleanup(
            IntegrationCleanupRequest(task_key=ticket.task_key, repo="owner/repo",
                                      repo_path=self.fixture.repo, db_path=self.db_path,
                                      confirm_cleanup=True),
            store=self.store, integration_store=self.integration)
        self.assertTrue(retry.ok, retry.summary)
        clear = self.tick()
        self.assertEqual(clear["merge_verified_not_completed"], [])
        self.assertTrue(clear["ok"], clear)
        self.assertEqual(self.status(ticket), schema.COMPLETED)


class LockReportTests(ConsumerFixture):
    def test_a_held_integration_lock_row_is_reported_and_left_untouched(self):
        queued = self.ticket()
        self.assertTrue(self.integration.acquire_integration_lock("owner/repo", owner="killed-runtime"))
        row = self.integration.get_integration_lock("owner/repo")
        result = self.tick()
        report = result["integration_lock_at_start"]
        self.assertTrue(report["held"])
        self.assertEqual((report["owner"], report["acquired_at"]), ("killed-runtime", row["acquired_at"]))
        self.assertEqual(result["stopped_reason"], "lock_unavailable")
        self.assertEqual(result["remaining_task_keys"], [queued.task_key])
        self.assertFalse(result["ok"])
        self.assertEqual(self.integration.get_integration_lock("owner/repo"), row)
        self.integration.release_integration_lock("owner/repo", owner="killed-runtime")
        clear = self.tick()
        self.assertEqual(clear["integration_lock_at_start"], {"held": False})
        self.assertEqual(self.status(queued), schema.NEEDS_REVIEW)


class IdempotenceTests(ConsumerFixture):
    def test_a_repeat_tick_with_nothing_new_changes_no_lifecycle_state(self):
        merged, stale = self.integrated(2)
        self.ticket()
        self.github_merge(merged)
        self.tick()
        self.tick()
        before_events = self.events()
        statuses = {key: self.store.get_task(key).status for key, _kind, _source in before_events}
        open_prs = [s["task_key"] for s in self.integration.list_open_pr_states("owner/repo")]
        calls = len(self.gh.calls)
        self.tick()
        new_events = self.events()[len(before_events):]
        # The watcher's per-poll §32 audit: exactly one pr_state_polled event
        # per open PR per confirmed poll, and nothing else.
        self.assertEqual(sorted(new_events),
                         sorted((key, "pr_state_polled", "integration_watcher") for key in open_prs))
        self.assertEqual({key: self.store.get_task(key).status for key in statuses}, statuses)
        self.assertTrue(all(c[:3] == ["gh", "pr", "view"] for c in self.gh.calls[calls:]))
        # With the PR poll unconfirmed, a repeat tick writes nothing at all.
        digest = self.db_digest()
        again = self.tick(confirm_pr_poll=False)
        self.assertTrue(again["ok"], again)
        self.assertEqual(self.db_digest(), digest)


class OrphanAndErrorTests(ConsumerFixture):
    def test_ready_but_unqueued_ticket_is_reported_and_never_enqueued(self):
        orphan = self.ticket(enqueue=False)
        for _ in range(2):
            result = self.tick()
            self.assertEqual([o["task_key"] for o in result["ready_for_integration_unqueued"]],
                             [orphan.task_key])
            self.assertFalse(result["ok"])
            self.assertEqual(result["tick_status"], "not_ok")
            self.assertFalse(self.integration.is_queued(orphan.task_key))
            self.assertEqual(self.status(orphan), schema.READY_FOR_INTEGRATION)

    def test_a_phase_that_raises_ends_the_pass_before_the_drain(self):
        queued = self.ticket()
        with patch("agent_taskflow.integration_tick.poll_target_freshness",
                   side_effect=RuntimeError("fixture failure")), \
                patch("agent_taskflow.integration_tick.integrate_task") as drain:
            result = self.tick()
        drain.assert_not_called()
        self.assertEqual(result["tick_status"], "error")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["stopped_reason"], "phase_error:target_freshness")
        self.assertEqual(self.phase(result, "target_freshness")["reason"], "RuntimeError: fixture failure")
        self.assertTrue(self.phase(result, "pr_outcomes")["ran"])
        self.assertFalse(self.phase(result, "queue_drain")["ran"])
        self.assertEqual(result["remaining_task_keys"], [queued.task_key])
        self.assertEqual(self.status(queued), schema.READY_FOR_INTEGRATION)

    def test_consumer_confirmations_require_the_consumer_phases(self):
        for name in ("confirm_pr_poll", "confirm_cleanup", "confirm_freshness"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                IntegrationTickRequest(repo="owner/repo", repo_path=self.fixture.repo,
                                       db_path=self.db_path, validator_specs=VALIDATORS,
                                       **{name: True})
            with self.subTest(name=name, value="string"), self.assertRaises(ValueError):
                self.request(**{name: "true"})

    def test_drain_only_requests_keep_the_f9_result_shape(self):
        self.ticket()
        result = run_integration_tick(IntegrationTickRequest(
            repo="owner/repo", repo_path=self.fixture.repo, db_path=self.db_path,
            validator_specs=VALIDATORS, dry_run=False, confirm_integration=True,
        ), github=self.github)
        for key in ("phases", "phase_order", "tick_status", "integration_lock_at_start",
                    "ready_for_integration_unqueued", "merge_verified_not_completed",
                    "confirmations", "drain_ok"):
            self.assertNotIn(key, result)
        self.assertEqual(result["status"], "drained")


if __name__ == "__main__":
    unittest.main()
