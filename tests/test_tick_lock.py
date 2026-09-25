"""V1-F10: per-cron-entry non-overlap locks (SPEC §47.3) and the cron examples.

Contention is exercised with real subprocesses: a holder process runs a tick
script's own ``main()`` (so its real lock code path takes the lock) with only
the tick body stubbed to wait, and the contender is the unmodified script.
"""

from __future__ import annotations

from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5_support import REPO_ROOT, make_fixture, worker_env  # noqa: E402

from agent_taskflow.tick_lock import (  # noqa: E402
    DATABASE_SIDE_FILES,
    EXIT_SKIPPED_OVERLAP,
    SKIPPED_OVERLAP,
    TickLock,
    TickLockPathError,
    execution_tick_lock_path,
    integration_tick_lock_path,
    skipped_overlap_result,
)

EXECUTION_TICK = REPO_ROOT / "scripts/run_parallel_scheduler_tick.py"
INTEGRATION_TICK = REPO_ROOT / "scripts/run_integration_tick.py"
CRON_DIR = REPO_ROOT / "deploy/cron"

# Runs a tick script's real main() in its own process, with the tick body
# replaced by a wait on a release file, so the real lock is held meanwhile.
HOLDER = r"""
import importlib.util, sys, time
from pathlib import Path
script, attribute, release = sys.argv[1], sys.argv[2], Path(sys.argv[3])
spec = importlib.util.spec_from_file_location("held_tick", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class Stub(dict):
    def to_dict(self):
        return {"stub": True}

def wait(*args, **kwargs):
    deadline = time.monotonic() + 300
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return Stub(ok=True)

setattr(module, attribute, wait)
raise SystemExit(module.main(sys.argv[4:]))
"""


