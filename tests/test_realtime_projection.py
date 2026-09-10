"""Step 3 read-only board / Ticket projection (SPEC §16, §17, §30, §31, §32.1).

Acceptance gate for:

* §43.11 — show runtime state in Mission Control
* §43.5 (display half) — never execute blocked / paused Tickets
* §43.15 / §43.16 (display half) — validators and PR render read-only
* §43.27 — GitHub CI does not mutate lifecycle
* §44 — paused cannot acquire work, blocked cannot execute, GitHub CI is not a
  Taskflow lifecycle authority, the projection is never an independent source
  of truth.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.attempt_store import AttemptStore
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.realtime_projection import (
    BOARD_SECTIONS,
    BOARD_SECTION_BLOCKED,
    BOARD_SECTION_PAUSED,
    BOARD_SECTION_READY,
    BOARD_SECTION_READY_FOR_REVIEW,
    BOARD_SECTION_RUNNING,
    DASH,
    DISPLAY_STATUS_SECTIONS,
    PR_FIELD_NAMES,
    UNSECTIONED_DISPLAY_STATUSES,
    build_board_projection,
    build_ticket_projection,
    projection_to_dict,
)
from agent_taskflow.runtime_progress import find_progress_estimates
from agent_taskflow.runtime_progress_store import RuntimeProgressStore
from agent_taskflow.status_vocab import (
    DISPLAY_STATUS_SEQUENCE,
    PERSISTED_TO_DISPLAY,
    to_display_status,
)
from agent_taskflow.store import TaskMirrorStore


class ProjectionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.repo_path.mkdir()
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()

        self.tasks = TaskMirrorStore(self.db_path)
        self.tasks.init_db()
        self.attempts = AttemptStore(self.db_path)
        self.attempts.init_db()
        self.progress = RuntimeProgressStore(self.db_path)
        self.progress.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add_task(
        self,
        task_key: str,
        status: str,
        *,
        project: str = "forms",
        blocked_reason: str | None = None,
        title: str | None = None,
    ) -> None:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.tasks.upsert_task(
            TaskRecord(
                task_key=task_key,
                project=project,
                status=status,
                repo_path=self.repo_path,
                artifact_dir=artifact_dir,
                blocked_reason=blocked_reason,
                title=title or f"Task {task_key}",
            )
        )

    def add_worktree(self, task_key: str, branch: str) -> None:
        self.tasks.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=task_key,
                repo_path=self.repo_path,
                worktree_path=self.repo_path / ".worktrees" / task_key,
                branch=branch,
                status="active",
            )
        )

    def sections(self, projection: object) -> dict[str, list[str]]:
        return {
            section.key: [ticket.task_key for ticket in section.tickets]
            for section in projection.sections  # type: ignore[attr-defined]
        }

    def add_pr_columns(self, **values: object) -> None:
        """Simulate the Step 2 watcher having landed the §32.1 columns."""
        types = {
            "pr_number": "INTEGER",
            "pr_merged": "INTEGER",
            "reintegration_count": "INTEGER",
            "reintegration_required": "INTEGER",
        }
        with sqlite3.connect(self.db_path) as conn:
            existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            for name in PR_FIELD_NAMES:
                if name not in existing:
                    conn.execute(
                        f"ALTER TABLE tasks ADD COLUMN {name} {types.get(name, 'TEXT')}"
                    )
            for task_key, columns in values.items():
                assert isinstance(columns, dict)
                assignments = ", ".join(f"{name} = ?" for name in columns)
                conn.execute(
                    f"UPDATE tasks SET {assignments} WHERE task_key = ?",
                    (*columns.values(), task_key.replace("_", "-")),
                )


class BoardSectionTests(ProjectionTestCase):
    """§16 — RUNNING / READY / BLOCKED / PAUSED / READY FOR REVIEW."""

    def test_board_exposes_exactly_the_five_spec_sections_in_order(self) -> None:
        projection = build_board_projection(self.db_path)
        self.assertEqual(
            tuple(section.key for section in projection.sections), BOARD_SECTIONS
        )
        self.assertEqual(
            BOARD_SECTIONS,
            (
                "RUNNING",
                "READY",
                "BLOCKED",
                "PAUSED",
                "READY FOR REVIEW",
            ),
        )

    def test_tickets_land_in_their_section(self) -> None:
        self.add_task("AT-101", "implementing")
        self.add_task("AT-109", "queued")
        self.add_task("AT-112", "blocked", blocked_reason="Waiting for AT-101")
        self.add_task("AT-098", "waiting_approval")

        sections = self.sections(build_board_projection(self.db_path))
        self.assertEqual(sections[BOARD_SECTION_RUNNING], ["AT-101"])
        self.assertEqual(sections[BOARD_SECTION_READY], ["AT-109"])
        self.assertEqual(sections[BOARD_SECTION_BLOCKED], ["AT-112"])
        self.assertEqual(sections[BOARD_SECTION_READY_FOR_REVIEW], ["AT-098"])

    def test_terminal_tickets_are_not_placed_in_the_five_sections(self) -> None:
        self.add_task("AT-001", "completed")
        self.add_task("AT-002", "canceled")
        projection = build_board_projection(self.db_path)
        for section in projection.sections:
            with self.subTest(section=section.key):
                self.assertEqual(section.tickets, ())
        self.assertEqual(
            sorted(ticket.task_key for ticket in projection.unsectioned),
            ["AT-001", "AT-002"],
        )

    def test_needs_decision_is_unsectioned_and_flagged_as_ambiguous(self) -> None:
        # Known ambiguity watchlist: §16 shows five sections and does not
        # include needs_decision. Step 3 flags rather than guessing, and the
        # note travels with every board payload.
        projection = build_board_projection(self.db_path)
        self.assertTrue(
            any("needs_decision" in note for note in projection.notes),
            projection.notes,
        )

    def test_needs_decision_ticket_is_not_placed_in_the_five_sections(self) -> None:
        self.add_task("AT-200", "needs_decision")
        projection = build_board_projection(self.db_path)
        for section in projection.sections:
            with self.subTest(section=section.key):
                self.assertEqual(section.tickets, ())
        self.assertEqual(
            [ticket.task_key for ticket in projection.unsectioned], ["AT-200"]
        )
        ticket = projection.find("AT-200")
        assert ticket is not None
        self.assertFalse(ticket.running)
        self.assertFalse(ticket.eligible_for_execution)


class StatusVocabularyBridgeTests(ProjectionTestCase):
    """SPEC §12.2 — status_vocab is the single bridge; the board keeps no copy."""

    def test_section_map_is_keyed_only_by_display_statuses(self) -> None:
        self.assertTrue(
            set(DISPLAY_STATUS_SECTIONS) <= set(DISPLAY_STATUS_SEQUENCE),
            set(DISPLAY_STATUS_SECTIONS) - set(DISPLAY_STATUS_SEQUENCE),
        )

    def test_every_display_status_is_either_sectioned_or_unsectioned(self) -> None:
        covered = set(DISPLAY_STATUS_SECTIONS) | set(UNSECTIONED_DISPLAY_STATUSES)
        self.assertEqual(covered, set(DISPLAY_STATUS_SEQUENCE))

    def test_projection_source_holds_no_legacy_status_table(self) -> None:
        # The bridge lives in status_vocab. A legacy spelling appearing as a
        # key in this module would mean the mapping was copied back in.
        source = (
            Path(__file__).resolve().parents[1]
            / "agent_taskflow"
            / "realtime_projection.py"
        ).read_text(encoding="utf-8")
        legacy_only = set(PERSISTED_TO_DISPLAY) - set(DISPLAY_STATUS_SEQUENCE)
        for status in sorted(legacy_only):
            with self.subTest(status=status):
                self.assertNotIn(f'"{status}"', source)

    def test_every_persisted_status_lands_somewhere_without_raising(self) -> None:
        for index, persisted in enumerate(sorted(PERSISTED_TO_DISPLAY)):
            task_key = f"AT-{700 + index}"
            with self.subTest(persisted=persisted):
                self.add_task(task_key, persisted)
        projection = build_board_projection(self.db_path)
        placed = {
            ticket.task_key
            for section in projection.sections
            for ticket in section.tickets
        } | {ticket.task_key for ticket in projection.unsectioned}
        self.assertEqual(len(placed), len(PERSISTED_TO_DISPLAY))

    def test_ticket_exposes_both_persisted_and_display_status(self) -> None:
        self.add_task("AT-101", "implementing")
        ticket = build_board_projection(self.db_path).find("AT-101")
        assert ticket is not None
        self.assertEqual(ticket.status, "implementing")
        self.assertEqual(ticket.display_status, "running")
        self.assertEqual(ticket.display()["status"], "implementing")
        self.assertEqual(ticket.display()["display_status"], "running")

    def test_display_status_always_matches_status_vocab(self) -> None:
        for index, persisted in enumerate(sorted(PERSISTED_TO_DISPLAY)):
            self.add_task(f"AT-{800 + index}", persisted)
        projection = build_board_projection(self.db_path)
        for index, persisted in enumerate(sorted(PERSISTED_TO_DISPLAY)):
            ticket = projection.find(f"AT-{800 + index}")
            assert ticket is not None
            with self.subTest(persisted=persisted):
                self.assertEqual(
                    ticket.display_status, to_display_status(persisted)
                )

    def test_legacy_review_spellings_share_the_review_section(self) -> None:
        # waiting_approval and waiting_for_review are distinct legacy values
        # that status_vocab collapses onto needs_review.
        self.add_task("AT-301", "waiting_approval")
        self.add_task("AT-302", "waiting_for_review")
        sections = self.sections(build_board_projection(self.db_path))
        self.assertEqual(
            sections[BOARD_SECTION_READY_FOR_REVIEW], ["AT-301", "AT-302"]
        )

    def test_unknown_status_renders_unsectioned_instead_of_raising(self) -> None:
        # A value outside both vocabularies must never break a render.
        self.add_task("AT-400", "queued")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET status = 'not-a-status' WHERE task_key = 'AT-400'"
            )
        projection = build_board_projection(self.db_path)
        ticket = projection.find("AT-400")
        assert ticket is not None
        self.assertIsNone(ticket.display_status)
        self.assertIsNone(ticket.section)
        self.assertFalse(ticket.running)
        self.assertFalse(ticket.eligible_for_execution)
        self.assertEqual(ticket.display()["display_status"], DASH)


class BlockedAndPausedDisplayTests(ProjectionTestCase):
    """§43.5 display half and §44 — never rendered as running or eligible."""

    def test_blocked_ticket_is_not_running_and_not_eligible(self) -> None:
        self.add_task("AT-112", "blocked", blocked_reason="Waiting for AT-101")
        projection = build_board_projection(self.db_path)
        ticket = projection.find("AT-112")
        assert ticket is not None
        self.assertEqual(ticket.section, BOARD_SECTION_BLOCKED)
        self.assertFalse(ticket.running)
        self.assertFalse(ticket.eligible_for_execution)
        self.assertTrue(ticket.blocked)

    def test_blocked_ticket_renders_its_blocker(self) -> None:
        self.add_task("AT-112", "blocked", blocked_reason="Waiting for AT-101")
        ticket = build_board_projection(self.db_path).find("AT-112")
        assert ticket is not None
        self.assertEqual(ticket.blocker, "AT-101")
        self.assertEqual(ticket.blocker_hint, "Waiting for AT-101")

    def test_blocked_ticket_without_a_known_blocker_key_still_renders(self) -> None:
        self.add_task("AT-113", "blocked", blocked_reason="governance violation")
        ticket = build_board_projection(self.db_path).find("AT-113")
        assert ticket is not None
        self.assertIsNone(ticket.blocker)
        self.assertEqual(ticket.blocker_hint, "governance violation")

    def test_blocked_ticket_with_no_reason_renders_a_dash(self) -> None:
        self.add_task("AT-114", "blocked")
        ticket = build_board_projection(self.db_path).find("AT-114")
        assert ticket is not None
        self.assertIsNone(ticket.blocker_hint)
        self.assertEqual(ticket.display()["blocker_hint"], DASH)

    def test_paused_ticket_is_never_rendered_as_eligible(self) -> None:
        self.add_task("AT-115", "paused")
        projection = build_board_projection(self.db_path)
        ticket = projection.find("AT-115")
        assert ticket is not None
        self.assertEqual(ticket.section, BOARD_SECTION_PAUSED)
        self.assertTrue(ticket.paused)
        self.assertFalse(ticket.running)
        self.assertFalse(ticket.eligible_for_execution)

    def test_only_ready_section_tickets_are_eligible_for_execution(self) -> None:
        self.add_task("AT-101", "implementing")
        self.add_task("AT-109", "queued")
        self.add_task("AT-112", "blocked")
        self.add_task("AT-098", "waiting_approval")
        projection = build_board_projection(self.db_path)
        eligible = sorted(
            ticket.task_key
            for section in projection.sections
            for ticket in section.tickets
            if ticket.eligible_for_execution
        )
        self.assertEqual(eligible, ["AT-109"])

    def test_only_running_section_tickets_are_marked_running(self) -> None:
        self.add_task("AT-101", "implementing")
        self.add_task("AT-109", "queued")
        self.add_task("AT-112", "blocked")
        projection = build_board_projection(self.db_path)
        running = sorted(
            ticket.task_key
            for section in projection.sections
            for ticket in section.tickets
            if ticket.running
        )
        self.assertEqual(running, ["AT-101"])


class RuntimeStateOnTheBoardTests(ProjectionTestCase):
    """§43.11 — the board reflects current_phase / current_activity / steps."""

    def test_board_ticket_carries_latest_attempt_progress(self) -> None:
        self.add_task("AT-101", "implementing")
        attempt_id = self.attempts.create_attempt("AT-101").attempt_id
        self.progress.record_step(
            attempt_id=attempt_id, step="Prepare", status="passed"
        )
        self.progress.record_step(
            attempt_id=attempt_id, step="Implementer", status="running"
        )
        self.progress.set_current_activity(
            attempt_id=attempt_id,
            phase="Implementer",
            activity="Adding frontend regression tests",
        )

        ticket = build_board_projection(self.db_path).find("AT-101")
        assert ticket is not None
        self.assertEqual(ticket.attempt_id, attempt_id)
        self.assertEqual(ticket.attempt_number, 1)
        self.assertEqual(ticket.current_phase, "Implementer")
        self.assertEqual(
            ticket.current_activity, "Adding frontend regression tests"
        )
        statuses = {step.name: step.status for step in ticket.steps}
        self.assertEqual(statuses["Prepare"], "passed")
        self.assertEqual(statuses["Implementer"], "running")
        self.assertEqual(statuses["Integration"], "pending")

    def test_ticket_without_progress_renders_all_steps_pending(self) -> None:
        self.add_task("AT-109", "queued")
        ticket = build_board_projection(self.db_path).find("AT-109")
        assert ticket is not None
        self.assertIsNone(ticket.current_phase)
        self.assertEqual(ticket.display()["current_activity"], DASH)
        self.assertTrue(all(step.status == "pending" for step in ticket.steps))

    def test_board_payload_contains_no_percentage_or_eta(self) -> None:
        self.add_task("AT-101", "implementing")
        attempt_id = self.attempts.create_attempt("AT-101").attempt_id
        self.progress.set_current_activity(
            attempt_id=attempt_id, phase="Implementer", activity="Writing tests"
        )
        payload = projection_to_dict(build_board_projection(self.db_path))
        self.assertEqual(find_progress_estimates(payload), ())


class TicketDetailProjectionTests(ProjectionTestCase):
    """§17 — repository, priority, status, branch, worktree, steps, activity."""

    def test_detail_projection_exposes_the_section_17_fields(self) -> None:
        self.add_task("AT-101", "implementing", title="Separate ending-page image")
        self.add_worktree("AT-101", "task/AT-101-separate-ending-image")
        attempt_id = self.attempts.create_attempt("AT-101").attempt_id
        self.progress.set_current_activity(
            attempt_id=attempt_id,
            phase="Implementer",
            activity="Adding frontend regression tests",
        )

        detail = build_ticket_projection(self.db_path, "AT-101")
        assert detail is not None
        ticket = detail.ticket
        self.assertEqual(ticket.task_key, "AT-101")
        self.assertEqual(ticket.title, "Separate ending-page image")
        self.assertEqual(ticket.repository, "forms")
        self.assertEqual(ticket.status, "implementing")
        self.assertEqual(ticket.branch, "task/AT-101-separate-ending-image")
        self.assertTrue(str(ticket.worktree_path).endswith(".worktrees/AT-101"))
        self.assertEqual(
            ticket.current_activity, "Adding frontend regression tests"
        )
        self.assertEqual(len(ticket.steps), 7)

    def test_missing_priority_column_renders_a_dash(self) -> None:
        # Priority is Step 1 metadata; it may not exist yet.
        self.add_task("AT-101", "implementing")
        detail = build_ticket_projection(self.db_path, "AT-101")
        assert detail is not None
        self.assertIsNone(detail.ticket.priority)
        self.assertEqual(detail.ticket.display()["priority"], DASH)

    def test_detail_projection_returns_none_for_unknown_ticket(self) -> None:
        self.assertIsNone(build_ticket_projection(self.db_path, "AT-999"))

    def test_detail_lists_artifact_links(self) -> None:
        self.add_task("AT-101", "implementing")
        log_path = self.artifact_root / "AT-101" / "worker.log"
        log_path.write_text("log", encoding="utf-8")
        self.tasks.record_task_artifact("AT-101", "worker_log", log_path)

        detail = build_ticket_projection(self.db_path, "AT-101")
        assert detail is not None
        types = [artifact["artifact_type"] for artifact in detail.artifacts]
        self.assertIn("worker_log", types)

    def test_detail_lists_validator_evidence_read_only(self) -> None:
        self.add_task("AT-101", "validating")
        self.tasks.record_validation_result(
            "AT-101",
            validator="pytest",
            status="passed",
            exit_code=0,
            summary="42 passed",
        )
        detail = build_ticket_projection(self.db_path, "AT-101")
        assert detail is not None
        validators = {item["validator"]: item["status"] for item in detail.validators}
        self.assertEqual(validators["pytest"], "passed")

    def test_detail_payload_contains_no_percentage_or_eta(self) -> None:
        self.add_task("AT-101", "implementing")
        payload = projection_to_dict(build_ticket_projection(self.db_path, "AT-101"))
        self.assertEqual(find_progress_estimates(payload), ())


class AttemptScopedDetailTests(ProjectionTestCase):
    """§14.0 — latest Attempt by default, earlier Attempts stay viewable."""

    def _two_attempts(self) -> tuple[str, str]:
        self.add_task("AT-101", "implementing")
        first = self.attempts.create_attempt("AT-101").attempt_id
        self.progress.record_step(
            attempt_id=first, step="Validator", status="failed"
        )
        self.attempts.close_attempt(
            first,
            status="validation_failed",
            reason_code="validators_failed",
            actor="test",
        )
        second = self.attempts.create_attempt("AT-101").attempt_id
        self.progress.record_step(
            attempt_id=second, step="Validator", status="running"
        )
        return first, second

    def test_default_selects_the_latest_attempt(self) -> None:
        _first, second = self._two_attempts()
        detail = build_ticket_projection(self.db_path, "AT-101")
        assert detail is not None
        self.assertEqual(detail.selected_attempt_id, second)
        statuses = {step.name: step.status for step in detail.ticket.steps}
        self.assertEqual(statuses["Validator"], "running")

    def test_earlier_attempt_remains_viewable(self) -> None:
        first, _second = self._two_attempts()
        detail = build_ticket_projection(self.db_path, "AT-101", attempt_id=first)
        assert detail is not None
        self.assertEqual(detail.selected_attempt_id, first)
        statuses = {step.name: step.status for step in detail.ticket.steps}
        self.assertEqual(statuses["Validator"], "failed")

    def test_detail_lists_every_attempt_oldest_first(self) -> None:
        first, second = self._two_attempts()
        detail = build_ticket_projection(self.db_path, "AT-101")
        assert detail is not None
        self.assertEqual(
            [item.attempt_id for item in detail.attempts], [first, second]
        )
        self.assertEqual([item.attempt_number for item in detail.attempts], [1, 2])

    def test_unknown_attempt_id_falls_back_to_latest_without_error(self) -> None:
        _first, second = self._two_attempts()
        detail = build_ticket_projection(
            self.db_path, "AT-101", attempt_id="attempt-missing"
        )
        assert detail is not None
        self.assertEqual(detail.selected_attempt_id, second)


class PrFieldsAreReadOnlyAndOptionalTests(ProjectionTestCase):
    """§32.1 — Step 2 is the sole writer; every field may be absent or null."""

    def test_pr_field_names_match_the_spec_list(self) -> None:
        self.assertEqual(
            PR_FIELD_NAMES,
            (
                "pr_number",
                "pr_url",
                "pr_state",
                "pr_merged",
                "pr_head_sha",
                "merge_commit_sha",
                "review_decision",
                "ci_status",
                "integrated_base_sha",
                "reintegration_count",
                "reintegration_required",
                "pr_last_polled_at",
            ),
        )

    def test_projection_renders_when_the_columns_do_not_exist_yet(self) -> None:
        self.add_task("AT-098", "waiting_approval")
        ticket = build_board_projection(self.db_path).find("AT-098")
        assert ticket is not None
        self.assertFalse(ticket.pr.available)
        display = ticket.pr.display()
        for name in PR_FIELD_NAMES:
            with self.subTest(field=name):
                self.assertEqual(display[name], DASH)

    def test_projection_renders_dash_for_null_columns(self) -> None:
        self.add_task("AT-098", "waiting_approval")
        self.add_pr_columns()
        ticket = build_board_projection(self.db_path).find("AT-098")
        assert ticket is not None
        self.assertTrue(ticket.pr.available)
        self.assertIsNone(ticket.pr.pr_number)
        self.assertEqual(ticket.pr.display()["pr_number"], DASH)

    def test_projection_renders_persisted_pr_identity(self) -> None:
        self.add_task("AT-098", "waiting_approval")
        self.add_pr_columns(
            **{
                "AT_098": {
                    "pr_number": 42,
                    "pr_url": "https://github.com/o/r/pull/42",
                    "pr_state": "open",
                    "review_decision": "approved",
                    "ci_status": "success",
                    "integrated_base_sha": "abc123",
                    "reintegration_count": 2,
                }
            }
        )
        ticket = build_board_projection(self.db_path).find("AT-098")
        assert ticket is not None
        self.assertEqual(ticket.pr.pr_number, 42)
        self.assertEqual(ticket.pr.pr_url, "https://github.com/o/r/pull/42")
        self.assertEqual(ticket.pr.pr_state, "open")
        self.assertEqual(ticket.pr.review_decision, "approved")
        self.assertEqual(ticket.pr.ci_status, "success")
        self.assertEqual(ticket.pr.integrated_base_sha, "abc123")
        self.assertEqual(ticket.pr.reintegration_count, 2)
        self.assertEqual(ticket.pr.display()["pr_number"], "42")

    def test_building_a_projection_creates_no_pr_columns(self) -> None:
        self.add_task("AT-098", "waiting_approval")
        build_board_projection(self.db_path)
        build_ticket_projection(self.db_path, "AT-098")
        with sqlite3.connect(self.db_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        for name in PR_FIELD_NAMES:
            with self.subTest(field=name):
                self.assertNotIn(name, columns)

    def test_partial_pr_columns_do_not_break_the_render(self) -> None:
        self.add_task("AT-098", "waiting_approval")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("ALTER TABLE tasks ADD COLUMN pr_number INTEGER")
            conn.execute(
                "UPDATE tasks SET pr_number = 7 WHERE task_key = 'AT-098'"
            )
        ticket = build_board_projection(self.db_path).find("AT-098")
        assert ticket is not None
        self.assertEqual(ticket.pr.pr_number, 7)
        self.assertIsNone(ticket.pr.pr_url)
        self.assertEqual(ticket.pr.display()["pr_url"], DASH)

    def test_reviewer_hints_come_only_from_persisted_state(self) -> None:
        self.add_task("AT-098", "waiting_approval")
        detail = build_ticket_projection(self.db_path, "AT-098")
        assert detail is not None
        self.assertEqual(detail.reviewer_hints, ())

        self.add_pr_columns(**{"AT_098": {"reintegration_count": 3}})
        detail = build_ticket_projection(self.db_path, "AT-098")
        assert detail is not None
        self.assertTrue(
            any("Re-integrated 3 times" in hint for hint in detail.reviewer_hints),
            detail.reviewer_hints,
        )


class GithubCiIsNotALifecycleAuthorityTests(ProjectionTestCase):
    """§43.27 and §44 — a red CI status changes no lifecycle field."""

    def _lifecycle_fingerprint(self) -> tuple[object, ...]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            tasks = conn.execute(
                "SELECT task_key, status, active_attempt_id, final_outcome, closed_at "
                "FROM tasks ORDER BY task_key"
            ).fetchall()
            attempts = conn.execute(
                "SELECT attempt_id, status, is_active FROM attempts ORDER BY attempt_id"
            ).fetchall()
            lifecycle_events = conn.execute(
                "SELECT COUNT(*) FROM lifecycle_events"
            ).fetchone()[0]
        return (
            tuple(tuple(row) for row in tasks),
            tuple(tuple(row) for row in attempts),
            lifecycle_events,
        )

    def test_red_ci_leaves_the_ticket_in_needs_review(self) -> None:
        self.add_task("AT-098", "waiting_approval")
        self.add_pr_columns(
            **{"AT_098": {"pr_number": 42, "pr_state": "open", "ci_status": "failure"}}
        )
        before = self._lifecycle_fingerprint()

        projection = build_board_projection(self.db_path)
        ticket = projection.find("AT-098")
        assert ticket is not None

        # Still in the review section, still read as needs-review.
        self.assertEqual(ticket.section, BOARD_SECTION_READY_FOR_REVIEW)
        self.assertEqual(ticket.status, "waiting_approval")
        self.assertEqual(ticket.pr.ci_status, "failure")
        # And nothing about lifecycle moved.
        self.assertEqual(self._lifecycle_fingerprint(), before)

    def test_red_ci_ticket_is_not_marked_blocked_or_failed(self) -> None:
        self.add_task("AT-098", "waiting_approval")
        self.add_pr_columns(**{"AT_098": {"ci_status": "failure"}})
        ticket = build_board_projection(self.db_path).find("AT-098")
        assert ticket is not None
        self.assertFalse(ticket.blocked)
        self.assertFalse(ticket.paused)
        self.assertFalse(ticket.running)

    def test_detail_projection_with_red_ci_writes_nothing(self) -> None:
        self.add_task("AT-098", "waiting_approval")
        self.add_pr_columns(**{"AT_098": {"ci_status": "failure"}})
        before = self._lifecycle_fingerprint()
        build_ticket_projection(self.db_path, "AT-098")
        self.assertEqual(self._lifecycle_fingerprint(), before)

    def test_ci_status_is_rendered_separately_from_taskflow_validators(self) -> None:
        # §30 — the two systems stay separated in the payload.
        self.add_task("AT-098", "waiting_approval")
        self.tasks.record_validation_result(
            "AT-098", validator="pytest", status="passed", exit_code=0
        )
        self.add_pr_columns(**{"AT_098": {"ci_status": "failure"}})
        detail = build_ticket_projection(self.db_path, "AT-098")
        assert detail is not None
        self.assertEqual(detail.ticket.pr.ci_status, "failure")
        self.assertEqual(
            [item["status"] for item in detail.validators], ["passed"]
        )


class ProjectionIsReadOnlyTests(ProjectionTestCase):
    """§44 — the projection is never an independent source of truth."""

    def test_building_projections_does_not_change_task_state(self) -> None:
        self.add_task("AT-101", "implementing")
        self.add_task("AT-109", "queued")
        with sqlite3.connect(self.db_path) as conn:
            before = conn.execute(
                "SELECT task_key, status, updated_at FROM tasks ORDER BY task_key"
            ).fetchall()

        build_board_projection(self.db_path)
        build_ticket_projection(self.db_path, "AT-101")

        with sqlite3.connect(self.db_path) as conn:
            after = conn.execute(
                "SELECT task_key, status, updated_at FROM tasks ORDER BY task_key"
            ).fetchall()
        self.assertEqual(after, before)

    def test_projection_status_always_matches_persisted_status(self) -> None:
        self.add_task("AT-101", "implementing")
        projection = build_board_projection(self.db_path)
        ticket = projection.find("AT-101")
        assert ticket is not None
        with sqlite3.connect(self.db_path) as conn:
            persisted = conn.execute(
                "SELECT status FROM tasks WHERE task_key = 'AT-101'"
            ).fetchone()[0]
        self.assertEqual(ticket.status, persisted)

    def test_projection_to_dict_is_json_safe(self) -> None:
        import json

        self.add_task("AT-101", "implementing")
        self.add_worktree("AT-101", "task/AT-101")
        payload = projection_to_dict(build_board_projection(self.db_path))
        json.dumps(payload)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
