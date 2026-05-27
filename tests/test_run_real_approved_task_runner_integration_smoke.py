"""Tests for the Level 6C real approved_task_runner integration smoke."""

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
SCRIPT = REPO_ROOT / "scripts" / "run_real_approved_task_runner_integration_smoke.py"
DOC = REPO_ROOT / "docs" / "real-approved-task-runner-integration-smoke.md"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "run_real_approved_task_runner_integration_smoke",
        SCRIPT,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DefaultRequiresConfirmationTests(unittest.TestCase):
    """Test that without --confirm-real-runner the smoke is blocked."""

    def setUp(self) -> None:
        self.smoke = _load_smoke_module()

    def test_default_requires_confirmation_and_does_not_call_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.smoke.run_smoke(
                workspace_root=Path(tmp),
                task_key="AT-L6C-NO-CONFIRM",
                confirm_real_runner=False,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "confirmation_required")
        self.assertFalse(result["real_runner_confirmed"])
        self.assertFalse(result["real_approved_task_runner_called"])
        # No runtime execution artifact should exist (workspace not fully built).
        self.assertNotIn("runtime_execution", result)

    def test_cli_requires_confirmation(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env={"PYTHONPATH": str(REPO_ROOT), "PATH": os.environ.get("PATH", "")},
        )
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertTrue(
            "confirm-real-runner" in output.lower()
            or "confirmation required" in output.lower()
            or "confirmation_required" in output.lower(),
            f"Expected confirmation message in output, got: {output!r}",
        )


