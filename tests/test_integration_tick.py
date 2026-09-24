"""F9: the tick drives the existing controller with real disposable git."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v1_step2_fixtures import FakeGhRunner, GitFixture

from agent_taskflow import integration_schema as schema
from agent_taskflow.github_pr_adapter import GitHubPrAdapter
from agent_taskflow.integration_controller import integrate_task
from agent_taskflow.integration_queue import IntegrationLock, enqueue_for_integration, queue_for_repo
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.integration_tick import IntegrationTickRequest, run_integration_tick
from agent_taskflow.integration_validators import IntegrationValidatorSpec
from agent_taskflow.models import TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.ticket_fields_schema import migrate_ticket_fields, TicketFieldsMigrationRequired
from agent_taskflow.ticket_models import TicketRecord
from agent_taskflow.ticket_store import TicketStore


VALIDATORS = (IntegrationValidatorSpec(
    "feature", (sys.executable, "-c", "from pathlib import Path; assert Path('feature.txt').read_text() == 'ok\\n'")
),)


class TickFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = GitFixture(self.root)
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        migrate_ticket_fields(self.db_path)
        self.tickets = TicketStore(self.db_path)
        self.integration = IntegrationStore(self.db_path)
        self.gh = FakeGhRunner()
        self.github = GitHubPrAdapter("owner/repo", runner=self.gh)

    def make_ticket(self, *, priority="normal", repo="owner/repo", at=None,
                    status=schema.READY_FOR_INTEGRATION, content="ok\n"):
        ticket = self.tickets.create_ticket(
            actor="f9-test",
            build=lambda key: TicketRecord(
                task_key=key, repository="fixture", prompt="Test integration tick",
                title=key, priority=priority, status=status, repo_path=self.fixture.repo,
                base_branch="main", branch=f"task/{key}",
                worktree_path=self.fixture.repo / ".worktrees" / key,
                artifact_dir=self.root / "artifacts" / key, github_repo=repo,
            ),
        )
        path = self.fixture.create_task_worktree(ticket.task_key)
        ticket.artifact_dir.mkdir(parents=True)
        self.fixture.commit_in(path, "feature.txt", content, "feature")
        self.store.upsert_task_worktree(TaskWorktreeRecord(
            task_key=ticket.task_key, repo_path=ticket.repo_path,
            worktree_path=path, branch=ticket.branch, base_branch="main",
            base_sha=self.fixture.target_sha(), status="active",
        ))
        enqueue_for_integration(self.integration, ticket.task_key, repo=repo,
                                enqueued_at=at, priority=priority)
        return ticket

    def request(self, **overrides):
        values = dict(repo="owner/repo", repo_path=self.fixture.repo,
                      db_path=self.db_path, validator_specs=VALIDATORS,
                      dry_run=False, confirm_integration=True)
        values.update(overrides)
        return IntegrationTickRequest(**values)

    def tick(self, **overrides):
        return run_integration_tick(self.request(**overrides), github=self.github)

    def status(self, ticket):
        return self.store.get_task(ticket.task_key).status


class IntegrationTickTests(TickFixture):
    def test_fifo_timestamp_then_sequence_ignores_priority_and_other_repo(self):
        late = self.make_ticket(priority="critical", at="2026-09-01T00:00:02Z")
        first = self.make_ticket(priority="low", at="2026-09-01T00:00:01Z")
        tied = self.make_ticket(priority="high", at="2026-09-01T00:00:01Z")
        other = self.make_ticket(repo="owner/other", at="2026-08-01T00:00:00Z")
        seen = []

        def observe(request, **kwargs):
            self.assertIsNone(self.integration.get_integration_lock("owner/repo"))
            self.assertEqual(request.validator_specs, VALIDATORS)
            self.assertEqual(request.db_path, self.db_path)
            self.assertTrue(request.draft)
            seen.append(request.task_key)
            return integrate_task(request, **kwargs)

        with patch("agent_taskflow.integration_tick.integrate_task", side_effect=observe):
            result = self.tick()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "drained")
        self.assertEqual(seen, [first.task_key, tied.task_key, late.task_key])
        self.assertEqual([o["task_key"] for o in result["outcomes"]], seen)
        self.assertEqual([self.status(t) for t in (first, tied, late)], [schema.NEEDS_REVIEW] * 3)
        self.assertEqual(self.status(other), schema.READY_FOR_INTEGRATION)
        self.assertEqual([e.task_key for e in queue_for_repo(self.integration, "owner/other")], [other.task_key])
        self.assertIsNone(self.integration.get_integration_lock("owner/repo"))
        creates = [c for c in self.gh.calls if c[:3] == ["gh", "pr", "create"]]
        self.assertEqual(len(creates), 3)
        self.assertTrue(all("--draft" in c for c in creates))
        for outcome in result["outcomes"]:
            safety = outcome["integration"]["safety"]
            self.assertFalse(safety["force_pushed"])
            self.assertFalse(safety["merged"])
            self.assertFalse(safety["cleanup_performed"])
            self.assertFalse(safety["lock_held_after_return"])
        calls_before = list(self.gh.calls)
        again = self.tick()
        self.assertEqual(again["outcomes"], [])
        self.assertTrue(again["ok"])
        self.assertEqual(self.gh.calls, calls_before)

    def test_dry_run_and_missing_confirmation_leave_tasks_queue_and_git_untouched(self):
        ticket = self.make_ticket()
        events = self.store.list_task_events(ticket.task_key)
        for dry_run, confirm in ((True, False), (True, True), (False, False)):
            with self.subTest(dry_run=dry_run, confirm=confirm):
                result = self.tick(dry_run=dry_run, confirm_integration=confirm)
                self.assertTrue(result["ok"], result)
                self.assertTrue(result["dry_run"])
                self.assertEqual(result["outcomes"][0]["status"], "dry_run")
                self.assertEqual(result["outcomes"][0]["integration"]["git_commands"], [])
                self.assertEqual(self.status(ticket), schema.READY_FOR_INTEGRATION)
                self.assertEqual(self.store.list_task_events(ticket.task_key), events)
                self.assertEqual(self.gh.calls, [])
                self.assertTrue(self.integration.is_queued(ticket.task_key))

    def test_lock_unavailable_stops_before_second_ticket_without_retry(self):
        first, second = self.make_ticket(), self.make_ticket()
        with IntegrationLock(self.integration, "owner/repo", owner="other-runtime"):
            with patch("agent_taskflow.integration_tick.integrate_task", wraps=integrate_task) as call:
                result = self.tick()
            self.assertEqual(call.call_count, 1)
            self.assertEqual(result["stopped_reason"], "lock_unavailable")
            self.assertEqual(result["remaining_task_keys"], [first.task_key, second.task_key])
            self.assertEqual(self.integration.get_integration_lock("owner/repo")["owner"], "other-runtime")
        self.assertEqual(self.gh.calls, [])
        self.assertTrue(self.tick()["ok"])

    def test_paused_failed_and_nonready_items_keep_controller_refusal(self):
        refused = [self.make_ticket(status=s) for s in ("paused", "failed", schema.NEEDS_REVIEW)]
        ready = self.make_ticket()
        result = self.tick()
        self.assertFalse(result["ok"])
        self.assertEqual([o["status"] for o in result["outcomes"][:3]], ["blocked"] * 3)
        self.assertTrue(all(o["controller_called"] for o in result["outcomes"]))
        self.assertEqual([self.status(t) for t in refused], ["paused", "failed", schema.NEEDS_REVIEW])
        self.assertEqual(self.status(ready), schema.NEEDS_REVIEW)
        self.assertEqual(result["remaining_task_keys"], [t.task_key for t in refused])

    def test_validator_failure_is_audited_once_and_does_not_starve_next_item(self):
        bad, good = self.make_ticket(content="wrong\n"), self.make_ticket()
        result = self.tick()
        self.assertFalse(result["ok"])
        self.assertEqual(result["outcomes"][0]["status"], "needs_decision")
        self.assertFalse(result["outcomes"][0]["integration"]["validators_passed"])
        self.assertEqual(self.status(bad), schema.NEEDS_DECISION)
        self.assertEqual(self.status(good), schema.NEEDS_REVIEW)
        before = self.store.list_task_events(bad.task_key)
        calls = list(self.gh.calls)
        again = self.tick()
        self.assertEqual(again["outcomes"][0]["status"], "blocked")
        self.assertEqual(self.store.list_task_events(bad.task_key), before)
        self.assertEqual(self.gh.calls, calls)

    def test_binding_mismatch_never_reaches_controller(self):
        ticket = self.make_ticket()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE tasks SET github_repo = ? WHERE task_key = ?", ("owner/wrong", ticket.task_key))
        with patch("agent_taskflow.integration_tick.integrate_task") as call:
            result = self.tick()
        call.assert_not_called()
        self.assertEqual(result["outcomes"][0]["reason"], "ticket_repository_mismatch")
        self.assertEqual(self.status(ticket), schema.READY_FOR_INTEGRATION)

    def test_unregistered_worktree_is_refused_even_when_both_records_agree(self):
        ticket = self.make_ticket()
        rogue = self.fixture.repo / ".worktrees" / "unregistered"
        rogue.mkdir()
        with sqlite3.connect(self.db_path) as conn:
            for table in ("tasks", "task_worktrees"):
                conn.execute(f"UPDATE {table} SET worktree_path = ? WHERE task_key = ?", (str(rogue), ticket.task_key))
        with patch("agent_taskflow.integration_tick.integrate_task") as call:
            result = self.tick()
        call.assert_not_called()
        self.assertTrue(result["outcomes"][0]["reason"].startswith("ticket_worktree_invalid:"))
        self.assertEqual(self.status(ticket), schema.READY_FOR_INTEGRATION)

    def test_unexpected_controller_error_stops_and_is_machine_readable(self):
        first, second = self.make_ticket(), self.make_ticket()
        with patch("agent_taskflow.integration_tick.integrate_task", side_effect=RuntimeError("fixture failure")) as call:
            result = self.tick()
        self.assertEqual(call.call_count, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stopped_reason"], "integration_error")
        self.assertEqual(result["outcomes"][0]["reason"], "RuntimeError: fixture failure")
        self.assertEqual(result["remaining_task_keys"], [first.task_key, second.task_key])

    def test_new_entries_wait_for_next_tick(self):
        first = self.make_ticket()
        created = []

        def add_after(request, **kwargs):
            result = integrate_task(request, **kwargs)
            created.append(self.make_ticket())
            return result

        with patch("agent_taskflow.integration_tick.integrate_task", side_effect=add_after):
            result = self.tick()
        self.assertEqual(result["visited_count"], 1)
        self.assertEqual(result["outcomes"][0]["task_key"], first.task_key)
        self.assertEqual(result["remaining_task_keys"], [created[0].task_key])
        self.assertTrue(self.tick()["ok"])

    def test_reenqueued_snapshot_entry_waits_for_later_tick(self):
        first, second = self.make_ticket(), self.make_ticket()

        def reenqueue_after(request, **kwargs):
            result = integrate_task(request, **kwargs)
            self.integration.dequeue(second.task_key)
            enqueue_for_integration(self.integration, second.task_key, repo="owner/repo")
            return result

        with patch("agent_taskflow.integration_tick.integrate_task", side_effect=reenqueue_after) as call:
            result = self.tick()
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result["outcomes"][1]["reason"], "queue_entry_changed")
        self.assertEqual(self.status(first), schema.NEEDS_REVIEW)
        self.assertEqual(self.status(second), schema.READY_FOR_INTEGRATION)
        self.assertTrue(self.tick()["ok"])

    def test_missing_or_unmigrated_database_is_not_initialized(self):
        missing = self.root / "missing.db"
        with self.assertRaisesRegex(ValueError, "existing initialized database"):
            self.tick(db_path=missing)
        self.assertFalse(missing.exists())
        missing.touch()
        with self.assertRaises(TicketFieldsMigrationRequired):
            self.tick(db_path=missing)
        with sqlite3.connect(missing) as conn:
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])

    def test_request_requires_explicit_nonempty_validators_and_absolute_paths(self):
        for overrides in ({"validator_specs": ()}, {"validator_specs": VALIDATORS * 2},
                          {"db_path": Path("relative.db")}, {"repo_path": Path("relative")},
                          {"dry_run": "false"}, {"confirm_integration": "true"}):
            with self.subTest(overrides=repr(overrides)), self.assertRaises(ValueError):
                self.request(**overrides)
        request = replace(self.request(), dry_run=True, confirm_integration=False)
        self.assertTrue(request.dry_run)
        self.assertFalse(request.confirm_integration)


if __name__ == "__main__":
    unittest.main()
