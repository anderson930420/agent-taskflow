"""Tests for the Level 7A one-shot task pipeline smoke."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.runtime_handoff_execution_from_handoff import (
    RUNTIME_FINISHED_EVENT_TYPE,
    RUNTIME_PREFLIGHT_EVENT_TYPE,
    RUNTIME_STARTED_EVENT_TYPE,
)
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_one_shot_task_pipeline_smoke.py"
DOC = REPO_ROOT / "docs" / "one-shot-task-pipeline.md"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "run_one_shot_task_pipeline_smoke",
        SCRIPT,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RunOneShotTaskPipelineSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.workspace_root = Path(cls.tmp.name)
        cls.smoke = _load_smoke_module()
        cls.summary = cls.smoke.run_smoke(
            workspace_root=cls.workspace_root,
            task_key="AT-L7A-ONE-SHOT-SMOKE-TEST",
        )
        cls.db_path = Path(cls.summary["db_path"])
        cls.store = TaskMirrorStore(cls.db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_smoke_returns_ok(self) -> None:
        self.assertTrue(self.summary["ok"])
        self.assertEqual(self.summary["task_key"], "AT-L7A-ONE-SHOT-SMOKE-TEST")
        self.assertEqual(self.summary["final_task_status"], "waiting_approval")

    def test_smoke_fake_runner_called_once(self) -> None:
        self.assertTrue(self.summary["runner"]["fake_runner_called"])
        self.assertEqual(self.summary["runner"]["call_count"], 1)
        self.assertEqual(
            self.summary["runner"]["runner_status"], "waiting_approval"
        )

    def test_smoke_evidence_counts(self) -> None:
        self.assertEqual(
            self.summary["evidence_counts"],
            {
                "scheduler_proposal": 1,
                "scheduler_confirmation": 1,
                "scheduler_confirmation_verifier_report": 1,
                "intake_runner_handoff": 1,
                "runtime_handoff_execution": 1,
                "runtime_audit_events": 3,
            },
        )

    def test_smoke_runtime_audit_event_order(self) -> None:
        events = self.store.list_runtime_audit_events(self.summary["task_key"])
        self.assertEqual(len(events), 3)
        kinds = [event["kind"] for event in events]
        self.assertEqual(
            kinds,
            [
                RUNTIME_PREFLIGHT_EVENT_TYPE,
                RUNTIME_STARTED_EVENT_TYPE,
                RUNTIME_FINISHED_EVENT_TYPE,
            ],
        )

    def test_smoke_forbidden_side_effect_counts_zero(self) -> None:
        self.assertEqual(
            self.summary["forbidden_side_effect_counts"],
            {"artifacts": 0, "events": 0, "payload_markers": 0},
        )

    def test_smoke_safety_flags(self) -> None:
        safety = self.summary["safety"]
        self.assertTrue(safety["one_task_only"])
        self.assertTrue(safety["operator_triggered"])
        self.assertFalse(safety["scheduler_loop_started"])
        self.assertFalse(safety["background_worker_started"])
        self.assertFalse(safety["automatic_task_picking_started"])
        self.assertFalse(safety["approved"])
        self.assertFalse(safety["merged"])
        self.assertFalse(safety["cleanup_performed"])
        self.assertTrue(safety["human_review_required"])


class RunOneShotTaskPipelineSmokeCliTests(unittest.TestCase):
    def test_smoke_cli_outputs_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp) / "ws"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--workspace-root",
                    str(workspace_root),
                    "--keep-workspace",
                ],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                env={"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["final_task_status"], "waiting_approval")
            self.assertEqual(payload["runner"]["call_count"], 1)
            self.assertEqual(
                payload["evidence_counts"]["scheduler_proposal"], 1
            )
            self.assertEqual(
                payload["evidence_counts"]["runtime_handoff_execution"], 1
            )
            self.assertEqual(
                payload["evidence_counts"]["runtime_audit_events"], 3
            )


class DocSafetyLanguageTests(unittest.TestCase):
    def test_doc_contains_safety_language(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        for needle in (
            "task_key` per invocation",
            "operator-triggered",
            "Dry-run",
            "approved_task_runner",
            "No GitHub Issue ingest",
            "Human review remains required",
            "No branch push",
            "No draft PR creation",
            "No approval, merge, or cleanup",
            "No Mission Control action UI",
            "No API endpoint",
        ):
            self.assertIn(needle, text, msg=f"missing safety language: {needle!r}")


if __name__ == "__main__":
    unittest.main()
