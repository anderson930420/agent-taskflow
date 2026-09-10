"""Step 3 SSE endpoint and read-only realtime API (SPEC §15, §15.1, §16, §17).

Acceptance gate for the transport half of §43.11: the SSE stream delivers
updates as state changes, every connection begins with a full state snapshot,
and reconnect uses no replay, no ``Last-Event-ID``, and no backfill (§15.1).
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app
from agent_taskflow.api.realtime import (
    SSE_EVENT_SNAPSHOT,
    SSE_EVENT_UPDATE,
    RealtimeEventSource,
    RealtimeStreamOptions,
    format_sse,
    iter_realtime_events,
)
from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.models import TaskRecord
from agent_taskflow.runtime_progress import find_progress_estimates
from agent_taskflow.runtime_progress_store import RuntimeProgressStore
from agent_taskflow.runtime_progress_schema import RUNTIME_PROGRESS_MIGRATION
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.ticket_fields_schema import migrate_ticket_fields
from agent_taskflow.ticket_models import (
    DEFAULT_TICKET_PRIORITY,
    TICKET_PRIORITY_SEQUENCE,
)


class SseFormattingTests(unittest.TestCase):
    """§15.1 — no event ids, so no Last-Event-ID contract can form."""

    def test_format_sse_emits_event_and_data_lines(self) -> None:
        frame = format_sse(SSE_EVENT_SNAPSHOT, {"ok": True})
        self.assertTrue(frame.startswith("event: snapshot\n"))
        self.assertIn('data: {"ok": true}\n', frame)
        self.assertTrue(frame.endswith("\n\n"))

    def test_format_sse_never_emits_an_id_line(self) -> None:
        frame = format_sse(SSE_EVENT_UPDATE, {"ok": True})
        for line in frame.splitlines():
            with self.subTest(line=line):
                self.assertFalse(line.startswith("id:"))

    def test_format_sse_keeps_payload_on_one_data_line(self) -> None:
        frame = format_sse(SSE_EVENT_UPDATE, {"text": "a\nb"})
        data_lines = [line for line in frame.splitlines() if line.startswith("data:")]
        self.assertEqual(len(data_lines), 1)


class RealtimeEventSourceTests(unittest.TestCase):
    """The stream is a projection of persisted state, never its own truth."""

    def setUp(self) -> None:
        self.state: dict[str, object] = {"tick": 0}

    def build(self) -> dict[str, object]:
        return dict(self.state)

    def test_first_event_is_always_a_full_snapshot(self) -> None:
        source = RealtimeEventSource(self.build)
        frame = source.initial_event()
        self.assertIn("event: snapshot", frame)
        payload = json.loads(frame.split("data: ", 1)[1].strip())
        self.assertEqual(payload, {"tick": 0})

    def test_reconnect_sends_a_new_full_snapshot_not_a_replay(self) -> None:
        first = RealtimeEventSource(self.build)
        first.initial_event()
        self.state["tick"] = 5
        # A brand new connection — no Last-Event-ID, no backfill of tick 1..4.
        second = RealtimeEventSource(self.build)
        payload = json.loads(second.initial_event().split("data: ", 1)[1].strip())
        self.assertEqual(payload, {"tick": 5})

    def test_poll_emits_an_update_only_when_state_changes(self) -> None:
        source = RealtimeEventSource(self.build)
        source.initial_event()
        self.assertIsNone(source.poll())
        self.state["tick"] = 1
        frame = source.poll()
        self.assertIsNotNone(frame)
        assert frame is not None
        self.assertIn("event: update", frame)
        self.assertIsNone(source.poll())

    def test_max_updates_bounds_the_stream(self) -> None:
        source = RealtimeEventSource(self.build, max_updates=1)
        source.initial_event()
        self.state["tick"] = 1
        self.assertIsNotNone(source.poll())
        self.assertTrue(source.exhausted)

    def test_iter_realtime_events_yields_snapshot_then_updates(self) -> None:
        source = RealtimeEventSource(self.build, max_updates=2)
        ticks = iter([1, 2])

        def sleep(_seconds: float) -> None:
            self.state["tick"] = next(ticks, self.state["tick"])

        frames = list(iter_realtime_events(source, sleep=sleep))
        self.assertIn("event: snapshot", frames[0])
        self.assertEqual(len(frames), 3)
        for frame in frames[1:]:
            with self.subTest(frame=frame):
                self.assertIn("event: update", frame)

    def test_stream_never_writes_anything(self) -> None:
        calls: list[str] = []

        def build() -> dict[str, object]:
            calls.append("read")
            return {"tick": 0}

        source = RealtimeEventSource(build, max_updates=0)
        list(iter_realtime_events(source, sleep=lambda _s: None))
        self.assertEqual(set(calls), {"read"})


class RealtimeApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.repo_path.mkdir()

        self.tasks = TaskMirrorStore(self.db_path)
        self.tasks.init_db()
        self.attempts = AttemptStore(self.db_path)
        self.attempts.init_db()
        self.progress = RuntimeProgressStore(self.db_path)
        self.progress.init_db()

        self.tasks.upsert_task(
            TaskRecord(
                task_key="AT-101",
                project="forms",
                status="implementing",
                repo_path=self.repo_path,
                title="Separate ending-page image",
            )
        )
        self.tasks.upsert_task(
            TaskRecord(
                task_key="AT-112",
                project="forms",
                status="blocked",
                repo_path=self.repo_path,
                blocked_reason="Waiting for AT-101",
            )
        )
        self.attempt_id = self.attempts.create_attempt("AT-101").attempt_id
        self.progress.record_step(
            attempt_id=self.attempt_id, step="Prepare", status="passed"
        )
        self.progress.set_current_activity(
            attempt_id=self.attempt_id,
            phase="Implementer",
            activity="Adding frontend regression tests",
        )

        # The API fails closed until Step 1's explicit migration has run
        # (scripts/migrate_ticket_fields.py). Fixtures call the function the
        # script wraps, per Step 1's fixture convention.
        migrate_ticket_fields(self.db_path)
        self.app = create_app(
            self.db_path,
            realtime_options=RealtimeStreamOptions(
                poll_interval=0.0, max_updates=1, max_polls=2
            ),
        )
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.tmp.cleanup()


class RealtimeBoardEndpointTests(RealtimeApiTestCase):
    def test_board_endpoint_returns_the_board_sections(self) -> None:
        response = self.client.get("/api/realtime/board")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        keys = [section["key"] for section in payload["sections"]]
        self.assertEqual(
            keys,
            [
                "NEEDS DECISION",
                "RUNNING",
                "READY",
                "BLOCKED",
                "PAUSED",
                "READY FOR REVIEW",
            ],
        )

    def test_board_endpoint_reflects_runtime_state(self) -> None:
        payload = self.client.get("/api/realtime/board").json()["item"]
        running = next(
            section for section in payload["sections"] if section["key"] == "RUNNING"
        )
        ticket = running["tickets"][0]
        self.assertEqual(ticket["task_key"], "AT-101")
        self.assertEqual(ticket["status"], "implementing")
        self.assertEqual(ticket["display_status"], "running")
        self.assertEqual(ticket["current_phase"], "Implementer")
        self.assertEqual(
            ticket["current_activity"], "Adding frontend regression tests"
        )
        statuses = {step["name"]: step["status"] for step in ticket["steps"]}
        self.assertEqual(statuses["Prepare"], "passed")
        self.assertEqual(statuses["Validator"], "pending")

    def test_board_endpoint_renders_blocked_with_its_blocker(self) -> None:
        payload = self.client.get("/api/realtime/board").json()["item"]
        blocked = next(
            section for section in payload["sections"] if section["key"] == "BLOCKED"
        )
        ticket = blocked["tickets"][0]
        self.assertEqual(ticket["task_key"], "AT-112")
        self.assertEqual(ticket["blocker_hint"], "Waiting for AT-101")
        self.assertFalse(ticket["running"])
        self.assertFalse(ticket["eligible_for_execution"])

    def test_needs_decision_ticket_is_on_top_read_only(self) -> None:
        # Human ruling 5, end to end through the API.
        self.tasks.upsert_task(
            TaskRecord(
                task_key="AT-200",
                project="forms",
                status="needs_decision",
                repo_path=self.repo_path,
            )
        )
        sections = self.client.get("/api/realtime/board").json()["item"]["sections"]
        top = sections[0]
        self.assertEqual(top["key"], "NEEDS DECISION")
        self.assertEqual([t["task_key"] for t in top["tickets"]], ["AT-200"])
        self.assertTrue(top["read_only"])
        self.assertEqual(top["actions"], [])
        ticket = top["tickets"][0]
        self.assertTrue(ticket["awaiting_decision"])
        self.assertFalse(ticket["eligible_for_execution"])
        self.assertFalse(ticket["running"])
        for section in sections[1:]:
            with self.subTest(section=section["key"]):
                self.assertNotIn(
                    "AT-200", [t["task_key"] for t in section["tickets"]]
                )

    def test_board_response_has_no_percentage_or_eta(self) -> None:
        payload = self.client.get("/api/realtime/board").json()
        self.assertEqual(find_progress_estimates(payload), ())

    def test_board_endpoint_does_not_change_task_status(self) -> None:
        before = self.client.get("/api/tasks/AT-101").json()["item"]
        self.client.get("/api/realtime/board")
        after = self.client.get("/api/tasks/AT-101").json()["item"]
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["updated_at"], before["updated_at"])


class RealtimeTicketEndpointTests(RealtimeApiTestCase):
    def test_ticket_endpoint_returns_section_17_fields(self) -> None:
        response = self.client.get("/api/tasks/AT-101/realtime")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["item"]
        ticket = payload["ticket"]
        self.assertEqual(ticket["repository"], "forms")
        self.assertEqual(ticket["status"], "implementing")
        # SPEC §12.2 — persisted spelling plus its §12 display name.
        self.assertEqual(ticket["display_status"], "running")
        self.assertEqual(ticket["display"]["status"], "implementing")
        self.assertEqual(ticket["display"]["display_status"], "running")
        self.assertEqual(ticket["current_phase"], "Implementer")
        self.assertEqual(len(ticket["steps"]), 7)
        self.assertIn("artifacts", payload)
        self.assertIn("validators", payload)

    def test_ticket_endpoint_renders_dash_for_absent_pr_fields(self) -> None:
        payload = self.client.get("/api/tasks/AT-101/realtime").json()["item"]
        display = payload["ticket"]["pr"]["display"]
        self.assertEqual(display["pr_number"], "—")
        self.assertEqual(display["ci_status"], "—")
        self.assertEqual(display["integrated_base_sha"], "—")

    def test_ticket_endpoint_supports_selecting_an_earlier_attempt(self) -> None:
        self.attempts.close_attempt(
            self.attempt_id,
            status="validation_failed",
            reason_code="validators_failed",
            actor="test",
        )
        second = self.attempts.create_attempt("AT-101").attempt_id

        latest = self.client.get("/api/tasks/AT-101/realtime").json()["item"]
        self.assertEqual(latest["selected_attempt_id"], second)

        earlier = self.client.get(
            f"/api/tasks/AT-101/realtime?attempt_id={self.attempt_id}"
        ).json()["item"]
        self.assertEqual(earlier["selected_attempt_id"], self.attempt_id)
        statuses = {
            step["name"]: step["status"] for step in earlier["ticket"]["steps"]
        }
        self.assertEqual(statuses["Prepare"], "passed")

    def test_attempts_endpoint_lists_every_attempt(self) -> None:
        response = self.client.get("/api/tasks/AT-101/attempts")
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual([item["attempt_number"] for item in items], [1])

    def test_unknown_ticket_returns_404(self) -> None:
        self.assertEqual(
            self.client.get("/api/tasks/AT-999/realtime").status_code, 404
        )
        self.assertEqual(
            self.client.get("/api/tasks/AT-999/attempts").status_code, 404
        )

    def test_ticket_response_has_no_percentage_or_eta(self) -> None:
        payload = self.client.get("/api/tasks/AT-101/realtime").json()
        self.assertEqual(find_progress_estimates(payload), ())


class SseStreamEndpointTests(RealtimeApiTestCase):
    def read_stream(self, url: str = "/api/realtime/stream") -> str:
        with self.client.stream("GET", url) as response:
            self.assertEqual(response.status_code, 200)
            self.assertTrue(
                response.headers["content-type"].startswith("text/event-stream")
            )
            return "".join(response.iter_text())

    def test_stream_sends_a_full_snapshot_first(self) -> None:
        body = self.read_stream()
        self.assertTrue(body.lstrip().startswith("event: snapshot"))
        snapshot = json.loads(
            body.split("event: snapshot\n", 1)[1]
            .split("data: ", 1)[1]
            .split("\n", 1)[0]
        )
        keys = [section["key"] for section in snapshot["sections"]]
        self.assertEqual(
            keys,
            [
                "NEEDS DECISION",
                "RUNNING",
                "READY",
                "BLOCKED",
                "PAUSED",
                "READY FOR REVIEW",
            ],
        )

    def test_stream_delivers_an_update_when_state_changes(self) -> None:
        polls: list[int] = []

        def on_poll(index: int) -> None:
            # Deterministic seam: change persisted state between the snapshot
            # and the first poll, then leave it alone.
            polls.append(index)
            if index == 1:
                self.progress.record_step(
                    attempt_id=self.attempt_id,
                    step="Implementer",
                    status="running",
                )

        app = create_app(
            self.db_path,
            realtime_options=RealtimeStreamOptions(
                poll_interval=0.0, max_updates=1, max_polls=3, on_poll=on_poll
            ),
        )
        with TestClient(app) as client:
            with client.stream("GET", "/api/realtime/stream") as response:
                body = "".join(response.iter_text())

        self.assertIn("event: update", body)
        update = json.loads(
            body.split("event: update\n", 1)[1].split("data: ", 1)[1].split("\n", 1)[0]
        )
        running = next(
            section for section in update["sections"] if section["key"] == "RUNNING"
        )
        statuses = {
            step["name"]: step["status"] for step in running["tickets"][0]["steps"]
        }
        self.assertEqual(statuses["Implementer"], "running")

    def test_stream_never_emits_an_event_id(self) -> None:
        body = self.read_stream()
        for line in body.splitlines():
            with self.subTest(line=line):
                self.assertFalse(line.startswith("id:"))

    def test_stream_ignores_a_last_event_id_header(self) -> None:
        with self.client.stream(
            "GET", "/api/realtime/stream", headers={"Last-Event-ID": "42"}
        ) as response:
            body = "".join(response.iter_text())
        self.assertTrue(body.lstrip().startswith("event: snapshot"))

    def test_stream_disables_proxy_buffering_and_caching(self) -> None:
        with self.client.stream("GET", "/api/realtime/stream") as response:
            headers = dict(response.headers)
            "".join(response.iter_text())
        self.assertEqual(headers.get("cache-control"), "no-cache")
        self.assertEqual(headers.get("x-accel-buffering"), "no")

    def test_stream_payload_has_no_percentage_or_eta(self) -> None:
        body = self.read_stream()
        for line in body.splitlines():
            if line.startswith("data: "):
                with self.subTest(line=line):
                    payload = json.loads(line[len("data: ") :])
                    self.assertEqual(find_progress_estimates(payload), ())

    def test_stream_does_not_change_task_status(self) -> None:
        before = self.client.get("/api/tasks/AT-101").json()["item"]
        self.read_stream()
        after = self.client.get("/api/tasks/AT-101").json()["item"]
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["updated_at"], before["updated_at"])

    def test_ticket_scoped_stream_sends_a_ticket_snapshot(self) -> None:
        body = self.read_stream("/api/tasks/AT-101/realtime/stream")
        self.assertTrue(body.lstrip().startswith("event: snapshot"))
        snapshot = json.loads(
            body.split("data: ", 1)[1].split("\n", 1)[0]
        )
        self.assertEqual(snapshot["ticket"]["task_key"], "AT-101")

    def test_ticket_scoped_stream_returns_404_for_unknown_ticket(self) -> None:
        response = self.client.get("/api/tasks/AT-999/realtime/stream")
        self.assertEqual(response.status_code, 404)


PROJECTS_YAML = """\
projects:
  forms:
    project_slug: forms
    task_key_prefix: FM
    repo_path: {repo_path}
    github_repo: example/forms
    artifacts_root: {artifacts_root}
    worktrees_dir: {worktrees_dir}
    default_branch: main
    branch_prefix: task/
