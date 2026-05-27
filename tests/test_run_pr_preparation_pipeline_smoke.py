"""Tests for scripts/run_pr_preparation_pipeline_smoke.py."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_pr_preparation_pipeline_smoke.py"
DOC = REPO_ROOT / "docs" / "pr-preparation-pipeline.md"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "run_pr_preparation_pipeline_smoke",
        SCRIPT,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RunPRPreparationPipelineSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.workspace_root = Path(cls.tmp.name)
        cls.smoke = _load_smoke_module()
        cls.summary = cls.smoke.run_smoke(
            workspace_root=cls.workspace_root,
            task_key="AT-L7C-PR-PREP-SMOKE-TEST",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_smoke_returns_ok(self) -> None:
        self.assertTrue(self.summary["ok"])
        self.assertEqual(self.summary["task_key"], "AT-L7C-PR-PREP-SMOKE-TEST")

    def test_smoke_dry_run_calls_no_fake_mutations(self) -> None:
        dry_run = self.summary["dry_run"]
        self.assertTrue(dry_run["ok"])
        self.assertFalse(dry_run["github_mutated"])
        self.assertEqual(dry_run["branch_push_call_count"], 0)
        self.assertEqual(dry_run["draft_pr_call_count"], 0)

    def test_smoke_confirmed_calls_fake_push_and_draft_once(self) -> None:
        confirmed = self.summary["confirmed"]
        self.assertTrue(confirmed["ok"])
        self.assertEqual(confirmed["status"], "draft_pr_created")
        self.assertEqual(confirmed["branch_push_call_count"], 1)
        self.assertEqual(confirmed["draft_pr_call_count"], 1)
        self.assertTrue(confirmed["branch_pushed"])
        self.assertTrue(confirmed["draft_pr_created"])
        self.assertEqual(confirmed["pr_number"], 1)
        self.assertTrue(confirmed["pr_url"].endswith("/pull/1"))

    def test_smoke_forbidden_side_effect_counts_zero(self) -> None:
        self.assertEqual(
            self.summary["forbidden_side_effect_counts"],
            {"artifacts": 0, "events": 0, "payload_markers": 0},
        )
        safety = self.summary["safety"]
        self.assertFalse(safety["approved"])
        self.assertFalse(safety["merged"])
        self.assertFalse(safety["cleanup_performed"])
        self.assertTrue(safety["human_review_required"])
        self.assertFalse(safety["scheduler_loop_started"])
        self.assertFalse(safety["background_worker_started"])
        self.assertFalse(safety["automatic_task_picking_started"])


class RunPRPreparationPipelineSmokeCliTests(unittest.TestCase):
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
            self.assertTrue(payload["dry_run"]["ok"])
            self.assertEqual(payload["dry_run"]["branch_push_call_count"], 0)
            self.assertEqual(payload["dry_run"]["draft_pr_call_count"], 0)
            self.assertEqual(payload["confirmed"]["branch_push_call_count"], 1)
            self.assertEqual(payload["confirmed"]["draft_pr_call_count"], 1)
            self.assertTrue(payload["confirmed"]["branch_pushed"])
            self.assertTrue(payload["confirmed"]["draft_pr_created"])
            self.assertEqual(
                payload["forbidden_side_effect_counts"],
                {"artifacts": 0, "events": 0, "payload_markers": 0},
            )


class DocSafetyLanguageTests(unittest.TestCase):
    def test_doc_contains_safety_language(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        for needle in (
            "Level 7C",
            "waiting_approval",
            "stops for human review",
            "--confirm-prepare-pr",
            "--confirm-github-mutations",
            "--confirm-branch-push",
            "--confirm-draft-pr",
            "no GitHub Issue ingest",
            "no runtime execution",
            "no approved_task_runner",
            "no executor",
            "no validators",
            "no automatic task discovery",
            "no automatic task picking",
            "no scheduler loop",
            "no background worker",
            "no approval",
            "no merge",
            "no cleanup",
            "no Mission Control action UI",
            "no API endpoint",
            "one `task_key` per invocation",
            "all GitHub mutation confirmations required",
            "dry-run writes nothing",
            "human final review remains required",
        ):
            self.assertIn(needle, text, msg=f"missing safety language: {needle!r}")


if __name__ == "__main__":
    unittest.main()
