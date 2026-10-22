"""Tests for the Phase K3 read-only scheduler confirmation readback API."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app
from agent_taskflow.ticket_fields_schema import migrate_ticket_fields
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


class SchedulerConfirmationsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.repo_path = self.root / "repo"
        self.artifact_root = self.root / "artifacts"
        self.repo_path.mkdir()
        self.artifact_root.mkdir()

        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

        # The API fails closed until the explicit Step 1 migration has run.
        migrate_ticket_fields(self.db_path)
        self.client_context = TestClient(create_app(self.db_path))
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.tmp.cleanup()

    def _seed_task(self, task_key: str, *, status: str = "queued") -> None:
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title=f"API K3 readback {task_key}",
                status=status,
                repo_path=self.repo_path,
                artifact_dir=artifact_dir,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )

    def test_task_confirmation_readback_unknown_task_404(self) -> None:
        response = self.client.get(
            "/api/tasks/AT-K3-API-MISSING/scheduler-confirmations"
        )

        self.assertEqual(response.status_code, 404)

    def test_invalid_limit_rejected(self) -> None:
        response = self.client.get(
            "/api/scheduler/confirmations",
            params={"limit": -1},
        )

        self.assertEqual(response.status_code, 422)

    def test_task_endpoint_invalid_limit_rejected(self) -> None:
        self._seed_task("AT-K3-API-006")

        response = self.client.get(
            "/api/tasks/AT-K3-API-006/scheduler-confirmations",
            params={"limit": -1},
        )

        self.assertEqual(response.status_code, 422)

    def test_post_not_allowed_on_global_route(self) -> None:
        response = self.client.post("/api/scheduler/confirmations", json={})

        self.assertEqual(response.status_code, 405)

    def test_patch_not_allowed_on_global_route(self) -> None:
        response = self.client.patch("/api/scheduler/confirmations", json={})

        self.assertEqual(response.status_code, 405)

    def test_delete_not_allowed_on_global_route(self) -> None:
        response = self.client.delete("/api/scheduler/confirmations")

        self.assertEqual(response.status_code, 405)

    def test_post_not_allowed_on_task_route(self) -> None:
        self._seed_task("AT-K3-API-008")

        response = self.client.post(
            "/api/tasks/AT-K3-API-008/scheduler-confirmations",
            json={},
        )

        self.assertEqual(response.status_code, 405)

    def test_api_source_has_no_confirmation_creation_endpoint(self) -> None:
        main_path = (
            Path(__file__).resolve().parents[1]
            / "agent_taskflow"
            / "api"
            / "main.py"
        )
        text = main_path.read_text(encoding="utf-8")

        # The K3 readback API must not expose any mutating confirmation route.
        forbidden_route_decorators = (
            '@app.post("/api/scheduler/confirmations")',
            "@app.post('/api/scheduler/confirmations')",
            '@app.post("/api/tasks/{task_key}/scheduler-confirmations")',
            "@app.post('/api/tasks/{task_key}/scheduler-confirmations')",
            '@app.patch("/api/scheduler/confirmations")',
            "@app.patch('/api/scheduler/confirmations')",
            '@app.delete("/api/scheduler/confirmations")',
            "@app.delete('/api/scheduler/confirmations')",
        )
        for needle in forbidden_route_decorators:
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)

        # The confirmation creation helper must not be imported into the API.
        self.assertNotIn(
            "create_scheduler_confirmation_from_proposal", text
        )
        self.assertNotIn(
            "from agent_taskflow.scheduler_confirmation_from_proposal", text
        )

        # The confirmation readback handler section must not invoke any runtime,
        # executor, validator, runner, verifier, or handoff write surface.
        confirmation_section = self._extract_confirmation_section(text)
        for needle in (
            "approved_task_runner(",
            "approved_task_runner.",
            "intake_runner_handoff",
            "executor_run_started",
            "validation_result",
            "runtime_execution_started",
            "create_verifier_report",
            "record_task_event(",
            "record_task_artifact(",
            "record_approval_decision(",
            "update_task_status(",
            "dispatcher.dispatch_task",
        ):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, confirmation_section)

    def _extract_confirmation_section(self, text: str) -> str:
        """Return the source of the two K3 confirmation readback handlers."""
        section_lines: list[str] = []
        in_section = False
        section_indent: int | None = None
        for line in text.splitlines():
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            if (
                "/api/scheduler/confirmations" in line
                or "/api/tasks/{task_key}/scheduler-confirmations" in line
            ) and stripped.startswith("@app.get"):
                in_section = True
                section_indent = indent
                section_lines.append(line)
                continue
            if in_section:
                if stripped == "" or indent > (section_indent or 0):
                    section_lines.append(line)
                    continue
                if stripped.startswith("@app."):
                    if (
                        "/api/scheduler/confirmations" in line
                        or "/api/tasks/{task_key}/scheduler-confirmations" in line
                    ) and stripped.startswith("@app.get"):
                        section_lines.append(line)
                        continue
                    in_section = False
                else:
                    if indent <= (section_indent or 0):
                        in_section = False
                        continue
                    section_lines.append(line)
        return "\n".join(section_lines)


if __name__ == "__main__":
    unittest.main()
