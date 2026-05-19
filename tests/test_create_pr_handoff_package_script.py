from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.github_issue_ingestion import GitHubIssueSnapshot, render_issue_spec
from agent_taskflow.mission_contract import build_mission_contract, write_mission_contract
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "create_pr_handoff_package.py"


class CreatePrHandoffPackageScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.package_root = self.root / "packages"
        self.worktree = self.root / "worktree"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.base_sha = self._init_repo()
        self._git(["worktree", "add", "-b", "task/AT-HANDOFF-PKG-CLI", str(self.worktree), "main"])
        (self.worktree / "z-change.txt").write_text("z\n", encoding="utf-8")
        (self.worktree / "a-change.txt").write_text("a\n", encoding="utf-8")
        self._seed_task()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
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

    def _init_repo(self) -> str:
        self.repo.mkdir()
        self._git(["init"])
        self._git(["config", "user.email", "agent-taskflow@example.invalid"])
        self._git(["config", "user.name", "Agent Taskflow"])
        (self.repo / "README.md").write_text("# handoff package cli\n", encoding="utf-8")
        self._git(["add", "README.md"])
        self._git(["commit", "-m", "initial"])
        self._git(["branch", "-M", "main"])
        return self._git(["rev-parse", "main"]).stdout.strip()

    def _issue_snapshot(self) -> GitHubIssueSnapshot:
        return GitHubIssueSnapshot(
            number=1002,
            title="CLI PR handoff package",
            body="Task body",
            state="open",
            labels=("ready",),
            author="octocat",
            url="https://github.com/anderson930420/agent-taskflow/issues/1002",
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-02T00:00:00Z",
        )

    def _seed_task(self, *, status: str = "waiting_approval") -> None:
        task_key = "AT-HANDOFF-PKG-CLI"
        artifact_dir = self.artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="CLI handoff package task",
                status=status,
                repo_path=self.repo,
                artifact_dir=artifact_dir,
            )
        )
        self.store.upsert_task_worktree(
            TaskWorktreeRecord(
                task_key=task_key,
                repo_path=self.repo,
                worktree_path=self.worktree,
                branch=f"task/{task_key}",
                base_branch="main",
                base_sha=self.base_sha,
                status="active",
            )
        )
        issue_spec_path = artifact_dir / "issue_spec.md"
        issue_spec_path.write_text(
            render_issue_spec(
                repo="anderson930420/agent-taskflow",
                task_key=task_key,
                issue=self._issue_snapshot(),
                ingested_at="2026-05-03T00:00:00Z",
            ),
            encoding="utf-8",
        )
        self.store.record_task_artifact(task_key, "issue_spec", issue_spec_path)
        contract = build_mission_contract(
            task_key=task_key,
            goal="Create a PR handoff package from waiting approval",
            repo_path=self.repo,
            worktree_path=self.worktree,
            artifact_dir=artifact_dir,
            executor="noop",
            required_validators=("pytest",),
        )
        write_mission_contract(contract, artifact_dir=artifact_dir)
        executor_log = artifact_dir / "executor.log"
        executor_log.write_text("executor log\n", encoding="utf-8")
        run_id = self.store.create_executor_run(task_key, "noop")
        self.store.finish_executor_run(
            task_key,
            run_id,
            executor="noop",
            status="completed",
            exit_code=0,
            summary="executor summary",
            log_path=executor_log,
            artifacts={"log": executor_log},
        )
        self.store.record_task_artifact(task_key, "worker_log", executor_log)
        validator_log = artifact_dir / "pytest.log"
        validator_log.write_text("validator log\n", encoding="utf-8")
        self.store.record_validation_result(
            task_key,
            "pytest",
            status="passed",
            exit_code=0,
            summary="validator summary",
            log_path=validator_log,
            artifacts={"log": validator_log},
        )
        self.store.record_task_artifact(task_key, "review_log", validator_log)

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=REPO_ROOT,
            shell=False,
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
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("--task-key", result.stdout)

        missing = self._run(
            "--repo-path",
            str(self.repo),
            "--db-path",
            str(self.db_path),
            "--json",
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("--task-key", missing.stderr)

    def test_script_prints_valid_json(self) -> None:
        result = self._run(
            "--task-key",
            "AT-HANDOFF-PKG-CLI",
            "--repo-path",
            str(self.repo),
            "--db-path",
            str(self.db_path),
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["task_key"], "AT-HANDOFF-PKG-CLI")
        self.assertTrue(payload["summary"]["handoff_package_created"])

    def test_script_supports_pretty(self) -> None:
        result = self._run(
            "--task-key",
            "AT-HANDOFF-PKG-CLI",
            "--repo-path",
            str(self.repo),
            "--db-path",
            str(self.db_path),
            "--pretty",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("\n  ", result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["task_key"], "AT-HANDOFF-PKG-CLI")

    def test_script_dry_run_does_not_write_artifact_or_event(self) -> None:
        before_artifacts = len(self.store.list_task_artifacts("AT-HANDOFF-PKG-CLI"))
        before_events = len(self.store.list_task_events("AT-HANDOFF-PKG-CLI"))

        result = self._run(
            "--task-key",
            "AT-HANDOFF-PKG-CLI",
            "--repo-path",
            str(self.repo),
            "--db-path",
            str(self.db_path),
            "--artifact-root",
            str(self.package_root),
            "--dry-run",
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before_artifacts, len(self.store.list_task_artifacts("AT-HANDOFF-PKG-CLI")))
        self.assertEqual(before_events, len(self.store.list_task_events("AT-HANDOFF-PKG-CLI")))
        package_dir = self.package_root / "pr_handoff_package" / "AT-HANDOFF-PKG-CLI"
        self.assertFalse(package_dir.exists())

    def test_script_non_dry_run_writes_local_handoff_artifact_and_event(self) -> None:
        result = self._run(
            "--task-key",
            "AT-HANDOFF-PKG-CLI",
            "--repo-path",
            str(self.repo),
            "--db-path",
            str(self.db_path),
            "--artifact-root",
            str(self.package_root),
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["artifact_recorded"])
        self.assertTrue(payload["event_recorded"])
        package_dir = self.package_root / "pr_handoff_package" / "AT-HANDOFF-PKG-CLI"
        self.assertTrue((package_dir / "pr_handoff_package.json").is_file())
        self.assertTrue((package_dir / "pr_handoff_package.md").is_file())
        self.assertTrue(
            any(
                artifact.artifact_type == "pr_handoff_package"
                for artifact in self.store.list_task_artifacts("AT-HANDOFF-PKG-CLI")
            )
        )
        self.assertTrue(
            any(
                event.event_type == "pr_handoff_package_created"
                for event in self.store.list_task_events("AT-HANDOFF-PKG-CLI")
            )
        )

    def test_script_rejects_non_waiting_task_by_default(self) -> None:
        self._seed_task(status="blocked")
        result = self._run(
            "--task-key",
            "AT-HANDOFF-PKG-CLI",
            "--repo-path",
            str(self.repo),
            "--db-path",
            str(self.db_path),
            "--json",
        )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("waiting_approval", payload["summary"])

    def test_script_handles_missing_db_without_creating_file(self) -> None:
        missing_db = self.root / "missing.db"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--task-key",
                "AT-HANDOFF-PKG-CLI",
                "--repo-path",
                str(self.repo),
                "--db-path",
                str(missing_db),
                "--json",
            ],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertFalse(missing_db.exists())

    def test_script_does_not_update_task_status(self) -> None:
        before = self.store.get_task("AT-HANDOFF-PKG-CLI")
        result = self._run(
            "--task-key",
            "AT-HANDOFF-PKG-CLI",
            "--repo-path",
            str(self.repo),
            "--db-path",
            str(self.db_path),
            "--artifact-root",
            str(self.package_root),
            "--json",
        )
        after = self.store.get_task("AT-HANDOFF-PKG-CLI")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before.status, after.status)

    def test_script_does_not_prepare_worktree_or_dispatch_or_validate(self) -> None:
        result = self._run(
            "--task-key",
            "AT-HANDOFF-PKG-CLI",
            "--repo-path",
            str(self.repo),
            "--db-path",
            str(self.db_path),
            "--artifact-root",
            str(self.package_root),
            "--json",
        )

        payload = json.loads(result.stdout)
        self.assertTrue(payload["summary"]["handoff_package_created"])
        self.assertFalse(payload["safety"]["workspace_prepared"])
        self.assertFalse(payload["safety"]["executor_started"])
        self.assertFalse(payload["safety"]["validators_started"])

    def test_script_does_not_push_create_pr_merge_approve_or_cleanup(self) -> None:
        result = self._run(
            "--task-key",
            "AT-HANDOFF-PKG-CLI",
            "--repo-path",
            str(self.repo),
            "--db-path",
            str(self.db_path),
            "--artifact-root",
            str(self.package_root),
            "--json",
        )

        payload = json.loads(result.stdout)
        self.assertFalse(payload["safety"]["branch_pushed"])
        self.assertFalse(payload["safety"]["pr_created"])
        self.assertFalse(payload["safety"]["merged"])
        self.assertFalse(payload["safety"]["approved"])
        self.assertFalse(payload["safety"]["cleanup_performed"])
        self.assertFalse(payload["safety"]["branch_deleted"])
        self.assertFalse(payload["safety"]["worktree_deleted"])

    def test_script_source_does_not_include_mutation_helpers(self) -> None:
        script_text = SCRIPT.read_text(encoding="utf-8").lower()
        module_text = (
            REPO_ROOT / "agent_taskflow" / "pr_handoff_package.py"
        ).read_text(encoding="utf-8").lower()
        combined = script_text + "\n" + module_text

        forbidden = [
            "prepare_worktree",
            "dispatch",
            "update_task_status",
            "upsert_task",
        ]
        for item in forbidden:
            self.assertNotIn(item, combined)


if __name__ == "__main__":
    unittest.main()
