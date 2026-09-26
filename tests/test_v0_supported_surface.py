"""docs/v0-supported-surface.md lists every entrypoint once and pins ACTIVE_V0.

RULINGS 74/80/81 (V0 Scope Freeze, batch 1): V0 has one execution path. A new
script, API route, console script or runtime installer fails this test until it
gets a row, a label and a Ticket-guard decision in the surface table.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
import tempfile
import tomllib
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SURFACE_DOC = REPO_ROOT / "docs" / "v0-supported-surface.md"

KINDS = {"script", "api", "cli", "installer"}
LABELS = {"ACTIVE_V0", "FROZEN", "LEGACY"}
TICKET_GUARDS = {"n/a", "d1_refusal", "status_gated", "unsupported_in_v0", "owner_decision"}

# The §6.1 "one execution path" pin. Adding an ACTIVE_V0 entrypoint is a scope
# decision: change the doc, this set and the owner ruling together.
ACTIVE_V0_HAPPY_PATH = {
    "POST /api/tickets",
    "scripts/run_parallel_scheduler_tick.py",
    "scripts/run_integration_tick.py",
}
ACTIVE_V0_OPERATOR = {
    "scripts/run_api.py",
    "scripts/runtime_control.py",
    "scripts/reset_task_status.py",
    "scripts/reap_stale_runtime.py",
    "scripts/terminate_executor_process.py",
    "scripts/migrate_ticket_fields.py",
    "scripts/migrate_ticket_worktree_resources.py",
    "scripts/migrate_runtime_progress.py",
    "scripts/migrate_runtime_admission.py",
    "scripts/migrate_canonical_runtime_admission.py",
    "scripts/migrate_task_attempt_lifecycle.py",
    "scripts/migrate_attempt_resources.py",
    "scripts/migrate_lifecycle_control.py",
    "scripts/migrate_project_class_controls.py",
    "scripts/migrate_executor_process_lifecycle.py",
    "scripts/migrate_reset_lineage.py",
    "scripts/migrate_validator_process_lifecycle.py",
}
ACTIVE_V0_READS = {
    "GET /api/tickets",
    "GET /api/tickets/{task_key}",
    "GET /api/repositories",
    "GET /health",
    "GET /api/realtime/board",
    "GET /api/realtime/stream",
    "GET /api/tasks/{task_key}/realtime",
    "GET /api/tasks/{task_key}/realtime/stream",
    "GET /api/tasks/{task_key}/attempts",
    "GET /api/tasks/{task_key}/runtime-audits",
    "GET /api/tasks/{task_key}/evidence",
    "GET /api/tasks/{task_key}/review-evidence",
    "GET /api/tasks/{task_key}/validations",
    "GET /api/tasks/{task_key}/runs",
    "GET /api/tasks/{task_key}/artifacts",
    "GET /api/tasks/{task_key}/artifacts/{artifact_name}",
}
ACTIVE_V0_INSTALLERS = {
    "install_canonical_runtime_path",
    "install_attempt_scoped_runtime_path",
    "install_lifecycle_reason_compat",
    "install_lifecycle_runtime_path",
    "install_lifecycle_entrypoint_controls",
    "install_executor_process_reason_compat",
    "install_executor_process_runtime_path",
    "install_reset_runtime_path",
    "install_validator_process_reason_compat",
    "install_validator_process_runtime_path",
}

# G1-G14: the entrypoints (or the scripts over the module functions) that call
# agent_taskflow.v0_surface.require_supported_in_v0.
UNSUPPORTED_IN_V0 = {
    "POST /api/tasks/{task_key}/block",
    "POST /api/tasks/{task_key}/reject",
    "POST /api/tasks/{task_key}/prepare-workspace",
    "POST /api/tasks/{task_key}/approve",
    "POST /api/tasks/{task_key}/start",
    "scripts/archive_task_evidence_only.py",
    "scripts/push_task_branch.py",
    "scripts/record_existing_draft_pr.py",
    "scripts/confirm_local_cleanup.py",
    "scripts/confirm_remote_branch_cleanup.py",
    "scripts/prepare_task_workspace.py",
    "scripts/ingest_github_issue.py",
    "scripts/create_pi_smoke_task.py",
    "scripts/retry_advisory_evidence_transition.py",
}

_ROW = re.compile(r"^\|\s*`(?P<surface>[^`]+)`\s*\|(?P<rest>.*)\|\s*$")


def surface_rows() -> list[dict[str, str]]:
    text = SURFACE_DOC.read_text(encoding="utf-8")
    table = text.split("## Surface table", 1)[1]
    rows = []
    for line in table.splitlines():
        match = _ROW.match(line)
        if match is None:
            continue
        cells = [cell.strip() for cell in match.group("rest").split("|")]
        kind, label, guard = cells[0], cells[1], cells[2]
        note = "|".join(cells[3:]).strip()
        rows.append({
            "surface": match.group("surface"), "kind": kind, "label": label,
            "ticket_guard": guard, "note": note,
        })
    return rows


def app_routes() -> list[str]:
    from fastapi.routing import APIRoute

    from agent_taskflow.api.main import create_app

    with tempfile.TemporaryDirectory(prefix="v0-surface-") as tmp:
        app = create_app(Path(tmp) / "state.db")
    return [
        f"{method} {route.path}"
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in sorted(route.methods)
    ]


class V0SupportedSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = surface_rows()

    def surfaces(self, kind: str) -> list[str]:
        return [row["surface"] for row in self.rows if row["kind"] == kind]

    def assert_listed_once(self, kind: str, expected: list[str]) -> None:
        listed = self.surfaces(kind)
        duplicates = sorted(name for name, count in Counter(listed).items() if count > 1)
        self.assertEqual(duplicates, [], f"{kind} rows listed more than once")
        self.assertEqual(sorted(listed), sorted(set(expected)))
        self.assertEqual(len(expected), len(set(expected)))

    def test_every_script_is_listed_exactly_once(self) -> None:
        scripts = [f"scripts/{path.name}" for path in (REPO_ROOT / "scripts").glob("*.py")]
        self.assertGreater(len(scripts), 0)
        self.assert_listed_once("script", scripts)

    def test_every_api_route_is_listed_exactly_once(self) -> None:
        self.assert_listed_once("api", app_routes())

    def test_every_console_script_is_listed_exactly_once(self) -> None:
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assert_listed_once("cli", list(pyproject["project"]["scripts"]))

    def test_every_installer_call_is_listed_exactly_once(self) -> None:
        source = (REPO_ROOT / "agent_taskflow" / "__init__.py").read_text(encoding="utf-8")
        calls = re.findall(r"^(install_[a-z_]+)\(", source, flags=re.MULTILINE)
        self.assertEqual(len(calls), 11)
        self.assert_listed_once("installer", calls)

    def test_columns_use_the_closed_sets(self) -> None:
        self.assertGreater(len(self.rows), 0)
        for row in self.rows:
            with self.subTest(surface=row["surface"]):
                self.assertIn(row["kind"], KINDS)
                self.assertIn(row["label"], LABELS)
                self.assertIn(row["ticket_guard"], TICKET_GUARDS)
                self.assertTrue(row["note"])

    def test_active_v0_is_exactly_the_pinned_set(self) -> None:
        active = {row["surface"] for row in self.rows if row["label"] == "ACTIVE_V0"}
        self.assertEqual(
            active,
            ACTIVE_V0_HAPPY_PATH | ACTIVE_V0_OPERATOR | ACTIVE_V0_READS | ACTIVE_V0_INSTALLERS,
        )
        # No console script is on the V0 path.
        self.assertEqual(
            [row["surface"] for row in self.rows if row["kind"] == "cli" and row["label"] == "ACTIVE_V0"],
            [],
        )

    def test_unsupported_in_v0_rows_are_the_guarded_entrypoints(self) -> None:
        guarded = {row["surface"] for row in self.rows if row["ticket_guard"] == "unsupported_in_v0"}
        self.assertEqual(guarded, UNSUPPORTED_IN_V0)
        for row in self.rows:
            if row["surface"] in UNSUPPORTED_IN_V0:
                self.assertEqual(row["label"], "LEGACY", row["surface"])

    def test_the_doc_states_the_owner_rulings(self) -> None:
        text = SURFACE_DOC.read_text(encoding="utf-8")
        # RULINGS 81: the guards are the invariant, not the live DB's contents.
        self.assertIn("The entrypoint guards are the invariant", text)
        self.assertIn("migration-state evidence, not an invariant", text)
        # RULINGS 81: the only acceptable future Start-button design.
        self.assertIn(
            "Start button -> canonical domain command -> the Ticket enters a "
            "scheduler-claimable state -> the scheduler tick executes it",
            text,
        )
        self.assertIn("The HTTP endpoint never launches an executor.", text)
        kanban = [row for row in self.rows if row["surface"] == "scripts/kanban_accept_cleanup.py"]
        self.assertEqual(len(kanban), 1)
        self.assertEqual(kanban[0]["label"], "LEGACY")
        self.assertEqual(kanban[0]["ticket_guard"], "owner_decision")
        self.assertIn("FROZEN", kanban[0]["note"])


if __name__ == "__main__":
    unittest.main()
