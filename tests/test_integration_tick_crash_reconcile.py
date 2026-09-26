"""V1-TICK-RECONCILE (NB-1, OR-11): the integration tick reconciles crashed Tickets.

A dead integration holder can leave a lone Ticket in ``integrating``. Only a
flock acquire reconciles it (RULINGS 69), and before this fix only another
Ticket's ``integrate_task`` acquired the flock, so a lone case E stayed
``integrating`` silently for good. Now a confirmed tick that finds this
repository's Tickets in ``integrating`` calls the controller's
``reconcile_repository`` first.

The crashes are real: ``crash_tick.py`` runs the release's
``scripts/run_integration_tick.py`` with the cron flags and SIGKILLs its own
process at the crash point. Every later tick is the unmodified script. ``gh``
is a stateful fake on PATH; HOME-level state and the lock dir are temp.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from execution_policy_support import release_tree  # noqa: E402
from test_integration_tick import TickFixture  # noqa: E402
from v1_step2_fixtures import GitFixture, hold_integration_lock  # noqa: E402

from agent_taskflow import integration_git as git_ops
from agent_taskflow import integration_schema as schema
from agent_taskflow.integration_controller import reconcile_repository
from agent_taskflow.integration_crash_reconciliation import RUN_INTEGRATED, RUN_STARTED
from agent_taskflow.integration_repo_lock import IntegrationRepoLock
from agent_taskflow.models import TaskWorktreeRecord
from agent_taskflow.ticket_models import TicketRecord


HERE = Path(__file__).resolve().parent

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
sys.path.insert(0, os.environ["RECONCILE_TESTS"])
from v1_step2_fixtures import FakeGhRunner
state = os.environ["RECONCILE_GH_STATE"]
gh = FakeGhRunner()
if os.path.exists(state):
    saved = json.load(open(state))
    gh.pulls = {int(k): v for k, v in saved["pulls"].items()}
    gh._next_number = saved["next"]
    gh.calls = saved["calls"]
result = gh(["gh", *sys.argv[1:]])
json.dump({"pulls": gh.pulls, "next": gh._next_number, "calls": gh.calls}, open(state, "w"))
sys.stdout.write(result.stdout); sys.stderr.write(result.stderr)
sys.exit(result.returncode)
'''

CRASH_TICK = r'''import importlib.util, os, signal, sys
release = sys.argv[1]; point = sys.argv[2]
sys.path.insert(0, release)
from agent_taskflow import integration_controller as ic
from agent_taskflow import integration_schema as schema
from agent_taskflow.store import TaskMirrorStore
def die(*a, **k):
    os.kill(os.getpid(), signal.SIGKILL)
if point == "A_mid_rebase":
    # The rebase stopped on a conflict; die before the resolver or the abort.
    ic.resolve_conflicts = die
elif point == "E_before_needs_review":
    # The completion transaction committed; die at the needs_review CAS.
    real = TaskMirrorStore.update_task_status
    def patched(self, key, status, *a, **k):
        if status == schema.NEEDS_REVIEW:
            die()
        return real(self, key, status, *a, **k)
    TaskMirrorStore.update_task_status = patched
spec = importlib.util.spec_from_file_location("tick", release + "/scripts/run_integration_tick.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
raise SystemExit(mod.main(sys.argv[3:]))
'''


def db_digest(db_path: Path) -> tuple[str, list[str]]:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dump = list(conn.iterdump())
    return hashlib.sha256(db_path.read_bytes()).hexdigest(), dump


def dir_digest(path: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(path.iterdir())} if path.is_dir() else {}


class CrashReconcileFixture(TickFixture):
    def setUp(self) -> None:
        super().setUp()
        self.release = release_tree(self.root / "release", self.registry_path)
        self.script = self.release / "scripts" / "run_integration_tick.py"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(FAKE_GH)
        (bin_dir / "gh").chmod(0o755)
        (self.root / "crash_tick.py").write_text(CRASH_TICK)
        self.gh_state = self.root / "gh-state.json"
        self.env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "PYTHONPATH": str(self.release), "RECONCILE_TESTS": str(HERE),
                    "RECONCILE_GH_STATE": str(self.gh_state)}
        self.base_args = ["--db-path", str(self.db_path), "--repo", "owner/repo",
                          "--repo-path", str(self.fixture.repo), "--jsonl"]
        self.cron_args = [*self.base_args, "--confirm-integration", "--confirm-pr-poll",
                          "--confirm-cleanup", "--confirm-freshness"]

    def run_script(self, *args: str) -> tuple[int, dict]:
        done = subprocess.run([sys.executable, str(self.script), *(args or self.cron_args)],
                              env=self.env, capture_output=True, text=True, check=False)
        self.assertTrue(done.stdout.strip(), done.stderr)
        return done.returncode, json.loads(done.stdout)

    def crash_script(self, point: str) -> None:
        done = subprocess.run([sys.executable, str(self.root / "crash_tick.py"), str(self.release),
                               point, *self.cron_args], env=self.env, capture_output=True,
                              text=True, check=False)
        self.assertEqual(done.returncode, -9, done.stdout + done.stderr)

    def gh_calls(self, *prefix: str) -> list[list[str]]:
        calls = json.loads(self.gh_state.read_text())["calls"] if self.gh_state.exists() else []
        wanted = ["gh", *prefix]
        return [call for call in calls if call[:len(wanted)] == wanted]

    def worktree(self, ticket) -> Path:
        return self.fixture.repo / ".worktrees" / ticket.task_key

    def marker(self, ticket) -> str | None:
        return self.integration.get_integration_state(ticket.task_key)["last_integration_status"]

    def simulated_e(self):
        """A Ticket in lone case E, reached in-process: integrated, then status reset."""
        ticket = self.make_ticket()
        self.assertTrue(self.tick()["ok"])
        self.store.update_task_status(ticket.task_key, schema.INTEGRATING)
        self.assertEqual(self.marker(ticket), RUN_INTEGRATED)
        self.assertFalse(self.integration.is_queued(ticket.task_key))
        return ticket


class LoneCaseETests(CrashReconcileFixture):
    def test_lone_e_after_a_real_sigkill_is_reconciled_then_its_merge_recorded(self):
        ticket = self.make_ticket()
        self.crash_script("E_before_needs_review")
        self.assertEqual(self.status(ticket), schema.INTEGRATING)
        self.assertEqual(self.marker(ticket), RUN_INTEGRATED)
        self.assertFalse(self.integration.is_queued(ticket.task_key))
        pr = self.integration.get_pr_state(ticket.task_key)
        self.assertIsNotNone(pr["pr_number"])

        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertEqual([t["task_key"] for t in out["integrating_tickets"]], [ticket.task_key])
        crash = out["crash_reconciliation"]
        self.assertEqual((crash["ran"], crash["ok"], crash["status"]), (True, True, "reconciled"))
        self.assertTrue(crash["integration_lock"]["acquisition"]["acquired"])
        report, = crash["reports"]
        self.assertEqual((report["task_key"], report["case"]), (ticket.task_key, "E"))
        self.assertEqual(self.status(ticket), schema.NEEDS_REVIEW)
        # Reconciled before the PR poll, so the poll sees a needs_review Ticket.
        poll, = out["phases"]["pr_outcomes"]["outcomes"]
        self.assertFalse(poll["deferred"])

        # A repeat tick has nothing to reconcile and takes no lock for it.
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertEqual((out["integrating_tickets"], out["crash_reconciliation"]["status"]),
                         ([], "not_needed"))

        # A human merges the PR on GitHub; the next tick records it.
        sha = self.fixture.merge_branch_into_target(ticket.branch, method="merge")
        saved = json.loads(self.gh_state.read_text())
        saved["pulls"][str(pr["pr_number"])].update(
            state="MERGED", mergedAt="2026-09-26T00:00:00Z", mergeCommit={"oid": sha})
        self.gh_state.write_text(json.dumps(saved))
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertTrue(self.integration.get_pr_state(ticket.task_key)["pr_merged"])
        self.assertEqual(self.status(ticket), schema.COMPLETED)
        # One PR, created by the killed run; reconciliation never makes another.
        self.assertEqual(len(self.gh_calls("pr", "create")), 1)


class MidRebaseTests(CrashReconcileFixture):
    def killed_mid_rebase(self):
        ticket = self.make_ticket()
        self.fixture.commit_in(self.worktree(ticket), "shared.txt", "task\n", "task edit")
        self.fixture.advance_target("shared.txt", "target\n")
        self.crash_script("A_mid_rebase")
        self.assertEqual(self.status(ticket), schema.INTEGRATING)
        self.assertEqual(self.marker(ticket), RUN_STARTED)
        self.assertEqual(git_ops.in_progress_operation(self.worktree(ticket)), "rebase")
        return ticket

    def test_a_mid_rebase_after_a_real_sigkill_is_aborted_and_drained_in_the_same_tick(self):
        ticket = self.killed_mid_rebase()
        code, out = self.run_script()
        report, = out["crash_reconciliation"]["reports"]
        self.assertEqual((report["case"], report["aborted_operation"]), ("A", "rebase"))
        self.assertIsNone(git_ops.in_progress_operation(self.worktree(ticket)))
        # The same tick's drain passes _binding_error and calls the controller;
        # the real conflict with no resolver is a human decision, as designed.
        outcome, = out["outcomes"]
        self.assertTrue(outcome["controller_called"], outcome)
        self.assertEqual(outcome["status"], "needs_decision")
        self.assertEqual(self.status(ticket), schema.NEEDS_DECISION)
        self.assertEqual(code, 1)
        self.assertEqual(self.gh_calls("pr", "create"), [])

    def test_a_dry_run_lists_the_ticket_and_changes_nothing(self):
        ticket = self.killed_mid_rebase()
        before, locks = db_digest(self.db_path), dir_digest(self.lock_dir)
        code, out = self.run_script(*self.base_args, "--dry-run")
        # The drain's read-only preview still blocks the detached HEAD (exit 1).
        self.assertEqual(code, 1, out)
        self.assertEqual(out["outcomes"][0]["status"], "blocked")
        listed, = out["integrating_tickets"]
        self.assertEqual((listed["task_key"], listed["checkpoint"]), (ticket.task_key, RUN_STARTED))
        crash = out["crash_reconciliation"]
        self.assertEqual((crash["ran"], crash["status"], crash["reports"]), (False, "dry_run", []))
        self.assertEqual(db_digest(self.db_path), before)
        self.assertEqual(dir_digest(self.lock_dir), locks)
        self.assertEqual(git_ops.in_progress_operation(self.worktree(ticket)), "rebase")
        self.assertEqual(self.status(ticket), schema.INTEGRATING)


class GateTests(CrashReconcileFixture):
    def test_no_integrating_ticket_takes_no_flock_and_writes_nothing(self):
        ticket = self.make_ticket()
        self.assertTrue(self.tick()["ok"])
        self.assertEqual(self.status(ticket), schema.NEEDS_REVIEW)
        before, locks = db_digest(self.db_path), dir_digest(self.lock_dir)
        with patch.object(IntegrationRepoLock, "acquire",
                          side_effect=AssertionError("flock taken")) as acquire:
            for _ in range(2):
                result = self.tick()
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["integrating_tickets"], [])
                self.assertEqual(result["crash_reconciliation"]["status"], "not_needed")
        acquire.assert_not_called()
        self.assertEqual(db_digest(self.db_path), before)
        self.assertEqual(dir_digest(self.lock_dir), locks)

    def test_a_live_holder_leaves_the_ticket_untouched_and_the_tick_not_ok(self):
        ticket = self.simulated_e()
        hold_integration_lock(self, "owner/repo", self.fixture.repo)
        events = len(self.store.list_task_events(ticket.task_key))
        result = self.tick(consumer_phases=True, confirm_pr_poll=True,
                           confirm_cleanup=True, confirm_freshness=True)
        crash = result["crash_reconciliation"]
        self.assertEqual((crash["ok"], crash["status"], crash["reports"]),
                         (False, "lock_unavailable", []))
        self.assertFalse(result["ok"])
        self.assertEqual(result["tick_status"], "not_ok")
        self.assertEqual(self.status(ticket), schema.INTEGRATING)
        self.assertEqual(self.marker(ticket), RUN_INTEGRATED)
        self.assertEqual(len(self.store.list_task_events(ticket.task_key)), events)

    def test_another_repositorys_integrating_ticket_is_untouched(self):
        mine = self.simulated_e()
        other_clone = GitFixture(self.root / "other")
        theirs = self.tickets.create_ticket(
            actor="reconcile-test",
            build=lambda key: TicketRecord(
                task_key=key, repository="fixture-other", prompt="other repo",
                title=key, priority="normal", status=schema.READY_FOR_INTEGRATION,
                repo_path=other_clone.repo, base_branch="main", branch=f"task/{key}",
                worktree_path=other_clone.repo / ".worktrees" / key,
                artifact_dir=self.root / "artifacts" / key, github_repo="owner/other",
            ),
        )
        path = other_clone.create_task_worktree(theirs.task_key)
        self.store.upsert_task_worktree(TaskWorktreeRecord(
            task_key=theirs.task_key, repo_path=other_clone.repo, worktree_path=path,
            branch=theirs.branch, base_branch="main", base_sha=other_clone.target_sha(),
            status="active",
        ))
        self.store.update_task_status(theirs.task_key, schema.INTEGRATING)
        self.integration.update_integration_state(
            theirs.task_key, last_integration_run_id="dead", last_integration_status=RUN_STARTED)
        events = len(self.store.list_task_events(theirs.task_key))

        result = self.tick()
        self.assertEqual([t["task_key"] for t in result["integrating_tickets"]], [mine.task_key])
        self.assertEqual([r["task_key"] for r in result["crash_reconciliation"]["reports"]],
                         [mine.task_key])
        self.assertEqual(self.status(mine), schema.NEEDS_REVIEW)
        self.assertEqual(self.status(theirs), schema.INTEGRATING)
        self.assertEqual(self.marker(theirs), RUN_STARTED)
        self.assertEqual(len(self.store.list_task_events(theirs.task_key)), events)


class LockIdentityTests(CrashReconcileFixture):
    def test_reconcile_repository_takes_the_same_lock_as_integrate_task(self):
        ticket = self.make_ticket()
        result = self.tick()
        integrated = result["outcomes"][0]["integration"]["integration_lock"]
        self.assertEqual(git_ops.git_common_dir(self.fixture.repo),
                         git_ops.git_common_dir(self.worktree(ticket)))
        self.store.update_task_status(ticket.task_key, schema.INTEGRATING)
        # The binding check would raise git_common_dir_mismatch otherwise.
        reconciled = reconcile_repository(
            "owner/repo", repo_path=self.fixture.repo, task_key=ticket.task_key,
            db_path=self.db_path, owner="integration_tick",
        )
        self.assertEqual(reconciled["status"], "reconciled")
        lock = reconciled["integration_lock"]
        for key in ("key", "lock_path", "record_path", "git_common_dir"):
            self.assertEqual(lock[key], integrated[key], key)
        # The shared on-acquire block ran: no journal row of its own is left.
        self.assertIsNone(self.integration.get_integration_lock("owner/repo"))
        self.assertIsNone(json.loads(Path(lock["record_path"]).read_text())["holder"])
        self.assertEqual(self.status(ticket), schema.NEEDS_REVIEW)

    def test_the_tick_pass_clears_a_leftover_journal_row_like_integrate_task(self):
        ticket = self.simulated_e()
        self.assertTrue(self.integration.acquire_integration_lock("owner/repo", owner="killed"))
        row = self.integration.get_integration_lock("owner/repo")
        result = self.tick()
        self.assertEqual(result["crash_reconciliation"]["integration_lock"]["leftover_journal_row"], row)
        self.assertIsNone(self.integration.get_integration_lock("owner/repo"))
        self.assertEqual(self.status(ticket), schema.NEEDS_REVIEW)


if __name__ == "__main__":
    unittest.main()
