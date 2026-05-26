"""Tests for scripts/run_scheduler_confirmation_preparation_hardening_smoke.py."""

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
from agent_taskflow.scheduler_confirmation_from_proposal import (
    CONFIRMATION_ARTIFACT_TYPE,
    CONFIRMATION_EVENT_TYPE,
)
from agent_taskflow.scheduler_confirmation_readback import (
    list_task_scheduler_confirmation_readbacks,
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
    / "run_scheduler_confirmation_preparation_hardening_smoke.py"
)
DOC = REPO_ROOT / "docs" / "scheduler-confirmation-preparation-hardening-smoke.md"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "run_scheduler_confirmation_preparation_hardening_smoke",
        SCRIPT,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RunSchedulerConfirmationPreparationHardeningSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.workspace_root = Path(cls.tmp.name)
        cls.smoke = _load_smoke_module()
        cls.summary = cls.smoke.run_smoke(
            workspace_root=cls.workspace_root,
            task_key="AT-K5-CONFIRMATION-SMOKE-TEST",
        )
        cls.db_path = Path(cls.summary["db_path"])
        cls.store = TaskMirrorStore(cls.db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def _task_artifacts(self, artifact_type: str):
        return [
            artifact
            for artifact in self.store.list_task_artifacts(self.summary["task_key"])
            if artifact.artifact_type == artifact_type
        ]

    def _task_events(self, event_type: str):
        return [
            event
            for event in self.store.list_task_events(self.summary["task_key"])
            if event.event_type == event_type
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

    def test_smoke_script_module_run_smoke_returns_ok(self) -> None:
        self.assertTrue(self.summary["ok"])
        self.assertEqual(self.summary["task_key"], "AT-K5-CONFIRMATION-SMOKE-TEST")
        self.assertTrue(self.db_path.is_file())
        self.assertTrue(Path(self.summary["artifact_root"]).is_dir())
        self.assertTrue(self.summary["safety"]["confirmation_created"])
        self.assertEqual(
            self.summary["forbidden_side_effect_counts"],
            {"artifacts": 0, "events": 0, "payload_markers": 0},
        )

    def test_smoke_creates_expected_proposal_and_confirmation_evidence(self) -> None:
        self.assertEqual(len(self._task_artifacts(PROPOSAL_ARTIFACT_TYPE)), 1)
        self.assertEqual(len(self._task_events(PROPOSAL_EVENT_TYPE)), 1)
        self.assertEqual(len(self._task_artifacts(CONFIRMATION_ARTIFACT_TYPE)), 1)
        self.assertEqual(len(self._task_events(CONFIRMATION_EVENT_TYPE)), 1)

    def test_confirmation_artifact_safety_flags(self) -> None:
        artifact_path = Path(self.summary["confirmation"]["artifact_path"])
        self.assertTrue(artifact_path.is_file())
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        for key in (
            "confirmation_id",
            "proposal_id",
            "proposal_hash",
            "proposal_item_id",
            "item_hash",
            "recommended_command_kind",
            "proposal_artifact_path",
        ):
            self.assertIn(key, payload)
        self.assertTrue(payload["not_execution_permission"])
        self.assertTrue(payload["not_verifier_report"])
        self.assertTrue(payload["not_handoff"])
        self.assertTrue(payload["not_runtime"])
        self.assertTrue(payload["requires_next_gate"])

    def test_k3_readbacks_return_confirmation(self) -> None:
        readback = list_task_scheduler_confirmation_readbacks(
            self.store,
            self.summary["task_key"],
        )
        self.assertEqual(readback["count"], 1)
        self.assertEqual(self.summary["readbacks"]["helper_count"], 1)
        self.assertEqual(self.summary["readbacks"]["api_global_count"], 1)
        self.assertEqual(self.summary["readbacks"]["api_task_count"], 1)

        with TestClient(create_app(self.db_path)) as client:
            global_payload = client.get(
                "/api/scheduler/confirmations",
                params={"task_key": self.summary["task_key"]},
            ).json()
            task_payload = client.get(
                f"/api/tasks/{self.summary['task_key']}/scheduler-confirmations"
            ).json()
        self.assertEqual(global_payload["count"], 1)
        self.assertEqual(task_payload["count"], 1)

    def test_repeated_k3_gets_do_not_mutate_db(self) -> None:
        before = self._db_counts()
        with TestClient(create_app(self.db_path)) as client:
            client.get(
                "/api/scheduler/confirmations",
                params={"task_key": self.summary["task_key"]},
            )
            client.get(
                f"/api/tasks/{self.summary['task_key']}/scheduler-confirmations"
            )
            client.get(
                "/api/scheduler/confirmations",
                params={"task_key": self.summary["task_key"]},
            )
            client.get(
                f"/api/tasks/{self.summary['task_key']}/scheduler-confirmations"
            )
        self.assertEqual(self._db_counts(), before)

    def test_forbidden_side_effect_counts_zero(self) -> None:
        self.assertEqual(
            self.smoke._forbidden_side_effect_counts(self.db_path),
            {"artifacts": 0, "events": 0, "payload_markers": 0},
        )
        self.assertEqual(
            self.summary["forbidden_side_effect_counts"],
            {"artifacts": 0, "events": 0, "payload_markers": 0},
        )
        for key in (
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

    def test_cli_success_outputs_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--task-key",
                    "AT-K5-CONFIRMATION-SMOKE-CLI",
                    "--workspace-root",
                    tmp,
                    "--keep-workspace",
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["task_key"], "AT-K5-CONFIRMATION-SMOKE-CLI")
        self.assertTrue(payload["safety"]["proposal_created"])
        self.assertTrue(payload["safety"]["confirmation_created"])

    def test_cli_failure_for_relative_workspace(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        result = subprocess.run(
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

        self.assertEqual(result.returncode, 1)
        self.assertIn("workspace_root must be absolute", result.stderr)

    def test_doc_exists_and_contains_required_safety_language(self) -> None:
        self.assertTrue(DOC.is_file())
        text = DOC.read_text(encoding="utf-8")
        for phrase in (
            "K5 confirmation preparation hardening smoke",
            "proposal → eligibility → explicit confirmation → readback API",
            "scheduler_confirmation is not execution permission",
            "scheduler_confirmation is not verifier report",
            "scheduler_confirmation is not handoff",
            "scheduler_confirmation is not runtime execution",
            "Mission Control remains read-only",
            "no verifier report",
            "no handoff",
            "no runtime execution",
            "no approved_task_runner",
            "no executor",
            "no validators",
            "no GitHub mutation",
            "no approval / merge / cleanup",
            "no scheduler loop",
            "no background worker",
            "no automatic task picking",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_source_does_not_import_or_call_forbidden_runtime_paths(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")

        strict_forbidden = (
            "subprocess.run",
            "requests.post",
            "gh pr",
            "create_verifier_report(",
            "from agent_taskflow.approved_task_runner",
            "import agent_taskflow.approved_task_runner",
            "from agent_taskflow.executors",
            "import agent_taskflow.executors",
            "from agent_taskflow.validators",
            "import agent_taskflow.validators",
            "from agent_taskflow.queued_task_handoff",
            "import agent_taskflow.queued_task_handoff",
            "from agent_taskflow.scheduler_confirmation_verifier",
            "import agent_taskflow.scheduler_confirmation_verifier",
        )
        for needle in strict_forbidden:
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)

        forbidden_calls = (
            "approved_task_runner",
            "intake_runner_handoff",
            "runtime_execution_started",
            "runtime_execution_finished",
            "executor_run_started",
            "executor_run_finished",
            "validation_result",
            "scheduler_confirmation_verifier_report",
            "verifier_report",
        )
        for token in forbidden_calls:
            with self.subTest(token=token):
                self.assertNotIn(f"{token}(", text)
                self.assertNotIn(f"{token}.", text)

        # These marker strings are allowed as FORBIDDEN_* scan constants and
        # negative safety flags, but not as imports or callable paths.
        self.assertIn("FORBIDDEN_ARTIFACT_TYPES", text)
        self.assertIn("FORBIDDEN_EVENT_TYPES", text)
        self.assertIn("FORBIDDEN_PAYLOAD_MARKERS", text)


if __name__ == "__main__":
    unittest.main()
