from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_approved_task.py"


class RunApprovedTaskScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.worktree_root = self.root / "worktrees"
        self._init_repo()
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _init_repo(self) -> None:
        self._git("init", "-b", "main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.repo / "README.md").write_text("agent-taskflow\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial commit")

    def _add_task(self, task_key: str, *, status: str = "queued") -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title=f"Task {task_key}",
                status=status,
                repo_path=self.repo,
                artifact_dir=self.artifact_root / task_key,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )

    def run_script(self, *extra_args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--repo-path",
                str(self.repo),
                "--db-path",
                str(self.db_path),
                "--artifact-root",
                str(self.artifact_root),
                "--worktree-root",
                str(self.repo / ".worktrees"),
                "--validator",
                "policy",
                *extra_args,
            ],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def test_script_requires_task_key(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("--task-key", result.stdout)

        missing = subprocess.run(
            [sys.executable, str(SCRIPT), "--executor", "noop", "--repo-path", str(self.repo)],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("--task-key", missing.stderr)

    def test_script_requires_executor(self) -> None:
        missing = subprocess.run(
            [sys.executable, str(SCRIPT), "--task-key", "AT-GH-501", "--repo-path", str(self.repo)],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("--executor", missing.stderr)

    def test_script_requires_confirm_flag_for_non_dry_run(self) -> None:
        self._add_task("AT-GH-502")

        result = self.run_script(
            "--task-key",
            "AT-GH-502",
            "--executor",
            "noop",
            "--json",
        )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("--confirm-approved-task", payload["error"])
        self.assertFalse(payload["safety"]["human_approval_confirmed"])

    def test_script_prints_deterministic_json(self) -> None:
        self._add_task("AT-GH-503")

        result = self.run_script(
            "--task-key",
            "AT-GH-503",
            "--executor",
            "noop",
            "--confirm-approved-task",
            "--dry-run",
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["task_key"], "AT-GH-503")
        self.assertEqual(payload["executor"], "noop")
        self.assertEqual(payload["status"], "preview")
        self.assertTrue(payload["safety"]["read_only"])

    def test_script_refuses_non_queued_task(self) -> None:
        self._add_task("AT-GH-504", status="blocked")

        result = self.run_script(
            "--task-key",
            "AT-GH-504",
            "--executor",
            "noop",
            "--confirm-approved-task",
            "--json",
        )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("must be queued", payload["error"])
        self.assertFalse(payload["safety"]["task_status_changed"])

    def test_script_supports_dry_run_without_mutation(self) -> None:
        self._add_task("AT-GH-505")
        before_status = self.store.get_task("AT-GH-505").status
        before_events = len(self.store.list_task_events("AT-GH-505"))
        before_artifacts = len(self.store.list_task_artifacts("AT-GH-505"))

        result = self.run_script(
            "--task-key",
            "AT-GH-505",
            "--executor",
            "noop",
            "--dry-run",
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "preview")
        self.assertEqual(self.store.get_task("AT-GH-505").status, before_status)
        self.assertEqual(len(self.store.list_task_events("AT-GH-505")), before_events)
        self.assertEqual(len(self.store.list_task_artifacts("AT-GH-505")), before_artifacts)
        self.assertIsNone(self.store.get_task_worktree("AT-GH-505"))

    def test_script_claude_code_dry_run_does_not_invoke(self) -> None:
        # Without --claude-code-enable-invocation the executor stays dry-run even
        # in a confirmed run; no subprocess command is recorded.
        self._add_task("AT-GH-510")
        result = self.run_script(
            "--task-key",
            "AT-GH-510",
            "--executor",
            "claude-code",
            "--confirm-approved-task",
            "--dry-run",
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["executor"], "claude-code")
        self.assertEqual(payload["status"], "preview")

    def test_script_claude_code_enable_without_command_blocks(self) -> None:
        self._add_task("AT-GH-511")
        result = self.run_script(
            "--task-key",
            "AT-GH-511",
            "--executor",
            "claude-code",
            "--confirm-approved-task",
            "--claude-code-enable-invocation",
            "--json",
        )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("requires an explicit command", payload["error"])
        self.assertFalse(payload["safety"]["executor_started"])

    def test_script_claude_code_rejects_malformed_command_json(self) -> None:
        self._add_task("AT-GH-512")
        result = self.run_script(
            "--task-key",
            "AT-GH-512",
            "--executor",
            "claude-code",
            "--confirm-approved-task",
            "--claude-code-enable-invocation",
            "--claude-code-command-json",
            "not-json",
            "--json",
        )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("valid JSON", payload["summary"])

    def test_script_claude_code_real_invocation_runs_fake_command(self) -> None:
        # A fake argv command (never a shell string) is invoked with cwd set to
        # the prepared worktree; execution artifacts are written and the task
        # still progresses through validators + Codex evidence to waiting_approval.
        from agent_taskflow.codex_advisory_review import (
            CodexAdvisoryReviewRequest,
            generate_codex_advisory_review,
        )

        self._add_task("AT-GH-513")
        generate_codex_advisory_review(
            CodexAdvisoryReviewRequest(
                task_key="AT-GH-513",
                artifact_dir=self.artifact_root / "AT-GH-513",
                dry_run=True,
            )
        )
        script = self.root / "fake_claude.py"
        script.write_text(
            "\n".join(
                [
                    "import pathlib, sys",
                    "pathlib.Path('cli_claude_made_this.txt').write_text('x\\n')",
                    "print('cli fake claude stdout')",
                    "sys.exit(0)",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        command_json = json.dumps([sys.executable, str(script)])

        result = self.run_script(
            "--task-key",
            "AT-GH-513",
            "--executor",
            "claude-code",
            "--confirm-approved-task",
            "--skip-preflight",
            "--claude-code-enable-invocation",
            "--claude-code-command-json",
            command_json,
            "--claude-code-timeout-seconds",
            "60",
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "waiting_approval")
        artifact_dir = self.store.get_task("AT-GH-513").artifact_dir
        assert artifact_dir is not None
        execution = json.loads(
            (artifact_dir / "claude-code-execution.json").read_text(encoding="utf-8")
        )
        self.assertEqual(execution["status"], "completed")
        self.assertIs(execution["invocation_enabled"], True)
        self.assertEqual(execution["command"], [sys.executable, str(script)])
        self.assertIn("cli_claude_made_this.txt", execution["changed_files"])

    def _write_fake_claude_script(self) -> Path:
        """Write a fake Claude Code executable used by the CLI golden-path smoke.

        It is plain argv (never a shell string): it writes one file into its cwd
        (the prepared worktree), emits stdout and stderr, and exits 0.
        """

        script = self.root / "fake_claude_cli.py"
        script.write_text(
            "\n".join(
                [
                    "import pathlib, sys",
                    "pathlib.Path('cli_golden_made_this.txt').write_text('made by fake claude\\n')",
                    "print('cli golden fake claude stdout')",
                    "sys.stderr.write('cli golden fake claude stderr\\n')",
                    "sys.exit(0)",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return script

    def _write_codex_advisory_evidence(self, task_key: str) -> None:
        from agent_taskflow.codex_advisory_review import (
            CodexAdvisoryReviewRequest,
            generate_codex_advisory_review,
        )

        generate_codex_advisory_review(
            CodexAdvisoryReviewRequest(
                task_key=task_key,
                artifact_dir=self.artifact_root / task_key,
                dry_run=True,
            )
        )

    def test_run_approved_task_claude_code_real_invocation_golden_path_smoke(self) -> None:
        # v0.2.9 CLI golden-path smoke: drive the v0.2.8 opt-in real invocation
        # profile end-to-end through scripts/run_approved_task.py in a safe fake-
        # command environment (no real Claude Code is invoked). It proves the CLI
        # flags wire through, the command runs as argv in the prepared worktree,
        # stdout/stderr are captured, the execution artifact records the bounded-
        # implementer contract, and the Codex advisory evidence gate stays
        # authoritative for the waiting_approval transition.
        script = self._write_fake_claude_script()
        command_json = json.dumps([sys.executable, str(script)])

        # With valid Codex advisory evidence, the run reaches waiting_approval.
        self._add_task("AT-GH-910")
        self._write_codex_advisory_evidence("AT-GH-910")
        result = self.run_script(
            "--task-key",
            "AT-GH-910",
            "--executor",
            "claude-code",
            "--confirm-approved-task",
            "--skip-preflight",
            "--claude-code-enable-invocation",
            "--claude-code-command-json",
            command_json,
            "--claude-code-timeout-seconds",
            "60",
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["executor"], "claude-code")
        self.assertEqual(payload["status"], "waiting_approval")
        # waiting_approval is a handoff to a human reviewer, not approval.
        self.assertTrue(payload["safety"]["human_approval_required"])
        self.assertFalse(payload["safety"]["approved"])
        self.assertFalse(payload["safety"]["branch_pushed"])
        self.assertFalse(payload["safety"]["merged"])
        self.assertFalse(payload["safety"]["cleanup_performed"])

        artifact_dir = self.store.get_task("AT-GH-910").artifact_dir
        assert artifact_dir is not None
        execution = json.loads(
            (artifact_dir / "claude-code-execution.json").read_text(encoding="utf-8")
        )
        self.assertEqual(execution["schema_version"], "claude_code_executor.v1")
        self.assertEqual(execution["executor"], "claude-code")
        self.assertEqual(execution["status"], "completed")
        self.assertIs(execution["invocation_enabled"], True)
        self.assertEqual(execution["exit_code"], 0)
        self.assertIs(execution["timed_out"], False)
        # Command passed as argv (a list), never a shell string.
        self.assertEqual(execution["command"], [sys.executable, str(script)])
        self.assertIsInstance(execution["command"], list)
        # cwd is the prepared worktree; the fake command ran there.
        worktree_path = self.store.get_task_worktree("AT-GH-910").worktree_path
        self.assertEqual(execution["cwd"], str(worktree_path))
        self.assertIn("cli_golden_made_this.txt", execution["changed_files"])
        # Authority invariants all denied; human review required.
        self.assertEqual(execution["validation_authority"], "none")
        self.assertEqual(execution["approval_authority"], "none")
        self.assertEqual(execution["merge_authority"], "none")
        self.assertEqual(execution["cleanup_authority"], "none")
        self.assertIs(execution["human_review_required"], True)
        # stdout and stderr captured to their own logs.
        self.assertIn(
            "cli golden fake claude stdout",
            (artifact_dir / "claude-code-stdout.log").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "cli golden fake claude stderr",
            (artifact_dir / "claude-code-stderr.log").read_text(encoding="utf-8"),
        )

        # Without Codex advisory evidence, the identical successful invocation
        # stays blocked at the evidence gate and never reaches waiting_approval.
        self._add_task("AT-GH-911")
        blocked = self.run_script(
            "--task-key",
            "AT-GH-911",
            "--executor",
            "claude-code",
            "--confirm-approved-task",
            "--skip-preflight",
            "--claude-code-enable-invocation",
            "--claude-code-command-json",
            command_json,
            "--claude-code-timeout-seconds",
            "60",
            "--json",
        )
        self.assertNotEqual(blocked.returncode, 0)
        blocked_payload = json.loads(blocked.stdout)
        self.assertEqual(blocked_payload["status"], "blocked")
        self.assertEqual(blocked_payload["phase"], "codex_advisory_evidence")
        self.assertNotEqual(
            self.store.get_task("AT-GH-911").status, "waiting_approval"
        )

    def test_script_and_runner_do_not_reference_recommendation_or_forbidden_helpers(self) -> None:
        text = (SCRIPT.read_text(encoding="utf-8") + "\n" + (REPO_ROOT / "agent_taskflow" / "approved_task_runner.py").read_text(encoding="utf-8")).lower()
        forbidden = [
            "recommend_next_tasks",
            "recommended_next_task",
            "run_recommended",
            "from_recommendation",
            "git push",
            "gh pr create",
            "gh pr merge",
            "merge_pull_request",
            "create_pull_request",
            "push_task_branch",
            "delete_worktree",
            "delete_branch",
            "cleanup(",
        ]
        for item in forbidden:
            self.assertNotIn(item, text)


if __name__ == "__main__":
    unittest.main()
