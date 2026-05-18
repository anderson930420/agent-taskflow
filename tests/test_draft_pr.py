"""Tests for agent_taskflow.draft_pr."""

from __future__ import annotations

from dataclasses import dataclass
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.draft_pr import (
    DraftPrCreationRequest,
    DraftPrError,
    create_draft_pr,
)
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore


@dataclass
class FakeCompletedProcess:
    returncode: int
    stdout: str
    stderr: str = ""


class FakeRunner:
    def __init__(self, completed: list[FakeCompletedProcess] | None = None) -> None:
        self.completed = completed or [
            FakeCompletedProcess(
                returncode=0,
                stdout="https://github.com/anderson930420/agent-taskflow/pull/123\n",
            ),
            FakeCompletedProcess(
                returncode=0,
                stdout=json.dumps(
                    {
                        "url": "https://github.com/anderson930420/agent-taskflow/pull/123",
                        "number": 123,
                        "headRefName": "task/AT-DRAFT-001",
                        "baseRefName": "main",
                        "isDraft": True,
                    }
                ),
            ),
        ]
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> FakeCompletedProcess:
        self.calls.append({"args": args, "kwargs": kwargs})
        return self.completed.pop(0)


class DraftPrTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.worktree = self.root / "repo" / ".worktrees" / "AT-DRAFT-001"
        self.db_path = self.root / "state.db"
        self.artifact_dir = self.root / "artifacts" / "AT-DRAFT-001"
        self.handoff_dir = self.artifact_dir.parent / "pr_handoff" / "AT-DRAFT-001"
        self.handoff_json = self.handoff_dir / "pr_handoff.json"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self._create_valid_task()
        self._create_valid_handoff()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _create_valid_task(
        self,
        *,
        task_key: str = "AT-DRAFT-001",
        status: str = "waiting_approval",
        with_worktree: bool = True,
    ) -> None:
        self.repo.mkdir(parents=True, exist_ok=True)
        self.worktree.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert_task(
            TaskRecord(
                task_key=task_key,
                project="agent-taskflow",
                board="agent-taskflow",
                title="Draft PR creation test",
                status=status,
                repo_path=self.repo,
                artifact_dir=self.artifact_dir,
            )
        )
        if with_worktree:
            self.store.upsert_task_worktree(
                TaskWorktreeRecord(
                    task_key=task_key,
                    repo_path=self.repo,
                    worktree_path=self.worktree,
                    branch="task/AT-DRAFT-001",
                    base_branch="main",
                    base_sha="abc123",
                    status="active",
                )
            )

    def _create_valid_handoff(self, **overrides: Any) -> None:
        self.handoff_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "1",
            "artifact_type": "pr_handoff",
            "task_key": "AT-DRAFT-001",
            "task_status": "waiting_approval",
            "repo": "anderson930420/agent-taskflow",
            "worktree_path": str(self.worktree),
            "branch": "task/AT-DRAFT-001",
            "base_branch": "main",
            "base_sha": "abc123",
            "head_sha": "def456",
            "changed_files": ["feature.txt"],
            "proposed_pr": {
                "title": "AT-DRAFT-001: Draft PR creation test",
                "body": "Task: AT-DRAFT-001\n",
                "base_branch": "main",
                "head_branch": "task/AT-DRAFT-001",
                "draft_recommended": True,
                "create_command_preview": "gh pr create --draft",
            },
            "safety": {
                "pr_created": False,
                "pushed": False,
                "merged": False,
                "cleanup_performed": False,
                "github_mutated": False,
                "human_review_required": True,
            },
            "generated_at": "2026-05-18T00:00:00Z",
        }
        payload.update(overrides)
        self.handoff_json.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.store.record_task_artifact("AT-DRAFT-001", "pr_handoff", self.handoff_json)

    def _request(
        self,
        *,
        dry_run: bool = True,
        confirm: bool = False,
        task_key: str = "AT-DRAFT-001",
        handoff_json: Path | None = None,
    ) -> DraftPrCreationRequest:
        return DraftPrCreationRequest(
            task_key=task_key,
            db_path=self.db_path,
            handoff_json=handoff_json,
            dry_run=dry_run,
            confirm_create_pr=confirm,
        )

    def test_dry_run_without_confirm_never_calls_gh_and_outputs_preview(self) -> None:
        runner = FakeRunner()
        result = create_draft_pr(self._request(), runner=runner)

        self.assertEqual(result.status, "dry_run")
        self.assertEqual(runner.calls, [])
        self.assertIn("gh pr create --draft", result.command_preview)
        self.assertFalse(result.github_mutated)
        self.assertFalse(result.pr_created)

    def test_explicit_dry_run_with_confirm_still_never_calls_gh(self) -> None:
        runner = FakeRunner()
        result = create_draft_pr(
            self._request(dry_run=True, confirm=True),
            runner=runner,
        )

        self.assertEqual(result.status, "dry_run")
        self.assertEqual(runner.calls, [])

    def test_real_creation_path_requires_confirm_create_pr(self) -> None:
        runner = FakeRunner()
        result = create_draft_pr(
            self._request(dry_run=True, confirm=False),
            runner=runner,
        )

        self.assertEqual(result.status, "dry_run")
        self.assertEqual(runner.calls, [])

    def test_real_creation_executes_create_then_read_only_view_shell_false(self) -> None:
        runner = FakeRunner()
        result = create_draft_pr(
            self._request(dry_run=False, confirm=True),
            runner=runner,
        )

        self.assertEqual(result.status, "created")
        self.assertEqual(len(runner.calls), 2)
        create_call = runner.calls[0]
        self.assertEqual(
            create_call["args"][:5],
            ["gh", "pr", "create", "--draft", "--repo"],
        )
        self.assertIn("--base", create_call["args"])
        self.assertIn("--head", create_call["args"])
        self.assertIn("--title", create_call["args"])
        self.assertIn("--body", create_call["args"])
        self.assertNotIn("--json", create_call["args"])
        self.assertIs(create_call["kwargs"]["shell"], False)
        self.assertEqual(create_call["kwargs"]["cwd"], self.worktree)

        view_call = runner.calls[1]
        self.assertEqual(
            view_call["args"][:4],
            [
                "gh",
                "pr",
                "view",
                "https://github.com/anderson930420/agent-taskflow/pull/123",
            ],
        )
        self.assertIn("--repo", view_call["args"])
        self.assertIn("--json", view_call["args"])
        self.assertEqual(
            view_call["args"][view_call["args"].index("--json") + 1],
            "url,number,headRefName,baseRefName,isDraft",
        )
        self.assertIs(view_call["kwargs"]["shell"], False)
        self.assertEqual(view_call["kwargs"]["cwd"], self.worktree)

    def test_missing_pr_url_from_create_stdout_raises(self) -> None:
        runner = FakeRunner(
            [
                FakeCompletedProcess(returncode=0, stdout="created\n"),
            ]
        )

        with self.assertRaisesRegex(DraftPrError, "did not print"):
            create_draft_pr(self._request(dry_run=False, confirm=True), runner=runner)

    def test_gh_view_invalid_json_raises(self) -> None:
        runner = FakeRunner(
            [
                FakeCompletedProcess(
                    returncode=0,
                    stdout="https://github.com/anderson930420/agent-taskflow/pull/124\n",
                ),
                FakeCompletedProcess(returncode=0, stdout="not-json\n"),
            ]
        )

        with self.assertRaisesRegex(DraftPrError, "gh pr view returned invalid JSON"):
            create_draft_pr(self._request(dry_run=False, confirm=True), runner=runner)

    def test_gh_view_requires_is_draft_true(self) -> None:
        runner = FakeRunner(
            [
                FakeCompletedProcess(
                    returncode=0,
                    stdout="https://github.com/anderson930420/agent-taskflow/pull/124\n",
                ),
                FakeCompletedProcess(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "url": "https://github.com/anderson930420/agent-taskflow/pull/124",
                            "number": 124,
                            "headRefName": "task/AT-DRAFT-001",
                            "baseRefName": "main",
                            "isDraft": False,
                        }
                    ),
                ),
            ]
        )

        with self.assertRaisesRegex(DraftPrError, "draft PR"):
            create_draft_pr(self._request(dry_run=False, confirm=True), runner=runner)

    def test_gh_view_base_head_mismatch_raises(self) -> None:
        runner = FakeRunner(
            [
                FakeCompletedProcess(
                    returncode=0,
                    stdout="https://github.com/anderson930420/agent-taskflow/pull/125\n",
                ),
                FakeCompletedProcess(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "url": "https://github.com/anderson930420/agent-taskflow/pull/125",
                            "number": 125,
                            "headRefName": "task/AT-DRAFT-001",
                            "baseRefName": "release",
                            "isDraft": True,
                        }
                    ),
                ),
            ]
        )

        with self.assertRaisesRegex(DraftPrError, "baseRefName"):
            create_draft_pr(self._request(dry_run=False, confirm=True), runner=runner)

    def test_rejects_missing_task(self) -> None:
        with self.assertRaisesRegex(DraftPrError, "Task not found"):
            create_draft_pr(self._request(task_key="AT-MISSING"))

    def test_rejects_task_not_waiting_approval(self) -> None:
        self._create_valid_task(task_key="AT-NOT-READY", status="queued")

        with self.assertRaisesRegex(DraftPrError, "waiting_approval"):
            create_draft_pr(self._request(task_key="AT-NOT-READY"))

    def test_rejects_missing_task_worktree_record(self) -> None:
        self._create_valid_task(task_key="AT-NO-WORKTREE", with_worktree=False)

        with self.assertRaisesRegex(DraftPrError, "TaskWorktreeRecord missing"):
            create_draft_pr(self._request(task_key="AT-NO-WORKTREE"))

    def test_rejects_missing_pr_handoff_artifact(self) -> None:
        self._create_valid_task(task_key="AT-NO-HANDOFF")

        with self.assertRaisesRegex(DraftPrError, "pr_handoff artifact missing"):
            create_draft_pr(self._request(task_key="AT-NO-HANDOFF"))

    def test_rejects_missing_pr_handoff_json_file(self) -> None:
        missing = self.handoff_dir / "missing.json"
        self.store.record_task_artifact("AT-DRAFT-001", "pr_handoff", missing)

        with self.assertRaisesRegex(DraftPrError, "pr_handoff.json is missing"):
            create_draft_pr(self._request(handoff_json=missing))

    def test_rejects_non_conservative_handoff_safety_flags(self) -> None:
        unsafe = dict(json.loads(self.handoff_json.read_text(encoding="utf-8")))
        unsafe["safety"]["pr_created"] = True
        self.handoff_json.write_text(json.dumps(unsafe), encoding="utf-8")

        with self.assertRaisesRegex(DraftPrError, "safety.pr_created"):
            create_draft_pr(self._request())

    def test_rejects_handoff_when_draft_recommended_is_not_true(self) -> None:
        payload = json.loads(self.handoff_json.read_text(encoding="utf-8"))
        payload["proposed_pr"]["draft_recommended"] = False
        self.handoff_json.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(DraftPrError, "recommend a draft PR"):
            create_draft_pr(self._request())

    def test_records_draft_pr_created_event_after_successful_creation(self) -> None:
        result = create_draft_pr(
            self._request(dry_run=False, confirm=True),
            runner=FakeRunner(),
        )

        self.assertTrue(result.event_recorded)
        events = self.store.list_task_events("AT-DRAFT-001")
        self.assertTrue(any(event.event_type == "draft_pr_created" for event in events))

    def test_writes_draft_pr_json_artifact_after_successful_creation(self) -> None:
        result = create_draft_pr(
            self._request(dry_run=False, confirm=True),
            runner=FakeRunner(),
        )

        self.assertTrue(result.artifact_recorded)
        assert result.draft_pr_json_path is not None
        self.assertTrue(result.draft_pr_json_path.is_file())
        payload = json.loads(result.draft_pr_json_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["kind"], "draft_pr_created")
        artifacts = self.store.list_task_artifacts("AT-DRAFT-001")
        self.assertTrue(
            any(
                artifact.artifact_type == "draft_pr"
                and artifact.path == result.draft_pr_json_path
                for artifact in artifacts
            )
        )

    def test_dry_run_records_no_event_or_artifact(self) -> None:
        result = create_draft_pr(self._request(), runner=FakeRunner())

        self.assertFalse(result.event_recorded)
        self.assertFalse(result.artifact_recorded)
        self.assertFalse((self.handoff_dir / "draft_pr.json").exists())
        self.assertFalse(
            any(
                event.event_type == "draft_pr_created"
                for event in self.store.list_task_events("AT-DRAFT-001")
            )
        )
        self.assertFalse(
            any(
                artifact.artifact_type == "draft_pr"
                for artifact in self.store.list_task_artifacts("AT-DRAFT-001")
            )
        )


if __name__ == "__main__":
    unittest.main()