def wait_for(condition, *, timeout: float = 60.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


def recorded_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
        return json.loads(text).get("pid") if text else None
    except (OSError, ValueError):
        return None


def db_digest(db_path: Path) -> tuple[str, list[str]]:
    """Checkpoint, then hash the file and dump the rows (WAL-safe)."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dump = list(conn.iterdump())
    return hashlib.sha256(db_path.read_bytes()).hexdigest(), dump


def database_files(db_path: Path) -> list[Path]:
    """The database and the SQLite files beside it."""
    return [Path(f"{db_path}{suffix}") for suffix in DATABASE_SIDE_FILES]


def file_states(paths) -> dict[Path, str | None]:
    """Each file's sha256, or None if absent; read without opening SQLite."""
    return {path: hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            for path in paths}


class TickLockUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "state.db"
        self.db.touch()

    def test_paths_are_keyed_by_resolved_database_and_normalized_repository(self) -> None:
        link_dir = self.root / "link"
        link_dir.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(execution_tick_lock_path(self.db), self.root / "state.db.execution-tick.lock")
        self.assertEqual(execution_tick_lock_path(link_dir / "state.db"), execution_tick_lock_path(self.db))
        path = integration_tick_lock_path(self.db, "Owner/Repo.Name")
        self.assertEqual(path, self.root / "state.db.integration-tick.owner@repo.name.lock")
        self.assertEqual(integration_tick_lock_path(link_dir / "state.db", " owner/repo.name "), path)
        self.assertNotEqual(integration_tick_lock_path(self.db, "owner/other"), path)
        self.assertNotEqual(integration_tick_lock_path(self.root / "other.db", "owner/repo.name"), path)
        for bad in (lambda: execution_tick_lock_path(Path("state.db")),
                    lambda: integration_tick_lock_path(Path("state.db"), "owner/repo"),
                    lambda: integration_tick_lock_path(self.db, "not-a-repo"),
                    lambda: TickLock(Path("relative.lock"), holder={}, db_path=self.db),
                    lambda: TickLock(self.root / "tick.lock", holder={}, db_path=Path("state.db"))):
            with self.assertRaises(ValueError):
                bad()

    def test_second_holder_is_refused_and_release_clears_the_record(self) -> None:
        path = integration_tick_lock_path(self.db, "owner/repo")
        identity = {"kind": "integration_tick", "repo": "owner/repo"}
        first = TickLock(path, holder=identity, db_path=self.db)
        second = TickLock(path, holder=identity, db_path=self.db)
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        holder = second.recorded_holder()
        self.assertEqual(holder["pid"], os.getpid())
        self.assertEqual(holder["kind"], "integration_tick")
        self.assertIn("acquired_at", holder)
        skipped = skipped_overlap_result("integration_tick", second, repo="owner/repo")
        self.assertEqual(skipped["status"], SKIPPED_OVERLAP)
        self.assertEqual(skipped["exit_code"], EXIT_SKIPPED_OVERLAP)
        self.assertFalse(skipped["ok"])
        self.assertFalse(skipped["work_performed"])
        self.assertEqual(skipped["lock_path"], str(path))
        self.assertEqual(skipped["holder"]["pid"], os.getpid())
        first.release()
        self.assertIsNone(second.recorded_holder())
        self.assertTrue(second.acquire())
        second.release()

    def test_lock_never_creates_directories(self) -> None:
        lock = TickLock(self.root / "missing" / "tick.lock", holder={}, db_path=self.db)
        with self.assertRaises(FileNotFoundError):
            lock.acquire()
        self.assertFalse((self.root / "missing").exists())

    def test_exit_code_is_distinct_from_every_other_tick_exit(self) -> None:
        self.assertNotIn(EXIT_SKIPPED_OVERLAP, (0, 1, 2))

    def test_the_database_and_its_sqlite_files_are_never_a_lock_path(self) -> None:
        """Review N2: `--lock-path <db>` used to truncate the database."""
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE kept (value TEXT)")
            conn.execute("INSERT INTO kept VALUES ('operator data')")
            conn.commit()
        side = database_files(self.db)
        link_dir = self.root / "link"
        link_dir.symlink_to(self.root, target_is_directory=True)
        alias = self.root / "alias.db"
        alias.symlink_to(self.db)
        before = file_states(side)
        for lock_path, db_path in (
            *((path, self.db) for path in side),
            (link_dir / "state.db-wal", self.db),  # through a symlinked directory
            (alias, self.db),  # a symlink to the database
            (self.db, alias),  # the database named through a symlink
            (self.root / "alias.db-journal", alias),  # a side file of the name as given
        ):
            with self.subTest(lock_path=str(lock_path), db_path=str(db_path)):
                with self.assertRaises(TickLockPathError):
                    TickLock(lock_path, holder={}, db_path=db_path)
        self.assertEqual(file_states(side), before)
        self.assertFalse((self.root / "alias.db-journal").exists())
        # A hard link is a database file under another name: refused before any
        # write, even when the file is empty and so looks like an unused lock.
        Path(f"{self.db}-wal").touch()
        before = file_states(side)
        for target in (self.db, Path(f"{self.db}-wal")):
            with self.subTest(hard_link_to=target.name):
                hard = self.root / f"hard-{target.name}.lock"
                os.link(target, hard)
                lock = TickLock(hard, holder={}, db_path=self.db)
                with self.assertRaises(TickLockPathError):
                    lock.acquire()
                self.assertFalse(lock.held)
        self.assertEqual(file_states(side), before)
        self.assertEqual(Path(f"{self.db}-wal").read_bytes(), b"")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT value FROM kept").fetchall(), [("operator data",)])

    def test_a_file_that_is_not_a_holder_record_is_never_overwritten(self) -> None:
        for name, content in (
            ("notes.txt", b"operator notes, not a lock\n"),
            ("binary.bin", bytes(range(256))),
            ("other.json", b'{"not": "a holder record"}\n'),
            ("long.json", b'{"pid": 1, "acquired_at": "x", "pad": "' + b"x" * 5000 + b'"}\n'),
        ):
            with self.subTest(name=name):
                path = self.root / name
                path.write_bytes(content)
                lock = TickLock(path, holder={"kind": "test"}, db_path=self.db)
                with self.assertRaises(TickLockPathError):
                    lock.acquire()
                self.assertFalse(lock.held)
                self.assertEqual(path.read_bytes(), content)
                # The refused attempt kept no flock.
                with path.open("rb") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Reporting such a file as a holder never raises.
                self.assertIsInstance(lock.recorded_holder(), dict)
        target = self.root / "real.lock"
        target.write_bytes(b"")
        link = self.root / "link.lock"
        link.symlink_to(target)
        with self.assertRaises(TickLockPathError):
            TickLock(link, holder={}, db_path=self.db).acquire()
        self.assertEqual(target.read_bytes(), b"")

    def test_an_empty_file_or_a_record_left_by_a_killed_holder_is_reused(self) -> None:
        stale = json.dumps({"kind": "integration_tick", "pid": 4194305,
                            "acquired_at": "2026-09-25T00:00:00+00:00"}) + "\n"
        for content in ("", "\n", stale):
            with self.subTest(content=content):
                path = self.root / "tick.lock"
                path.write_text(content, encoding="utf-8")
                lock = TickLock(path, holder={"kind": "integration_tick"}, db_path=self.db)
                self.assertTrue(lock.acquire())
                self.assertEqual(lock.recorded_holder()["pid"], os.getpid())
                lock.release()
                self.assertEqual(path.read_text(encoding="utf-8"), "")


class ExecutionTickOverlapTests(unittest.TestCase):
    """Two concurrent execution-tick invocations against one database."""

    def setUp(self) -> None:
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        self.lock_path = execution_tick_lock_path(self.fx.db_path)
        self.release = self.fx.root / "release"

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(EXECUTION_TICK), *args], cwd=self.fx.root,
            env=worker_env(), capture_output=True, text=True, timeout=300, check=False,
        )

    def start_holder(self) -> subprocess.Popen:
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(EXECUTION_TICK), "run_scheduler_tick",
             str(self.release), "--db-path", str(self.fx.db_path)],
            cwd=self.fx.root, env=worker_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        def reap() -> None:
            if holder.poll() is None:
                holder.kill()
            holder.communicate(timeout=60)

        self.addCleanup(reap)
        wait_for(lambda: recorded_pid(self.lock_path) == holder.pid)
        return holder

    def test_contended_invocation_skips_at_once_and_does_no_work(self) -> None:
        ticket = self.fx.create_ticket("eligible").task_key
        holder = self.start_holder()
        before = db_digest(self.fx.db_path)
        started = time.monotonic()
        completed = self.run_cli("--db-path", str(self.fx.db_path))
        elapsed = time.monotonic() - started
        self.assertEqual(completed.returncode, EXIT_SKIPPED_OVERLAP, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], SKIPPED_OVERLAP)
        self.assertEqual(payload["kind"], "parallel_scheduler_tick")
        self.assertEqual(payload["lock_path"], str(self.lock_path))
        self.assertEqual(payload["holder"]["pid"], holder.pid)
        self.assertEqual(payload["db_path"], str(self.fx.db_path))
        self.assertFalse(payload["work_performed"])
        self.assertNotIn("started", payload)
        # No reap, no claim, no worker: the eligible Ticket is untouched.
        self.assertEqual(db_digest(self.fx.db_path), before)
        self.assertEqual(self.fx.status(ticket), "created")
        self.assertLess(elapsed, 60)
        self.assertIsNone(holder.poll())

        # The holder releases the lock on a normal return, too.
        self.fx.set_status(ticket, "cancelled")
        self.release.touch()
        self.assertEqual(holder.wait(timeout=60), 0)
        self.assertEqual(self.lock_path.read_text(encoding="utf-8"), "")
        after = self.run_cli("--db-path", str(self.fx.db_path), "--jsonl")
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertEqual(len(after.stdout.splitlines()), 1)
        self.assertEqual(json.loads(after.stdout)["started"], [])

    def test_sigkill_of_the_holder_releases_the_lock(self) -> None:
        holder = self.start_holder()
        self.assertEqual(self.run_cli("--db-path", str(self.fx.db_path)).returncode, EXIT_SKIPPED_OVERLAP)
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=60)
        completed = self.run_cli("--db-path", str(self.fx.db_path))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["started"], [])

    def test_flock_command_holder_contends_with_the_tick(self) -> None:
        """A hand-held flock(1) and the tick's own lock are the same lock."""
        self.lock_path.touch()
        # flock(1) hands its descriptor to `sleep`, so the whole group holds it.
        holder = subprocess.Popen(["flock", str(self.lock_path), "sleep", "120"],
                                  start_new_session=True)
        self.addCleanup(lambda: (os.killpg(holder.pid, signal.SIGKILL), holder.wait(timeout=60)))
        wait_for(lambda: self._flock_held())
        completed = self.run_cli("--db-path", str(self.fx.db_path), "--jsonl")
        self.assertEqual(completed.returncode, EXIT_SKIPPED_OVERLAP, completed.stderr)
        self.assertEqual(len(completed.stdout.splitlines()), 1)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], SKIPPED_OVERLAP)
        self.assertIsNone(payload["holder"])

    def _flock_held(self) -> bool:
        probe = TickLock(self.lock_path, holder={}, db_path=self.fx.db_path)
        if probe.acquire():
            probe.release()
            return False
        return True

    def test_explicit_lock_path_is_honoured_and_must_be_absolute(self) -> None:
        custom = self.fx.root / "custom.lock"
        holder = TickLock(custom, holder={"kind": "test"}, db_path=self.fx.db_path)
        self.assertTrue(holder.acquire())
        self.addCleanup(holder.release)
        skipped = self.run_cli("--db-path", str(self.fx.db_path), "--lock-path", str(custom))
        self.assertEqual(skipped.returncode, EXIT_SKIPPED_OVERLAP)
        # The default lock is a different file, so this invocation runs.
        self.assertEqual(self.run_cli("--db-path", str(self.fx.db_path)).returncode, 0)
        relative = self.run_cli("--db-path", str(self.fx.db_path), "--lock-path", "relative.lock")
        self.assertEqual(relative.returncode, 2)
        self.assertIn("--lock-path", relative.stderr)

    def test_missing_database_fails_closed_without_leaving_a_lock_file(self) -> None:
        missing = self.fx.root / "missing.db"
        completed = self.run_cli("--db-path", str(missing))
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(missing.exists())
        self.assertFalse(execution_tick_lock_path(missing).exists())

    def assert_error_line(self, completed: subprocess.CompletedProcess, error: str) -> None:
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(len(completed.stdout.splitlines()), 1, completed.stdout)
        payload = json.loads(completed.stdout)
        self.assertEqual((payload["kind"], payload["ok"], payload["status"]),
                         ("parallel_scheduler_tick", False, "error"))
        self.assertIn(error, payload["reason"])
        self.assertNotIn("Traceback", completed.stderr)

    def test_a_lock_path_naming_the_database_or_other_data_is_refused(self) -> None:
        """Review N2 through the script: nothing is truncated and no work is done."""
        ticket = self.fx.create_ticket("eligible").task_key
        notes = self.fx.root / "notes.txt"
        notes.write_text("operator notes\n", encoding="utf-8")
        watched = [*database_files(self.fx.db_path), notes]
        before = file_states(watched)
        for lock_path in watched:
            with self.subTest(lock_path=str(lock_path)):
                completed = self.run_cli("--db-path", str(self.fx.db_path),
                                         "--lock-path", str(lock_path), "--jsonl")
                self.assert_error_line(completed, "TickLockPathError")
        self.assertEqual(file_states(watched), before)
        self.assertEqual(self.fx.status(ticket), "created")

    def test_a_lock_that_cannot_be_opened_is_one_error_json_line(self) -> None:
        """Review N8: exit 2 with a logged error, never a traceback and exit 1."""
        missing = self.fx.root / "no-such-directory"
        for flags in ((), ("--jsonl",)):
            with self.subTest(flags=flags):
                completed = self.run_cli("--db-path", str(self.fx.db_path),
                                         "--lock-path", str(missing / "tick.lock"), *flags)
                self.assert_error_line(completed, "FileNotFoundError")
        self.assertFalse(missing.exists())

    @unittest.skipIf(os.geteuid() == 0, "root can write to a read-only directory")
    def test_an_unwritable_database_directory_is_one_error_json_line(self) -> None:
        sealed = self.fx.root / "sealed"
        sealed.mkdir()
        db = sealed / "state.db"
        with closing(sqlite3.connect(self.fx.db_path)) as source, closing(sqlite3.connect(db)) as copy:
            source.backup(copy)
        sealed.chmod(0o555)
        self.addCleanup(sealed.chmod, 0o755)
        before = file_states(database_files(db))
        self.assert_error_line(self.run_cli("--db-path", str(db)), "PermissionError")
        self.assertEqual(file_states(database_files(db)), before)
        self.assertFalse(execution_tick_lock_path(db).exists())


