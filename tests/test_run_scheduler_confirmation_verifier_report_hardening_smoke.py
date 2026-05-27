"""Tests for Level 4A verifier report hardening smoke."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.scheduler_confirmation_from_proposal import (
    CONFIRMATION_ARTIFACT_TYPE,
    CONFIRMATION_EVENT_TYPE,
)
from agent_taskflow.scheduler_confirmation_verifier_report import (
    VERIFIER_REPORT_ARTIFACT_TYPE,
    VERIFIER_REPORT_EVENT_TYPE,
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
    / "run_scheduler_confirmation_verifier_report_hardening_smoke.py"
)
DOC = REPO_ROOT / "docs" / "scheduler-confirmation-verifier-report-hardening-smoke.md"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "run_scheduler_confirmation_verifier_report_hardening_smoke",
        SCRIPT,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RunSchedulerConfirmationVerifierReportHardeningSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.workspace_root = Path(cls.tmp.name)
        cls.smoke = _load_smoke_module()
        cls.summary = cls.smoke.run_smoke(
            workspace_root=cls.workspace_root,
            task_key="AT-L4A-VERIFIER-REPORT-SMOKE-TEST",
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

    def test_run_smoke_returns_ok(self) -> None:
        self.assertTrue(self.summary["ok"])
        self.assertEqual(
            self.summary["task_key"],
            "AT-L4A-VERIFIER-REPORT-SMOKE-TEST",
        )
        self.assertTrue(self.db_path.is_file())
        self.assertTrue(Path(self.summary["artifact_root"]).is_dir())
        self.assertTrue(self.summary["binding"]["verification_passed"])
        self.assertEqual(self.summary["binding"]["reasons"], [])
        self.assertEqual(self.summary["binding"]["warning_count"], 0)

    def test_creates_expected_proposal_confirmation_and_report_evidence(self) -> None:
        self.assertEqual(len(self._task_artifacts(PROPOSAL_ARTIFACT_TYPE)), 1)
        self.assertEqual(len(self._task_events(PROPOSAL_EVENT_TYPE)), 1)
        self.assertEqual(len(self._task_artifacts(CONFIRMATION_ARTIFACT_TYPE)), 1)
        self.assertEqual(len(self._task_events(CONFIRMATION_EVENT_TYPE)), 1)
        self.assertEqual(len(self._task_artifacts(VERIFIER_REPORT_ARTIFACT_TYPE)), 1)
        self.assertEqual(len(self._task_events(VERIFIER_REPORT_EVENT_TYPE)), 1)

    def test_verifier_report_artifact_safety_flags(self) -> None:
        artifact_path = Path(self.summary["verifier_report"]["artifact_path"])
        self.assertTrue(artifact_path.is_file())
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["verifier_report_id"],
            self.summary["verifier_report"]["verifier_report_id"],
        )
        self.assertEqual(
            payload["confirmation_id"],
            self.summary["confirmation"]["confirmation_id"],
        )
        self.assertTrue(payload["verification_passed"])
        self.assertTrue(payload["not_execution_permission"])
        self.assertTrue(payload["not_handoff"])
        self.assertTrue(payload["not_runtime"])
        self.assertTrue(payload["requires_next_gate"])
        self.assertTrue(payload["safety"]["verifier_report_created"])
        self.assertFalse(payload["safety"]["handoff_created"])
        self.assertFalse(payload["safety"]["runtime_started"])
        self.assertFalse(payload["safety"]["approved_task_runner_called"])
        self.assertFalse(payload["safety"]["executor_started"])
        self.assertFalse(payload["safety"]["validators_started"])
        self.assertFalse(payload["safety"]["github_mutated"])
        self.assertFalse(payload["safety"]["approved"])
        self.assertFalse(payload["safety"]["merged"])
        self.assertFalse(payload["safety"]["cleanup_performed"])

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

    def test_cli_smoke_outputs_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--task-key",
                    "AT-L4A-VERIFIER-REPORT-SMOKE-CLI",
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
        self.assertEqual(
            payload["task_key"],
            "AT-L4A-VERIFIER-REPORT-SMOKE-CLI",
        )
        self.assertTrue(payload["safety"]["proposal_created"])
        self.assertTrue(payload["safety"]["confirmation_created"])
        self.assertTrue(payload["safety"]["verifier_report_created"])
        self.assertEqual(
            payload["forbidden_side_effect_counts"],
            {"artifacts": 0, "events": 0, "payload_markers": 0},
        )

    def test_relative_workspace_fails(self) -> None:
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

    def test_doc_safety_language_exists(self) -> None:
        self.assertTrue(DOC.is_file())
        text = DOC.read_text(encoding="utf-8")
        for phrase in (
            "Level 4A minimal verifier report path",
            "scheduler_confirmation -> verifier binding check -> explicit verifier report",
            "not handoff",
            "not runtime execution",
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
            "scheduler_confirmation_verifier_report is not execution permission",
            "scheduler_confirmation_verifier_report is not handoff",
            "scheduler_confirmation_verifier_report is not runtime execution",
            "scheduler_confirmation_verifier_report requires next gate",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
