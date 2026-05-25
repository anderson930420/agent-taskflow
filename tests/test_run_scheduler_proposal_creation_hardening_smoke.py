"""Tests for scripts/run_scheduler_proposal_creation_hardening_smoke.py."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app
from agent_taskflow.scheduler_proposal_readback import (
    list_task_scheduler_proposal_readbacks,
)
from agent_taskflow.scheduler_proposals import (
    PROPOSAL_ARTIFACT_TYPE,
    PROPOSAL_EVENT_TYPE,
)
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    REPO_ROOT
    / "scripts"
    / "run_scheduler_proposal_creation_hardening_smoke.py"
)


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "run_scheduler_proposal_creation_hardening_smoke",
        SCRIPT,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RunSchedulerProposalCreationHardeningSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.workspace_root = Path(cls.tmp.name)
        cls.smoke = _load_smoke_module()
        cls.summary = cls.smoke.run_smoke(
            workspace_root=cls.workspace_root,
            task_key="AT-J4-PROPOSAL-SMOKE-TEST",
        )
        cls.db_path = Path(cls.summary["db_path"])
        cls.store = TaskMirrorStore(cls.db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def _proposal_artifacts(self):
        return [
            artifact
            for artifact in self.store.list_task_artifacts(self.summary["task_key"])
            if artifact.artifact_type == PROPOSAL_ARTIFACT_TYPE
        ]

    def _proposal_events(self):
        return [
            event
            for event in self.store.list_task_events(self.summary["task_key"])
            if event.event_type == PROPOSAL_EVENT_TYPE
        ]

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

    def _forbidden_event_count(self, event_type: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                """
                SELECT COUNT(*)
                FROM task_events
                WHERE task_key = ?
                  AND event_type = ?
                """,
                (self.summary["task_key"], event_type),
            ).fetchone()[0]

    def _forbidden_artifact_count(self, artifact_type: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                """
                SELECT COUNT(*)
                FROM task_artifacts
                WHERE task_key = ?
                  AND artifact_type = ?
                """,
                (self.summary["task_key"], artifact_type),
            ).fetchone()[0]

    def _payload_marker_count(self, marker: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                """
                SELECT COUNT(*)
                FROM task_events
                WHERE task_key = ?
                  AND payload_json IS NOT NULL
                  AND payload_json LIKE ?
                """,
                (self.summary["task_key"], f"%{marker}%"),
            ).fetchone()[0]

    def test_run_smoke_succeeds_in_temp_workspace(self) -> None:
        self.assertTrue(self.summary["ok"])
        self.assertEqual(self.summary["task_key"], "AT-J4-PROPOSAL-SMOKE-TEST")
        self.assertTrue(self.db_path.is_file())
        self.assertTrue(Path(self.summary["artifact_root"]).is_dir())

    def test_run_smoke_creates_exactly_one_scheduler_proposal_artifact_row(self) -> None:
        self.assertEqual(len(self._proposal_artifacts()), 1)

    def test_run_smoke_creates_exactly_one_scheduler_proposal_created_event(self) -> None:
        self.assertEqual(len(self._proposal_events()), 1)

    def test_run_smoke_returns_proposal_identifiers_and_hashes(self) -> None:
        proposal = self.summary["proposal"]
        for key in (
            "proposal_id",
            "proposal_hash",
            "proposal_item_id",
            "item_hash",
        ):
            self.assertIsInstance(proposal[key], str)
            self.assertTrue(proposal[key])
        self.assertEqual(len(proposal["proposal_hash"]), 64)
        self.assertEqual(len(proposal["item_hash"]), 64)

    def test_proposal_artifact_exists_and_json_is_valid(self) -> None:
        artifact_path = Path(self.summary["proposal"]["artifact_path"])
        self.assertTrue(artifact_path.is_file())
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["proposal_hash"], self.summary["proposal"]["proposal_hash"])
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["task_key"], self.summary["task_key"])

    def test_j2_helper_readback_count_is_one(self) -> None:
        readback = list_task_scheduler_proposal_readbacks(
            self.store,
            self.summary["task_key"],
        )
        self.assertEqual(readback["count"], 1)
        self.assertEqual(self.summary["readbacks"]["helper_count"], 1)

    def test_j2_api_global_readback_count_is_one(self) -> None:
        with TestClient(create_app(self.db_path)) as client:
            response = client.get(
                "/api/scheduler/proposals",
                params={"task_key": self.summary["task_key"]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(self.summary["readbacks"]["api_global_count"], 1)

    def test_j2_api_task_readback_count_is_one(self) -> None:
        with TestClient(create_app(self.db_path)) as client:
            response = client.get(
                f"/api/tasks/{self.summary['task_key']}/scheduler-proposals"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(self.summary["readbacks"]["api_task_count"], 1)

    def test_repeated_j2_api_get_does_not_mutate_db_counts(self) -> None:
        before = self._db_counts()
        with TestClient(create_app(self.db_path)) as client:
            client.get(
                "/api/scheduler/proposals",
                params={"task_key": self.summary["task_key"]},
            )
            client.get(
                f"/api/tasks/{self.summary['task_key']}/scheduler-proposals"
            )
            client.get(
                "/api/scheduler/proposals",
                params={"task_key": self.summary["task_key"]},
            )
            client.get(
                f"/api/tasks/{self.summary['task_key']}/scheduler-proposals"
            )

        self.assertEqual(self._db_counts(), before)

    def test_safety_block_says_proposal_created_true(self) -> None:
        self.assertTrue(self.summary["safety"]["proposal_created"])

    def test_safety_block_says_downstream_actions_false(self) -> None:
        for key in (
            "confirmation_created",
            "verifier_report_created",
            "handoff_created",
            "runtime_started",
            "approved_task_runner_called",
            "executor_started",
            "validators_started",
            "github_mutated",
            "approved",
            "merged",
            "cleanup_performed",
        ):
            self.assertFalse(self.summary["safety"][key], key)
        self.assertTrue(self.summary["safety"]["not_execution_permission"])

    def test_forbidden_side_effect_counts_are_zero(self) -> None:
        self.assertEqual(
            self.summary["forbidden_side_effect_counts"],
            {"artifacts": 0, "events": 0, "payload_markers": 0},
        )

    def test_no_confirmation_artifact_or_event_exists(self) -> None:
        self.assertEqual(self._forbidden_artifact_count("scheduler_confirmation"), 0)
        self.assertEqual(self._forbidden_event_count("scheduler_confirmation_created"), 0)

    def test_no_verifier_report_artifact_or_event_exists(self) -> None:
        self.assertEqual(
            self._forbidden_artifact_count(
                "scheduler_confirmation_verifier_report"
            ),
            0,
        )
        self.assertEqual(self._forbidden_artifact_count("verifier_report"), 0)
        self.assertEqual(
            self._forbidden_event_count(
                "scheduler_confirmation_verifier_report"
            ),
            0,
        )
        self.assertEqual(self._forbidden_event_count("verifier_report"), 0)

    def test_no_intake_runner_handoff_artifact_or_event_exists(self) -> None:
        self.assertEqual(self._forbidden_artifact_count("intake_runner_handoff"), 0)
        self.assertEqual(
            self._forbidden_event_count("intake_runner_handoff_created"),
            0,
        )

    def test_no_runtime_audit_event_exists(self) -> None:
        self.assertEqual(self.store.list_runtime_audit_events(self.summary["task_key"]), [])
        self.assertEqual(self._forbidden_event_count("runtime_preflight_finished"), 0)
        self.assertEqual(self._forbidden_event_count("runtime_execution_started"), 0)
        self.assertEqual(self._forbidden_event_count("runtime_execution_finished"), 0)

    def test_no_approved_task_runner_executor_or_validator_event_exists(self) -> None:
        self.assertEqual(self.store.list_executor_runs(self.summary["task_key"]), [])
        self.assertEqual(self.store.list_validation_results(self.summary["task_key"]), [])
        for marker in (
            "approved_task_runner",
            "executor_run_started",
            "executor_run_finished",
            "validation_result",
        ):
            self.assertEqual(self._payload_marker_count(marker), 0, marker)

    def test_mission_control_source_remains_read_only_for_scheduler_proposals(self) -> None:
        self.smoke._assert_mission_control_read_only_source()

    def test_script_cli_returns_exit_code_zero_and_parseable_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--task-key",
                    "AT-J4-PROPOSAL-SMOKE-CLI",
                    "--workspace-root",
                    tmp,
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["task_key"], "AT-J4-PROPOSAL-SMOKE-CLI")

    def test_script_cli_failure_path_returns_nonzero(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        relative_workspace = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--workspace-root",
                "relative-workspace",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertNotEqual(relative_workspace.returncode, 0)

        with tempfile.TemporaryDirectory() as tmp:
            invalid_task_key = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--workspace-root",
                    tmp,
                    "--task-key",
                    "invalid task key",
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
        self.assertNotEqual(invalid_task_key.returncode, 0)


if __name__ == "__main__":
    unittest.main()
