"""Unit tests for agent_taskflow.integration_repo_lock (RULINGS 69, owner decision D3)."""

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

from agent_taskflow import integration_git as git_ops
from agent_taskflow import integration_repo_lock as repo_lock
from agent_taskflow.integration_repo_lock import (
    FailClosedExternalHoldProbe,
    IntegrationRepoLock,
    IntegrationRepoLockError,
    LOCK_DIR_ENV,
    resolve_lock_dir,
    run_integration_child,
)
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.store import TaskMirrorStore
from v1_step2_fixtures import isolate_integration_lock_dir  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[1]


def pid_alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in {"Z", "X", "x"}


def wait_for(predicate, timeout: float = 60.0) -> None:
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


class RecordingProbe:
    """A test double for the OR-8.1 takeover check (D2 will supply systemd's)."""

    def __init__(self, gone: set[str]) -> None:
        self.gone = set(gone)
        self.asked: list[str] = []

    def is_gone(self, hold) -> bool:
        self.asked.append(hold["identifier"])
        return hold["identifier"] in self.gone


class LockTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.lock_dir = isolate_integration_lock_dir(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.clone = self.root / "clone" / ".git"
        self.clone.mkdir(parents=True)
        self.other_clone = self.root / "other" / ".git"
        self.other_clone.mkdir(parents=True)

    def lock(self, repo: str = "owner/repo", *, clone: Path | None = None, **kwargs):
        lock = IntegrationRepoLock(
            repo,
            git_common_dir=clone or self.clone,
            run_id=kwargs.pop("run_id", "run-1"),
            task_key=kwargs.pop("task_key", "AT-1"),
            db_path=self.root / "state.db",
            **kwargs,
        )
        self.addCleanup(lock.release)
        return lock

    def record(self) -> dict:
        return json.loads((self.lock_dir / "owner@repo.json").read_text())


class ExclusionTests(LockTestCase):
    def test_a_second_writer_is_excluded_until_the_first_releases(self) -> None:
        first, second = self.lock(), self.lock(run_id="run-2")
        self.assertTrue(first.acquire().acquired)
        held = second.acquire()
        self.assertFalse(held.acquired)
        self.assertEqual(held.reason, "held")
        self.assertEqual(held.active_holder["run_id"], "run-1")
        self.assertFalse(second.held)
        first.release()
        self.assertTrue(second.acquire().acquired)

    def test_the_key_is_the_normalized_owner_name(self) -> None:
        upper, lower = self.lock("Owner/Repo"), self.lock(" owner/repo ")
        self.assertEqual(upper.key, "owner/repo")
        self.assertEqual(upper.lock_path, self.lock_dir / "owner@repo.lock")
        self.assertEqual(upper.lock_path, lower.lock_path)
        self.assertTrue(upper.acquire().acquired)
        self.assertFalse(lower.acquire().acquired)
        with self.assertRaises(ValueError):
            IntegrationRepoLock("not-a-repo", git_common_dir=self.clone, run_id="r",
                                task_key="AT-1", db_path=self.root / "db")

    def test_different_repositories_do_not_contend(self) -> None:
        self.assertTrue(self.lock("owner/repo").acquire().acquired)
        self.assertTrue(self.lock("owner/other").acquire().acquired)

    def test_a_journal_row_alone_blocks_nothing(self) -> None:
        store = TaskMirrorStore(self.root / "state.db")
        store.init_db()
        integration = IntegrationStore(store=store)
        self.assertTrue(integration.acquire_integration_lock("owner/repo", owner="killed"))
        self.assertTrue(self.lock().acquire().acquired)

    def test_the_journal_row_key_is_normalized_and_old_keys_are_found(self) -> None:
        store = TaskMirrorStore(self.root / "state.db")
        store.init_db()
        integration = IntegrationStore(store=store)
        self.assertTrue(integration.acquire_integration_lock("Owner/Repo", owner="a"))
        self.assertEqual(integration.get_integration_lock("owner/repo")["repo"], "owner/repo")
        self.assertFalse(integration.acquire_integration_lock("owner/repo", owner="b"))
        self.assertTrue(integration.release_integration_lock("OWNER/repo", owner="a"))
        self.assertIsNone(integration.get_integration_lock("owner/repo"))


class HolderRecordTests(LockTestCase):
    def test_the_record_is_written_on_acquire_and_cleared_on_release(self) -> None:
        lock = self.lock(owner="integration_tick")
        self.assertTrue(lock.acquire().acquired)
        record = self.record()
        holder = record["holder"]
        self.assertEqual(record["repo"], "owner/repo")
        self.assertEqual(record["git_common_dir"], str(self.clone.resolve()))
        self.assertEqual(holder["pid"], os.getpid())
        self.assertEqual(holder["boot_id"], repo_lock.read_boot_id())
        self.assertEqual(holder["host"], record["host"])
        self.assertEqual(holder["run_id"], "run-1")
        self.assertEqual(holder["task_key"], "AT-1")
        self.assertEqual(holder["db_path"], str(self.root / "state.db"))
        self.assertEqual(holder["git_common_dir"], str(self.clone.resolve()))
        self.assertEqual(holder["owner"], "integration_tick")
        self.assertEqual(holder["children"], [])
        lock.release()
        released = self.record()
        self.assertIsNone(released["holder"])
        self.assertIsNotNone(released["released_at"])
        # The binding outlives the holder, so a mismatch is still refused.
        self.assertEqual(released["git_common_dir"], str(self.clone.resolve()))
        self.assertTrue(flock_free(lock.lock_path))

    def test_a_git_common_dir_mismatch_is_refused_and_nothing_changes(self) -> None:
        first = self.lock()
        self.assertTrue(first.acquire().acquired)
        first.release()
        before = (self.lock_dir / "owner@repo.json").read_bytes()
        other = self.lock(clone=self.other_clone)
        with self.assertRaises(IntegrationRepoLockError) as refused:
            other.acquire()
        self.assertEqual(refused.exception.code, "git_common_dir_mismatch")
        self.assertFalse(other.held)
        self.assertEqual((self.lock_dir / "owner@repo.json").read_bytes(), before)
        self.assertTrue(flock_free(other.lock_path))

    def test_a_mismatch_is_refused_even_while_the_lock_is_held(self) -> None:
        self.assertTrue(self.lock().acquire().acquired)
        with self.assertRaises(IntegrationRepoLockError) as refused:
            self.lock(clone=self.other_clone).acquire()
        self.assertEqual(refused.exception.code, "git_common_dir_mismatch")

    def test_a_host_mismatch_is_refused(self) -> None:
        first = self.lock()
        self.assertTrue(first.acquire().acquired)
        first.release()
        with mock.patch.object(repo_lock.socket, "gethostname", return_value="another-host"):
            other = self.lock()
        with self.assertRaises(IntegrationRepoLockError) as refused:
            other.acquire()
        self.assertEqual(refused.exception.code, "host_mismatch")

    def test_a_corrupt_record_is_refused_and_left_untouched(self) -> None:
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        path = self.lock_dir / "owner@repo.json"
        valid = self.lock()
        self.assertTrue(valid.acquire().acquired)
        valid.release()
        good = json.loads(path.read_text())
        corruptions = {
            "garbage": b"not json\x00",
            "empty": b"",
            "list": b"[]",
            "schema": json.dumps({**good, "schema_version": "v0"}).encode(),
            "repo": json.dumps({**good, "repo": "owner/other"}).encode(),
            "holder": json.dumps({**good, "holder": {"pid": "one"}}).encode(),
            "holds": json.dumps({**good, "external_holds": [{"kind": "unit"}]}).encode(),
        }
        for name, raw in corruptions.items():
            with self.subTest(corruption=name):
                path.write_bytes(raw)
                lock = self.lock()
                with self.assertRaises(IntegrationRepoLockError) as refused:
                    lock.acquire()
                self.assertEqual(refused.exception.code, "corrupt_holder_record")
                self.assertEqual(path.read_bytes(), raw)
                self.assertFalse(lock.held)
                self.assertTrue(flock_free(lock.lock_path))

    def test_a_symlinked_lock_file_is_refused(self) -> None:
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        target = self.root / "elsewhere"
        target.write_text("")
        (self.lock_dir / "owner@repo.lock").symlink_to(target)
        with self.assertRaises(IntegrationRepoLockError) as refused:
            self.lock().acquire()
        self.assertEqual(refused.exception.code, "lock_file_symlink")

    def test_a_live_holder_recorded_on_a_free_flock_is_refused(self) -> None:
        # What a deleted-and-recreated lock file looks like: the recorded
        # holder is alive, yet the new inode's flock is free.
        held = self.lock()
        self.assertTrue(held.acquire().acquired)
        record = self.record()
        os.close(held._fd)  # drop the flock but keep the live holder recorded
        held._fd, held._record = None, None
        (self.lock_dir / "owner@repo.json").write_text(json.dumps(record))
        with self.assertRaises(IntegrationRepoLockError) as refused:
            self.lock(run_id="run-2").acquire()
        self.assertEqual(refused.exception.code, "live_holder_without_flock")

    def test_a_dead_holders_record_is_taken_over_and_reported(self) -> None:
        dead = self.lock()
        self.assertTrue(dead.acquire().acquired)
        record = self.record()
        record["holder"]["pid"] = 2**22 + 12345  # beyond pid_max: not a live process
        os.close(dead._fd)
        dead._fd, dead._record = None, None
        (self.lock_dir / "owner@repo.json").write_text(json.dumps(record))
        acquisition = self.lock(run_id="run-2").acquire()
        self.assertTrue(acquisition.acquired)
        self.assertEqual(acquisition.previous_holder["run_id"], "run-1")
        self.assertEqual(self.record()["holder"]["run_id"], "run-2")


class LockDirTests(unittest.TestCase):
    def test_explicit_then_setting_then_home_default(self) -> None:
        with mock.patch.dict(os.environ, {LOCK_DIR_ENV: "/from/setting"}):
            self.assertEqual(resolve_lock_dir("/explicit"), Path("/explicit"))
            self.assertEqual(resolve_lock_dir(), Path("/from/setting"))
        env = {key: value for key, value in os.environ.items() if key != LOCK_DIR_ENV}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(Path, "home", return_value=Path("/home/someone")):
            self.assertEqual(
                resolve_lock_dir(), Path("/home/someone/.agent-taskflow/locks/integration")
            )

    def test_the_default_does_not_depend_on_the_code_or_database_location(self) -> None:
        env = {key: value for key, value in os.environ.items() if key != LOCK_DIR_ENV}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(Path, "home", return_value=Path("/home/someone")):
            default = resolve_lock_dir()
        self.assertNotIn(str(REPO_ROOT), str(default))

    def test_a_relative_directory_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            resolve_lock_dir("relative/locks")
        with mock.patch.dict(os.environ, {LOCK_DIR_ENV: "relative"}):
            with self.assertRaises(ValueError):
                resolve_lock_dir()


class ChildTests(LockTestCase):
    def test_a_child_inherits_the_flock_descriptor_and_leads_its_own_group(self) -> None:
        lock = self.lock()
        self.assertTrue(lock.acquire().acquired)
        with lock.inherited_by_children():
            completed = run_integration_child(
                [sys.executable, "-c",
                 f"import os; os.fstat({lock.fd}); print(os.getpgid(0), os.getsid(0), os.getpid())"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        pgid, sid, pid = completed.stdout.split()
        self.assertEqual(pgid, pid)
        self.assertEqual(sid, pid)
        self.assertEqual(self.record()["holder"]["children"], [])

    def test_without_an_active_lock_it_is_plain_subprocess_run(self) -> None:
        lock = self.lock()
        self.assertTrue(lock.acquire().acquired)
        completed = run_integration_child(
            [sys.executable, "-c", f"import os; os.fstat({lock.fd})"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertNotEqual(completed.returncode, 0)

    def test_a_timeout_terminates_the_whole_group(self) -> None:
        lock = self.lock()
        self.assertTrue(lock.acquire().acquired)
        pid_file = self.root / "grandchild.pid"
        script = f"sleep 60 & echo $! > {pid_file}; sleep 60"
        with lock.inherited_by_children():
            with self.assertRaises(subprocess.TimeoutExpired):
                run_integration_child(["/bin/sh", "-c", script], timeout=1,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        grandchild = int(pid_file.read_text())
        wait_for(lambda: not pid_alive(grandchild), timeout=10)
        self.assertEqual(self.record()["holder"]["children"], [])

    def test_a_descendant_left_behind_is_terminated_when_the_leader_exits(self) -> None:
        lock = self.lock()
        self.assertTrue(lock.acquire().acquired)
        with lock.inherited_by_children():
            completed = run_integration_child(
                ["/bin/sh", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
        straggler = int(completed.stdout.strip())
        self.assertFalse(pid_alive(straggler))
        lock.release()
        self.assertTrue(flock_free(lock.lock_path))

    def test_git_runs_without_background_gc_only_under_the_lock(self) -> None:
        seen = []

        def capture(argv, **kwargs):
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        lock = self.lock()
        self.assertTrue(lock.acquire().acquired)
        with mock.patch.object(git_ops, "run_integration_child", side_effect=capture):
            git_ops.run_git(self.root, ["status"])
            with lock.inherited_by_children():
                git_ops.run_git(self.root, ["status"])
        self.assertEqual(seen[0], ["git", "status"])
        self.assertEqual(seen[1], ["git", *git_ops.GIT_NO_BACKGROUND_WORK, "status"])
        self.assertIn("gc.auto=0", seen[1])
        self.assertIn("gc.autoDetach=false", seen[1])

    def test_popen_options_the_lock_owns_cannot_be_overridden(self) -> None:
        lock = self.lock()
        self.assertTrue(lock.acquire().acquired)
        with self.assertRaises(ValueError):
            lock.run_child(["true"], start_new_session=False)


# A holder in its own process, so it can be SIGKILLed: it takes the lock and
# runs one managed child that sleeps, printing the child's pid when started.
HOLDER = r"""
import os, subprocess, sys
from pathlib import Path
from agent_taskflow.integration_repo_lock import IntegrationRepoLock
lock = IntegrationRepoLock("owner/repo", git_common_dir=sys.argv[1], run_id="dead-run",
                           task_key="AT-1", db_path=sys.argv[2])
assert lock.acquire().acquired
with lock.inherited_by_children():
    lock.run_child([sys.executable, "-c",
                    "import pathlib, sys, time; pathlib.Path(sys.argv[1]).touch(); time.sleep(120)",
                    sys.argv[3]])
"""


class CrashedHolderTests(LockTestCase):
    def start_holder(self) -> tuple[subprocess.Popen, dict]:
        marker = self.root / "child-started"
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(self.clone), str(self.root / "state.db"),
             str(marker)],
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        def reap() -> None:
            if holder.poll() is None:
                holder.kill()
            holder.communicate(timeout=60)

        self.addCleanup(reap)
        wait_for(marker.exists)
        child, = self.record()["holder"]["children"]
        self.addCleanup(lambda: _killpg(child["pgid"]))
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(timeout=60)
        return holder, child

    def test_an_orphaned_child_keeps_the_lock_until_it_is_dead(self) -> None:
        holder, child = self.start_holder()
        self.assertTrue(pid_alive(child["pid"]))
        waiting = self.lock(run_id="run-2", terminate_orphaned_children=False)
        refused = waiting.acquire()
        self.assertFalse(refused.acquired)
        self.assertEqual(refused.reason, "held")
        self.assertEqual(refused.active_holder["pid"], holder.pid)
        self.assertTrue(pid_alive(child["pid"]))
        _killpg(child["pgid"])
        wait_for(lambda: not pid_alive(child["pid"]))
        taken = waiting.acquire()
        self.assertTrue(taken.acquired)
        self.assertEqual(taken.previous_holder["run_id"], "dead-run")
        self.assertEqual(taken.terminated_children, ())

    def test_takeover_terminates_the_orphaned_group_before_taking_the_lock(self) -> None:
        holder, child = self.start_holder()
        taken = self.lock(run_id="run-2").acquire()
        self.assertTrue(taken.acquired)
        self.assertFalse(pid_alive(child["pid"]))
        self.assertEqual([c["pid"] for c in taken.terminated_children], [child["pid"]])
        self.assertEqual(taken.previous_holder["pid"], holder.pid)

    def test_a_live_holders_children_are_never_touched(self) -> None:
        live = self.lock()
        self.assertTrue(live.acquire().acquired)
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True, pass_fds=(live.fd,),
        )
        self.addCleanup(lambda: (child.kill(), child.wait()))
        live._set_children([{"pid": child.pid, "pgid": child.pid, "session_id": child.pid,
                             "start_ticks": None, "boot_id": repo_lock.read_boot_id()}])
        self.assertFalse(self.lock(run_id="run-2").acquire().acquired)
        self.assertIsNone(child.poll())


class ExternalHoldTests(LockTestCase):
    def crashed_with_holds(self, *identifiers: str) -> None:
        dead = self.lock()
        self.assertTrue(dead.acquire().acquired)
        for identifier in identifiers:
            dead.record_external_hold("systemd_unit", identifier, cgroup=f"/x/{identifier}")
        # A crash: the flock goes with the process, the record stays.
        os.close(dead._fd)
        dead._fd, dead._record = None, None
        record = self.record()
        record["holder"]["pid"] = 2**22 + 12345
        (self.lock_dir / "owner@repo.json").write_text(json.dumps(record))

    def test_holds_are_recorded_in_the_holder_record(self) -> None:
        lock = self.lock()
        self.assertTrue(lock.acquire().acquired)
        hold = lock.record_external_hold("systemd_unit", "taskflow-v-1.service", cgroup="/c")
        self.assertEqual(self.record()["external_holds"], [hold])
        self.assertEqual((hold["kind"], hold["identifier"], hold["cgroup"]),
                         ("systemd_unit", "taskflow-v-1.service", "/c"))

    def test_the_default_probe_fails_closed(self) -> None:
        self.crashed_with_holds("unit-a")
        blocked = self.lock(run_id="run-2").acquire()
        self.assertFalse(blocked.acquired)
        self.assertEqual(blocked.reason, "external_hold_active")
        self.assertEqual([h["identifier"] for h in blocked.external_holds], ["unit-a"])
        self.assertFalse(FailClosedExternalHoldProbe().is_gone({"kind": "x", "identifier": "y"}))

    def test_takeover_waits_until_every_hold_is_proven_gone(self) -> None:
        self.crashed_with_holds("unit-a", "unit-b")
        probe = RecordingProbe(gone={"unit-a"})
        blocked = self.lock(run_id="run-2", external_hold_probe=probe).acquire()
        self.assertFalse(blocked.acquired)
        self.assertEqual([h["identifier"] for h in blocked.external_holds], ["unit-b"])
        self.assertEqual(sorted(probe.asked), ["unit-a", "unit-b"])
        self.assertEqual(len(self.record()["external_holds"]), 2)
        probe.gone.add("unit-b")
        taken = self.lock(run_id="run-3", external_hold_probe=probe).acquire()
        self.assertTrue(taken.acquired)
        self.assertEqual(self.record()["external_holds"], [])
        self.assertEqual(taken.previous_holder["run_id"], "run-1")

    def test_a_clean_release_keeps_unproven_holds_for_the_successor(self) -> None:
        probe = RecordingProbe(gone=set())
        lock = self.lock(external_hold_probe=probe)
        self.assertTrue(lock.acquire().acquired)
        lock.record_external_hold("cgroup", "/sys/fs/cgroup/x")
        self.assertFalse(lock.clear_external_hold("cgroup", "/sys/fs/cgroup/x"))
        lock.release()
        self.assertEqual(len(self.record()["external_holds"]), 1)
        self.assertFalse(self.lock(run_id="run-2").acquire().acquired)
        probe.gone.add("/sys/fs/cgroup/x")
        succ = self.lock(run_id="run-3", external_hold_probe=probe)
        self.assertTrue(succ.acquire().acquired)

    def test_a_hold_proven_gone_is_cleared_by_its_holder(self) -> None:
        probe = RecordingProbe(gone={"unit-a"})
        lock = self.lock(external_hold_probe=probe)
        self.assertTrue(lock.acquire().acquired)
        lock.record_external_hold("systemd_unit", "unit-a")
        self.assertTrue(lock.clear_external_hold("systemd_unit", "unit-a"))
        self.assertEqual(self.record()["external_holds"], [])

    def test_holds_can_only_be_recorded_while_holding(self) -> None:
        with self.assertRaises(RuntimeError):
            self.lock().record_external_hold("systemd_unit", "unit-a")


def _killpg(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


if __name__ == "__main__":
    unittest.main()
