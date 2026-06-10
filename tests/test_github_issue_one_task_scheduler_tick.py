from __future__ import annotations

import fcntl
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_taskflow.execution_observability import (
    summarize_scheduler_tick_payload,
    to_observability_dict,
)
from agent_taskflow.github_issue_discovery import GitHubIssueDiscoveryIssue
from agent_taskflow.github_issue_ingestion import GitHubIssueSnapshot
from agent_taskflow.github_issue_one_task_scheduler_tick import (
    GITHUB_ISSUE_ONE_TASK_SCHEDULER_TICK_SCHEMA_VERSION,
    GITHUB_ISSUE_ONE_TASK_SCHEDULER_TICK_SOURCE,
    GitHubIssueOneTaskSchedulerTickRequest,
    run_github_issue_one_task_scheduler_tick,
)


def discovery_issue(
    number: int,
    *,
    title: str | None = None,
    state: str = "open",
    labels: tuple[str, ...] = (),
) -> GitHubIssueDiscoveryIssue:
    return GitHubIssueDiscoveryIssue(
        number=number,
        title=title or f"Issue {number}",
        state=state,
        labels=labels,
        url=f"https://github.com/anderson930420/agent-taskflow/issues/{number}",
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-02T00:00:00Z",
    )


def issue_snapshot(number: int) -> GitHubIssueSnapshot:
    return GitHubIssueSnapshot(
        number=number,
        title=f"Issue {number}",
        body="Issue body for scheduler tick test.",
        state="open",
        labels=("ready",),
        author="octocat",
        url=f"https://github.com/anderson930420/agent-taskflow/issues/{number}",
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-02T00:00:00Z",
    )


class GitHubIssueOneTaskSchedulerTickTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.local_repo = self.root / "repo"
        self.local_repo.mkdir()
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()
        self.worktree_root = self.root / "worktrees"
        self.lock_path = self.root / "scheduler.lock"
        self.repo = "anderson930420/agent-taskflow"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def request(self, **overrides: Any) -> GitHubIssueOneTaskSchedulerTickRequest:
        values: dict[str, Any] = {
            "repo": self.repo,
            "db_path": self.db_path,
            "local_repo_path": self.local_repo,
            "artifact_root": self.artifact_root,
            "lock_path": self.lock_path,
        }
        values.update(overrides)
        return GitHubIssueOneTaskSchedulerTickRequest(**values)

    def test_dry_run_acquires_lock_and_calls_automation_without_writes(self) -> None:
        discovery_calls: list[int] = []

        def forbidden_ingestion(repo: str, issue_number: int) -> GitHubIssueSnapshot:
            raise AssertionError("dry-run scheduler tick must not ingest")

        def forbidden_runner(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("dry-run scheduler tick must not call runner")

        result = run_github_issue_one_task_scheduler_tick(
            self.request(),
            discovery_fetcher=lambda request: (
                discovery_calls.append(request.limit)
                or [discovery_issue(701, title="Dry run issue", labels=("ready",))]
            ),
            ingestion_fetcher=forbidden_ingestion,
            approved_task_runner_fn=forbidden_runner,
            branch_push_fn=forbidden_runner,
            draft_pr_fn=forbidden_runner,
        )

        self.assertTrue(result["ok"], msg=f"result: {result!r}")
        self.assertEqual(
            result["schema_version"],
            GITHUB_ISSUE_ONE_TASK_SCHEDULER_TICK_SCHEMA_VERSION,
        )
        self.assertEqual(result["source"], GITHUB_ISSUE_ONE_TASK_SCHEDULER_TICK_SOURCE)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(discovery_calls, [100])
        self.assertTrue(self.lock_path.exists())
        self.assertTrue(result["lock"]["acquired"])
        self.assertFalse(result["lock"]["contended"])
        self.assertTrue(result["lock"]["released"])
        self.assertFalse(result["runner_config"]["configured"])
        self.assertEqual(result["automation"]["status"], "dry_run")
        self.assertEqual(result["automation"]["selected_issue"]["number"], 701)
        self.assertIsNone(result["selected_task_key"])
        self.assertFalse(self.db_path.exists())

        safety = result["safety"]
        self.assertTrue(safety["scheduled_tick"])
        self.assertTrue(safety["one_tick_only"])
        self.assertTrue(safety["one_issue_only"])
        self.assertTrue(safety["one_task_only"])
        self.assertTrue(safety["lock_acquired"])
        self.assertFalse(safety["lock_contended"])
        self.assertTrue(safety["dry_run"])
        self.assertFalse(safety["confirmed"])
        self.assertFalse(safety["runner_configured"])
        self.assertTrue(safety["automation_called"])
        self.assertTrue(safety["discovery_called"])
        self.assertFalse(safety["issue_ingested"])
        self.assertFalse(safety["watcher_called"])
        self.assertFalse(safety["approved_task_runner_called"])
        self.assertFalse(safety["github_mutated"])
        self.assertFalse(safety["branch_pushed"])
        self.assertFalse(safety["draft_pr_created"])

    def test_confirmed_tick_passes_controlled_preset_and_propagates_result(self) -> None:
        seen: dict[str, Any] = {}

        def fake_automation(request: Any, **kwargs: Any) -> dict[str, Any]:
            seen["request"] = request
            seen["kwargs"] = kwargs
            return {
                "ok": True,
                "status": "completed_one_task",
                "mode": "confirmed",
                "repo": request.repo,
                "selected_task_key": "AT-GH-702",
                "safety": {
                    "discovery_called": True,
                    "issue_ingested": True,
                    "watcher_called": True,
                    "approved_task_runner_called": True,
                    "github_mutated": True,
                    "branch_pushed": True,
                    "draft_pr_created": True,
                },
            }

        with mock.patch(
            "agent_taskflow.github_issue_one_task_scheduler_tick."
            "run_github_issue_one_task_automation",
            side_effect=fake_automation,
        ):
            result = run_github_issue_one_task_scheduler_tick(
                self.request(
                    confirmed=True,
                    issue_limit=7,
                    include_labels=("ready",),
                    exclude_labels=("skip",),
                    operator="codex",
                    operator_note="scheduled tick test",
                    remote="upstream",
                    base_branch="main",
                ),
                discovery_fetcher=lambda request: [],
                ingestion_fetcher=lambda repo, issue_number: issue_snapshot(
                    issue_number
                ),
                approved_task_runner_fn=lambda **kwargs: {"ok": True},
                branch_push_fn=lambda **kwargs: {"ok": True},
                draft_pr_fn=lambda **kwargs: {"ok": True},
            )

        automation_request = seen["request"]
        self.assertFalse(automation_request.dry_run)
        self.assertTrue(automation_request.select_first_issue)
        self.assertTrue(automation_request.confirm_select_first_issue)
        self.assertTrue(automation_request.confirm_ingest_issue)
        self.assertTrue(automation_request.confirm_run_watcher_one_task)
        self.assertTrue(automation_request.confirm_run_one_shot_pipeline)
        self.assertTrue(automation_request.confirm_prepare_pr)
        self.assertTrue(automation_request.confirm_github_mutations)
        self.assertTrue(automation_request.confirm_branch_push)
        self.assertTrue(automation_request.confirm_draft_pr)
        self.assertTrue(automation_request.draft)
        self.assertEqual(automation_request.issue_limit, 7)
        self.assertEqual(automation_request.include_labels, ("ready",))
        self.assertEqual(automation_request.exclude_labels, ("skip",))
        self.assertEqual(automation_request.operator, "codex")
        self.assertEqual(automation_request.operator_note, "scheduled tick test")
        self.assertEqual(automation_request.remote, "upstream")
        self.assertEqual(automation_request.base_branch, "main")

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed_one_task")
        self.assertEqual(result["mode"], "confirmed")
        self.assertFalse(result["runner_config"]["configured"])
        self.assertEqual(result["selected_task_key"], "AT-GH-702")
        self.assertEqual(result["automation"]["selected_task_key"], "AT-GH-702")
        safety = result["safety"]
        self.assertFalse(safety["dry_run"])
        self.assertTrue(safety["confirmed"])
        self.assertFalse(safety["runner_configured"])
        self.assertTrue(safety["automation_called"])
        self.assertTrue(safety["discovery_called"])
        self.assertTrue(safety["issue_ingested"])
        self.assertTrue(safety["watcher_called"])
        self.assertTrue(safety["approved_task_runner_called"])
        self.assertTrue(safety["github_mutated"])
        self.assertTrue(safety["branch_pushed"])
        self.assertTrue(safety["draft_pr_created"])

    def test_confirmed_tick_defaults_to_execution_only(self) -> None:
        seen: dict[str, Any] = {}

        def fake_automation(request: Any, **kwargs: Any) -> dict[str, Any]:
            seen["request"] = request
            return {
                "ok": True,
                "status": "execution_completed",
                "mode": "confirmed",
                "repo": request.repo,
                "selected_task_key": "AT-GH-704",
                "safety": {
                    "discovery_called": True,
                    "issue_ingested": True,
                    "watcher_called": False,
                    "approved_task_runner_called": True,
                    "github_mutated": False,
                    "branch_pushed": False,
                    "draft_pr_created": False,
                },
                "publication": {
                    "skipped": True,
                    "reason": "publish_after_execution_false",
                },
            }

        with mock.patch(
            "agent_taskflow.github_issue_one_task_scheduler_tick."
            "run_github_issue_one_task_automation",
            side_effect=fake_automation,
        ):
            result = run_github_issue_one_task_scheduler_tick(
                self.request(confirmed=True)
            )

        # Scheduler confirmed tick is execution-only by default.
        self.assertFalse(seen["request"].publish_after_execution)
        self.assertEqual(result["status"], "execution_completed")
        self.assertFalse(result["publication_config"]["publish_after_execution"])
        self.assertEqual(result["publication_config"]["mode"], "execution_only")
        self.assertIn(
            "task-to-draft-pr",
            result["publication_config"]["next_operator_action"],
        )
        safety = result["safety"]
        self.assertFalse(safety["publish_after_execution"])
        self.assertTrue(safety["approved_task_runner_called"])
        self.assertFalse(safety["watcher_called"])
        self.assertFalse(safety["github_mutated"])
        self.assertFalse(safety["branch_pushed"])
        self.assertFalse(safety["draft_pr_created"])

    def test_confirmed_tick_publish_after_execution_opt_in_passthrough(self) -> None:
        seen: dict[str, Any] = {}

        def fake_automation(request: Any, **kwargs: Any) -> dict[str, Any]:
            seen["request"] = request
            return {
                "ok": True,
                "status": "completed_one_task",
                "mode": "confirmed",
                "repo": request.repo,
                "selected_task_key": "AT-GH-705",
                "safety": {
                    "discovery_called": True,
                    "issue_ingested": True,
                    "watcher_called": True,
                    "approved_task_runner_called": True,
                    "github_mutated": True,
                    "branch_pushed": True,
                    "draft_pr_created": True,
                },
            }

        with mock.patch(
            "agent_taskflow.github_issue_one_task_scheduler_tick."
            "run_github_issue_one_task_automation",
            side_effect=fake_automation,
        ):
            result = run_github_issue_one_task_scheduler_tick(
                self.request(confirmed=True, publish_after_execution=True)
            )

        # Explicit opt-in forwards to the publication path.
        self.assertTrue(seen["request"].publish_after_execution)
        self.assertEqual(result["status"], "completed_one_task")
        self.assertTrue(result["publication_config"]["publish_after_execution"])
        self.assertEqual(result["publication_config"]["mode"], "publication")
        self.assertIsNone(result["publication_config"]["next_operator_action"])
        safety = result["safety"]
        self.assertTrue(safety["publish_after_execution"])
        self.assertTrue(safety["branch_pushed"])
        self.assertTrue(safety["draft_pr_created"])

    def test_confirmed_tick_builds_configured_approved_runner(self) -> None:
        seen: dict[str, Any] = {}

        def fake_automation(request: Any, **kwargs: Any) -> dict[str, Any]:
            runner = kwargs["approved_task_runner_fn"]
            seen["runner_payload"] = runner(
                task_key="AT-GH-703",
                handoff={},
                handoff_id="handoff-test",
                runtime_execution_id="runtime-test",
                db_path=request.db_path,
                artifact_root=request.artifact_root,
            )
            return {
                "ok": True,
                "status": "completed_one_task",
                "mode": "confirmed",
                "repo": request.repo,
                "selected_task_key": "AT-GH-703",
                "safety": {
                    "discovery_called": True,
                    "issue_ingested": True,
                    "watcher_called": True,
                    "approved_task_runner_called": True,
                    "github_mutated": False,
                    "branch_pushed": False,
                    "draft_pr_created": False,
                },
            }

        with mock.patch(
            "agent_taskflow.github_issue_one_task_scheduler_tick."
            "run_github_issue_one_task_automation",
            side_effect=fake_automation,
        ):
            with mock.patch(
                "agent_taskflow.github_issue_one_task_scheduler_tick.run_approved_task"
            ) as fake_runner:
                fake_runner.return_value.to_dict.return_value = {
                    "ok": True,
                    "status": "waiting_approval",
                    "phase": "waiting_approval",
                    "summary": {"final_task_status": "waiting_approval"},
                    "safety": {
                        "executor_started": True,
                        "validators_started": True,
                        "github_mutated": False,
                    },
                }
                result = run_github_issue_one_task_scheduler_tick(
                    self.request(
                        confirmed=True,
                        executor="shell",
                        validators=("pytest", "policy"),
                        worktree_root=self.worktree_root,
                        base_branch="main",
                        approved_task_preflight=False,
                        command=("python", "-m", "pytest"),
                    )
                )

        self.assertTrue(result["ok"])
        self.assertTrue(result["runner_config"]["configured"])
        self.assertEqual(result["runner_config"]["executor"], "shell")
        self.assertEqual(result["runner_config"]["validators"], ["pytest", "policy"])
        self.assertEqual(result["runner_config"]["worktree_root"], str(self.worktree_root))
        self.assertEqual(result["runner_config"]["command"], ["python", "-m", "pytest"])
        self.assertFalse(result["runner_config"]["preflight"])
        self.assertTrue(result["safety"]["runner_configured"])
        self.assertEqual(seen["runner_payload"]["status"], "waiting_approval")

        runner_request = fake_runner.call_args.args[0]
        self.assertEqual(runner_request.task_key, "AT-GH-703")
        self.assertEqual(runner_request.executor, "shell")
        self.assertEqual(runner_request.repo_path, self.local_repo)
        self.assertEqual(runner_request.db_path, self.db_path)
        self.assertEqual(runner_request.artifact_root, self.artifact_root)
        self.assertEqual(runner_request.worktree_root, self.worktree_root)
        self.assertEqual(runner_request.base_branch, "main")
        self.assertEqual(runner_request.validators, ("pytest", "policy"))
        self.assertTrue(runner_request.confirm_approved_task)
        self.assertFalse(runner_request.dry_run)
        self.assertFalse(runner_request.preflight)
        self.assertEqual(runner_request.command, ("python", "-m", "pytest"))

    def test_confirmed_tick_threads_executor_profile_to_automation(self) -> None:
        seen: dict[str, Any] = {}

        def fake_automation(request: Any, **kwargs: Any) -> dict[str, Any]:
            seen["request"] = request
            return {
                "ok": True,
                "status": "completed_one_task",
                "mode": "confirmed",
                "repo": request.repo,
                "selected_task_key": "AT-GH-720",
                "safety": {},
            }

        with mock.patch(
            "agent_taskflow.github_issue_one_task_scheduler_tick."
            "run_github_issue_one_task_automation",
            side_effect=fake_automation,
        ):
            run_github_issue_one_task_scheduler_tick(
                self.request(
                    confirmed=True,
                    model="claude-sonnet-4-6",
                    provider="anthropic",
                    tools=("read", "write", "read"),
                    pi_bin="pi",
                ),
                discovery_fetcher=lambda request: [],
                ingestion_fetcher=lambda repo, issue_number: issue_snapshot(
                    issue_number
                ),
                approved_task_runner_fn=lambda **kwargs: {"ok": True},
                branch_push_fn=lambda **kwargs: {"ok": True},
                draft_pr_fn=lambda **kwargs: {"ok": True},
            )

        automation_request = seen["request"]
        self.assertEqual(automation_request.model, "claude-sonnet-4-6")
        self.assertEqual(automation_request.provider, "anthropic")
        # Tools are normalized: stripped, de-duplicated, order preserved.
        self.assertEqual(automation_request.tools, ("read", "write"))
        self.assertEqual(automation_request.pi_bin, "pi")

    def test_dry_run_tick_threads_executor_profile_to_automation(self) -> None:
        seen: dict[str, Any] = {}

        def fake_automation(request: Any, **kwargs: Any) -> dict[str, Any]:
            seen["request"] = request
            return {
                "ok": True,
                "status": "dry_run",
                "mode": "dry_run",
                "repo": request.repo,
                "selected_task_key": None,
                "safety": {"discovery_called": True},
            }

        with mock.patch(
            "agent_taskflow.github_issue_one_task_scheduler_tick."
            "run_github_issue_one_task_automation",
            side_effect=fake_automation,
        ):
            run_github_issue_one_task_scheduler_tick(
                self.request(model="claude-sonnet-4-6", provider="anthropic"),
                discovery_fetcher=lambda request: [],
            )

        automation_request = seen["request"]
        self.assertTrue(automation_request.dry_run)
        self.assertEqual(automation_request.model, "claude-sonnet-4-6")
        self.assertEqual(automation_request.provider, "anthropic")

    def test_tick_without_profile_leaves_automation_profile_unset(self) -> None:
        seen: dict[str, Any] = {}

        def fake_automation(request: Any, **kwargs: Any) -> dict[str, Any]:
            seen["request"] = request
            return {
                "ok": True,
                "status": "dry_run",
                "mode": "dry_run",
                "repo": request.repo,
                "selected_task_key": None,
                "safety": {"discovery_called": True},
            }

        with mock.patch(
            "agent_taskflow.github_issue_one_task_scheduler_tick."
            "run_github_issue_one_task_automation",
            side_effect=fake_automation,
        ):
            run_github_issue_one_task_scheduler_tick(
                self.request(),
                discovery_fetcher=lambda request: [],
            )

        automation_request = seen["request"]
        self.assertIsNone(automation_request.model)
        self.assertIsNone(automation_request.provider)
        self.assertIsNone(automation_request.tools)
        self.assertIsNone(automation_request.pi_bin)

    def test_confirmed_tick_threads_executor_profile_into_approved_runner(self) -> None:
        captured: dict[str, Any] = {}

        def fake_automation(request: Any, **kwargs: Any) -> dict[str, Any]:
            runner = kwargs["approved_task_runner_fn"]
            runner(
                task_key="AT-GH-730",
                db_path=request.db_path,
                artifact_root=request.artifact_root,
            )
            return {
                "ok": True,
                "status": "execution_completed",
                "mode": "confirmed",
                "repo": request.repo,
                "selected_task_key": "AT-GH-730",
                "safety": {"approved_task_runner_called": True},
            }

        with mock.patch(
            "agent_taskflow.github_issue_one_task_scheduler_tick."
            "run_github_issue_one_task_automation",
            side_effect=fake_automation,
        ):
            with mock.patch(
                "agent_taskflow.github_issue_one_task_scheduler_tick.run_approved_task"
            ) as fake_runner:
                fake_runner.return_value.to_dict.return_value = {
                    "ok": True,
                    "status": "waiting_approval",
                    "safety": {},
                }
                run_github_issue_one_task_scheduler_tick(
                    self.request(
                        confirmed=True,
                        executor="pi",
                        model="claude-sonnet-4-6",
                        provider="anthropic",
                        tools=("read", "write"),
                        pi_bin="/custom/pi",
                    )
                )

        runner_request = fake_runner.call_args.args[0]
        captured["runner_request"] = runner_request
        self.assertEqual(runner_request.executor, "pi")
        self.assertEqual(runner_request.model, "claude-sonnet-4-6")
        self.assertEqual(runner_request.provider, "anthropic")
        self.assertEqual(runner_request.tools, ("read", "write"))
        self.assertEqual(runner_request.pi_bin, "/custom/pi")

    def test_lock_contention_returns_locked_without_calling_automation(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with mock.patch(
                    "agent_taskflow.github_issue_one_task_scheduler_tick."
                    "run_github_issue_one_task_automation",
                    side_effect=AssertionError(
                        "contended scheduler tick must not call automation"
                    ),
                ) as fake_automation:
                    result = run_github_issue_one_task_scheduler_tick(
                        self.request(confirmed=True)
                    )
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        self.assertFalse(fake_automation.called)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "locked")
        self.assertIsNone(result["automation"])
        self.assertIsNone(result["selected_task_key"])
        self.assertFalse(result["lock"]["acquired"])
        self.assertTrue(result["lock"]["contended"])
        safety = result["safety"]
        self.assertFalse(safety["lock_acquired"])
        self.assertTrue(safety["lock_contended"])
        self.assertFalse(safety["automation_called"])
        self.assertFalse(safety["discovery_called"])
        self.assertFalse(safety["issue_ingested"])
        self.assertFalse(safety["watcher_called"])
        self.assertFalse(safety["approved_task_runner_called"])
        self.assertFalse(safety["branch_pushed"])
        self.assertFalse(safety["draft_pr_created"])

    def test_confirmed_no_eligible_issue_is_safe_noop(self) -> None:
        def forbidden_ingestion(repo: str, issue_number: int) -> GitHubIssueSnapshot:
            raise AssertionError("no-eligible scheduler tick must not ingest")

        def forbidden_runner(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("no-eligible scheduler tick must not run work")

        result = run_github_issue_one_task_scheduler_tick(
            self.request(confirmed=True),
            discovery_fetcher=lambda request: [],
            ingestion_fetcher=forbidden_ingestion,
            approved_task_runner_fn=forbidden_runner,
            branch_push_fn=forbidden_runner,
            draft_pr_fn=forbidden_runner,
        )

        self.assertTrue(result["ok"], msg=f"result: {result!r}")
        self.assertEqual(result["status"], "no_eligible_issues")
        self.assertEqual(result["mode"], "confirmed")
        self.assertEqual(result["automation"]["status"], "no_eligible_issues")
        self.assertIsNone(result["automation"]["selected_issue"])
        self.assertIsNone(result["automation"]["ingestion"])
        self.assertIsNone(result["automation"]["watcher"])
        self.assertIsNone(result["selected_task_key"])
        self.assertFalse(self.db_path.exists())
        safety = result["safety"]
        self.assertTrue(safety["automation_called"])
        self.assertTrue(safety["discovery_called"])
        self.assertFalse(safety["issue_ingested"])
        self.assertFalse(safety["watcher_called"])
        self.assertFalse(safety["approved_task_runner_called"])
        self.assertFalse(safety["branch_pushed"])
        self.assertFalse(safety["draft_pr_created"])

        summary = to_observability_dict(summarize_scheduler_tick_payload(result))
        self.assertEqual(
            summary["schema_version"], "execution_observability_summary.v1"
        )
        self.assertEqual(summary["source"], "scheduler_tick")
        self.assertEqual(summary["status"], "no_eligible_issues")
        self.assertIsNone(summary["task_key"])
        self.assertEqual(summary["publication_mode"], "execution_only")
        self.assertFalse(summary["safety"]["github_mutated"])
        self.assertFalse(summary["safety"]["branch_deleted"])
        self.assertFalse(summary["safety"]["worktree_deleted"])

    def test_safety_invariants_preserve_human_final_gates(self) -> None:
        result = run_github_issue_one_task_scheduler_tick(
            self.request(),
            discovery_fetcher=lambda request: [],
            ingestion_fetcher=lambda repo, issue_number: issue_snapshot(
                issue_number
            ),
            approved_task_runner_fn=lambda **kwargs: {"ok": False},
            branch_push_fn=lambda **kwargs: {"ok": False},
            draft_pr_fn=lambda **kwargs: {"ok": False},
        )

        safety = result["safety"]
        self.assertFalse(safety["approved"])
        self.assertFalse(safety["merged"])
        self.assertFalse(safety["cleanup_performed"])
        self.assertFalse(safety["branch_deleted"])
        self.assertFalse(safety["worktree_deleted"])
        self.assertFalse(safety["scheduler_loop_started"])
        self.assertFalse(safety["background_worker_started"])
        self.assertFalse(safety["multi_task_batch_started"])
        self.assertTrue(safety["human_review_required"])

        source = Path(
            "agent_taskflow/github_issue_one_task_scheduler_tick.py"
        ).read_text(encoding="utf-8")
        forbidden = (
            "while True",
            "schedule.every",
            "asyncio.sleep",
            "threading.Thread",
            "Thread(",
            "merge_pull_request",
            "record_approval_decision(",
            "delete_worktree",
            "git worktree remove",
            "git branch -d",
            "git push --delete",
        )
        for needle in forbidden:
            self.assertNotIn(needle, source, needle)


class RecordingEngine:
    """Test engine that records each request and returns a fixed result."""

    def __init__(self, result: Any = None) -> None:
        self.calls: list[Any] = []
        self._result = result

    def execute(self, request: Any) -> Any:
        from agent_taskflow.execution_engine_contract import (
            ExecutionEngineResult,
            ExecutionEngineSafety,
        )

        self.calls.append(request)
        if self._result is not None:
            return self._result
        return ExecutionEngineResult(
            ok=True,
            task_key=request.task_key,
            status="waiting_approval",
            summary="recording engine result",
            safety=ExecutionEngineSafety(),
        )


class SchedulerTickExecutionEngineOptInTests(unittest.TestCase):
    """P5-d: opt-in ExecutionEngine path, off by default."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.local_repo = self.root / "repo"
        self.local_repo.mkdir()
        self.db_path = self.root / "state.db"
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()
        self.lock_path = self.root / "scheduler.lock"
        self.repo = "anderson930420/agent-taskflow"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def request(self, **overrides: Any) -> GitHubIssueOneTaskSchedulerTickRequest:
        values: dict[str, Any] = {
            "repo": self.repo,
            "db_path": self.db_path,
            "local_repo_path": self.local_repo,
            "artifact_root": self.artifact_root,
            "lock_path": self.lock_path,
        }
        values.update(overrides)
        return GitHubIssueOneTaskSchedulerTickRequest(**values)

    @staticmethod
    def _execution_completed_automation(task_key: str = "AT-GH-808") -> Any:
        def fake_automation(request: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "status": "execution_completed",
                "mode": "confirmed",
                "repo": request.repo,
                "selected_task_key": task_key,
                "selected_issue": {"number": 808},
                "safety": {
                    "discovery_called": True,
                    "issue_ingested": True,
                    "approved_task_runner_called": True,
                    "github_mutated": False,
                    "branch_pushed": False,
                    "draft_pr_created": False,
                },
                "publication": {
                    "skipped": True,
                    "reason": "publish_after_execution_false",
                },
            }

        return fake_automation

    def test_request_default_use_execution_engine_is_false(self) -> None:
        self.assertFalse(self.request().use_execution_engine)
        self.assertFalse(self.request(confirmed=True).use_execution_engine)

    def test_dry_run_with_use_execution_engine_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.request(use_execution_engine=True)
        self.assertIn("use_execution_engine requires confirmed", str(ctx.exception))

    def test_confirmed_with_use_execution_engine_is_accepted(self) -> None:
        request = self.request(confirmed=True, use_execution_engine=True)
        self.assertTrue(request.use_execution_engine)
        self.assertTrue(request.confirmed)
        self.assertFalse(request.dry_run)

    def test_default_tick_does_not_build_or_call_engine(self) -> None:
        engine = RecordingEngine()
        with mock.patch(
            "agent_taskflow.github_issue_one_task_scheduler_tick."
            "run_github_issue_one_task_automation",
            side_effect=self._execution_completed_automation(),
        ):
            result = run_github_issue_one_task_scheduler_tick(
                self.request(confirmed=True),
                execution_engine=engine,
            )

        # Default path: no opt-in flag -> no engine block, engine never called.
        self.assertNotIn("execution_engine", result)
        self.assertEqual(len(engine.calls), 0)

    def test_opt_in_routes_one_task_through_engine_exactly_once(self) -> None:
        engine = RecordingEngine()
        with mock.patch(
            "agent_taskflow.github_issue_one_task_scheduler_tick."
            "run_github_issue_one_task_automation",
            side_effect=self._execution_completed_automation(),
        ):
            result = run_github_issue_one_task_scheduler_tick(
                self.request(confirmed=True, use_execution_engine=True),
                execution_engine=engine,
            )

        self.assertEqual(len(engine.calls), 1)
        engine_request = engine.calls[0]
        from agent_taskflow.execution_engine_contract import (
            REQUEST_SOURCE_SCHEDULED_TICK,
        )

        self.assertEqual(engine_request.source, REQUEST_SOURCE_SCHEDULED_TICK)
        self.assertEqual(engine_request.task_key, "AT-GH-808")
        metadata = engine_request.metadata
        self.assertIs(metadata["publish_after_execution"], False)
        self.assertEqual(metadata["mode"], "execution_only")
        self.assertIs(metadata["execution_only"], True)
        self.assertIs(metadata["one_task_only"], True)
        self.assertIs(metadata["scheduler_tick"], True)

        block = result["execution_engine"]
        self.assertTrue(block["enabled"])
        self.assertTrue(block["executed"])
        self.assertTrue(block["ok"])
        self.assertEqual(block["engine_invocation_count"], 1)
        self.assertEqual(block["request_source"], REQUEST_SOURCE_SCHEDULED_TICK)
        self.assertTrue(block["shadow_compare"]["matched"])
        self.assertIsNotNone(block["observability_summary"])

        # The whole tick payload (legacy + engine evidence) is JSON-compatible.
        import json

        json.dumps(result)

    def test_opt_in_engine_result_is_evidence_only(self) -> None:
        engine = RecordingEngine()
        with mock.patch(
            "agent_taskflow.github_issue_one_task_scheduler_tick."
            "run_github_issue_one_task_automation",
            side_effect=self._execution_completed_automation(),
        ):
            result = run_github_issue_one_task_scheduler_tick(
                self.request(confirmed=True, use_execution_engine=True),
                execution_engine=engine,
            )

        # Legacy decision fields are untouched by the engine path.
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "execution_completed")
        self.assertEqual(result["publication_config"]["mode"], "execution_only")
        self.assertFalse(result["safety"]["approved"])
        self.assertFalse(result["safety"]["merged"])

        safety = result["execution_engine"]["safety"]
        self.assertFalse(safety["approval_authority"])
        self.assertFalse(safety["approved"])
        self.assertFalse(safety["merged"])
        self.assertFalse(safety["branch_pushed"])
        self.assertFalse(safety["draft_pr_created"])
        self.assertFalse(safety["cleanup_performed"])
        self.assertFalse(safety["branch_deleted"])
        self.assertFalse(safety["worktree_deleted"])
        self.assertFalse(safety["scheduler_loop_started"])
        self.assertFalse(safety["background_worker_started"])
        self.assertFalse(safety["multi_task_batch_started"])
        self.assertTrue(safety["human_review_required"])

    def test_opt_in_engine_failure_returns_structured_block(self) -> None:
        class RaisingEngine:
            def execute(self, request: Any) -> Any:
                raise RuntimeError("boom")

        with mock.patch(
            "agent_taskflow.github_issue_one_task_scheduler_tick."
            "run_github_issue_one_task_automation",
            side_effect=self._execution_completed_automation(),
        ):
            result = run_github_issue_one_task_scheduler_tick(
                self.request(confirmed=True, use_execution_engine=True),
                execution_engine=RaisingEngine(),
            )

        block = result["execution_engine"]
        self.assertTrue(block["executed"])
        self.assertFalse(block["ok"])
        self.assertEqual(block["status"], "engine_error")
        self.assertIn("boom", block["error"])
        # No fallback to publish/merge/cleanup: legacy decision preserved.
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "execution_completed")
        self.assertFalse(result["safety"]["approved"])
        self.assertFalse(result["safety"]["merged"])
        self.assertFalse(block["safety"]["branch_deleted"])
        self.assertFalse(block["safety"]["worktree_deleted"])

    def test_opt_in_no_eligible_issue_does_not_call_engine(self) -> None:
        def fake_no_eligible(request: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "status": "no_eligible_issues",
                "mode": "confirmed",
                "repo": request.repo,
                "selected_task_key": None,
                "selected_issue": None,
                "safety": {"discovery_called": True},
            }

        engine = RecordingEngine()
        with mock.patch(
            "agent_taskflow.github_issue_one_task_scheduler_tick."
            "run_github_issue_one_task_automation",
            side_effect=fake_no_eligible,
        ):
            result = run_github_issue_one_task_scheduler_tick(
                self.request(confirmed=True, use_execution_engine=True),
                execution_engine=engine,
            )

        self.assertEqual(len(engine.calls), 0)
        block = result["execution_engine"]
        self.assertFalse(block["executed"])
        self.assertEqual(block["status"], "not_executed")

    def test_opt_in_default_engine_is_approved_task_adapter(self) -> None:
        # With no injected engine, the default facade is the P4-c adapter. Patch
        # run_approved_task so the adapter performs no real side effect.
        with mock.patch(
            "agent_taskflow.github_issue_one_task_scheduler_tick."
            "run_github_issue_one_task_automation",
            side_effect=self._execution_completed_automation(),
        ):
            with mock.patch(
                "agent_taskflow.execution_engine_approved_task_adapter."
                "run_approved_task"
            ) as fake_runner:
                fake_runner.return_value = {
                    "ok": True,
                    "status": "waiting_approval",
                    "safety": {"executor_started": True, "validators_started": True},
                }
                result = run_github_issue_one_task_scheduler_tick(
                    self.request(confirmed=True, use_execution_engine=True),
                )

        block = result["execution_engine"]
        self.assertEqual(block["engine"], "ApprovedTaskRunnerExecutionEngineAdapter")
        self.assertTrue(block["executed"])
        self.assertEqual(fake_runner.call_count, 1)

    def test_opt_in_payload_still_summarizes_legacy_fields(self) -> None:
        engine = RecordingEngine()
        with mock.patch(
            "agent_taskflow.github_issue_one_task_scheduler_tick."
            "run_github_issue_one_task_automation",
            side_effect=self._execution_completed_automation(),
        ):
            result = run_github_issue_one_task_scheduler_tick(
                self.request(confirmed=True, use_execution_engine=True),
                execution_engine=engine,
            )

        summary = to_observability_dict(summarize_scheduler_tick_payload(result))
        self.assertEqual(summary["source"], "scheduler_tick")
        self.assertEqual(summary["status"], "execution_completed")
        self.assertEqual(summary["task_key"], "AT-GH-808")
        self.assertEqual(summary["publication_mode"], "execution_only")

    def test_opt_in_does_not_introduce_unsafe_constructs_in_helper(self) -> None:
        source = Path(
            "agent_taskflow/scheduler_execution_engine_opt_in.py"
        ).read_text(encoding="utf-8")
        for needle in (
            "while True",
            "merge_pull_request",
            "record_approval_decision(",
            "git push",
            "delete_worktree",
            "git worktree remove",
        ):
            self.assertNotIn(needle, source, needle)


if __name__ == "__main__":
    unittest.main()