"""


class TicketReproductionTests(unittest.TestCase):
    """Review item 3c — the reviewer's own reproduction, end to end.

    POST /api/tickets, then GET /api/realtime/board (the Ticket appears), then
    GET /api/tasks/<id>/realtime (200, priority populated). Nothing here seeds
    `tasks` directly: the Ticket exists only because Step 1's route wrote it,
    so this proves the board and detail endpoints read the canonical table.

    Also the production shape: the database carries only what an operator
    runs before starting the API — the base schema and Step 1's explicit
    Ticket-column migration — and no attempt or progress migration.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        repo_path = root / "sandbox" / "forms"
        repo_path.mkdir(parents=True)
        config_path = root / "projects.yaml"
        config_path.write_text(
            PROJECTS_YAML.format(
                repo_path=repo_path,
                artifacts_root=root / "sandbox" / "artifacts",
                worktrees_dir=repo_path / ".worktrees",
            ),
            encoding="utf-8",
        )
        (root / "db").mkdir()
        self.db_path = root / "db" / "state.db"
        self.priority = next(
            p for p in TICKET_PRIORITY_SEQUENCE if p != DEFAULT_TICKET_PRIORITY
        )
        # The API fails closed until Step 1's explicit migration has run.
        TaskMirrorStore(self.db_path).init_db()
        migrate_ticket_fields(self.db_path)
        self.client_context = TestClient(
            create_app(
                self.db_path,
                projects_config_path=config_path,
                realtime_options=RealtimeStreamOptions(
                    poll_interval=0.0, max_updates=1, max_polls=2
                ),
            )
        )
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.tmp.cleanup()

    def create_ticket(self, **extra: object) -> dict[str, object]:
        response = self.client.post(
            "/api/tickets",
            json={
                "repository": "forms",
                "prompt": "Separate the ending page image from the hero",
                "priority": self.priority,
                **extra,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def board_ticket(self, task_key: str) -> tuple[str, dict[str, object]]:
        board = self.client.get("/api/realtime/board")
        self.assertEqual(board.status_code, 200)
        for section in board.json()["item"]["sections"]:
            for ticket in section["tickets"]:
                if ticket["task_key"] == task_key:
                    return section["key"], ticket
        self.fail(f"{task_key} is not on the board")

    def test_created_ticket_appears_on_the_board(self) -> None:
        created = self.create_ticket()
        section, ticket = self.board_ticket(created["task_key"])
        # Persisted `created` is the §12 display status `ready` (§12.1, §12.2).
        self.assertEqual(section, "READY")
        self.assertEqual(ticket["status"], created["status"])
        self.assertEqual(ticket["display_status"], "ready")
        self.assertTrue(ticket["eligible_for_execution"])
        self.assertEqual(ticket["priority"], self.priority)

    def test_detail_returns_200_with_priority_populated(self) -> None:
        created = self.create_ticket()
        item = created["item"]
        response = self.client.get(f"/api/tasks/{created['task_key']}/realtime")
        self.assertEqual(response.status_code, 200, response.text)
        ticket = response.json()["item"]["ticket"]
        self.assertEqual(ticket["priority"], self.priority)
        self.assertEqual(ticket["display"]["priority"], self.priority)
        self.assertEqual(ticket["repository"], "forms")

    def test_detail_reads_branch_and_worktree_from_tasks(self) -> None:
        # Step 1 writes these on `tasks` and creates no `task_worktrees` row.
        created = self.create_ticket()
        item = created["item"]
        ticket = self.client.get(
            f"/api/tasks/{created['task_key']}/realtime"
        ).json()["item"]["ticket"]
        self.assertEqual(ticket["branch"], item["branch"])
        self.assertEqual(ticket["worktree_path"], item["worktree_path"])
        self.assertNotEqual(ticket["display"]["branch"], "—")

    def test_blocked_ticket_renders_its_blocker_from_tasks(self) -> None:
        blocker = self.create_ticket()
        blocked = self.create_ticket(blocked_by=blocker["task_key"])
        section, ticket = self.board_ticket(blocked["task_key"])
        self.assertEqual(section, "BLOCKED")
        self.assertEqual(ticket["blocker"], blocker["task_key"])
        self.assertEqual(ticket["blocker_hint"], f"Waiting for {blocker['task_key']}")
        self.assertFalse(ticket["eligible_for_execution"])

    def test_ticket_is_on_the_board_with_no_progress_schema_installed(self) -> None:
        created = self.create_ticket()
        with sqlite3.connect(self.db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertNotIn("attempt_progress", tables)
        self.assertNotIn("attempts", tables)
        _section, ticket = self.board_ticket(created["task_key"])
        self.assertTrue(all(step["status"] == "pending" for step in ticket["steps"]))


class DefaultAppWiringTests(unittest.TestCase):
    def test_startup_runs_no_step3_migration(self) -> None:
        # 3a — nothing may run Step 3's migration at process startup.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            # Step 1's gate needs its migration first; this test only asserts
            # that startup then installs none of Step 3's schema.
            TaskMirrorStore(db_path).init_db()
            migrate_ticket_fields(db_path)
            with TestClient(create_app(db_path)):
                pass
            with sqlite3.connect(db_path) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                recorded = conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE name = ?",
                    (RUNTIME_PROGRESS_MIGRATION,),
                ).fetchone()
        self.assertNotIn("attempt_progress", tables)
        self.assertNotIn("attempt_observed_steps", tables)
        self.assertIsNone(recorded)

    def test_default_app_exposes_realtime_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(Path(tmp) / "state.db")
            paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
        self.assertIn("/api/realtime/board", paths)
        self.assertIn("/api/realtime/stream", paths)
        self.assertIn("/api/tasks/{task_key}/realtime", paths)
        self.assertIn("/api/tasks/{task_key}/realtime/stream", paths)
        self.assertIn("/api/tasks/{task_key}/attempts", paths)

    def test_default_stream_options_are_unbounded(self) -> None:
        options = RealtimeStreamOptions()
        self.assertIsNone(options.max_updates)
        self.assertIsNone(options.max_polls)
        self.assertIsNone(options.on_poll)
        self.assertGreater(options.poll_interval, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
