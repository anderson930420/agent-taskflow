"""G1-G14: non-V0 entrypoints refuse a V1 Ticket before doing anything.

RULINGS 74/80/81 (V0 Scope Freeze, batch 1; docs/v0-supported-surface.md).
For each guard the Ticket case asserts that:

* the entrypoint refuses with ``v1_ticket_legacy_entrypoint_refused`` in its
  native form (HTTP 409, a JSON result with exit 2, or its blocked result or
  refusal error);
* every table in the database is unchanged, and so is every file under the
  fixture root (the git repository and artifacts included);
* no subprocess and no injected git/gh runner was called.

A legacy task (no ``prompt``) is the control: it still gets past the guard
and reaches the entrypoint's prior behaviour.
"""

from __future__ import annotations

from contextlib import ExitStack, closing, contextmanager, redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any, Iterator
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from step5_support import make_fixture  # noqa: E402

from agent_taskflow.models import TaskWorktreeRecord  # noqa: E402
from agent_taskflow.store import SCHEMA_MIGRATIONS, TaskMirrorStore  # noqa: E402
from agent_taskflow.ticket_lifecycle import LEGACY_ENTRYPOINT_REFUSED  # noqa: E402
from agent_taskflow.v0_surface import UnsupportedInV0, require_supported_in_v0  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_KEY = "AT-9100"


class Reached(Exception):
    """Raised by a stand-in to prove a legacy call got past the guard."""


def load_script(name: str) -> Any:
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"v0_guard_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Dataclasses in a script resolve their module through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_main(module: Any, argv: list[str]) -> tuple[int, dict[str, Any]]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = module.main(argv)
    return code, json.loads(output.getvalue())


class RecordingRunner:
    """A git/gh runner that records every call and never runs anything."""

    def __init__(self, *, raise_on_call: bool = True) -> None:
        self.calls: list[Any] = []
        self.raise_on_call = raise_on_call

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(args[0] if args else kwargs.get("args"))
        if self.raise_on_call:
            raise Reached(f"subprocess called: {self.calls[-1]!r}")
        return subprocess.CompletedProcess(args[0] if args else [], 0, "", "")


class GuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        self.ticket = self.fx.create_ticket("A V0 Ticket").task_key
        self.fx.add_legacy_task(LEGACY_KEY)
        self.db_path = self.fx.db_path

    # ------------------------------------------------------------------
    def db_snapshot(self) -> dict[str, list[tuple]]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                )
            ]
            snapshot = {
                table: sorted(
                    conn.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr,
                )
                for table in tables
            }
            snapshot["__schema__"] = conn.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
        return snapshot

    def tree_snapshot(self) -> dict[str, str]:
        files: dict[str, str] = {}
        for path in sorted(self.fx.root.rglob("*")):
            relative = str(path.relative_to(self.fx.root))
            if relative.startswith("state.db"):
                continue
            if path.is_dir():
                files[relative] = "<dir>"
            else:
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return files

    @contextmanager
    def assert_untouched(self) -> Iterator[RecordingRunner]:
        """Snapshot, forbid every subprocess, and compare afterwards."""
        tables = [name for name in self.db_snapshot() if name != "__schema__"]
        for required in ("tasks", "task_worktrees", "task_events"):
            self.assertIn(required, tables)
        self.assertTrue(any(name.startswith("runtime_") for name in tables), tables)
        db_before = self.db_snapshot()
        tree_before = self.tree_snapshot()
        runner = RecordingRunner()
        with ExitStack() as stack:
            for name in ("run", "Popen", "check_output", "check_call", "call"):
                stack.enter_context(mock.patch.object(subprocess, name, runner))
            yield runner
        self.assertEqual(runner.calls, [], "a subprocess was started")
        self.assertEqual(self.db_snapshot(), db_before, "the database changed")
        self.assertEqual(self.tree_snapshot(), tree_before, "a file changed")

    def assert_refusal_text(self, text: str) -> None:
        self.assertIn(LEGACY_ENTRYPOINT_REFUSED, text)
        self.assertIn(self.ticket, text)

    def use_an_older_schema(self) -> None:
        """Forget the store's latest migration, as a DB from an older release.

        On a current schema ``init_db`` writes nothing observable; on this one,
        an ``init_db`` that ran before the guard records the migration again.
        """
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            deleted = conn.execute(
                "DELETE FROM schema_migrations WHERE name = ?", (SCHEMA_MIGRATIONS[-1],),
            ).rowcount
        self.assertEqual(deleted, 1)

    def add_worktree_record(self, task_key: str) -> Path:
        worktree = self.fx.repo / ".worktrees" / task_key
        worktree.mkdir(parents=True, exist_ok=True)
        TaskMirrorStore(self.db_path).upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=task_key,
                repo_path=self.fx.repo,
                worktree_path=worktree,
                branch=f"task/{task_key}",
                base_branch="main",
                status="active",
            )
        )
        return worktree


