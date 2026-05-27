"""Tests for scripts/run_pr_preparation_pipeline.py."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_pr_preparation_pipeline.py"
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "run_pr_preparation_pipeline_smoke.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "run_pr_preparation_pipeline_smoke_for_cli_tests",
        SMOKE_SCRIPT,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _seed_ready_workspace(workspace: Path, task_key: str = "AT-L7C-CLI-TEST") -> dict[str, Path | str]:
    smoke = _load_smoke_module()
    db_path = workspace / "state.db"
    repo_path = workspace / "repo"
    artifact_root = workspace / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    store = TaskMirrorStore(db_path)
    store.init_db()
    base_sha, branch = smoke._init_repo(repo_path, task_key)
    smoke._seed_waiting_approval_task(
        store=store,
        task_key=task_key,
        repo_path=repo_path,
        artifact_root=artifact_root,
        base_sha=base_sha,
        branch=branch,
    )
    return {
        "db_path": db_path,
        "artifact_root": artifact_root,
        "task_key": task_key,
    }


class RunPRPreparationPipelineScriptTests(unittest.TestCase):
    def test_script_help(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--task-key", result.stdout)
        self.assertIn("--db-path", result.stdout)
        self.assertIn("--artifact-root", result.stdout)
        self.assertIn("--confirm-prepare-pr", result.stdout)
        self.assertIn("--confirm-github-mutations", result.stdout)
        self.assertIn("--confirm-branch-push", result.stdout)
        self.assertIn("--confirm-draft-pr", result.stdout)

    def test_script_dry_run_no_mutation_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seeded = _seed_ready_workspace(Path(tmp))
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--task-key",
                    str(seeded["task_key"]),
                    "--db-path",
                    str(seeded["db_path"]),
                    "--artifact-root",
                    str(seeded["artifact_root"]),
                ],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                env={"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "dry_run")
            self.assertFalse(payload["safety"]["github_mutated"])

            store = TaskMirrorStore(Path(str(seeded["db_path"])))
            artifacts = store.list_task_artifacts(str(seeded["task_key"]))
            events = store.list_task_events(str(seeded["task_key"]))
            self.assertFalse(any(a.artifact_type == "pr_handoff" for a in artifacts))
            self.assertFalse(any(a.artifact_type == "branch_push" for a in artifacts))
            self.assertFalse(any(a.artifact_type == "draft_pr" for a in artifacts))
            self.assertFalse(any(e.event_type == "pr_handoff_created" for e in events))
            self.assertFalse(any(e.event_type == "branch_push_completed" for e in events))
            self.assertFalse(any(e.event_type == "draft_pr_created" for e in events))

    def test_script_requires_all_confirmation_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seeded = _seed_ready_workspace(Path(tmp), task_key="AT-L7C-CLI-FLAGS")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--task-key",
                    str(seeded["task_key"]),
                    "--db-path",
                    str(seeded["db_path"]),
                    "--artifact-root",
                    str(seeded["artifact_root"]),
                    "--confirm-prepare-pr",
                ],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                env={"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(result.returncode, 1, result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "failed")
            self.assertIn("--confirm-github-mutations", payload["reasons"][0])
            self.assertIn("--confirm-branch-push", payload["reasons"][0])
            self.assertIn("--confirm-draft-pr", payload["reasons"][0])
            store = TaskMirrorStore(Path(str(seeded["db_path"])))
            artifacts = store.list_task_artifacts(str(seeded["task_key"]))
            self.assertFalse(any(a.artifact_type == "pr_handoff" for a in artifacts))
            self.assertFalse(any(a.artifact_type == "branch_push" for a in artifacts))
            self.assertFalse(any(a.artifact_type == "draft_pr" for a in artifacts))

    def test_source_has_no_forbidden_calls(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        forbidden = (
            "from agent_taskflow.approved_task_runner",
            "from agent_taskflow.executors",
            "from agent_taskflow.validators",
            "from agent_taskflow.api",
            "local_cleanup_confirm",
            "remote_branch_cleanup_confirm",
            "task_closeout_confirm",
            "while True",
            "threading.Thread",
            "asyncio.sleep",
            "schedule.every",
            "subprocess.run",
            "gh pr create",
            "git push",
        )
        for needle in forbidden:
            self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
