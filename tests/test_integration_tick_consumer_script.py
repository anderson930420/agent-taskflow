"""V1-F10: the integration tick CLI (flags, exit codes, JSON, non-overlap lock).

Real-subprocess runs put a ``gh`` stub first on PATH that always fails, so no
test can reach real GitHub even if a phase unexpectedly polled a PR.
"""

from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_integration_consumer_tick import VALIDATORS, ConsumerFixture
from test_tick_lock import (HOLDER, database_files, db_digest, file_states, recorded_pid,
                            wait_for)

from agent_taskflow import integration_schema as schema
from agent_taskflow.integration_validators import IntegrationValidatorSpec
from agent_taskflow.tick_lock import EXIT_SKIPPED_OVERLAP, TickLock, integration_tick_lock_path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_integration_tick.py"
spec = importlib.util.spec_from_file_location("f10_tick_cli", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def _pid_alive(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in {"Z", "X", "x"}


def _kill_group(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _flock_free(path):
    import fcntl

    descriptor = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    finally:
        os.close(descriptor)
    return True


class ScriptFixture(ConsumerFixture):
    def setUp(self) -> None:
        super().setUp()
        self.config = self.root / "validators.json"
        self.write_config(VALIDATORS)
        stub = self.root / "bin"
        stub.mkdir()
        (stub / "gh").write_text("#!/bin/sh\necho 'gh must not be called by this test' >&2\nexit 97\n")
        (stub / "gh").chmod(0o755)
        self.env = {**os.environ, "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}",
                    "PYTHONPATH": str(SCRIPT.parents[1])}
        self.lock_path = integration_tick_lock_path(self.db_path, "owner/repo")

    def write_config(self, specs):
        self.config.write_text(json.dumps([
            {"name": s.name, "command": list(s.command), "timeout_seconds": s.timeout_seconds}
            for s in specs
        ]))

    def arguments(self, repo="owner/repo"):
        return ["--db-path", str(self.db_path), "--repo", repo,
                "--repo-path", str(self.fixture.repo), "--validator-config", str(self.config)]

    def run_script(self, *extra, repo="owner/repo", **kwargs):
        return subprocess.run([sys.executable, str(SCRIPT), *self.arguments(repo), *extra],
                              capture_output=True, text=True, env=self.env, timeout=300,
                              check=False, **kwargs)

    def main(self, *extra, result=None):
        with patch.object(cli, "run_integration_tick", return_value=result or {"ok": True}) as tick, \
                redirect_stdout(io.StringIO()) as output:
            code = cli.main([*self.arguments(), *extra])
        return code, tick, output.getvalue()


class ConsumerScriptTests(ScriptFixture):
    def test_default_preview_runs_every_phase_and_writes_nothing(self):
        queued = self.ticket()
        digest = db_digest(self.db_path)
        completed = self.run_script()
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        result = json.loads(completed.stdout)
        self.assertEqual(result["phase_order"],
                         ["pr_outcomes", "verified_merge_cleanup", "target_freshness", "queue_drain"])
        self.assertTrue(all(phase["ran"] and not phase["confirmed"]
                            for phase in result["phases"].values()))
        self.assertEqual((result["tick_status"], result["status"], result["dry_run"]),
                         ("ok", "dry_run", True))
        self.assertEqual([o["task_key"] for o in result["outcomes"]], [queued.task_key])
        self.assertEqual(db_digest(self.db_path), digest)
        # The run released its lock and cleared its holder record.
        self.assertEqual(self.lock_path.read_text(encoding="utf-8"), "")

    def test_each_confirmation_flag_maps_to_its_own_phase(self):
        for flags, expected in (
            ((), dict(confirm_pr_poll=False, confirm_cleanup=False, confirm_freshness=False,
                      confirm_integration=False, dry_run=True)),
            (("--confirm-pr-poll",), dict(confirm_pr_poll=True, confirm_cleanup=False,
                                          confirm_freshness=False, confirm_integration=False)),
            (("--confirm-cleanup",), dict(confirm_cleanup=True, confirm_pr_poll=False)),
            (("--confirm-freshness",), dict(confirm_freshness=True, confirm_cleanup=False)),
            (("--confirm-integration",), dict(confirm_integration=True, dry_run=False,
                                              confirm_pr_poll=False)),
            (("--confirm-pr-poll", "--confirm-cleanup", "--confirm-freshness",
              "--confirm-integration"),
             dict(confirm_pr_poll=True, confirm_cleanup=True, confirm_freshness=True,
                  confirm_integration=True, dry_run=False)),
        ):
            with self.subTest(flags=flags):
                code, tick, _output = self.main(*flags)
                self.assertEqual(code, 0)
                request = tick.call_args.args[0]
                self.assertTrue(request.consumer_phases)
                for name, value in expected.items():
                    self.assertEqual(getattr(request, name), value, name)

    def test_dry_run_cannot_be_combined_with_a_phase_confirmation(self):
        for flag in ("--confirm-pr-poll", "--confirm-cleanup", "--confirm-freshness",
                     "--confirm-integration"):
            with self.subTest(flag=flag), patch.object(cli, "run_integration_tick") as tick, \
                    redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cli.main([*self.arguments(), "--dry-run", flag])
                self.assertEqual(raised.exception.code, 2)
                tick.assert_not_called()

    def test_exit_codes_distinguish_ok_not_ok_error_and_skipped_overlap(self):
        for result, code in (({"ok": True, "tick_status": "ok"}, 0),
                             ({"ok": False, "tick_status": "not_ok"}, 1),
                             ({"ok": False, "tick_status": "error"}, 2)):
            with self.subTest(result=result):
                self.assertEqual(self.main(result=result)[0], code)
        with patch.object(cli, "run_integration_tick", side_effect=RuntimeError("boom")), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(self.arguments()), 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "error")
        holder = TickLock(self.lock_path, holder={"kind": "integration_tick"}, db_path=self.db_path)
        self.assertTrue(holder.acquire())
        try:
            code, tick, output = self.main("--jsonl")
        finally:
            holder.release()
        self.assertEqual(code, EXIT_SKIPPED_OVERLAP)
        tick.assert_not_called()
        self.assertEqual(json.loads(output)["status"], "skipped_overlap")

    def test_not_ok_real_run_exits_one_and_jsonl_is_one_line(self):
        orphan = self.ticket(enqueue=False)
        completed = self.run_script("--jsonl")
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(len(completed.stdout.splitlines()), 1)
        result = json.loads(completed.stdout)
        self.assertEqual(result["tick_status"], "not_ok")
        self.assertEqual([o["task_key"] for o in result["ready_for_integration_unqueued"]],
                         [orphan.task_key])

    def test_missing_database_is_an_error_and_leaves_no_lock_file(self):
        missing = self.root / "missing.db"
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--db-path", str(missing), "--repo", "owner/repo",
             "--repo-path", str(self.fixture.repo), "--validator-config", str(self.config)],
            capture_output=True, text=True, env=self.env, timeout=300, check=False)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(json.loads(completed.stdout)["status"], "error")
        self.assertFalse(missing.exists())
        self.assertFalse(integration_tick_lock_path(missing, "owner/repo").exists())

    def test_a_lock_path_naming_the_database_or_other_data_is_refused(self):
        """Review N2 through the script: nothing is truncated and no work is done."""
        queued = self.ticket()
        notes = self.root / "notes.txt"
        notes.write_text("operator notes\n", encoding="utf-8")
        watched = [*database_files(self.db_path), notes]
        before = file_states(watched)
        for lock_path, error in (*((path, "TickLockPathError") for path in watched),
                                 (self.root / "no-such-directory" / "tick.lock", "FileNotFoundError")):
            with self.subTest(lock_path=str(lock_path)):
                completed = self.run_script("--confirm-integration", "--lock-path", str(lock_path),
                                            "--jsonl")
                self.assertEqual(completed.returncode, 2, completed.stderr + completed.stdout)
                self.assertEqual(len(completed.stdout.splitlines()), 1)
                payload = json.loads(completed.stdout)
                self.assertEqual((payload["kind"], payload["status"]), ("integration_tick", "error"))
                self.assertIn(error, payload["reason"])
        self.assertEqual(file_states(watched), before)
        self.assertFalse((self.root / "no-such-directory").exists())
        self.assertEqual(self.status(queued), schema.READY_FOR_INTEGRATION)
        self.assertTrue(self.integration.is_queued(queued.task_key))