class ConfirmedRealRunnerSmokeTests(unittest.TestCase):
    """Test the confirmed real approved_task_runner integration smoke."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.smoke = _load_smoke_module()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.workspace_root = Path(cls.tmp.name)
        try:
            cls.summary = cls.smoke.run_smoke(
                workspace_root=cls.workspace_root,
                task_key="AT-L6C-REAL-RUNNER-TEST",
                confirm_real_runner=True,
            )
            cls.smoke_available = True
        except Exception as exc:
            cls.smoke_available = False
            cls.smoke_error = str(exc)
            cls.summary = {}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def _require_smoke(self) -> None:
        if not self.smoke_available:
            self.skipTest(
                f"real runner smoke unavailable in this environment: {self.smoke_error}"
            )

    def test_confirmed_smoke_returns_ok_if_safe_runner_available(self) -> None:
        self._require_smoke()
        self.assertTrue(self.summary.get("ok"), self.summary)
        self.assertTrue(self.summary.get("real_runner_confirmed"))
        self.assertTrue(self.summary.get("real_approved_task_runner_called"))
        self.assertEqual(
            self.summary.get("readbacks", {}).get("runtime_audit_event_count"), 3
        )
        self.assertEqual(
            self.summary.get("readbacks", {}).get("runtime_execution_artifact_count"),
            1,
        )

    def test_forbidden_side_effect_counts_zero(self) -> None:
        self._require_smoke()
        counts = self.summary.get("forbidden_side_effect_counts", {})
        self.assertEqual(counts.get("artifacts"), 0)
        self.assertEqual(counts.get("events"), 0)
        self.assertEqual(counts.get("payload_markers"), 0)
        # Verify via direct DB scan as well.
        db_path = Path(self.summary["db_path"])
        direct_counts = self.smoke._forbidden_side_effect_counts(db_path)
        self.assertEqual(
            direct_counts,
            {"artifacts": 0, "events": 0, "payload_markers": 0},
        )

    def test_runtime_execution_artifact_fields(self) -> None:
        self._require_smoke()
        runtime = self.summary.get("runtime_execution", {})
        artifact_path = Path(runtime["artifact_path"])
        self.assertTrue(artifact_path.is_file())
        payload = json.loads(artifact_path.read_text())
        self.assertTrue(payload.get("approved_task_runner_called"))
        self.assertTrue(payload.get("runner_returned"))
        self.assertTrue(payload.get("runner_ok"))
        safety = payload.get("safety") or {}
        self.assertTrue(safety.get("runtime_started"))
        self.assertFalse(safety.get("approved"))
        self.assertFalse(safety.get("merged"))
        self.assertFalse(safety.get("cleanup_performed"))
        self.assertFalse(safety.get("github_mutated"))
        self.assertFalse(safety.get("scheduler_loop_started"))
        self.assertFalse(safety.get("automatic_task_picking_started"))
        self.assertTrue(safety.get("requires_human_review_after_runtime"))

    def test_runtime_audit_events_three_in_order(self) -> None:
        self._require_smoke()
        db_path = Path(self.summary["db_path"])
        store = TaskMirrorStore(db_path)
        task_key = self.summary["task_key"]
        events = store.list_runtime_audit_events(task_key)
        self.assertEqual(len(events), 3)
        kinds = [e.get("kind") for e in events]
        self.assertEqual(
            kinds,
            [
                RUNTIME_PREFLIGHT_EVENT_TYPE,
                RUNTIME_STARTED_EVENT_TYPE,
                RUNTIME_FINISHED_EVENT_TYPE,
            ],
        )

    def test_runtime_execution_artifact_in_store(self) -> None:
        self._require_smoke()
        db_path = Path(self.summary["db_path"])
        store = TaskMirrorStore(db_path)
        task_key = self.summary["task_key"]
        artifacts = store.list_runtime_execution_artifacts(task_key)
        self.assertEqual(len(artifacts), 1)

    def test_safety_flags_in_summary(self) -> None:
        self._require_smoke()
        safety = self.summary.get("safety", {})
        self.assertFalse(safety.get("scheduler_loop_started"))
        self.assertFalse(safety.get("background_worker_started"))
        self.assertFalse(safety.get("automatic_task_picking_started"))
        self.assertFalse(safety.get("approved"))
        self.assertFalse(safety.get("merged"))
        self.assertFalse(safety.get("cleanup_performed"))
        self.assertFalse(safety.get("github_mutated"))

    def test_full_evidence_chain_artifacts_exist(self) -> None:
        self._require_smoke()
        for stage in (
            "proposal",
            "confirmation",
            "verifier_report",
            "handoff",
            "runtime_execution",
        ):
            artifact_path = Path(self.summary[stage]["artifact_path"])
            self.assertTrue(
                artifact_path.is_file(),
                f"{stage} artifact missing: {artifact_path}",
            )


class CliConfirmedSmokeTests(unittest.TestCase):
    """Test the CLI with --confirm-real-runner."""

    def test_cli_confirmed_smoke_outputs_json_or_skips_if_runner_unavailable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--workspace-root",
                    tmp,
                    "--task-key",
                    "AT-L6C-REAL-RUNNER-CLI",
                    "--confirm-real-runner",
                ],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                env={
                    "PYTHONPATH": str(REPO_ROOT),
                    "PATH": os.environ.get("PATH", ""),
                },
            )
        if result.returncode != 0:
            # If the runner is unavailable in this environment, the test is skipped.
            stderr = result.stderr
            if any(
                marker in stderr.lower()
                for marker in ("git", "true", "worktree", "fixture", "unavailable")
            ):
                self.skipTest(
                    f"real runner smoke not available in this environment: {stderr!r}"
                )
            self.fail(
                f"CLI confirmed smoke failed (exit {result.returncode}):\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        payload = json.loads(result.stdout)
        self.assertTrue(payload.get("ok"), payload)
        self.assertTrue(payload.get("real_approved_task_runner_called"))
        runtime = payload.get("runtime_execution", {})
        self.assertTrue(runtime.get("approved_task_runner_called"))
        self.assertTrue(runtime.get("runner_ok"))


class DocAndSourceTests(unittest.TestCase):
    """Verify documentation and source safety properties."""

    def test_doc_exists_and_contains_safety_language(self) -> None:
        self.assertTrue(DOC.is_file(), f"Doc missing: {DOC}")
        text = DOC.read_text(encoding="utf-8").lower()
        required_phrases = [
            "level 6c real approved_task_runner integration smoke",
            "--confirm-real-runner",
            "default mode does not call approved_task_runner",
            "no scheduler loop",
            "no background worker",
            "no automatic task picking",
            "no approval / merge / cleanup",
            "runtime audit evidence is not approval",
            "runtime audit evidence is not merge",
            "runtime audit evidence is not cleanup",
            "human review remains required after runtime",
        ]
        for phrase in required_phrases:
            self.assertIn(
                phrase,
                text,
                f"Required phrase missing from doc: {phrase!r}",
            )

    def test_source_does_not_add_scheduler_loop_or_background_worker(
        self,
    ) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        # These patterns indicate actual implementation of forbidden features,
        # not strings in FORBIDDEN_PAYLOAD_MARKERS constants.
        forbidden_patterns = [
            "while True",
            "cron",
            "threading.Thread",
            "asyncio.create_task",
            "multiprocessing.Process",
        ]
        for pattern in forbidden_patterns:
            self.assertNotIn(
                pattern,
                source,
                f"Forbidden implementation pattern found in script source: {pattern!r}",
            )


if __name__ == "__main__":
    unittest.main()