class RequireSupportedInV0Tests(GuardTestCase):
    def test_refuses_a_ticket_and_passes_a_legacy_task(self) -> None:
        with self.assertRaises(UnsupportedInV0) as caught:
            require_supported_in_v0(self.db_path, self.ticket, entrypoint="x")
        self.assertEqual(caught.exception.reason_code, LEGACY_ENTRYPOINT_REFUSED)
        self.assert_refusal_text(str(caught.exception))
        self.assertIsNone(require_supported_in_v0(self.db_path, LEGACY_KEY, entrypoint="x"))

    def test_fails_closed_like_d1(self) -> None:
        with self.assertRaises(UnsupportedInV0):
            require_supported_in_v0(None, LEGACY_KEY, entrypoint="x")
        unreadable = self.fx.root / "not-a-db.db"
        unreadable.write_text("not sqlite", encoding="utf-8")
        with self.assertRaises(UnsupportedInV0):
            require_supported_in_v0(unreadable, LEGACY_KEY, entrypoint="x")
        # A database that does not exist yet holds no Ticket (first run).
        self.assertIsNone(
            require_supported_in_v0(self.fx.root / "missing.db", LEGACY_KEY, entrypoint="x")
        )


class ApiGuardTests(GuardTestCase):
    """G1-G5: the legacy action routes answer 409 for a Ticket."""

    def setUp(self) -> None:
        super().setUp()
        from fastapi.testclient import TestClient

        from agent_taskflow.api.main import create_app

        self.dispatches: list[str] = []

        def dispatcher_factory(store: Any, validators: Any) -> Any:
            test = self

            class Dispatcher:
                def dispatch_task(self, task_key: str, **kwargs: Any) -> Any:
                    test.dispatches.append(task_key)
                    raise Reached(f"dispatched {task_key}")

            return Dispatcher()

        self.client = TestClient(create_app(self.db_path, dispatcher_factory=dispatcher_factory))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.prepared: list[Any] = []

        def fake_prepare(request: Any, **kwargs: Any) -> Any:
            self.prepared.append(request.task_key)
            raise Reached(f"prepared {request.task_key}")

        patcher = mock.patch("agent_taskflow.api.main.prepare_task_workspace", fake_prepare)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, key: str, action: str, body: dict[str, Any]) -> Any:
        return self.client.post(f"/api/tasks/{key}/{action}", json=body)

    def assert_409(self, response: Any, action: str) -> None:
        self.assertEqual(response.status_code, 409, response.text)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["action"], action)
        self.assert_refusal_text(body["message"])

    def test_g1_block(self) -> None:
        with self.assert_untouched():
            self.assert_409(self.post(self.ticket, "block", {"blocked_reason": "stop"}), "block")
        legacy = self.post(LEGACY_KEY, "block", {"blocked_reason": "stop"})
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertEqual(self.fx.status(LEGACY_KEY), "blocked")

    def test_g2_reject(self) -> None:
        self.fx.set_status(self.ticket, "blocked", blocked_reason="dependency")
        self.fx.set_status(LEGACY_KEY, "blocked", blocked_reason="legacy")
        body = {"decided_by": "operator_cli"}
        with self.assert_untouched():
            self.assert_409(self.post(self.ticket, "reject", body), "reject")
        legacy = self.post(LEGACY_KEY, "reject", body)
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertEqual(self.fx.status(LEGACY_KEY), "rejected")

    def test_g3_prepare_workspace(self) -> None:
        with self.assert_untouched():
            self.assert_409(self.post(self.ticket, "prepare-workspace", {}), "prepare-workspace")
        self.assertEqual(self.prepared, [])
        with self.assertRaises(Reached):
            self.post(LEGACY_KEY, "prepare-workspace", {})
        self.assertEqual(self.prepared, [LEGACY_KEY])

    def test_g4_approve(self) -> None:
        self.fx.set_status(self.ticket, "waiting_approval")
        self.fx.set_status(LEGACY_KEY, "waiting_approval")
        body = {"decided_by": "operator_cli"}
        with self.assert_untouched():
            self.assert_409(self.post(self.ticket, "approve", body), "approve")
        legacy = self.post(LEGACY_KEY, "approve", body)
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertEqual(self.fx.status(LEGACY_KEY), "accepted")

    def test_g5_start(self) -> None:
        with self.assert_untouched():
            for body in ({}, {"dry_run": True}, {"executor": "manual"}):
                with self.subTest(body=body):
                    self.assert_409(self.post(self.ticket, "start", body), "start")
            # A finished Ticket is refused as a Ticket, not by its status.
            self.fx.set_status(self.ticket, "ready_for_integration")
            db_after_seed = self.db_snapshot()
            self.assert_409(self.post(self.ticket, "start", {}), "start")
            self.assertEqual(self.db_snapshot(), db_after_seed)
            self.fx.set_status(self.ticket, "created")
        self.assertEqual(self.dispatches, [])
        with self.assertRaises(Reached):
            self.post(LEGACY_KEY, "start", {"executor": "noop", "dry_run": True})
        self.assertEqual(self.dispatches, [LEGACY_KEY])


