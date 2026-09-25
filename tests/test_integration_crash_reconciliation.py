"""Crash rehearsal for the integration flock and its reconciliation (RULINGS 69).

A real ``integrate_task`` runs in its own process against a local bare origin,
with GitHub faked, and is SIGKILLed at each crash point. A new writer in this
process then takes the lock, and the test checks the case it reconciled:
A, D and E recover automatically; B, C and F go to ``needs_decision`` with
their reason codes. Nothing reaches real GitHub.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_taskflow import integration_schema as schema
from agent_taskflow.github_pr_adapter import GitHubPrAdapter
from agent_taskflow.integration_controller import IntegrationRequest, integrate_task
from agent_taskflow.integration_crash_reconciliation import (
    REASON_AMBIGUOUS,
    REASON_PR_WITHOUT_RECORD,
    REASON_PUSHED_WITHOUT_PR,
    RUN_CREATING_PR,
    RUN_INTEGRATED,
    RUN_PUBLISHING,
    RUN_STARTED,
    classify_crash,
    reconcile_integrating_tickets,
)
from agent_taskflow.integration_queue import enqueue_for_integration
from agent_taskflow.integration_repo_lock import IntegrationRepoLock
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.integration_validators import IntegrationValidatorSpec
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore
from v1_step2_fixtures import (  # noqa: E402
    FakeGhRunner,
    GitFixture,
    git,
    isolate_integration_lock_dir,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
GREEN = (IntegrationValidatorSpec(name="unit", command=("true",)),)


def pid_alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in {"Z", "X", "x"}


def wait_for(predicate, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def flock_free(path: Path) -> bool:
    descriptor = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    finally:
        os.close(descriptor)
    return True


def killpg(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# The crashing holder: a real integrate_task in its own process. Crash points
# that need no timing kill the process from inside at an exact checkpoint;
# the others signal readiness through a marker file and wait to be killed.
HOLDER = r"""
import json, os, signal, sys, time
from pathlib import Path

cfg = json.loads(sys.argv[1])
sys.path.insert(0, cfg["tests_dir"])
from v1_step2_fixtures import FakeGhRunner
from agent_taskflow import integration_schema as schema
from agent_taskflow.github_pr_adapter import GitHubPrAdapter
from agent_taskflow.integration_controller import IntegrationRequest, integrate_task
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.integration_validators import IntegrationValidatorSpec
from agent_taskflow.store import TaskMirrorStore


def die(*args, **kwargs):
    os.kill(os.getpid(), signal.SIGKILL)


point = cfg["crash_point"]
fake = FakeGhRunner(start_number=cfg["start_number"])
for number in cfg["existing_prs"]:
    fake.set_pr(number, headRefName=cfg["branch"], baseRefName="main")


def runner(args, **kwargs):
    if point == "create_pr" and args[:3] == ["gh", "pr", "create"]:
        Path(cfg["marker"]).touch()
        time.sleep(120)
    return fake(args, **kwargs)


if point == "completion":
    IntegrationStore.record_integration_completed = die
if point == "needs_review":
    real = TaskMirrorStore.update_task_status

    def update(self, key, status, **kwargs):
        if status == schema.NEEDS_REVIEW:
            die()
        return real(self, key, status, **kwargs)

    TaskMirrorStore.update_task_status = update

