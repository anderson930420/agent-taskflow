from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.task_execution_package import (
    EVENT_SOURCE,
    EVENT_TYPE,
    IMPLEMENTATION_PROMPT_FILENAME,
    ISSUE_SPEC_FILENAME,
    MAX_INLINE_SOURCE_CHARS,
    PACKAGE_FILENAME,
    PACKAGE_ARTIFACT_TYPE,
    PROMPT_ARTIFACT_TYPE,
    SCHEMA_VERSION,
    TaskExecutionPackageRequest,
    create_task_execution_package,
)


def _issue_spec_text(*, title: str = "Add widget", body: str = "Do the thing.") -> str:
    return "\n".join(
        [
            "# GitHub Issue Spec",
            "",
            "- Task key: AT-EXEC-1",
            "- Repository: example/repo",
            "- Issue number: 42",
            "- Issue URL: https://example.invalid/issues/42",
            "- Issue state: open",
            f"- Title: {title}",
            "- Labels: (none)",
            "",
            "## Body",
            "",
            body,
            "",
        ]
    )


class TaskExecutionPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.artifact_root = self.root / "artifacts"
        self.artifact_dir = self.artifact_root / "AT-EXEC-1"
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_task(
        self,
        *,
        task_key: str = "AT-EXEC-1",
        status: str = "queued",
        title: str | None = "Implement widget",
        with_artifact_dir: bool = True,
    ) -> TaskRecord:
        task = TaskRecord(
            task_key=task_key,
            project="agent-taskflow",
            board="agent-taskflow",
            title=title,
            status=status,
            repo_path=self.repo,
            artifact_dir=self.artifact_dir if with_artifact_dir else None,
        )
        self.store.upsert_task(task)
        return task

    def _request(self, **overrides: object) -> TaskExecutionPackageRequest:
        kwargs: dict[str, object] = {
            "task_key": "AT-EXEC-1",
            "db_path": self.db_path,
            "artifact_root": self.artifact_root,
            "dry_run": True,
            "confirm": False,
        }
        kwargs.update(overrides)
        return TaskExecutionPackageRequest(**kwargs)  # type: ignore[arg-type]

    # 1. blocks when task does not exist
    def test_blocks_when_task_missing(self) -> None:
        result = create_task_execution_package(self._request())
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("Task not found", result["error"])
        self.assertIs(result["safety"]["execution_package_created"], False)

    # 2. blocks when task status is not queued
    def test_blocks_when_status_not_queued(self) -> None:
        self._seed_task(status="waiting_approval")
        result = create_task_execution_package(self._request())
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("status=queued", result["error"])
        self.assertEqual(result["task_status_before"], "waiting_approval")

    # 3. dry-run returns preview and writes no files/events/artifacts
    def test_dry_run_writes_nothing(self) -> None:
        self._seed_task()
        result = create_task_execution_package(self._request(dry_run=True, confirm=False))
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "dry_run")
        self.assertFalse(result["safety"]["db_written"])
        self.assertFalse(result["safety"]["artifact_written"])
        self.assertFalse(result["safety"]["execution_package_created"])
        self.assertFalse(result["safety"]["implementation_prompt_created"])
        self.assertFalse((self.artifact_dir / IMPLEMENTATION_PROMPT_FILENAME).exists())
        self.assertFalse((self.artifact_dir / PACKAGE_FILENAME).exists())
        self.assertEqual(self.store.list_task_artifacts("AT-EXEC-1"), [])
        events = [e for e in self.store.list_task_events("AT-EXEC-1") if e.event_type == EVENT_TYPE]
        self.assertEqual(events, [])

    # 4. confirmed run writes implementation_prompt.md
    def test_confirm_writes_implementation_prompt(self) -> None:
        self._seed_task()
        result = create_task_execution_package(self._request(dry_run=False, confirm=True))
        self.assertTrue(result["ok"])
        prompt_path = self.artifact_dir / IMPLEMENTATION_PROMPT_FILENAME
        self.assertTrue(prompt_path.exists())
        text = prompt_path.read_text(encoding="utf-8")
        self.assertIn("# Implementation Prompt — AT-EXEC-1", text)
        self.assertIn("AGENTS.md", text)
        self.assertIn("WORKFLOW.md", text)
        self.assertIn("Implement widget", text)

    # 5. confirmed run writes task_execution_package.json
    def test_confirm_writes_package_json(self) -> None:
        self._seed_task()
        result = create_task_execution_package(self._request(dry_run=False, confirm=True))
        self.assertTrue(result["ok"])
        package_path = self.artifact_dir / PACKAGE_FILENAME
        self.assertTrue(package_path.exists())
        payload = json.loads(package_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["task_key"], "AT-EXEC-1")
        self.assertEqual(payload["project"], "agent-taskflow")
        self.assertEqual(payload["status_before"], "queued")
        self.assertEqual(payload["repo_path"], str(self.repo))
        self.assertEqual(payload["artifact_dir"], str(self.artifact_dir))
        self.assertEqual(payload["implementation_prompt_path"], str(self.artifact_dir / IMPLEMENTATION_PROMPT_FILENAME))
        self.assertEqual(payload["required_validators"], ["pytest", "policy", "changed-files"])
        self.assertFalse(payload["dry_run"])
        self.assertFalse(payload["safety"]["executor_started"])

    # 6. confirmed run records artifacts in store
    def test_confirm_records_artifacts(self) -> None:
        self._seed_task()
        create_task_execution_package(self._request(dry_run=False, confirm=True))
        artifacts = self.store.list_task_artifacts("AT-EXEC-1")
        kinds = {(record.artifact_type, str(record.path)) for record in artifacts}
        self.assertIn(
            (PROMPT_ARTIFACT_TYPE, str(self.artifact_dir / IMPLEMENTATION_PROMPT_FILENAME)),
            kinds,
        )
        self.assertIn(
            (PACKAGE_ARTIFACT_TYPE, str(self.artifact_dir / PACKAGE_FILENAME)),
            kinds,
        )

    # 7. confirmed run records task_execution_package_created event
    def test_confirm_records_event(self) -> None:
        self._seed_task()
        create_task_execution_package(self._request(dry_run=False, confirm=True))
        events = [e for e in self.store.list_task_events("AT-EXEC-1") if e.event_type == EVENT_TYPE]
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.source, EVENT_SOURCE)
        payload = json.loads(event.payload_json or "{}")
        self.assertEqual(payload["kind"], EVENT_TYPE)
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["task_key"], "AT-EXEC-1")
        self.assertEqual(
            payload["implementation_prompt_path"],
            str(self.artifact_dir / IMPLEMENTATION_PROMPT_FILENAME),
        )

    # 8. safety block confirms no executor/workspace/validator/push/PR/merge/cleanup
    def test_safety_block_confirms_no_runtime_actions(self) -> None:
        self._seed_task()
        result = create_task_execution_package(self._request(dry_run=False, confirm=True))
        safety = result["safety"]
        for key in (
            "workspace_prepared",
            "executor_started",
            "validators_started",
            "branch_pushed",
            "pr_created",
            "merged",
            "approved",
            "cleanup_performed",
            "background_worker_started",
        ):
            self.assertFalse(safety[key], f"safety.{key} must be False")
        self.assertTrue(safety["execution_package_created"])
        self.assertTrue(safety["implementation_prompt_created"])
        self.assertTrue(safety["db_written"])
        self.assertTrue(safety["artifact_written"])

    # 9. builder can use github_issue_ingested event payload as source evidence
    def test_uses_github_issue_ingested_event_as_source(self) -> None:
        self._seed_task(title=None)
        self.store.record_task_event(
            "AT-EXEC-1",
            "github_issue_ingested",
            "github_issue_intake",
            message="ingested",
            payload={
                "kind": "github_issue_ingested",
                "repo": "anthropic-experimental/agent-taskflow",
                "issue_number": 42,
                "issue_url": "https://example.invalid/issues/42",
                "title": "Add widget",
                "task_key": "AT-EXEC-1",
                "status": "queued",
            },
        )
        result = create_task_execution_package(self._request(dry_run=False, confirm=True))
        self.assertTrue(result["ok"])
        evidence = result["source_evidence"]
        self.assertIsNone(evidence["issue_spec_artifact_path"])
        self.assertIsNone(evidence["issue_spec_file_path"])
        self.assertIsNotNone(evidence["github_issue_ingested_event"])
        self.assertEqual(evidence["github_issue_ingested_event"]["issue_number"], 42)
        prompt_text = (self.artifact_dir / IMPLEMENTATION_PROMPT_FILENAME).read_text(encoding="utf-8")
        self.assertIn("anthropic-experimental/agent-taskflow#42", prompt_text)
        self.assertIn("Add widget", prompt_text)

    # builder inlines issue body from recorded issue_spec artifact
    def test_inlines_body_from_recorded_issue_spec_artifact(self) -> None:
        self._seed_task(title=None)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        spec_path = self.artifact_dir / ISSUE_SPEC_FILENAME
        spec_path.write_text(
            _issue_spec_text(
                title="Inline this title",
                body="Implementation requirement: do X then Y.\nSecond paragraph.",
            ),
            encoding="utf-8",
        )
        self.store.record_task_artifact("AT-EXEC-1", "issue_spec", spec_path)

        result = create_task_execution_package(self._request(dry_run=False, confirm=True))
        self.assertTrue(result["ok"])
        evidence = result["source_evidence"]
        self.assertEqual(evidence["issue_spec_artifact_path"], str(spec_path))

        prompt_text = (self.artifact_dir / IMPLEMENTATION_PROMPT_FILENAME).read_text(encoding="utf-8")
        # Executor-visible inlined content
        self.assertIn("Executor-visible task content:", prompt_text)
        self.assertIn("Title: Inline this title", prompt_text)
        self.assertIn("Body:", prompt_text)
        self.assertIn("Implementation requirement: do X then Y.", prompt_text)
        self.assertIn("Second paragraph.", prompt_text)
        # The audit path must NOT be inlined in the executor-visible prompt
        # (Phase 6E+3.5 — prevent external_directory read temptation).
        self.assertNotIn(str(spec_path), prompt_text)
        # The audit-only label and "already inlined" wording must be present.
        self.assertIn("task_execution_package.json for audit", prompt_text)
        self.assertIn("already inlined", prompt_text)
        # The package JSON must still carry the absolute path for auditability.
        package_payload = result["package"]
        self.assertEqual(
            package_payload["source_evidence"]["issue_spec_artifact_path"],
            str(spec_path),
        )

    # builder inlines body from artifact_dir/issue_spec.md without an artifact record
    def test_inlines_body_from_artifact_dir_issue_spec_file(self) -> None:
        self._seed_task(title=None)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        spec_path = self.artifact_dir / ISSUE_SPEC_FILENAME
        spec_path.write_text(
            _issue_spec_text(
                title="File-based title",
                body="Body discovered via artifact_dir scan.",
            ),
            encoding="utf-8",
        )

        result = create_task_execution_package(self._request(dry_run=False, confirm=True))
        self.assertTrue(result["ok"])
        evidence = result["source_evidence"]
        self.assertIsNone(evidence["issue_spec_artifact_path"])
        self.assertEqual(evidence["issue_spec_file_path"], str(spec_path))

        prompt_text = (self.artifact_dir / IMPLEMENTATION_PROMPT_FILENAME).read_text(encoding="utf-8")
        self.assertIn("Title: File-based title", prompt_text)
        self.assertIn("Body discovered via artifact_dir scan.", prompt_text)
        # No absolute spec path in the executor-visible prompt.
        self.assertNotIn(str(spec_path), prompt_text)
        # Audit metadata still carried in source_evidence.
        self.assertEqual(
            result["source_evidence"]["issue_spec_file_path"], str(spec_path),
        )

    # long issue body is truncated and includes the truncation notice
    def test_long_issue_body_truncated_with_notice(self) -> None:
        self._seed_task(title=None)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        long_body = "A" * (MAX_INLINE_SOURCE_CHARS + 5000)
        spec_path = self.artifact_dir / ISSUE_SPEC_FILENAME
        spec_path.write_text(
            _issue_spec_text(title="Long body", body=long_body),
            encoding="utf-8",
        )
        self.store.record_task_artifact("AT-EXEC-1", "issue_spec", spec_path)

        result = create_task_execution_package(self._request(dry_run=False, confirm=True))
        self.assertTrue(result["ok"])
        prompt_text = (self.artifact_dir / IMPLEMENTATION_PROMPT_FILENAME).read_text(encoding="utf-8")

        self.assertIn(
            f"[source truncated after {MAX_INLINE_SOURCE_CHARS} characters]",
            prompt_text,
        )
        # Full untruncated body should not be present
        self.assertNotIn("A" * (MAX_INLINE_SOURCE_CHARS + 1), prompt_text)
        # But the truncated prefix should be present
        self.assertIn("A" * 1000, prompt_text)

    # bounded excerpt fallback when issue_spec exists but has no parseable body
    def test_bounded_excerpt_when_body_missing(self) -> None:
        self._seed_task(title=None)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        spec_path = self.artifact_dir / ISSUE_SPEC_FILENAME
        # Spec without "## Body" section
        spec_path.write_text(
            "# GitHub Issue Spec\n\n- Title: Just metadata\n",
            encoding="utf-8",
        )
        self.store.record_task_artifact("AT-EXEC-1", "issue_spec", spec_path)

        result = create_task_execution_package(self._request(dry_run=False, confirm=True))
        self.assertTrue(result["ok"])
        prompt_text = (self.artifact_dir / IMPLEMENTATION_PROMPT_FILENAME).read_text(encoding="utf-8")
        self.assertIn("Title: Just metadata", prompt_text)
        self.assertIn("Source excerpt", prompt_text)
        self.assertIn("# GitHub Issue Spec", prompt_text)

    # 10. builder can fallback to TaskRecord title when no issue artifact/event exists
    def test_falls_back_to_task_title(self) -> None:
        self._seed_task(title="Implement widget")
        result = create_task_execution_package(self._request(dry_run=False, confirm=True))
        evidence = result["source_evidence"]
        self.assertIsNone(evidence["issue_spec_artifact_path"])
        self.assertIsNone(evidence["issue_spec_file_path"])
        self.assertIsNone(evidence["github_issue_ingested_event"])
        self.assertEqual(evidence["title_fallback"], "Implement widget")
        prompt_text = (self.artifact_dir / IMPLEMENTATION_PROMPT_FILENAME).read_text(encoding="utf-8")
        self.assertIn("Treat the task title as the source intent: Implement widget", prompt_text)


class TaskExecutionPackageRequestTests(unittest.TestCase):
    def test_dry_run_and_confirm_are_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            TaskExecutionPackageRequest(
                task_key="AT-EXEC-1",
                db_path=Path("/tmp/state.db"),
                artifact_root=Path("/tmp/artifacts"),
                dry_run=True,
                confirm=True,
            )

    def test_non_dry_run_requires_confirm(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires confirm=True"):
            TaskExecutionPackageRequest(
                task_key="AT-EXEC-1",
                db_path=Path("/tmp/state.db"),
                artifact_root=Path("/tmp/artifacts"),
                dry_run=False,
                confirm=False,
            )

    def test_empty_required_validator_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "required_validators entries must be non-empty"):
            TaskExecutionPackageRequest(
                task_key="AT-EXEC-1",
                db_path=Path("/tmp/state.db"),
                artifact_root=Path("/tmp/artifacts"),
                required_validators=("pytest", "   "),
            )


if __name__ == "__main__":
    unittest.main()