class ScriptGuardTests(GuardTestCase):
    """G6, G11, G13: scripts print one JSON result and exit 2."""

    def test_g6_archive_task_evidence_only(self) -> None:
        script = load_script("archive_task_evidence_only")
        base = ["--reason-code", "obsolete_queued", "--db-path", str(self.db_path)]
        for mode in (["--dry-run"], ["--confirm-evidence-archive"]):
            with self.subTest(mode=mode), self.assert_untouched():
                code, payload = run_main(script, ["--task-key", self.ticket, *base, *mode])
                self.assertEqual(code, 2)
                self.assertFalse(payload["ok"])
                self.assert_refusal_text(payload["error"])
        code, payload = run_main(
            script, ["--task-key", LEGACY_KEY, *base, "--confirm-evidence-archive"],
        )
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.fx.status(LEGACY_KEY), "archived")

    def test_g11_prepare_task_workspace(self) -> None:
        script = load_script("prepare_task_workspace")
        with self.assert_untouched():
            code, payload = run_main(
                script, ["--task-key", self.ticket, "--db-path", str(self.db_path)],
            )
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assert_refusal_text(payload["summary"])
        self.use_an_older_schema()
        with self.assert_untouched():
            code, _ = run_main(script, ["--task-key", self.ticket, "--db-path", str(self.db_path)])
        self.assertEqual(code, 2)
        code, payload = run_main(
            script, ["--task-key", LEGACY_KEY, "--db-path", str(self.db_path)],
        )
        self.assertEqual(code, 0, payload)
        self.assertTrue((self.fx.repo / ".worktrees" / LEGACY_KEY).is_dir())

    def test_g13_create_pi_smoke_task(self) -> None:
        script = load_script("create_pi_smoke_task")
        base = [
            "--db-path", str(self.db_path),
            "--repo-path", str(self.fx.repo),
            "--artifact-root", str(self.fx.artifacts),
        ]
        with self.assert_untouched():
            code, payload = run_main(script, ["--task-key", self.ticket, *base])
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assert_refusal_text(payload["summary"])
        output = io.StringIO()
        with redirect_stdout(output):
            code = script.main(["--task-key", LEGACY_KEY, *base])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(self.fx.task_row(LEGACY_KEY)["executor"], "pi")


