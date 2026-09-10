"""Source-level tests for the Step 3 Mission Control live surfaces.

Acceptance gate for the UI half of §43.11, §43.5, §43.15/§43.16:

* the live board renders the five §16 sections
* the live Ticket page renders the §17 fields
* BLOCKED / PAUSED render in their own sections with their blocker and are
  never presented as running or actionable-as-executable
* validator evidence and PR identity render read-only, with ``—`` for nulls
* reconnect follows §15.1 — a fresh snapshot, no ``Last-Event-ID``, no replay
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def strip_comments(source: str) -> str:
    """Drop block comments and whole-line ``//`` comments.

    Used so a negative assertion checks what the component *does*, not what
    its documentation mentions. URLs are never mangled because only lines that
    start with ``//`` are removed.
    """
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return "\n".join(
        line
        for line in without_blocks.splitlines()
        if not line.lstrip().startswith("//")
    )


FRONTEND = REPO_ROOT / "mission-control"

REALTIME_LIB = FRONTEND / "lib" / "realtime.ts"
STEP_LIST = FRONTEND / "components" / "ExecutionStepList.tsx"
LIVE_BOARD = FRONTEND / "components" / "LiveBoard.tsx"
LIVE_TICKET = FRONTEND / "components" / "LiveTicketPanel.tsx"
LIVE_PAGE = FRONTEND / "app" / "live" / "page.tsx"
API = FRONTEND / "lib" / "api.ts"
TYPES = FRONTEND / "lib" / "types.ts"
TASK_PAGE = FRONTEND / "app" / "tasks" / "[taskKey]" / "page.tsx"


class RealtimeFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.realtime_lib = REALTIME_LIB.read_text(encoding="utf-8")
        cls.step_list = STEP_LIST.read_text(encoding="utf-8")
        cls.live_board = LIVE_BOARD.read_text(encoding="utf-8")
        cls.live_ticket = LIVE_TICKET.read_text(encoding="utf-8")
        cls.live_page = LIVE_PAGE.read_text(encoding="utf-8")
        cls.api = API.read_text(encoding="utf-8")
        cls.types = TYPES.read_text(encoding="utf-8")
        cls.task_page = TASK_PAGE.read_text(encoding="utf-8")
        cls.new_surface = "\n".join(
            [
                cls.realtime_lib,
                cls.step_list,
                cls.live_board,
                cls.live_ticket,
                cls.live_page,
            ]
        )


class LiveBoardTests(RealtimeFrontendTests):
    def test_board_renders_the_five_spec_sections(self) -> None:
        for label in ("RUNNING", "READY", "BLOCKED", "PAUSED", "READY FOR REVIEW"):
            with self.subTest(label=label):
                self.assertIn(label, self.realtime_lib)

    def test_board_section_order_matches_spec_section_16(self) -> None:
        order = [
            self.realtime_lib.index(f'"{label}"')
            for label in ("RUNNING", "READY", "BLOCKED", "PAUSED", "READY FOR REVIEW")
        ]
        self.assertEqual(order, sorted(order))

    def test_board_renders_current_phase_and_activity(self) -> None:
        self.assertIn("current_phase", self.live_board)
        self.assertIn("current_activity", self.live_board)

    def test_board_renders_blocker_hint_for_blocked_tickets(self) -> None:
        self.assertIn("blocker_hint", self.live_board)

    def test_board_never_labels_a_non_running_ticket_as_running(self) -> None:
        # The running badge is gated on the server-computed flag only.
        self.assertIn("ticket.running", self.live_board)

    def test_board_does_not_render_execution_actions(self) -> None:
        for label in ("Start", "Dispatch", "Run now", "Retry", "Approve", "Merge"):
            with self.subTest(label=label):
                self.assertNotIn(f">{label}<", self.new_surface)

    def test_live_page_mounts_the_live_board(self) -> None:
        self.assertIn("LiveBoard", self.live_page)
        self.assertIn("getRealtimeBoard", self.live_page)

    def test_live_page_is_reachable_from_the_dashboard(self) -> None:
        board = (FRONTEND / "components" / "TaskBoard.tsx").read_text(
            encoding="utf-8"
        )
        self.assertIn('href="/live"', board)


class ExecutionStepListTests(RealtimeFrontendTests):
    def test_step_list_renders_every_first_level_step(self) -> None:
        for step in (
            "Prepare",
            "Scout",
            "Planner",
            "Implementer",
            "Reviewer",
            "Validator",
            "Integration",
        ):
            with self.subTest(step=step):
                self.assertIn(step, self.realtime_lib)

    def test_step_list_uses_glyphs_not_numbers(self) -> None:
        for glyph in ("✓", "●", "○"):
            with self.subTest(glyph=glyph):
                self.assertIn(glyph, self.realtime_lib)

    def test_step_list_covers_every_step_status(self) -> None:
        for status in ("pending", "running", "passed", "failed", "blocked"):
            with self.subTest(status=status):
                self.assertIn(status, self.realtime_lib)


class SseReconnectTests(RealtimeFrontendTests):
    def test_board_subscribes_with_event_source(self) -> None:
        self.assertIn("EventSource", self.live_board)
        self.assertIn("snapshot", self.live_board)
        self.assertIn("update", self.live_board)

    def test_ticket_panel_subscribes_with_event_source(self) -> None:
        self.assertIn("EventSource", self.live_ticket)

    def test_frontend_never_sends_or_stores_a_last_event_id(self) -> None:
        for name, source in (
            ("realtime lib", self.realtime_lib),
            ("live board", self.live_board),
            ("live ticket", self.live_ticket),
            ("api", self.api),
        ):
            with self.subTest(source=name):
                code = strip_comments(source)
                self.assertNotIn("Last-Event-ID", code)
                self.assertNotIn("lastEventId", code)

    def test_reconnect_replaces_state_with_the_new_snapshot(self) -> None:
        # §15.1 — snapshot replaces, it never merges into a replayed log.
        self.assertIn("setBoard(", self.live_board)
        self.assertNotIn("prev.concat", self.live_board)
        self.assertNotIn("appendEvent", self.live_board)


class TicketPageTests(RealtimeFrontendTests):
    def test_ticket_panel_renders_the_section_17_fields(self) -> None:
        for label in (
            "Repository",
            "Priority",
            "Status",
            "Branch",
            "Worktree",
            "Execution",
            "Current activity",
            "Artifacts",
        ):
            with self.subTest(label=label):
                self.assertIn(label, self.live_ticket)

    def test_ticket_panel_renders_read_only_pr_identity(self) -> None:
        for label in ("PR", "Review", "CI", "Integrated base"):
            with self.subTest(label=label):
                self.assertIn(label, self.live_ticket)
        self.assertIn("pr.display", self.live_ticket)

    def test_ticket_panel_renders_taskflow_validators_separately_from_ci(self) -> None:
        self.assertIn("Taskflow validators", self.live_ticket)
        self.assertIn("GitHub CI", self.live_ticket)

    def test_ticket_panel_states_ci_is_not_a_lifecycle_authority(self) -> None:
        self.assertIn("not a Taskflow lifecycle authority", self.live_ticket)

    def test_ticket_panel_renders_dash_for_missing_values(self) -> None:
        self.assertIn("—", self.live_ticket)

    def test_ticket_panel_lets_earlier_attempts_stay_viewable(self) -> None:
        self.assertIn("attempt", self.live_ticket.lower())
        self.assertIn("selected_attempt_id", self.live_ticket)

    def test_task_detail_page_mounts_the_live_ticket_panel(self) -> None:
        self.assertIn("LiveTicketPanel", self.task_page)


class ReadOnlyApiClientTests(RealtimeFrontendTests):
    def test_api_client_exposes_read_only_realtime_helpers(self) -> None:
        self.assertIn("getRealtimeBoard", self.api)
        self.assertIn("getTicketRealtime", self.api)
        self.assertIn("realtimeStreamUrl", self.api)
        self.assertIn("ticketRealtimeStreamUrl", self.api)

    def test_types_declare_the_realtime_payloads(self) -> None:
        for name in (
            "RuntimeStepStatus",
            "RuntimeObservedStep",
            "BoardTicket",
            "BoardSection",
            "BoardProjection",
            "TicketProjection",
            "TicketPrView",
        ):
            with self.subTest(name=name):
                self.assertIn(name, self.types)

    def test_realtime_types_treat_every_pr_field_as_optional(self) -> None:
        for field in (
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
        ):
            with self.subTest(field=field):
                self.assertRegex(self.types, rf"{field}\?:")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