integrate_task(
    IntegrationRequest(
        task_key=cfg["task_key"], repo="owner/repo", db_path=Path(cfg["db_path"]),
        validator_specs=[IntegrationValidatorSpec("v", tuple(cfg["validator"]))],
        dry_run=False, confirm_integration=True, owner="crash-holder",
    ),
    github=GitHubPrAdapter("owner/repo", runner=runner),
)
sys.exit(3)
"""


class CrashRehearsalCase(unittest.TestCase):
    def setUp(self) -> None:
        self.lock_dir = isolate_integration_lock_dir(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.fixture = GitFixture(self.root)
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.integration = IntegrationStore(store=self.store)
        self.marker = self.root / "crash-point-reached"

    # -- fixtures ----------------------------------------------------------
    def make_task(self, key: str) -> Path:
        worktree = self.fixture.create_task_worktree(key)
        self.fixture.commit_in(worktree, f"{key}.txt", f"{key}\n", f"feature {key}")
        artifacts = self.root / "artifacts" / key
        artifacts.mkdir(parents=True)
        self.store.upsert_task(TaskRecord(
            task_key=key, project="demo", status=schema.READY_FOR_INTEGRATION,
            repo_path=self.fixture.repo, title=f"{key} title", artifact_dir=artifacts,
        ))
        self.store.upsert_task_worktree(TaskWorktreeRecord(
            task_key=key, repo_path=self.fixture.repo, worktree_path=worktree,
            branch=f"task/{key}", base_branch="main", base_sha=self.fixture.target_sha(),
            status="active",
        ))
        enqueue_for_integration(self.integration, key, repo="owner/repo")
        return worktree

    def slow_validator(self) -> list[str]:
        return [sys.executable, "-c",
                f"import pathlib, time; pathlib.Path({str(self.marker)!r}).touch(); time.sleep(120)"]

    def install_slow_push_hook(self) -> Path:
        hook = self.fixture.origin / "hooks" / "pre-receive"
        hook.write_text(f"#!/bin/sh\ntouch {self.marker}\nsleep 120\n")
        hook.chmod(0o755)
        return hook

    def crash(self, key: str, point: str, *, validator=None, existing_prs=(),
              start_number: int = 41) -> dict:
        """Run the holder until it dies at ``point``; return its lock record."""
        config = {
            "tests_dir": str(TESTS_DIR), "db_path": str(self.db_path), "task_key": key,
            "crash_point": point, "marker": str(self.marker), "branch": f"task/{key}",
            "validator": list(validator or ["true"]), "existing_prs": list(existing_prs),
            "start_number": start_number,
        }
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER, json.dumps(config)],
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        def reap() -> None:
            if holder.poll() is None:
                holder.kill()
            holder.communicate(timeout=60)

        self.addCleanup(reap)
        if point in ("completion", "needs_review"):
            _, stderr = holder.communicate(timeout=300)
            self.assertEqual(holder.returncode, -signal.SIGKILL, stderr)
        else:
            wait_for(lambda: self.marker.exists() or holder.poll() is not None)
            if holder.poll() is not None:
                self.fail(f"the holder exited before {point}: {holder.communicate()[1]}")
            os.kill(holder.pid, signal.SIGKILL)
            holder.wait(timeout=60)
        self.holder_pid = holder.pid
        self.assertEqual(self.status(key), schema.INTEGRATING)
        return json.loads((self.lock_dir / "owner@repo.json").read_text())

    # -- the new writer ----------------------------------------------------
    def integrate(self, key: str, *, gh: FakeGhRunner | None = None):
        self.gh = gh or FakeGhRunner(start_number=60)
        return integrate_task(
            IntegrationRequest(
                task_key=key, repo="owner/repo", db_path=self.db_path,
                validator_specs=GREEN, dry_run=False, confirm_integration=True,
                owner="new-writer",
            ),
            store=self.store, integration_store=self.integration,
            github=GitHubPrAdapter("owner/repo", runner=self.gh),
        )

    def status(self, key: str) -> str:
        return self.store.get_task(key).status

    def events(self, key: str, event_type: str) -> list[dict]:
        return [json.loads(event.payload_json or "{}")
                for event in self.store.list_task_events(key) if event.event_type == event_type]

    def only_reconciliation(self, result) -> dict:
        report, = result.integration_lock["crash_reconciliation"]
        return report

    def assert_idempotent(self, key: str) -> None:
        """A repeat acquire reconciles nothing and changes nothing."""
        def snapshot():
            return (self.status(key), len(self.store.list_task_events(key)),
                    self.integration.get_pr_state(key), self.integration.is_queued(key),
                    self.integration.get_integration_state(key)["last_integration_status"])

        before = snapshot()
        lock = IntegrationRepoLock(
            "owner/repo", git_common_dir=(self.fixture.repo / ".git").resolve(),
            run_id="again", task_key=key, db_path=self.db_path,
        )
        acquisition = lock.acquire()
        self.assertTrue(acquisition.acquired)
        self.assertIsNone(acquisition.previous_holder)
        try:
            reports = reconcile_integrating_tickets(
                task_store=self.store, integration=self.integration, repo_key=lock.key,
                git_common_dir=lock.git_common_dir, reconciler_run_id="again",
                lock_evidence={},
            )
        finally:
            lock.release()
        self.assertEqual(reports, [])
        self.assertEqual(snapshot(), before)

    def assert_needs_decision(self, key: str, result, *, case: str, reason_code: str) -> None:
        report = self.only_reconciliation(result)
        self.assertEqual((report["task_key"], report["case"], report["reason_code"]),
                         (key, case, reason_code))
        self.assertFalse(report["automated_pr_discovery"])
        self.assertEqual(self.status(key), schema.NEEDS_DECISION)
        self.assertEqual(result.status, "blocked")
        blocked = self.events(key, "integration_blocked")[-1]
        self.assertEqual((blocked["reason_code"], blocked["crash_case"]), (reason_code, case))
        evidence = json.loads(Path(report["evidence_path"]).read_text())
        self.assertEqual((evidence["case"], evidence["reason_code"]), (case, reason_code))
        artifacts = [a.artifact_type for a in self.store.list_task_artifacts(key)]
        self.assertIn("integration_crash_reconciliation", artifacts)
        # Human decision: nothing was looked up on GitHub.
        self.assertEqual(self.gh.calls, [])


class OrphanedChildTests(CrashRehearsalCase):
    def test_a_killed_holders_validator_keeps_the_lock_then_case_a_reconciles(self) -> None:
        self.make_task("AT-1")
        record = self.crash("AT-1", "validator", validator=self.slow_validator())
        child, = record["holder"]["children"]
        self.addCleanup(killpg, child["pgid"])
        self.assertEqual(record["holder"]["pid"], self.holder_pid)
        self.assertEqual(self.integration.get_integration_state("AT-1")["last_integration_status"],
                         RUN_STARTED)
        # The validator outlived the holder and still holds the flock.
        self.assertTrue(pid_alive(child["pid"]))
        self.assertFalse(flock_free(self.lock_dir / "owner@repo.lock"))
        waiting = IntegrationRepoLock(
            "owner/repo", git_common_dir=(self.fixture.repo / ".git").resolve(),
            run_id="waiting", task_key="AT-1", db_path=self.db_path,
            terminate_orphaned_children=False,
        )
        self.assertFalse(waiting.acquire().acquired)
        self.assertTrue(pid_alive(child["pid"]))
        self.assertEqual(self.status("AT-1"), schema.INTEGRATING)

        # The next writer ends the orphaned tree, then takes the lock and
        # reconciles: A goes back to ready, keeps its queue slot, and reruns.
        result = self.integrate("AT-1")
        self.assertFalse(pid_alive(child["pid"]))
        acquisition = result.integration_lock["acquisition"]
        self.assertEqual([c["pid"] for c in acquisition["terminated_children"]], [child["pid"]])
        self.assertEqual(acquisition["previous_holder"]["pid"], self.holder_pid)
        self.assertEqual(result.integration_lock["leftover_journal_row"]["owner"], "crash-holder")
        report = self.only_reconciliation(result)
        self.assertEqual((report["case"], report["checkpoint"], report["queued"]),
                         ("A", RUN_STARTED, True))
        self.assertEqual(report["action"],
                         f"{schema.INTEGRATING} -> {schema.READY_FOR_INTEGRATION}")
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(self.status("AT-1"), schema.NEEDS_REVIEW)
        self.assertIsNone(self.integration.get_integration_lock("owner/repo"))
        self.assertEqual(self.integration.get_pr_state("AT-1")["reintegration_count"], 0)
        self.assert_idempotent("AT-1")

    def test_case_a_keeps_the_fifo_position(self) -> None:
        self.make_task("AT-1")
        self.make_task("AT-2")
        first = [row["task_key"] for row in self.integration.list_queue("owner/repo")]
        record = self.crash("AT-1", "validator", validator=self.slow_validator())
        self.addCleanup(killpg, record["holder"]["children"][0]["pgid"])
        # A writer for the *other* Ticket reconciles AT-1 but integrates AT-2.
        result = self.integrate("AT-2")
        self.assertEqual(self.only_reconciliation(result)["task_key"], "AT-1")
        self.assertEqual(self.status("AT-1"), schema.READY_FOR_INTEGRATION)
        self.assertEqual(first, ["AT-1", "AT-2"])
        self.assertEqual([row["task_key"] for row in self.integration.list_queue("owner/repo")],
                         ["AT-1"])
        self.assertEqual(self.integration.get_integration_state("AT-1")["last_integration_status"],
                         "crash_reconciled")

    def test_a_killed_holders_git_push_keeps_the_lock_then_case_b_needs_decision(self) -> None:
        self.make_task("AT-1")
        self.install_slow_push_hook()
        record = self.crash("AT-1", "push")
        child, = record["holder"]["children"]
        self.addCleanup(killpg, child["pgid"])
        self.assertEqual(child["argv0"], "git")
        self.assertEqual(self.integration.get_integration_state("AT-1")["last_integration_status"],
                         RUN_PUBLISHING)
        self.assertTrue(pid_alive(child["pid"]))
        self.assertFalse(flock_free(self.lock_dir / "owner@repo.lock"))

        result = self.integrate("AT-1")
        self.assertFalse(pid_alive(child["pid"]))
        self.assert_needs_decision("AT-1", result, case="B", reason_code=REASON_PUSHED_WITHOUT_PR)
        self.assertIsNone(self.integration.get_pr_state("AT-1")["pr_number"])
        self.assert_idempotent("AT-1")


class CheckpointCrashTests(CrashRehearsalCase):
    def test_case_c_pr_may_exist_without_a_record_needs_decision(self) -> None:
        self.make_task("AT-1")
        self.crash("AT-1", "create_pr")
        state = self.integration.get_integration_state("AT-1")
        self.assertEqual(state["last_integration_status"], RUN_CREATING_PR)
        self.assertIsNone(self.integration.get_pr_state("AT-1")["pr_number"])
        result = self.integrate("AT-1")
        self.assert_needs_decision("AT-1", result, case="C", reason_code=REASON_PR_WITHOUT_RECORD)
        self.assert_idempotent("AT-1")

    def test_case_d_initial_pr_recorded_reruns_without_counting_a_reintegration(self) -> None:
        self.make_task("AT-1")
        self.crash("AT-1", "completion")
        pr_state = self.integration.get_pr_state("AT-1")
        self.assertEqual(pr_state["pr_number"], 42)
        self.assertIsNone(pr_state["integrated_base_sha"])
        self.assertTrue(self.integration.is_queued("AT-1"))

        gh = FakeGhRunner(start_number=60)
        gh.set_pr(42, headRefName="task/AT-1", baseRefName="main")
        result = self.integrate("AT-1", gh=gh)
        report = self.only_reconciliation(result)
        self.assertEqual((report["case"], report["checkpoint"], report["pr_number"]),
                         ("D", RUN_CREATING_PR, 42))
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(self.status("AT-1"), schema.NEEDS_REVIEW)
        after = self.integration.get_pr_state("AT-1")
        # The same PR; no second PR was opened.
        self.assertEqual(after["pr_number"], 42)
        self.assertFalse(any(call[:3] == ["gh", "pr", "create"] for call in gh.calls))
        self.assertEqual(after["integrated_base_sha"], self.fixture.target_sha())
        self.assertEqual(after["reintegration_count"], 0)
        self.assert_idempotent("AT-1")

    def test_case_d_reintegration_pushed_reruns_and_counts_once(self) -> None:
        worktree = self.make_task("AT-1")
        gh = FakeGhRunner(start_number=41)
        self.assertTrue(self.integrate("AT-1", gh=gh).ok)
        # The target advances; the Ticket is re-queued for re-integration.
        self.fixture.advance_target("advanced.txt")
        self.integration.update_pr_state("AT-1", reintegration_required=True)
        self.store.update_task_status("AT-1", schema.READY_FOR_INTEGRATION,
                                      expected_current_status=schema.NEEDS_REVIEW)
        enqueue_for_integration(self.integration, "AT-1", repo="owner/repo")
        hook = self.install_slow_push_hook()
        record = self.crash("AT-1", "push", existing_prs=[42])
        self.addCleanup(killpg, record["holder"]["children"][0]["pgid"])
        self.assertEqual(self.integration.get_integration_state("AT-1")["last_integration_status"],
                         RUN_PUBLISHING)
        self.assertEqual(self.integration.get_pr_state("AT-1")["reintegration_count"], 0)

        hook.unlink()
        gh.set_pr(42, headRefName="task/AT-1", baseRefName="main")
        result = self.integrate("AT-1", gh=gh)
        report = self.only_reconciliation(result)
        self.assertEqual((report["case"], report["pr_number"]), ("D", 42))
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(result.mode, "reintegration")
        after = self.integration.get_pr_state("AT-1")
        self.assertEqual(after["pr_number"], 42)
        self.assertEqual(after["reintegration_count"], 1)
        self.assertEqual(after["integrated_base_sha"], self.fixture.target_sha())
        self.assertEqual(git(worktree, "rev-parse", "HEAD").strip(),
                         git(self.fixture.origin, "rev-parse", "task/AT-1").strip())
        self.assert_idempotent("AT-1")

    def test_case_e_integrated_and_dequeued_goes_to_review(self) -> None:
        self.make_task("AT-1")
        self.crash("AT-1", "needs_review")
        self.assertEqual(self.integration.get_integration_state("AT-1")["last_integration_status"],
                         RUN_INTEGRATED)
        self.assertFalse(self.integration.is_queued("AT-1"))
        base = self.integration.get_pr_state("AT-1")["integrated_base_sha"]
        result = self.integrate("AT-1")
        report = self.only_reconciliation(result)
        self.assertEqual((report["case"], report["pr_number"]), ("E", 42))
        self.assertEqual(self.status("AT-1"), schema.NEEDS_REVIEW)
        self.assertEqual(result.status, "blocked")  # nothing left to integrate
        self.assertEqual(self.integration.get_pr_state("AT-1")["integrated_base_sha"], base)
        self.assertEqual(self.gh.calls, [])
        self.assert_idempotent("AT-1")

    def test_case_f_ambiguous_state_needs_decision(self) -> None:
        self.make_task("AT-1")
        record = self.crash("AT-1", "validator", validator=self.slow_validator())
        self.addCleanup(killpg, record["holder"]["children"][0]["pgid"])
        # Half-written: the checkpoint says "before publishing", yet the queue
        # row is gone, so returning it to ready would lose it from the queue.
        self.integration.dequeue("AT-1")
        result = self.integrate("AT-1")
        self.assert_needs_decision("AT-1", result, case="F", reason_code=REASON_AMBIGUOUS)
        self.assert_idempotent("AT-1")


class InProgressOperationTests(CrashRehearsalCase):
    def test_case_a_aborts_an_in_progress_rebase(self) -> None:
        from agent_taskflow import integration_git as git_ops

        worktree = self.make_task("AT-1")
        self.fixture.commit_in(worktree, "shared.txt", "task\n", "task edit")
        self.fixture.advance_target("shared.txt", "target\n")
        git(worktree, "fetch", "origin")
        with self.assertRaises(AssertionError):
            git(worktree, "rebase", "origin/main")  # stops on the conflict
        self.assertEqual(git_ops.in_progress_operation(worktree), "rebase")
        # What a holder killed mid-rebase leaves behind.
        self.integration.update_integration_state(
            "AT-1", last_integration_run_id="dead", last_integration_status=RUN_STARTED
        )
        self.store.update_task_status("AT-1", schema.INTEGRATING)
        self.integration.acquire_integration_lock("owner/repo", owner="crash-holder")

        result = self.integrate("AT-1")
        report = self.only_reconciliation(result)
        self.assertEqual((report["case"], report["aborted_operation"]), ("A", "rebase"))
        self.assertIsNone(git_ops.in_progress_operation(worktree))
        # The rerun meets the same conflict and stops for a decision the
        # ordinary way (no resolver), not as a crash case.
        self.assertEqual(self.status("AT-1"), schema.NEEDS_DECISION)
        self.assertTrue(result.conflict_detected)
        self.assertEqual(self.events("AT-1", "integration_crash_reconciled")[0]["case"], "A")


class NeedsReviewUnderLockTests(CrashRehearsalCase):
    def test_the_needs_review_hand_off_happens_while_the_flock_is_held(self) -> None:
        self.make_task("AT-1")
        seen = []
        real = TaskMirrorStore.update_task_status

        def spy(store, key, status, **kwargs):
            if status == schema.NEEDS_REVIEW:
                seen.append(flock_free(self.lock_dir / "owner@repo.lock"))
            return real(store, key, status, **kwargs)

        with mock.patch.object(TaskMirrorStore, "update_task_status", spy):
            result = self.integrate("AT-1")
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(seen, [False])
        self.assertTrue(flock_free(self.lock_dir / "owner@repo.lock"))

    def test_every_integration_git_call_runs_without_background_gc(self) -> None:
        from agent_taskflow import integration_git as git_ops

        self.make_task("AT-1")
        calls = []
        real = git_ops.run_integration_child

        def spy(argv, **kwargs):
            calls.append(list(argv))
            return real(argv, **kwargs)

        with mock.patch.object(git_ops, "run_integration_child", side_effect=spy):
            self.assertTrue(self.integrate("AT-1").ok)
        under_lock = [argv for argv in calls if "push" in argv or "fetch" in argv]
        self.assertTrue(under_lock)
        for argv in under_lock:
            self.assertEqual(argv[1:1 + len(git_ops.GIT_NO_BACKGROUND_WORK)],
                             list(git_ops.GIT_NO_BACKGROUND_WORK))


class RefusalTests(CrashRehearsalCase):
    def test_a_lock_bound_to_another_clone_refuses_and_changes_nothing(self) -> None:
        from agent_taskflow.integration_repo_lock import IntegrationRepoLockError

        self.make_task("AT-1")
        other = GitFixture(self.root / "other-clone")
        lock = IntegrationRepoLock(
            "owner/repo", git_common_dir=(other.repo / ".git").resolve(),
            run_id="other", task_key="AT-0", db_path=self.db_path,
        )
        self.assertTrue(lock.acquire().acquired)
        lock.release()
        events = len(self.store.list_task_events("AT-1"))
        with self.assertRaises(IntegrationRepoLockError) as refused:
            self.integrate("AT-1")
        self.assertEqual(refused.exception.code, "git_common_dir_mismatch")
        self.assertEqual(self.status("AT-1"), schema.READY_FOR_INTEGRATION)
        self.assertTrue(self.integration.is_queued("AT-1"))
        self.assertEqual(len(self.store.list_task_events("AT-1")), events)
        self.assertIsNone(self.integration.get_integration_lock("owner/repo"))
        self.assertEqual(self.gh.calls, [])


class ClassificationTests(unittest.TestCase):
    """The deterministic table, including the combinations no crash can reach."""

    def test_the_case_table(self) -> None:
        rows = [
            ((RUN_STARTED, None, None, True), "A"),
            ((RUN_STARTED, 7, "base", True), "A"),
            ((RUN_STARTED, None, None, False), "F"),
            ((RUN_PUBLISHING, None, None, True), "B"),
            ((RUN_PUBLISHING, None, None, False), "B"),
            ((RUN_CREATING_PR, None, None, True), "C"),
            ((RUN_PUBLISHING, 7, "old-base", True), "D"),
            ((RUN_CREATING_PR, 7, None, True), "D"),
            ((RUN_CREATING_PR, 7, None, False), "F"),
            ((RUN_INTEGRATED, 7, "base", False), "E"),
            ((RUN_INTEGRATED, 7, "base", True), "F"),
            ((RUN_INTEGRATED, None, None, False), "F"),
            ((None, None, None, True), "F"),
            (("needs_decision", 7, "base", True), "F"),
            (("merge_detected", 7, "base", False), "F"),
        ]
        for (marker, pr, base, queued), case in rows:
            with self.subTest(marker=marker, pr=pr, base=base, queued=queued):
                got = classify_crash(marker, pr_number=pr, integrated_base_sha=base,
                                     queued=queued)
                self.assertEqual(got[0], case)
        self.assertEqual(
            classify_crash(RUN_STARTED, pr_number=None, integrated_base_sha=None, queued=True,
                           attribution_conflict="another repo")[:2],
            ("F", REASON_AMBIGUOUS),
        )
        self.assertEqual(classify_crash(RUN_PUBLISHING, pr_number=None, integrated_base_sha=None,
                                        queued=True)[1], REASON_PUSHED_WITHOUT_PR)
        self.assertEqual(classify_crash(RUN_CREATING_PR, pr_number=None, integrated_base_sha=None,
                                        queued=True)[1], REASON_PR_WITHOUT_RECORD)


class OtherRepositoryTests(CrashRehearsalCase):
    def test_another_repositorys_integrating_ticket_is_left_alone(self) -> None:
        other = GitFixture(self.root / "other")
        worktree = other.create_task_worktree("AT-9")
        self.store.upsert_task(TaskRecord(
            task_key="AT-9", project="demo", status=schema.INTEGRATING, repo_path=other.repo,
        ))
        self.store.upsert_task_worktree(TaskWorktreeRecord(
            task_key="AT-9", repo_path=other.repo, worktree_path=worktree, branch="task/AT-9",
            base_branch="main", base_sha=other.target_sha(), status="active",
        ))
        enqueue_for_integration(self.integration, "AT-9", repo="owner/other")
        self.make_task("AT-1")
        result = self.integrate("AT-1")
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(result.integration_lock["crash_reconciliation"], [])
        self.assertEqual(self.status("AT-9"), schema.INTEGRATING)


if __name__ == "__main__":
    unittest.main()
