"""Tests for the Level 6A minimal runtime handoff execution smoke."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.runtime_handoff_execution_from_handoff import (
    RUNTIME_EXECUTION_ARTIFACT_TYPE,
    RUNTIME_FINISHED_EVENT_TYPE,
    RUNTIME_PREFLIGHT_EVENT_TYPE,
    RUNTIME_STARTED_EVENT_TYPE,
)
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_minimal_runtime_handoff_execution_smoke.py"
DOC = REPO_ROOT / "docs" / "minimal-runtime-handoff-execution-smoke.md"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "run_minimal_runtime_handoff_execution_smoke",
        SCRIPT,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RunMinimalRuntimeHandoffExecutionSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.workspace_root = Path(cls.tmp.name)
        cls.smoke = _load_smoke_module()
        cls.summary = cls.smoke.run_smoke(
            workspace_root=cls.workspace_root,
            task_key="AT-L6A-RUNTIME-SMOKE-TEST",
        )
        cls.db_path = Path(cls.summary["db_path"])
        cls.store = TaskMirrorStore(cls.db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_run_smoke_returns_ok(self) -> None:
        self.assertTrue(self.summary["ok"])
        self.assertEqual(
            self.summary["task_key"],
            "AT-L6A-RUNTIME-SMOKE-TEST",
        )
        self.assertTrue(self.summary["preflight"]["preflight_passed"])
        self.assertEqual(self.summary["preflight"]["reasons"], [])

    def test_creates_full_evidence_chain(self) -> None:
        for stage in ("proposal", "confirmation", "verifier_report", "handoff", "runtime_execution"):
            artifact_path = Path(self.summary[stage]["artifact_path"])
            self.assertTrue(artifact_path.is_file(), f"{stage} artifact missing: {artifact_path}")

    def test_runtime_audit_events_count_three(self) -> None:
        self.assertEqual(self.summary["readbacks"]["runtime_audit_event_count"], 3)
        self.assertEqual(self.summary["readbacks"]["runtime_execution_artifact_count"], 1)
        events = self.store.list_runtime_audit_events(self.summary["task_key"])
        kinds = [event["kind"] for event in events]
        self.assertEqual(
            kinds,
            [
                RUNTIME_PREFLIGHT_EVENT_TYPE,
                RUNTIME_STARTED_EVENT_TYPE,
                RUNTIME_FINISHED_EVENT_TYPE,
            ],
        )

    def test_runtime_execution_artifact_safety(self) -> None:
        runtime_path = Path(self.summary["runtime_execution"]["artifact_path"])
        payload = json.loads(runtime_path.read_text())
        self.assertTrue(payload["approved_task_runner_called"])
        self.assertTrue(payload["runner_ok"])
        safety = payload["safety"]
        self.assertTrue(safety["runtime_started"])
        self.assertFalse(safety["approved"])
        self.assertFalse(safety["merged"])
        self.assertFalse(safety["cleanup_performed"])
        self.assertFalse(safety["github_mutated"])
        self.assertFalse(safety["scheduler_loop_started"])
        self.assertFalse(safety["automatic_task_picking_started"])
        self.assertTrue(safety["requires_human_review_after_runtime"])

    def test_fake_runner_invoked_exactly_once(self) -> None:
        runtime_artifacts = [
            artifact
            for artifact in self.store.list_task_artifacts(self.summary["task_key"])
            if artifact.artifact_type == RUNTIME_EXECUTION_ARTIFACT_TYPE
        ]
        self.assertEqual(len(runtime_artifacts), 1)

    def test_forbidden_side_effect_counts_zero(self) -> None:
        self.assertEqual(
            self.summary["forbidden_side_effect_counts"],
            {"artifacts": 0, "events": 0, "payload_markers": 0},
        )
        self.assertEqual(
            self.smoke._forbidden_side_effect_counts(self.db_path),
            {"artifacts": 0, "events": 0, "payload_markers": 0},
        )

    def test_cli_smoke_outputs_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--workspace-root",
                    tmp,
                    "--task-key",
                    "AT-L6A-RUNTIME-SMOKE-CLI",
                ],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                env={"PYTHONPATH": str(REPO_ROOT), "PATH": os.environ.get("PATH", "")},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["runtime_execution"]["approved_task_runner_called"])

    def test_relative_workspace_fails(self) -> None:
        with self.assertRaises((ValueError, self.smoke.SmokeFailure)):
            self.smoke.run_smoke(workspace_root=Path("relative/path"))

    def test_doc_safety_language(self) -> None:
        self.assertTrue(DOC.is_file())
        text = DOC.read_text().lower()
        for phrase in (
            "explicit operator-gated",
            "dry-run does not call",
            "approved_task_runner",
            "no scheduler loop",
            "no background worker",
            "no approval",
            "no merge",
            "no cleanup",
            "human review",
        ):
            self.assertIn(phrase, text, f"missing doc phrase: {phrase}")


if __name__ == "__main__":
    unittest.main()
