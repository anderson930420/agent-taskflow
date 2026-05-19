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
SCRIPT = REPO_ROOT / "scripts" / "recommend_next_tasks.py"


class RecommendNextTasksScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add_task(
        self,
        task_key: str,
        *,
        title: str,
        project: str = "agent-taskflow",
        created_at: str = "2026-05-01T00:00:00Z",
        labels: tuple[str, ...] = (),
        issue_spec: bool = True,
    ) -> None:
        artifact_dir = self.root / "artifacts" / task_key
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project=project,
                board=project,
                title=title,
                status="queued",
                repo_path=self.repo,
                artifact_dir=artifact_dir,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        self.store.record_task_event(
            task_key,
            "github_issue_ingested",
            "github",
            payload={
                "kind": "github_issue_ingested",
                "repo": "anderson930420/agent-taskflow",
                "issue_number": int(task_key.rsplit("-", 1)[-1]),
                "labels": list(labels),
                "selected_intake": True,
            },
        )
        if issue_spec:
            self.store.record_task_artifact(task_key, "issue_spec", artifact_dir / "issue_spec.md")

    def run_script(self, *extra_args: str, db_path: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--db-path",
                str(db_path or self.db_path),
                *extra_args,
            ],
            cwd=REPO_ROOT,
            shell=False,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def test_help_flag_succeeds(self) -> None:
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
        self.assertIn("--db-path", result.stdout)
        self.assertIn("--project", result.stdout)
        self.assertIn("--max-risk", result.stdout)

    def test_script_prints_valid_json(self) -> None:
        self.add_task("AT-GH-301", title="Ready task", labels=("ready",))
        result = self.run_script("--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["recommended_next_task"]["task_key"], "AT-GH-301")
        self.assertTrue(payload["recommended_next_task"]["requires_human_confirmation"])

    def test_script_handles_empty_db_without_creating_a_file(self) -> None:
        missing = self.root / "missing" / "state.db"
        result = self.run_script("--json", db_path=missing)

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["summary"]["queued_task_count"], 0)
        self.assertFalse(missing.exists())
        self.assertEqual(payload["ranked_tasks"], [])

    def test_script_recommends_from_queued_tasks(self) -> None:
        self.add_task("AT-GH-302", title="Plain task", created_at="2026-05-02T00:00:00Z")
        self.add_task("AT-GH-303", title="Ready task", labels=("ready",))

        result = self.run_script("--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["recommended_next_task"]["task_key"], "AT-GH-303")
        self.assertEqual(
            [item["task_key"] for item in payload["ranked_tasks"]],
            ["AT-GH-303", "AT-GH-302"],
        )

    def test_script_respects_limit(self) -> None:
        self.add_task("AT-GH-304", title="First task", labels=("ready",))
        self.add_task("AT-GH-305", title="Second task")
        self.add_task("AT-GH-306", title="Third task")

        result = self.run_script("--json", "--limit", "1")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(len(payload["ranked_tasks"]), 1)
        self.assertEqual(payload["recommended_next_task"]["task_key"], payload["ranked_tasks"][0]["task_key"])

    def test_script_respects_project_filter(self) -> None:
        self.add_task("AT-GH-307", title="Agent taskflow task", project="agent-taskflow", labels=("ready",))
        self.add_task("BJ-307", title="Bullet journal task", project="bullet-journal")

        result = self.run_script("--json", "--project", "agent-taskflow")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual([item["task_key"] for item in payload["ranked_tasks"]], ["AT-GH-307"])
        self.assertEqual(payload["summary"]["queued_task_count"], 1)

    def test_script_respects_include_and_exclude_label_filters(self) -> None:
        self.add_task("AT-GH-308", title="Docs task", labels=("docs",))
        self.add_task("AT-GH-309", title="Ready task", labels=("ready",))

        include_result = self.run_script("--json", "--include-label", "docs")
        include_payload = json.loads(include_result.stdout)
        self.assertEqual([item["task_key"] for item in include_payload["ranked_tasks"]], ["AT-GH-308"])

        exclude_result = self.run_script("--json", "--exclude-label", "docs")
        exclude_payload = json.loads(exclude_result.stdout)
        self.assertNotIn("AT-GH-308", [item["task_key"] for item in exclude_payload["ranked_tasks"]])

    def test_script_does_not_write_db_or_update_task_status(self) -> None:
        self.add_task("AT-GH-310", title="Ready task", labels=("ready",))
        before_status = self.store.get_task("AT-GH-310").status
        before_events = len(self.store.list_task_events("AT-GH-310"))
        before_artifacts = len(self.store.list_task_artifacts("AT-GH-310"))

        result = self.run_script("--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.store.get_task("AT-GH-310").status, before_status)
        self.assertEqual(len(self.store.list_task_events("AT-GH-310")), before_events)
        self.assertEqual(len(self.store.list_task_artifacts("AT-GH-310")), before_artifacts)
        self.assertIsNone(self.store.get_task_worktree("AT-GH-310"))

    def test_script_does_not_prepare_worktree_dispatch_or_merge(self) -> None:
        self.add_task("AT-GH-311", title="Ready task", labels=("ready",))

        result = self.run_script("--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["safety"]["workspace_prepared"])
        self.assertFalse(payload["safety"]["executor_started"])
        self.assertFalse(payload["safety"]["validators_started"])
        self.assertFalse(payload["safety"]["branch_pushed"])
        self.assertFalse(payload["safety"]["pr_created"])
        self.assertFalse(payload["safety"]["merged"])
        self.assertFalse(payload["safety"]["approved"])
        self.assertFalse(payload["safety"]["cleanup_performed"])

    def test_script_handles_missing_db_without_creating_a_file(self) -> None:
        missing = self.root / "absent" / "state.db"
        result = self.run_script("--json", db_path=missing)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(missing.exists())
        payload = json.loads(result.stdout)
        self.assertEqual(payload["summary"]["queued_task_count"], 0)


if __name__ == "__main__":
    unittest.main()