class IntegrationTickOverlapTests(ScriptFixture):
    def start_holder(self, release: Path) -> subprocess.Popen:
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(SCRIPT), "run_integration_tick", str(release),
             *self.arguments(), "--confirm-integration"],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        def reap():
            if holder.poll() is None:
                holder.kill()
            holder.communicate(timeout=60)

        self.addCleanup(reap)
        wait_for(lambda: recorded_pid(self.lock_path) == holder.pid)
        return holder

    def test_second_invocation_skips_at_once_other_repositories_still_run(self):
        queued = self.ticket()
        holder = self.start_holder(self.root / "release")
        digest = db_digest(self.db_path)
        started = time.monotonic()
        skipped = self.run_script("--confirm-integration", "--jsonl")
        self.assertLess(time.monotonic() - started, 60)
        self.assertEqual(skipped.returncode, EXIT_SKIPPED_OVERLAP, skipped.stderr)
        self.assertEqual(len(skipped.stdout.splitlines()), 1)
        payload = json.loads(skipped.stdout)
        self.assertEqual((payload["status"], payload["kind"], payload["repo"]),
                         ("skipped_overlap", "integration_tick", "owner/repo"))
        self.assertEqual(payload["holder"]["pid"], holder.pid)
        self.assertEqual(payload["lock_path"], str(self.lock_path))
        self.assertFalse(payload["work_performed"])
        self.assertEqual(db_digest(self.db_path), digest)
        self.assertEqual(self.status(queued), schema.READY_FOR_INTEGRATION)
        self.assertTrue(self.integration.is_queued(queued.task_key))
        # Keyed by (database, repository): another repository is not blocked.
        other = self.run_script(repo="owner/other")
        self.assertEqual(other.returncode, 0, other.stderr + other.stdout)
        self.assertEqual(json.loads(other.stdout)["repo"], "owner/other")
        # SIGKILL of the holder releases the lock; the next run does the work.
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=60)
        after = self.run_script()
        self.assertEqual(after.returncode, 0, after.stderr + after.stdout)
        self.assertEqual([o["task_key"] for o in json.loads(after.stdout)["outcomes"]],
                         [queued.task_key])

    def test_sigkill_mid_integration_no_longer_wedges_the_repository(self):
        """RULINGS 69 closes the liveness gap orchestrator ruling OR-4 recorded.

        Killing a tick during validation used to leave the per-repo lock row
        and wedge the repository. The non-overlap lock is still gone at once.
        The integration flock stays held while the orphaned validator is
        alive, because the validator inherited its descriptor. The next tick's
        integrate_task terminates that orphan, takes the flock, reconciles the
        killed Ticket (crash case A), clears the row and integrates both.
        """
        marker = self.root / "validator-started"
        self.write_config([IntegrationValidatorSpec("slow", (
            sys.executable, "-c",
            f"import pathlib, time; pathlib.Path({str(marker)!r}).touch(); time.sleep(120)"))])
        killed, waiting = self.ticket(), self.ticket()
        tick = subprocess.Popen([sys.executable, str(SCRIPT), *self.arguments(), "--confirm-integration"],
                                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)

        def reap():
            try:
                os.killpg(tick.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            tick.communicate(timeout=60)

        self.addCleanup(reap)
        wait_for(marker.exists, timeout=120)
        self.assertEqual(recorded_pid(self.lock_path), tick.pid)
        record_path = self.lock_dir / "owner@repo.json"
        record = json.loads(record_path.read_text())
        self.assertEqual(record["holder"]["pid"], tick.pid)
        child, = record["holder"]["children"]
        self.addCleanup(lambda: _kill_group(child["pgid"]))
        os.kill(tick.pid, signal.SIGKILL)
        tick.wait(timeout=60)
        row = self.integration.get_integration_lock("owner/repo")
        self.assertEqual(row["owner"], "integration_tick")
        self.assertEqual(self.status(killed), schema.INTEGRATING)
        # The orphaned validator is alive and still holds the flock.
        self.assertTrue(_pid_alive(child["pid"]))
        self.assertFalse(_flock_free(self.lock_dir / "owner@repo.lock"))

        self.write_config(VALIDATORS)
        after = self.run_script("--confirm-integration")
        # The gh stub fails every call, so each integration stops at
        # `gh pr create`; what matters is that neither hits the lock.
        self.assertEqual(after.returncode, 1, after.stderr + after.stdout)
        result = json.loads(after.stdout)
        self.assertEqual(result["integration_lock_at_start"]["owner"], "integration_tick")
        self.assertEqual(result["integration_lock_at_start"]["acquired_at"], row["acquired_at"])
        self.assertEqual([o["status"] for o in result["outcomes"]],
                         ["needs_decision", "needs_decision"])
        self.assertTrue(all("gh pr create failed with 97" in o["reason"]
                            for o in result["outcomes"]))
        self.assertIsNone(result["stopped_reason"])
        lock = result["outcomes"][0]["integration"]["integration_lock"]
        self.assertEqual(lock["leftover_journal_row"], row)
        self.assertEqual([c["pid"] for c in lock["acquisition"]["terminated_children"]],
                         [child["pid"]])
        self.assertEqual(lock["acquisition"]["previous_holder"]["pid"], tick.pid)
        reconciled, = lock["crash_reconciliation"]
        self.assertEqual((reconciled["task_key"], reconciled["case"]), (killed.task_key, "A"))
        self.assertFalse(_pid_alive(child["pid"]))
        self.assertEqual(self.status(killed), schema.NEEDS_DECISION)
        self.assertEqual(self.status(waiting), schema.NEEDS_DECISION)
        self.assertIsNone(self.integration.get_integration_lock("owner/repo"))
        self.assertIsNone(json.loads(record_path.read_text())["holder"])