class ModuleGuardTests(GuardTestCase):
    """G7-G10, G12, G14: module functions refuse in their own result form."""

    def test_g7_push_task_branch(self) -> None:
        from agent_taskflow.branch_push import BranchPushError, BranchPushRequest, push_task_branch

        self.add_worktree_record(self.ticket)
        for kwargs in ({"dry_run": True}, {"dry_run": False, "confirm_push": True}):
            request = BranchPushRequest(task_key=self.ticket, db_path=self.db_path, **kwargs)
            with self.subTest(kwargs=kwargs), self.assert_untouched():
                runner = RecordingRunner()
                with self.assertRaises(BranchPushError) as caught:
                    push_task_branch(request, runner=runner)
                self.assertEqual(runner.calls, [])
                self.assert_refusal_text(str(caught.exception))
        self.use_an_older_schema()
        with self.assert_untouched(), self.assertRaises(BranchPushError):
            push_task_branch(BranchPushRequest(task_key=self.ticket, db_path=self.db_path))
        self.add_worktree_record(LEGACY_KEY)
        runner = RecordingRunner()
        with self.assertRaises(Reached):
            push_task_branch(BranchPushRequest(task_key=LEGACY_KEY, db_path=self.db_path), runner=runner)
        self.assertEqual(runner.calls[0][:2], ["git", "rev-parse"])

    def test_g8_record_existing_draft_pr(self) -> None:
        from agent_taskflow.draft_pr_record import DraftPrRecordRequest, record_existing_draft_pr

        for kwargs in ({"dry_run": True}, {"confirm_record_existing_pr": True, "allow_non_waiting": True}):
            request = DraftPrRecordRequest(
                task_key=self.ticket, repo="owner/repo", pr_number=7,
                repo_path=self.fx.repo, db_path=self.db_path, **kwargs,
            )
            with self.subTest(kwargs=kwargs), self.assert_untouched():
                runner = RecordingRunner()
                result = record_existing_draft_pr(request, runner=runner)
                self.assertEqual(runner.calls, [])
            self.assertFalse(result.ok)
            self.assertEqual(result.status, "blocked")
            self.assertFalse(result.artifact_recorded)
            self.assert_refusal_text(result.error)
        legacy = record_existing_draft_pr(
            DraftPrRecordRequest(
                task_key=LEGACY_KEY, repo="owner/repo", pr_number=7,
                repo_path=self.fx.repo, db_path=self.db_path, dry_run=True,
            ),
            runner=RecordingRunner(raise_on_call=False),
        )
        # The legacy task reaches the PR-handoff readiness check, as before.
        self.assertNotIn(LEGACY_ENTRYPOINT_REFUSED, json.dumps(legacy.to_dict()))
        self.assertTrue(legacy.handoff)

    def test_g9_confirm_local_cleanup(self) -> None:
        from agent_taskflow.local_cleanup_confirm import (
            LocalCleanupConfirmRequest,
            confirm_local_cleanup,
        )

        self.add_worktree_record(self.ticket)
        for kwargs in ({"dry_run": True}, {"confirm_local_cleanup": True, "delete_local_branch": True}):
            request = LocalCleanupConfirmRequest(
                task_key=self.ticket, repo_path=self.fx.repo, db_path=self.db_path, **kwargs,
            )
            with self.subTest(kwargs=kwargs), self.assert_untouched():
                runner = RecordingRunner()
                result = confirm_local_cleanup(request, runner=runner)
                self.assertEqual(runner.calls, [])
            self.assertFalse(result.ok)
            self.assertEqual(result.status, "blocked")
            self.assert_refusal_text(result.error)
        legacy = confirm_local_cleanup(
            LocalCleanupConfirmRequest(task_key=LEGACY_KEY, repo_path=self.fx.repo, db_path=self.db_path),
            runner=RecordingRunner(),
        )
        self.assertEqual(legacy.error, f"TaskWorktreeRecord missing for task: {LEGACY_KEY}")

    def test_g10_confirm_remote_branch_cleanup(self) -> None:
        from agent_taskflow.remote_branch_cleanup_confirm import (
            RemoteBranchCleanupConfirmRequest,
            confirm_remote_branch_cleanup,
        )

        self.add_worktree_record(self.ticket)
        for kwargs in ({"dry_run": True}, {"confirm_remote_branch_delete": True}):
            request = RemoteBranchCleanupConfirmRequest(
                task_key=self.ticket, repo_path=self.fx.repo, db_path=self.db_path, **kwargs,
            )
            with self.subTest(kwargs=kwargs), self.assert_untouched():
                runner = RecordingRunner()
                result = confirm_remote_branch_cleanup(request, runner=runner)
                self.assertEqual(runner.calls, [])
            self.assertFalse(result.ok)
            self.assertEqual(result.status, "blocked")
            self.assert_refusal_text(result.error)
        legacy = confirm_remote_branch_cleanup(
            RemoteBranchCleanupConfirmRequest(task_key=LEGACY_KEY, repo_path=self.fx.repo, db_path=self.db_path),
            runner=RecordingRunner(),
        )
        self.assertEqual(legacy.error, f"TaskWorktreeRecord missing for task: {LEGACY_KEY}")

    def test_g12_ingest_github_issue(self) -> None:
        from agent_taskflow.github_issue_ingestion import (
            GitHubIssueIngestionError,
            GitHubIssueIngestionRequest,
            GitHubIssueSnapshot,
            ingest_github_issue,
        )

        fetched: list[int] = []

        def fetcher(repo: str, number: int) -> GitHubIssueSnapshot:
            fetched.append(number)
            return GitHubIssueSnapshot(
                number=number, title="Issue", body="body", state="OPEN", labels=(),
                author=None, url=None, created_at=None, updated_at=None,
            )

        store = TaskMirrorStore(self.db_path)
        for dry_run in (True, False):
            request = GitHubIssueIngestionRequest(
                repo="owner/repo", issue_number=5, local_repo_path=self.fx.repo,
                artifact_root=self.fx.artifacts, task_key=self.ticket, dry_run=dry_run,
            )
            with self.subTest(dry_run=dry_run), self.assert_untouched():
                with self.assertRaises(GitHubIssueIngestionError) as caught:
                    ingest_github_issue(request, store=store, fetcher=fetcher)
            self.assert_refusal_text(str(caught.exception))
        self.assertEqual(fetched, [])
        legacy = ingest_github_issue(
            GitHubIssueIngestionRequest(
                repo="owner/repo", issue_number=5, local_repo_path=self.fx.repo,
                artifact_root=self.fx.artifacts, task_key=LEGACY_KEY,
            ),
            store=store,
            fetcher=fetcher,
        )
        self.assertEqual((legacy.status, fetched), ("reused", [5]))

    def test_g14_run_advisory_evidence_retry(self) -> None:
        from agent_taskflow.advisory_evidence_retry import (
            AdvisoryEvidenceRetryError,
            AdvisoryEvidenceRetryRequest,
            run_advisory_evidence_retry,
        )

        self.fx.set_status(self.ticket, "blocked", blocked_reason="advisory")
        artifact_dir = Path(self.fx.task_row(self.ticket)["artifact_dir"])
        for confirm in (False, True):
            request = AdvisoryEvidenceRetryRequest(
                task_key=self.ticket, artifact_dir=artifact_dir, operator="operator-cli",
                db_path=self.db_path, confirm_transition=confirm,
            )
            with self.subTest(confirm=confirm), self.assert_untouched():
                with self.assertRaises(AdvisoryEvidenceRetryError) as caught:
                    run_advisory_evidence_retry(request)
            self.assert_refusal_text(str(caught.exception))
        legacy = run_advisory_evidence_retry(
            AdvisoryEvidenceRetryRequest(
                task_key=LEGACY_KEY, artifact_dir=self.fx.artifacts / LEGACY_KEY,
                operator="operator-cli", db_path=self.db_path,
            )
        )
        # The legacy task gets its read-only precondition report, as before.
        self.assertFalse(legacy.mutated)
        self.assertEqual(legacy.observed_status, "queued")


if __name__ == "__main__":
    unittest.main()
