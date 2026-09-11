"""V1 Step 5 acceptance: One Ticket = One Worktree (SPEC §9, §43.4; ruling 26)."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5_support import (  # noqa: E402
    RecordingExecutor,
    git,
    git_worktrees,
    make_fixture,
)

from agent_taskflow.task_status_reset import (  # noqa: E402
    TaskStatusResetRequest,
    reset_task_status,
)
from agent_taskflow.ticket_worktree import (  # noqa: E402
    ACTION_CREATED,
    ACTION_EXISTING,
    ACTION_REFUSED,
    TICKET_WORKTREE_CREATED,
    TICKET_WORKTREE_REFUSED,
    TICKET_WORKTREE_REUSED,
    ensure_ticket_worktree,
)
from agent_taskflow.ticket_worktree_schema import (  # noqa: E402
    TICKET_WORKTREE_MIGRATION_SCRIPT,
    TicketWorktreeMigrationRequired,
)


class Step5WorktreeTestCase(unittest.TestCase):
    migrate_step5 = True

    def setUp(self) -> None:
        self.fx = make_fixture(migrate_step5=self.migrate_step5)
        self.addCleanup(self.fx.cleanup)

    def retry(self, task_key: str, from_status: str):
        return reset_task_status(
            TaskStatusResetRequest(
                task_key=task_key,
                db_path=self.fx.db_path,
                from_status=from_status,
                reason="retry after a failed Attempt",
                actor="step5-test",
                confirm_reset=True,
            )
        )


class OneWorktreePerTicketTests(Step5WorktreeTestCase):
    def test_step1_ticket_gets_one_worktree_one_row_and_dispatch_runs(self) -> None:
        ticket = self.fx.create_ticket("Add the ending page image")
        self.assertEqual(self.fx.task_worktree_rows(ticket.task_key), [])
        self.assertEqual(git_worktrees(self.fx.repo), [])

        executor = RecordingExecutor()
        result = self.fx.dispatch(ticket.task_key, executor)

        self.assertEqual(result.status, "waiting_approval", result.summary)
        self.assertEqual(
            git_worktrees(self.fx.repo),
            [(ticket.worktree_path, f"refs/heads/{ticket.branch}")],
        )
        rows = self.fx.task_worktree_rows(ticket.task_key)
        self.assertEqual(len(rows), 1)
        self.assertEqual(Path(rows[0]["worktree_path"]), ticket.worktree_path)
        self.assertEqual(rows[0]["branch"], ticket.branch)
        self.assertEqual(Path(executor.contexts[0].worktree_path), ticket.worktree_path)
        resources = self.fx.attempt_resources(ticket.task_key)
        self.assertEqual(len(resources), 1)
        self.assertEqual(Path(resources[0]["worktree_path"]), ticket.worktree_path)
        self.assertEqual(resources[0]["branch_name"], ticket.branch)
        self.assertIn(TICKET_WORKTREE_CREATED, self.fx.event_kinds(ticket.task_key))

    def test_retry_reuses_the_same_worktree_as_the_last_attempt_left_it(self) -> None:
        ticket = self.fx.create_ticket("Retry keeps the worktree")
        first = RecordingExecutor("failed", write_file="attempt-one.txt")
        self.assertEqual(self.fx.dispatch(ticket.task_key, first).status, "failed")
        self.assertEqual(self.fx.status(ticket.task_key), "failed")
        self.assertTrue((ticket.worktree_path / "attempt-one.txt").is_file())

        reset = self.retry(ticket.task_key, "failed")
        self.assertEqual(reset.to_status, "created")
        self.assertEqual(self.fx.status(ticket.task_key), "created")

        second = RecordingExecutor()
        result = self.fx.dispatch(ticket.task_key, second)

        self.assertEqual(result.status, "waiting_approval", result.summary)
        self.assertIn("attempt-one.txt", second.seen_files[0])
        self.assertEqual(Path(second.contexts[0].worktree_path), ticket.worktree_path)
        self.assertNotEqual(first.contexts[0].attempt_id, second.contexts[0].attempt_id)
        self.assertEqual(
            git_worktrees(self.fx.repo),
            [(ticket.worktree_path, f"refs/heads/{ticket.branch}")],
        )
        resources = self.fx.attempt_resources(ticket.task_key)
        self.assertEqual([r["attempt_number"] for r in resources], [1, 2])
        self.assertEqual({r["worktree_path"] for r in resources}, {str(ticket.worktree_path)})
        self.assertEqual({r["branch_name"] for r in resources}, {ticket.branch})
        self.assertEqual(len({r["artifact_root"] for r in resources}), 2)
        self.assertEqual(len({r["lock_path"] for r in resources}), 2)
        reuse = [
            e for e in self.fx.events(ticket.task_key)
            if e["payload"].get("kind") == TICKET_WORKTREE_REUSED
        ]
        self.assertEqual([e["payload"]["attempt_number"] for e in reuse], [1, 2])
        self.assertEqual([e["payload"]["dirty"] for e in reuse], [False, True])
        self.assertTrue(all(e["payload"]["cleaned"] is False for e in reuse))
        self.assertEqual(len(self.fx.task_worktree_rows(ticket.task_key)), 1)

    def test_ensure_is_idempotent_and_audits_creation_once(self) -> None:
        ticket = self.fx.create_ticket("Idempotent worktree")
        first = ensure_ticket_worktree(self.fx.db_path, ticket.task_key)
        events_after_first = self.fx.events(ticket.task_key)
        second = ensure_ticket_worktree(self.fx.db_path, ticket.task_key)

        self.assertEqual(first.action, ACTION_CREATED)
        self.assertEqual(second.action, ACTION_EXISTING)
        self.assertEqual(self.fx.events(ticket.task_key), events_after_first)
        self.assertEqual(self.fx.event_kinds(ticket.task_key).count(TICKET_WORKTREE_CREATED), 1)
        self.assertEqual(len(git_worktrees(self.fx.repo)), 1)


class FailClosedWorktreeTests(Step5WorktreeTestCase):
    def assert_failed_without_touching_path(self, ticket, result, expected: str) -> None:
        self.assertEqual(result.status, "failed")
        self.assertIn(expected, result.summary)
        self.assertEqual(self.fx.status(ticket.task_key), "failed")
        self.assertIn(TICKET_WORKTREE_REFUSED, self.fx.event_kinds(ticket.task_key))
        last = self.fx.status_events(ticket.task_key)[-1]
        self.assertEqual(last["payload"]["status"], "failed")
        self.assertIn(expected, last["message"])
        self.assertEqual(self.fx.attempts(ticket.task_key), [])
        self.assertEqual(self.fx.leases(ticket.task_key), [])

    def test_plain_directory_at_the_path_is_neither_deleted_nor_recreated(self) -> None:
        ticket = self.fx.create_ticket("Path is a plain directory")
        ticket.worktree_path.mkdir(parents=True)
        (ticket.worktree_path / "keep.txt").write_text("operator data\n", encoding="utf-8")
        executor = RecordingExecutor()

        result = self.fx.dispatch(ticket.task_key, executor)

        self.assert_failed_without_touching_path(ticket, result, "is not a git worktree")
        self.assertEqual(executor.contexts, [])
        self.assertTrue((ticket.worktree_path / "keep.txt").is_file())
        self.assertEqual(git_worktrees(self.fx.repo), [])

    def test_worktree_on_another_branch_is_refused(self) -> None:
        ticket = self.fx.create_ticket("Wrong branch")
        git(self.fx.repo, "worktree", "add", str(ticket.worktree_path), "-b", "someone-else", "main")

        result = self.fx.dispatch(ticket.task_key, RecordingExecutor())

        self.assert_failed_without_touching_path(ticket, result, "refs/heads/someone-else")
        self.assertEqual(
            git_worktrees(self.fx.repo),
            [(ticket.worktree_path, "refs/heads/someone-else")],
        )

    def test_ticket_branch_without_its_worktree_is_refused(self) -> None:
        ticket = self.fx.create_ticket("Branch exists already")
        git(self.fx.repo, "branch", ticket.branch, "main")

        result = self.fx.dispatch(ticket.task_key, RecordingExecutor())

        self.assert_failed_without_touching_path(ticket, result, "already exists without its worktree")
        self.assertFalse(ticket.worktree_path.exists())

    def test_refused_ensure_reports_and_audits(self) -> None:
        ticket = self.fx.create_ticket("Refused ensure")
        ticket.worktree_path.mkdir(parents=True)
        result = ensure_ticket_worktree(self.fx.db_path, ticket.task_key)
        self.assertEqual(result.action, ACTION_REFUSED)
        self.assertFalse(result.ok)
        refused = [
            e for e in self.fx.events(ticket.task_key)
            if e["payload"].get("kind") == TICKET_WORKTREE_REFUSED
        ]
        self.assertEqual(len(refused), 1)
        self.assertFalse(refused[0]["payload"]["deleted"])
        self.assertFalse(refused[0]["payload"]["recreated"])


class MigrationRequiredTests(Step5WorktreeTestCase):
    migrate_step5 = False

    def test_ticket_dispatch_refuses_untouched_and_names_the_script(self) -> None:
        ticket = self.fx.create_ticket("No Step 5 migration")
        # The dispatch entry's lazy lifecycle migration backfills a missing
        # task_id on every row (F1 handoff §4.3, ruling 14): apply it first so
        # the snapshot measures the refusal alone.
        from agent_taskflow.attempt_schema import migrate_task_attempt_lifecycle

        migrate_task_attempt_lifecycle(self.fx.db_path)
        before = self.fx.task_row(ticket.task_key)
        events_before = self.fx.events(ticket.task_key)

        result = self.fx.dispatch(ticket.task_key, RecordingExecutor())

        self.assertNotEqual(result.status, "waiting_approval")
        self.assertIn(TICKET_WORKTREE_MIGRATION_SCRIPT, result.summary)
        self.assertEqual(self.fx.task_row(ticket.task_key), before)
        self.assertEqual(self.fx.events(ticket.task_key), events_before)
        self.assertEqual(git_worktrees(self.fx.repo), [])

    def test_ensure_fails_closed_before_touching_anything(self) -> None:
        ticket = self.fx.create_ticket("Ensure without migration")
        with self.assertRaises(TicketWorktreeMigrationRequired) as ctx:
            ensure_ticket_worktree(self.fx.db_path, ticket.task_key)
        self.assertIn(TICKET_WORKTREE_MIGRATION_SCRIPT, str(ctx.exception))
        self.assertFalse(ticket.worktree_path.exists())

    def test_legacy_task_still_runs_without_the_migration(self) -> None:
        self.fx.add_legacy_task("AT-LEGACY-1")
        from agent_taskflow.models import TaskWorktreeRecord
        from agent_taskflow.store import TaskMirrorStore

        worktree = self.fx.repo / ".worktrees" / "AT-LEGACY-1"
        TaskMirrorStore(self.fx.db_path).upsert_task_worktree(
            TaskWorktreeRecord(
                task_key="AT-LEGACY-1",
                repo_path=self.fx.repo,
                worktree_path=worktree,
                branch="task/AT-LEGACY-1",
                base_branch="main",
                status="active",
            )
        )
        result = self.fx.dispatch("AT-LEGACY-1", RecordingExecutor())
        self.assertEqual(result.status, "waiting_approval", result.summary)


class LegacyFreshWorktreeContractTests(Step5WorktreeTestCase):
    """Ruling 26c: legacy tasks keep a fresh branch and worktree per Attempt."""

    def test_legacy_retry_gets_a_new_worktree_even_after_the_rebuild(self) -> None:
        from agent_taskflow.models import TaskWorktreeRecord
        from agent_taskflow.store import TaskMirrorStore

        self.fx.add_legacy_task("AT-LEGACY-2")
        TaskMirrorStore(self.fx.db_path).upsert_task_worktree(
            TaskWorktreeRecord(
                task_key="AT-LEGACY-2",
                repo_path=self.fx.repo,
                worktree_path=self.fx.repo / ".worktrees" / "AT-LEGACY-2",
                branch="task/AT-LEGACY-2",
                base_branch="main",
                status="active",
            )
        )
        self.assertEqual(self.fx.dispatch("AT-LEGACY-2", RecordingExecutor("failed")).status, "blocked")
        self.retry("AT-LEGACY-2", "blocked")
        self.assertEqual(self.fx.status("AT-LEGACY-2"), "queued")
        self.assertEqual(self.fx.dispatch("AT-LEGACY-2", RecordingExecutor()).status, "waiting_approval")

        resources = self.fx.attempt_resources("AT-LEGACY-2")
        self.assertEqual(len(resources), 2)
        self.assertNotEqual(resources[0]["worktree_path"], resources[1]["worktree_path"])
        self.assertNotEqual(resources[0]["branch_name"], resources[1]["branch_name"])


if __name__ == "__main__":
    unittest.main()
