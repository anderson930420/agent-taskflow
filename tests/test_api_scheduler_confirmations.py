"""Tests for the Phase K3 read-only scheduler confirmation readback API."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app
from agent_taskflow.models import TaskRecord
from agent_taskflow.scheduler_candidate_proposals import (
    SchedulerCandidateProposalRequest,
    create_scheduler_proposal_from_candidate,
)
from agent_taskflow.scheduler_confirmation_from_proposal import (
    SchedulerConfirmationFromProposalRequest,
    create_scheduler_confirmation_from_proposal,
)
from agent_taskflow.store import TaskMirrorStore


FORBIDDEN_ARTIFACT_TYPES = (
    "scheduler_confirmation_verifier_report",
    "verifier_report",
    "intake_runner_handoff",
    "runtime_handoff_execution",
    "validation_result",
    "approval_decision",
    "merge_recorded",
    "cleanup",
)

FORBIDDEN_EVENT_TYPES = (
    "scheduler_confirmation_verifier_report",
    "verifier_report",
    "intake_runner_handoff_created",
    "runtime_preflight_finished",
    "runtime_execution_started",
    "runtime_execution_finished",
    "executor_run_started",
    "executor_run_finished",
    "validation_result",
    "approval_decision",
    "merge_recorded",
    "cleanup",
)

FORBIDDEN_PAYLOAD_MARKERS = (
    "scheduler_confirmation_verifier_report",
    "intake_runner_handoff_created",
    "runtime_execution_started",
    "runtime_execution_finished",
    "approved_task_runner",
    "executor_run_started",
    "executor_run_finished",
    "validation_result",
)


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

    def _create_proposal(self, task_key: str) -> dict[str, Any]:
        self._seed_task(task_key)
        payload = create_scheduler_proposal_from_candidate(
            SchedulerCandidateProposalRequest(
                task_key=task_key,
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                dry_run=False,
                confirm_create_proposal=True,
            )
        )
        self.assertTrue(payload["ok"], payload)
        return payload["proposal"]

    def _create_confirmation(self, task_key: str) -> dict[str, Any]:
        proposal = self._create_proposal(task_key)
        result = create_scheduler_confirmation_from_proposal(
            SchedulerConfirmationFromProposalRequest(
                db_path=self.db_path,
                artifact_root=self.artifact_root,
                task_key=task_key,
                proposal_item_id=proposal["proposal_item_id"],
                proposal_hash=proposal["proposal_hash"],
                proposal_id=proposal["proposal_id"],
                item_hash=proposal["item_hash"],
                recommended_command_kind=proposal["recommended_command_kind"],
                proposal_artifact_path=Path(proposal["proposal_artifact_path"]),
                dry_run=False,
                confirm_create_confirmation=True,
                operator="test-operator",
                operator_note="K3 API test",
            )
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "created")
        return result["confirmation"]

    def _db_counts(self) -> dict[str, int]:
        with sqlite3.connect(self.db_path) as conn:
            return {
                "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                "events": conn.execute(
                    "SELECT COUNT(*) FROM task_events"
                ).fetchone()[0],
                "artifacts": conn.execute(
                    "SELECT COUNT(*) FROM task_artifacts"
                ).fetchone()[0],
                "worktrees": conn.execute(
                    "SELECT COUNT(*) FROM task_worktrees"
                ).fetchone()[0],
            }

    def _forbidden_side_effect_counts(self) -> dict[str, int]:
        with sqlite3.connect(self.db_path) as conn:
            artifact_placeholders = ",".join("?" for _ in FORBIDDEN_ARTIFACT_TYPES)
            event_placeholders = ",".join("?" for _ in FORBIDDEN_EVENT_TYPES)
            marker_clause = " OR ".join(
                "payload_json LIKE ?" for _ in FORBIDDEN_PAYLOAD_MARKERS
            )
            artifacts = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM task_artifacts
                WHERE artifact_type IN ({artifact_placeholders})
                """,
                FORBIDDEN_ARTIFACT_TYPES,
            ).fetchone()[0]
            events = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM task_events
                WHERE event_type IN ({event_placeholders})
                """,
                FORBIDDEN_EVENT_TYPES,
            ).fetchone()[0]
            payload_markers = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM task_events
                WHERE payload_json IS NOT NULL
                  AND ({marker_clause})
                """,
                tuple(f"%{marker}%" for marker in FORBIDDEN_PAYLOAD_MARKERS),
            ).fetchone()[0]
        return {
            "artifacts": artifacts,
            "events": events,
            "payload_markers": payload_markers,
        }

    def _all_dict_keys(self, value: Any) -> set[str]:
        keys: set[str] = set()
        if isinstance(value, dict):
            for key, item in value.items():
                keys.add(str(key))
                keys.update(self._all_dict_keys(item))
        elif isinstance(value, list):
            for item in value:
                keys.update(self._all_dict_keys(item))
        return keys

    def test_global_confirmation_readback_api(self) -> None:
        confirmation = self._create_confirmation("AT-K3-API-001")

        response = self.client.get("/api/scheduler/confirmations")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "read_only")
        self.assertEqual(payload["count"], 1)
        item = payload["items"][0]
        self.assertEqual(item["task_key"], "AT-K3-API-001")
        self.assertEqual(item["confirmation_id"], confirmation["confirmation_id"])
        self.assertEqual(item["proposal_id"], confirmation["proposal_id"])
        self.assertEqual(item["proposal_hash"], confirmation["proposal_hash"])
        self.assertEqual(
            item["proposal_item_id"], confirmation["proposal_item_id"]
        )
        self.assertEqual(item["item_hash"], confirmation["item_hash"])
        self.assertEqual(
            item["recommended_command_kind"],
            confirmation["recommended_command_kind"],
        )
        self.assertEqual(item["artifact_path"], confirmation["artifact_path"])
        self.assertTrue(item["not_execution_permission"])
        self.assertTrue(item["not_verifier_report"])
        self.assertTrue(item["not_handoff"])
        self.assertTrue(item["not_runtime"])
        self.assertTrue(item["requires_next_gate"])
        self.assertTrue(payload["safety"]["read_only"])
        self.assertTrue(payload["safety"]["not_execution_permission"])

    def test_task_confirmation_readback_api(self) -> None:
        confirmation = self._create_confirmation("AT-K3-API-002")

        response = self.client.get(
            "/api/tasks/AT-K3-API-002/scheduler-confirmations"
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        item = payload["items"][0]
        self.assertEqual(item["confirmation_id"], confirmation["confirmation_id"])
        self.assertEqual(item["proposal_item_id"], confirmation["proposal_item_id"])

    def test_task_confirmation_readback_unknown_task_404(self) -> None:
        response = self.client.get(
            "/api/tasks/AT-K3-API-MISSING/scheduler-confirmations"
        )

        self.assertEqual(response.status_code, 404)

    def test_global_confirmation_readback_task_filter(self) -> None:
        self._create_confirmation("AT-K3-API-003")
        self._create_confirmation("AT-K3-API-004")

        response = self.client.get(
            "/api/scheduler/confirmations",
            params={"task_key": "AT-K3-API-004"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["filters"]["task_key"], "AT-K3-API-004")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["task_key"], "AT-K3-API-004")

    def test_limit_zero(self) -> None:
        self._create_confirmation("AT-K3-API-005")

        response = self.client.get(
            "/api/scheduler/confirmations",
            params={"limit": 0},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["items"], [])

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

    def test_get_calls_do_not_mutate_db(self) -> None:
        self._create_confirmation("AT-K3-API-007")
        before = self._db_counts()

        for _ in range(3):
            self.client.get("/api/scheduler/confirmations")
            self.client.get("/api/tasks/AT-K3-API-007/scheduler-confirmations")

        self.assertEqual(self._db_counts(), before)

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

    def test_get_does_not_create_forbidden_artifacts_or_events(self) -> None:
        self._create_confirmation("AT-K3-API-009")
        before_forbidden = self._forbidden_side_effect_counts()

        self.client.get("/api/scheduler/confirmations")
        self.client.get("/api/tasks/AT-K3-API-009/scheduler-confirmations")

        after_forbidden = self._forbidden_side_effect_counts()
        self.assertEqual(after_forbidden, before_forbidden)
        self.assertEqual(after_forbidden["artifacts"], 0)
        self.assertEqual(after_forbidden["events"], 0)
        self.assertEqual(after_forbidden["payload_markers"], 0)

    def test_response_says_not_execution_permission(self) -> None:
        self._create_confirmation("AT-K3-API-010")

        payload = self.client.get("/api/scheduler/confirmations").json()

        self.assertTrue(payload["safety"]["not_execution_permission"])
        self.assertTrue(payload["items"][0]["not_execution_permission"])
        self.assertTrue(payload["items"][0]["safety"]["not_execution_permission"])
        self.assertIn(
            "not execution permission", payload["readback_note"].lower()
        )

    def test_response_says_not_verifier_report_not_handoff_not_runtime(self) -> None:
        self._create_confirmation("AT-K3-API-011")

        payload = self.client.get("/api/scheduler/confirmations").json()

        self.assertTrue(payload["safety"]["not_verifier_report"])
        self.assertTrue(payload["safety"]["not_handoff"])
        self.assertTrue(payload["safety"]["not_runtime"])
        item = payload["items"][0]
        self.assertTrue(item["not_verifier_report"])
        self.assertTrue(item["not_handoff"])
        self.assertTrue(item["not_runtime"])
        self.assertTrue(item["requires_next_gate"])

    def test_api_does_not_expose_creation_action_fields(self) -> None:
        self._create_confirmation("AT-K3-API-012")

        payload = self.client.get("/api/scheduler/confirmations").json()
        keys = self._all_dict_keys(payload)

        for forbidden_key in (
            "confirm",
            "run",
            "execute",
            "create_handoff",
            "approve",
            "merge",
            "cleanup",
            "action",
            "actions",
        ):
            self.assertNotIn(forbidden_key, keys)

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
