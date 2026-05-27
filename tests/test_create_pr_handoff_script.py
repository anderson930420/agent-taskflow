"""Tests for scripts/create_pr_handoff.py."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.mission_contract import build_mission_contract, write_mission_contract
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "create_pr_handoff.py"
MODULE = REPO_ROOT / "agent_taskflow" / "pr_handoff.py"


class CreatePrHandoffScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.db_path = self.root / "state.db"
        self.output_dir = self.root / "handoffs"
        self.artifact_dir = self.root / "artifacts" / "AT-CLI-HANDOFF"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.base_sha = self._init_git_repo()
        self.worktree = self.root / "worktree"
        self._git(["worktree", "add", "-b", "task/AT-CLI-HANDOFF", str(self.worktree), "main"])
        (self.worktree / "cli-change.txt").write_text("cli handoff\n", encoding="utf-8")
        self._add_task(status="waiting_approval")
        self._add_worktree()
        self._add_review_evidence()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd or self.repo,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.returncode != 0:
            self.fail(f"git {' '.join(args)} failed: {completed.stderr}")
        return completed

    def _init_git_repo(self) -> str:
        self.repo.mkdir()
        self._git(["init"])
        self._git(["config", "user.email", "agent-taskflow@example.invalid"])
        self._git(["config", "user.name", "Agent Taskflow"])
        (self.repo / "README.md").write_text("# cli handoff test\n", encoding="utf-8")
        self._git(["add", "README.md"])
        self._git(["commit", "-m", "initial"])
        self._git(["branch", "-M", "main"])
        return self._git(["rev-parse", "main"]).stdout.strip()

    def _add_task(self, *, status: str) -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key="AT-CLI-HANDOFF",
                project="agent-taskflow",
                status=status,
                repo_path=self.repo,
                artifact_dir=self.artifact_dir,
                title="CLI handoff task",
            )
        )

    def _add_worktree(self) -> None:
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key="AT-CLI-HANDOFF",
                repo_path=self.repo,
                worktree_path=self.worktree,
                branch="task/AT-CLI-HANDOFF",
                base_branch="main",
                base_sha=self.base_sha,
                status="active",
            )
        )

    def _add_review_evidence(self) -> None:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        result_path = self.artifact_dir / "result.txt"
        log_path = self.artifact_dir / "validator.log"
        result_path.write_text("ok\n", encoding="utf-8")
        log_path.write_text("passed\n", encoding="utf-8")
        contract = build_mission_contract(
            task_key="AT-CLI-HANDOFF",
            goal="CLI handoff",
            repo_path=self.repo,
            worktree_path=self.worktree,
            artifact_dir=self.artifact_dir,
            executor="cli-test-executor",
            required_validators=("cli-validator",),
        )
        write_mission_contract(contract, artifact_dir=self.artifact_dir)
        self.store.record_task_artifact("AT-CLI-HANDOFF", "other", result_path)
        run_id = self.store.create_executor_run("AT-CLI-HANDOFF", "cli-test-executor")
        self.store.finish_executor_run(
            "AT-CLI-HANDOFF",
            run_id,
            executor="cli-test-executor",
            status="completed",
            exit_code=0,
            summary="executor completed",
            log_path=result_path,
            artifacts={"result": result_path},
        )
        self.store.record_validation_result(
            "AT-CLI-HANDOFF",
            "cli-validator",
            status="passed",
            exit_code=0,
            summary="validator passed",
            log_path=log_path,
            artifacts={"log": log_path},
        )

    def _run_script(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--task-key",
                "AT-CLI-HANDOFF",
                "--db-path",
                str(self.db_path),
                "--output-dir",
                str(self.output_dir),
                "--repo",
                "anderson930420/agent-taskflow",
                *extra,
            ],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_help_succeeds(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertIn("--task-key", completed.stdout)
        self.assertIn("--dry-run", completed.stdout)

    def test_cli_success_emits_json(self) -> None:
        completed = self._run_script()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "created")
        self.assertEqual(payload["package"]["task_key"], "AT-CLI-HANDOFF")
        self.assertIn("cli-change.txt", payload["package"]["changed_files"])
        self.assertTrue(Path(payload["json_path"]).is_file())
        self.assertTrue(Path(payload["markdown_path"]).is_file())

    def test_cli_failure_exits_nonzero_with_clear_json_message(self) -> None:
        self.store.update_task_status(
            "AT-CLI-HANDOFF",
            "queued",
            source="test",
        )

        completed = self._run_script()

        self.assertNotEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("waiting_approval", payload["summary"])

    def test_script_and_module_do_not_execute_forbidden_commands(self) -> None:
        content = SCRIPT.read_text(encoding="utf-8") + "\n" + MODULE.read_text(encoding="utf-8")
        forbidden_execution_patterns = [
            r"subprocess\.run\([^)]*gh\s+pr\s+create",
            r"subprocess\.run\([^)]*git\s+push",
            r"subprocess\.run\([^)]*git\s+merge",
            r"subprocess\.run\([^)]*git\s+rebase",
            r"subprocess\.run\([^)]*worktree\s+remove",
            r"subprocess\.run\([^)]*branch\s+-D",
            r"shell\s*=\s*True",
        ]
        for pattern in forbidden_execution_patterns:
            self.assertIsNone(re.search(pattern, content, re.DOTALL), pattern)


if __name__ == "__main__":
    unittest.main()