class CronExampleTests(unittest.TestCase):
    """deploy/cron/*.cron.example: shape, safety comments, and a real run."""

    PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")

    def entries(self, name: str) -> tuple[str, list[str]]:
        text = (CRON_DIR / name).read_text(encoding="utf-8")
        lines = [line for line in text.splitlines()
                 if line.strip() and not line.lstrip().startswith("#") and "=" not in line.split()[0]]
        return text, lines

    def command(self, line: str, values: dict[str, str]) -> str:
        command = line.split(None, 5)[5]
        return self.PLACEHOLDER.sub(lambda match: values[match.group(1)], command)

    def test_examples_are_uninstalled_examples_with_one_line_each(self) -> None:
        for name, script, schedule in (
            ("v1-execution-tick.cron.example", "run_parallel_scheduler_tick.py", "*/5 * * * *"),
            ("v1-integration-tick.cron.example", "run_integration_tick.py", "2-59/5 * * * *"),
        ):
            with self.subTest(name=name):
                text, lines = self.entries(name)
                self.assertTrue(text.startswith(
                    "# Example only. Copy and adapt outside the repository before installing."))
                prose = " ".join(text.replace("#", " ").split())
                self.assertIn("human operator action (SPEC §47.4)", prose)
                self.assertIn("skipped_overlap", prose)
                self.assertIn("exits 75", prose)
                self.assertIn("Do NOT also wrap this line in flock(1) on that same file", prose)
                self.assertEqual(len(lines), 1, lines)
                line = lines[0]
                self.assertTrue(line.startswith(schedule + " "), line)
                self.assertIn(f"/scripts/{script}", line)
                self.assertIn('--db-path "{{DB}}"', line)
                self.assertIn("--jsonl >> ", line)
                self.assertIn(" 2>> ", line)
                self.assertNotIn("flock", line)
                self.assertNotIn("%", line)
                self.assertNotIn(".agent-taskflow", line)

    def test_integration_confirmations_are_explicit_and_marked_human_choice(self) -> None:
        text, (line,) = self.entries("v1-integration-tick.cron.example")
        self.assertIn("HUMAN CHOICE (human decision H4)", text)
        for flag in ("--confirm-pr-poll", "--confirm-cleanup", "--confirm-freshness",
                     "--confirm-integration"):
            self.assertIn(flag, line)
            self.assertIn(f"#   {flag}", text)
        self.assertNotIn("cancelled", line)
        self.assertNotIn("delete", line)

    def test_example_lines_run_verbatim_and_append_one_jsonl_line_per_run(self) -> None:
        fx = make_fixture()
        self.addCleanup(fx.cleanup)
        logs = fx.root / "logs"
        logs.mkdir()
        validators = fx.root / "validators.json"
        validators.write_text(json.dumps([{"name": "noop", "command": ["true"]}]))
        values = {"PYTHON": sys.executable, "TASKFLOW_HOME": str(REPO_ROOT), "DB": str(fx.db_path),
                  "LOG_DIR": str(logs), "REPO": "owner/repo", "REPO_PATH": str(fx.repo),
                  "VALIDATOR_CONFIG": str(validators), "REPO_LOG_KEY": "owner@repo"}
        env = {**worker_env(), "PATH": "/usr/local/bin:/usr/bin:/bin"}
        for name, log in (("v1-execution-tick.cron.example", "execution-tick.jsonl"),
                          ("v1-integration-tick.cron.example", "integration-tick.owner@repo.jsonl")):
            with self.subTest(name=name):
                _text, (line,) = self.entries(name)
                command = self.command(line, values)
                self.assertNotIn("{{", command)
                for _ in range(2):
                    completed = subprocess.run(["/bin/sh", "-c", command], cwd=fx.root, env=env,
                                               capture_output=True, text=True, timeout=300)
                    self.assertEqual(completed.returncode, 0, (logs / log).read_text())
                records = [json.loads(item) for item in (logs / log).read_text().splitlines()]
                self.assertEqual(len(records), 2)
                self.assertTrue(all("status" not in r or r["status"] != SKIPPED_OVERLAP for r in records))
        integration = [json.loads(item) for item in
                       (logs / "integration-tick.owner@repo.jsonl").read_text().splitlines()]
        # Nothing to do in this fixture; every phase ran, confirmed, and wrote nothing.
        self.assertTrue(all(record["tick_status"] == "ok" for record in integration))
        self.assertTrue(all(record["confirmations"] == {
            "pr_outcomes": True, "verified_merge_cleanup": True,
            "target_freshness": True, "queue_drain": True} for record in integration))


if __name__ == "__main__":
    unittest.main()
