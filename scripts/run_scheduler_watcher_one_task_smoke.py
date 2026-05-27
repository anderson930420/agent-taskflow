#!/usr/bin/env python3
"""Smoke test for the Level 8B one-task-at-a-time confirmed watcher."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.mission_contract import (  # noqa: E402
    build_mission_contract,
    write_mission_contract,
)
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord  # noqa: E402
from agent_taskflow.scheduler_watcher_one_task import (  # noqa: E402
    SchedulerWatcherOneTaskRequest,
    run_scheduler_watcher_one_task,
)
from agent_taskflow.store import TaskMirrorStore  # noqa: E402
from agent_taskflow.tasks import normalize_task_key  # noqa: E402


ELIGIBLE_TASK_KEY = "AT-L8B-WATCHER-ELIGIBLE"
BLOCKED_TASK_KEY = "AT-L8B-WATCHER-BLOCKED"
WAITING_TASK_KEY = "AT-L8B-WATCHER-WAITING"

DEFAULT_PROJECT = "agent-taskflow"
DEFAULT_REPO = "anderson930420/agent-taskflow"

FORBIDDEN_ARTIFACT_TYPES = (
    "local_cleanup",
    "remote_branch_cleanup",
    "task_closeout",
)
FORBIDDEN_EVENT_TYPES = (
    "local_cleanup_completed",
    "remote_branch_cleanup_completed",
    "task_closeout_completed",
)
FORBIDDEN_PAYLOAD_MARKERS = (
    '"approved": true',
    '"merged": true',
    '"cleanup_performed": true',
    '"scheduler_loop_started": true',
    '"background_worker_started": true',
    '"automatic_task_picking_started": true',
    '"multi_task_batch_started": true',
)


class SmokeFailure(RuntimeError):
    """Raised when the smoke violates the expected contract."""


@dataclass
class _FakeApprovedTaskRunner:
    call_count: int = 0

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        db_path = Path(kwargs["db_path"])
        task_key = str(kwargs["task_key"])
        artifact_root = Path(kwargs["artifact_root"])
        store = TaskMirrorStore(db_path)

        task = store.get_task(task_key)
        artifact_dir = task.artifact_dir if task is not None else artifact_root / task_key
        artifact_dir.mkdir(parents=True, exist_ok=True)

        executor_log = artifact_dir / "executor.log"
        if not executor_log.exists():
            executor_log.write_text("executor log\n", encoding="utf-8")
        run_id = store.create_executor_run(task_key, "noop")
        store.finish_executor_run(
            task_key,
            run_id,
            executor="noop",
            status="completed",
            exit_code=0,
            summary="fake executor completed for Level 8B smoke",
            log_path=executor_log,
            artifacts={"log": executor_log},
        )
        store.record_task_artifact(task_key, "worker_log", executor_log)

        validator_log = artifact_dir / "pytest.log"
        if not validator_log.exists():
            validator_log.write_text("validator log\n", encoding="utf-8")
        store.record_validation_result(
            task_key,
            "pytest",
            status="passed",
            exit_code=0,
            summary="fake validator passed for Level 8B smoke",
            log_path=validator_log,
            artifacts={"log": validator_log},
        )
        store.record_task_artifact(task_key, "review_log", validator_log)

        store.update_task_status(
            task_key,
            "waiting_approval",
            source="level-8b-smoke-runner",
            message="fake approved task runner completed for Level 8B smoke",
        )
        return {
            "ok": True,
            "status": "waiting_approval",
            "phase": "level-8b-smoke-runner",
            "summary": "fake approved task runner completed",
            "artifacts": {
                "executor_log": str(executor_log),
                "validator_log": str(validator_log),
            },
            "safety": {
                "executor_started": False,
                "validators_started": False,
                "github_mutated": False,
                "branch_pushed": False,
                "pr_created": False,
                "merged": False,
                "approved": False,
                "cleanup_performed": False,
                "scheduler_loop_started": False,
                "background_worker_started": False,
                "automatic_task_picking_started": False,
            },
        }


@dataclass
class _FakeBranchPush:
    call_count: int = 0

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        task_key = str(kwargs["task_key"])
        artifact_root = Path(kwargs["artifact_root"])
        repo_path = Path(kwargs["repo_path"])
        branch = str(kwargs["branch"])
        remote = str(kwargs.get("remote") or "origin")
        head_sha = _git(repo_path, "rev-parse", "HEAD")
        base_sha = _git(repo_path, "rev-parse", "main")
        artifact_path = artifact_root / "branch_push" / task_key / "branch_push.json"
        payload = {
            "kind": "branch_push_completed",
            "artifact_type": "branch_push",
            "task_key": task_key,
            "task_status": "waiting_approval",
            "remote": remote,
            "branch": branch,
            "refspec": f"HEAD:{branch}",
            "worktree_path": str(repo_path),
            "base_branch": "main",
            "base_sha": base_sha,
            "head_sha": head_sha,
            "dry_run_performed": True,
            "dry_run_ok": True,
            "push_performed": True,
            "push_ok": True,
            "branch_pushed": True,
            "pr_created": False,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "requires_human_confirmation": True,
            "safety": {
                "human_confirmation_required": True,
                "human_confirmation_confirmed": True,
                "branch_pushed": True,
                "pr_created": False,
                "merged": False,
                "approved": False,
                "cleanup_performed": False,
                "branch_deleted": False,
                "worktree_deleted": False,
                "background_worker_started": False,
            },
        }
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        store = TaskMirrorStore(Path(kwargs["db_path"]))
        store.record_task_artifact(task_key, "branch_push", artifact_path)
        store.record_task_event(
            task_key,
            "branch_push_completed",
            "branch_push_confirm",
            message="Fake branch push completed",
            payload={**payload, "artifact_path": str(artifact_path)},
        )
        return {
            "ok": True,
            "status": "pushed",
            "task_key": task_key,
            "remote": remote,
            "branch": branch,
            "branch_pushed": True,
            "push_ok": True,
            "branch_push_json_path": str(artifact_path),
            "summary": {"branch_pushed": True},
            "safety": payload["safety"],
        }


@dataclass
class _FakeDraftPR:
    call_count: int = 0

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        task_key = str(kwargs["task_key"])
        artifact_root = Path(kwargs["artifact_root"])
        repo = str(kwargs["repo"])
        base = str(kwargs["base"])
        head = str(kwargs["head"])
        artifact_path = artifact_root / "draft_pr" / task_key / "draft_pr.json"
        pr_number = 1
        pr_url = f"https://github.com/{repo}/pull/{pr_number}"
        payload = {
            "kind": "draft_pr_created",
            "artifact_type": "draft_pr",
            "task_key": task_key,
            "repo": repo,
            "base_branch": base,
            "head_branch": head,
            "title": f"{task_key}: Level 8B watcher one-task smoke",
            "draft": True,
            "pr_number": pr_number,
            "pr_url": pr_url,
            "branch_push_verified": True,
            "verified": True,
            "pr_created": True,
            "draft_pr_created": True,
            "merged": False,
            "approved": False,
            "cleanup_performed": False,
            "issue_closed": False,
            "requires_human_confirmation": True,
            "safety": {
                "human_confirmation_required": True,
                "human_confirmation_confirmed": True,
                "branch_push_required_before_pr": True,
                "pr_created": True,
                "draft_pr": True,
                "draft_pr_verified": True,
                "merged": False,
                "approved": False,
                "cleanup_performed": False,
                "issue_closed": False,
                "branch_deleted": False,
                "worktree_deleted": False,
                "background_worker_started": False,
            },
        }
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        store = TaskMirrorStore(Path(kwargs["db_path"]))
        store.record_task_artifact(task_key, "draft_pr", artifact_path)
        store.record_task_event(
            task_key,
            "draft_pr_created",
            "draft_pr_confirm",
            message="Fake draft PR created",
            payload={**payload, "artifact_path": str(artifact_path)},
        )
        return {
            "ok": True,
            "status": "draft_pr_created",
            "task_key": task_key,
            "draft_pr": {
                "created": True,
                "draft": True,
                "number": pr_number,
                "url": pr_url,
                "artifact_path": str(artifact_path),
            },
            "summary": {
                "draft_pr_created": True,
                "merged": False,
                "approved": False,
                "cleanup_performed": False,
                "requires_human_review": True,
            },
            "safety": payload["safety"],
        }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _require_absolute_path(path: str | Path, field_name: str) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        raise ValueError(f"{field_name} must be absolute: {path}")
    return resolved


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        shell=False,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise SmokeFailure(f"git {' '.join(args)} failed: {completed.stderr}")
    return completed.stdout.strip()


def _init_repo(repo_path: Path, task_key: str) -> tuple[str, str]:
    repo_path.mkdir(parents=True)
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "user.email", "agent-taskflow@example.invalid")
    _git(repo_path, "config", "user.name", "Agent Taskflow")
    (repo_path / "README.md").write_text(
        "# Level 8B watcher one-task smoke\n", encoding="utf-8"
    )
    _git(repo_path, "add", "README.md")
    _git(repo_path, "commit", "-m", "initial")
    base_sha = _git(repo_path, "rev-parse", "HEAD")
    branch = f"task/{task_key}"
    _git(repo_path, "switch", "-c", branch)
    (repo_path / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(repo_path, "add", "feature.txt")
    _git(repo_path, "commit", "-m", "feature")
    return base_sha, branch


def _seed_queued_task(
    *,
    store: TaskMirrorStore,
    task_key: str,
    repo_path: Path,
    artifact_root: Path,
    base_sha: str,
    branch: str,
) -> None:
    artifact_dir = artifact_root / task_key
    artifact_dir.mkdir(parents=True, exist_ok=True)
    store.upsert_task(
        TaskRecord(
            task_key=task_key,
            project=DEFAULT_PROJECT,
            board=DEFAULT_PROJECT,
            title="Level 8B watcher eligible task",
            status="queued",
            repo_path=repo_path,
            artifact_dir=artifact_dir,
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-01T00:00:00Z",
        )
    )
    store.upsert_task_worktree(
        TaskWorktreeRecord(
            task_key=task_key,
            repo_path=repo_path,
            worktree_path=repo_path,
            branch=branch,
            base_branch="main",
            base_sha=base_sha,
            status="active",
        )
    )

    issue_spec_path = artifact_dir / "issue_spec.md"
    issue_spec_path.write_text(_issue_spec_text(task_key), encoding="utf-8")
    store.record_task_artifact(task_key, "issue_spec", issue_spec_path)

    contract = build_mission_contract(
        task_key=task_key,
        goal="Run Level 8B watcher one-task smoke",
        repo_path=repo_path,
        worktree_path=repo_path,
        artifact_dir=artifact_dir,
        executor="noop",
        required_validators=("pytest",),
    )
    write_mission_contract(contract, artifact_dir=artifact_dir)


def _seed_extra_task(
    *,
    store: TaskMirrorStore,
    task_key: str,
    status: str,
    title: str,
    repo_path: Path,
    artifact_root: Path,
    blocked_reason: str | None = None,
) -> None:
    artifact_dir = artifact_root / task_key
    artifact_dir.mkdir(parents=True, exist_ok=True)
    store.upsert_task(
        TaskRecord(
            task_key=task_key,
            project=DEFAULT_PROJECT,
            board=DEFAULT_PROJECT,
            title=title,
            status=status,
            repo_path=repo_path,
            artifact_dir=artifact_dir,
            blocked_reason=blocked_reason,
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-01T00:00:00Z",
        )
    )


def _issue_spec_text(task_key: str) -> str:
    return "\n".join(
        [
            "# Offline Issue Spec",
            "",
            f"- Repository: {DEFAULT_REPO}",
            "- Issue number: 818",
            "- Issue URL: https://github.com/anderson930420/agent-taskflow/issues/818",
            "- Issue state: open",
            "- Title: Level 8B watcher one-task smoke",
            "- Labels: smoke",
            "- Author: octocat",
            "- Created at: 2026-05-01T00:00:00Z",
            "- Updated at: 2026-05-02T00:00:00Z",
            "- Ingested at: 2026-05-03T00:00:00Z",
            f"- Task key: {task_key}",
            "",
            "Offline issue fixture for Level 8B smoke.",
            "",
        ]
    )


def _forbidden_side_effect_counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        artifact_placeholders = ",".join("?" for _ in FORBIDDEN_ARTIFACT_TYPES)
        event_placeholders = ",".join("?" for _ in FORBIDDEN_EVENT_TYPES)
        artifacts = conn.execute(
            f"SELECT COUNT(*) FROM task_artifacts WHERE artifact_type IN ({artifact_placeholders})",
            FORBIDDEN_ARTIFACT_TYPES,
        ).fetchone()[0]
        events = conn.execute(
            f"SELECT COUNT(*) FROM task_events WHERE event_type IN ({event_placeholders})",
            FORBIDDEN_EVENT_TYPES,
        ).fetchone()[0]
        payload_rows = conn.execute(
            "SELECT payload_json FROM task_events WHERE payload_json IS NOT NULL"
        ).fetchall()
    markers = sum(
        sum(1 for marker in FORBIDDEN_PAYLOAD_MARKERS if marker in row[0])
        for row in payload_rows
    )
    return {"artifacts": artifacts, "events": events, "payload_markers": markers}


def _evidence_counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        artifacts = conn.execute("SELECT COUNT(*) FROM task_artifacts").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    return {"artifacts": artifacts, "events": events}


def run_smoke(*, workspace_root: Path) -> dict[str, Any]:
    workspace_root = _require_absolute_path(workspace_root, "workspace_root")
    db_path = workspace_root / "scheduler-watcher-one-task-smoke.db"
    repo_path = workspace_root / "repo"
    artifact_root = workspace_root / "artifacts"
    workspace_root.mkdir(parents=True, exist_ok=True)
    artifact_root.mkdir(parents=True, exist_ok=True)

    eligible_key = normalize_task_key(ELIGIBLE_TASK_KEY)
    blocked_key = normalize_task_key(BLOCKED_TASK_KEY)
    waiting_key = normalize_task_key(WAITING_TASK_KEY)

    store = TaskMirrorStore(db_path)
    store.init_db()
    base_sha, branch = _init_repo(repo_path, eligible_key)
    _seed_queued_task(
        store=store,
        task_key=eligible_key,
        repo_path=repo_path,
        artifact_root=artifact_root,
        base_sha=base_sha,
        branch=branch,
    )
    _seed_extra_task(
        store=store,
        task_key=blocked_key,
        status="blocked",
        title="Level 8B watcher blocked task",
        repo_path=repo_path,
        artifact_root=artifact_root,
        blocked_reason="waiting on human decision",
    )
    _seed_extra_task(
        store=store,
        task_key=waiting_key,
        status="waiting_approval",
        title="Level 8B watcher waiting task",
        repo_path=repo_path,
        artifact_root=artifact_root,
    )

    fake_runner = _FakeApprovedTaskRunner()
    fake_branch_push = _FakeBranchPush()
    fake_draft_pr = _FakeDraftPR()

    counts_before_dry = _evidence_counts(db_path)
    dry_run_result = run_scheduler_watcher_one_task(
        SchedulerWatcherOneTaskRequest(
            db_path=db_path,
            artifact_root=artifact_root,
            dry_run=True,
            task_key=eligible_key,
        ),
        approved_task_runner_fn=fake_runner,
        branch_push_fn=fake_branch_push,
        draft_pr_fn=fake_draft_pr,
    )
    _require(dry_run_result.get("ok") is True, f"dry-run not ok: {dry_run_result!r}")
    _require(
        dry_run_result.get("status") == "dry_run",
        f"dry-run status mismatch: {dry_run_result!r}",
    )
    _require(
        (dry_run_result.get("preview") or {}).get("candidate_count") == 1,
        f"dry-run candidate_count mismatch: {dry_run_result!r}",
    )
    _require(fake_runner.call_count == 0, "dry-run called fake runner")
    _require(fake_branch_push.call_count == 0, "dry-run called fake branch push")
    _require(fake_draft_pr.call_count == 0, "dry-run called fake draft PR")
    _require(
        counts_before_dry == _evidence_counts(db_path),
        "dry-run mutated evidence counts",
    )
    _assert_dry_run_safety(dry_run_result.get("safety") or {})

    confirmed = run_scheduler_watcher_one_task(
        SchedulerWatcherOneTaskRequest(
            db_path=db_path,
            artifact_root=artifact_root,
            dry_run=False,
            confirm_run_watcher_one_task=True,
            task_key=eligible_key,
            confirm_run_one_shot_pipeline=True,
            confirm_prepare_pr=True,
            confirm_github_mutations=True,
            confirm_branch_push=True,
            confirm_draft_pr=True,
            operator="level-8b-smoke",
            operator_note="Level 8B watcher one-task smoke",
        ),
        approved_task_runner_fn=fake_runner,
        branch_push_fn=fake_branch_push,
        draft_pr_fn=fake_draft_pr,
    )
    _require(confirmed.get("ok") is True, f"confirmed not ok: {confirmed!r}")
    _require(
        confirmed.get("status") == "completed_one_task",
        f"confirmed status mismatch: {confirmed!r}",
    )
    _require(
        confirmed.get("selected_task_key") == eligible_key,
        f"confirmed selected_task_key mismatch: {confirmed!r}",
    )
    _require(fake_runner.call_count == 1, "fake runner not called once")
    _require(fake_branch_push.call_count == 1, "fake branch push not called once")
    _require(fake_draft_pr.call_count == 1, "fake draft PR not called once")

    confirmed_safety = confirmed.get("safety") or {}
    _require(
        confirmed_safety.get("processed_task_count") == 1,
        f"processed_task_count mismatch: {confirmed_safety!r}",
    )
    _assert_confirmed_no_forbidden(confirmed_safety)

    forbidden_counts_after_confirmed = _forbidden_side_effect_counts(db_path)
    _require(
        forbidden_counts_after_confirmed == {
            "artifacts": 0,
            "events": 0,
            "payload_markers": 0,
        },
        f"forbidden side effects after confirmed: {forbidden_counts_after_confirmed}",
    )

    blocked_task = TaskMirrorStore(db_path).get_task(blocked_key)
    waiting_task = TaskMirrorStore(db_path).get_task(waiting_key)
    _require(
        blocked_task is not None and blocked_task.status == "blocked",
        "blocked task was modified",
    )
    _require(
        waiting_task is not None and waiting_task.status == "waiting_approval",
        "waiting task was modified",
    )

    evidence_after_confirmed = _evidence_counts(db_path)
    resume_confirmed = run_scheduler_watcher_one_task(
        SchedulerWatcherOneTaskRequest(
            db_path=db_path,
            artifact_root=artifact_root,
            dry_run=False,
            confirm_run_watcher_one_task=True,
            task_key=eligible_key,
            resume_existing=True,
            resume_pr_preparation=True,
            confirm_run_one_shot_pipeline=True,
            confirm_prepare_pr=True,
            confirm_github_mutations=True,
            confirm_branch_push=True,
            confirm_draft_pr=True,
            operator="level-8b-smoke",
            operator_note="Level 8B watcher one-task resume smoke",
        ),
        approved_task_runner_fn=fake_runner,
        branch_push_fn=fake_branch_push,
        draft_pr_fn=fake_draft_pr,
    )
    _require(
        resume_confirmed.get("ok") is True,
        f"resume not ok: {resume_confirmed!r}",
    )
    _require(
        resume_confirmed.get("status") == "completed_one_task",
        f"resume status mismatch: {resume_confirmed!r}",
    )
    resume_task_to_draft_pr_status = (
        (resume_confirmed.get("task_to_draft_pr") or {}).get("status")
    )
    _require(
        resume_task_to_draft_pr_status == "draft_pr_already_created",
        f"resume task_to_draft_pr status mismatch: {resume_task_to_draft_pr_status!r}",
    )
    _require(fake_runner.call_count == 1, "resume called fake runner again")
    _require(
        fake_branch_push.call_count == 1, "resume called fake branch push again"
    )
    _require(fake_draft_pr.call_count == 1, "resume called fake draft PR again")
    _require(
        evidence_after_confirmed == _evidence_counts(db_path),
        "resume changed evidence counts",
    )

    resume_safety = resume_confirmed.get("safety") or {}
    _require(
        resume_safety.get("processed_task_count") in (0, 1),
        f"resume processed_task_count out of range: {resume_safety!r}",
    )
    _assert_confirmed_no_forbidden(resume_safety)
    forbidden_counts_after_resume = _forbidden_side_effect_counts(db_path)
    _require(
        forbidden_counts_after_resume == {
            "artifacts": 0,
            "events": 0,
            "payload_markers": 0,
        },
        f"forbidden side effects after resume: {forbidden_counts_after_resume}",
    )

    return {
        "ok": True,
        "task_key": eligible_key,
        "db_path": str(db_path),
        "workspace_root": str(workspace_root),
        "artifact_root": str(artifact_root),
        "dry_run": {
            "candidate_count": (dry_run_result.get("preview") or {}).get(
                "candidate_count"
            ),
            "runner_call_count": 0,
            "branch_push_call_count": 0,
            "draft_pr_call_count": 0,
        },
        "confirmed": {
            "status": confirmed.get("status"),
            "selected_task_key": confirmed.get("selected_task_key"),
            "processed_task_count": confirmed_safety.get("processed_task_count"),
            "runner_call_count": fake_runner.call_count,
            "branch_push_call_count": fake_branch_push.call_count,
            "draft_pr_call_count": fake_draft_pr.call_count,
        },
        "resume": {
            "status": resume_confirmed.get("status"),
            "task_to_draft_pr_status": resume_task_to_draft_pr_status,
            "runner_call_count_after_resume": fake_runner.call_count,
            "branch_push_call_count_after_resume": fake_branch_push.call_count,
            "draft_pr_call_count_after_resume": fake_draft_pr.call_count,
        },
        "safety": {
            "one_task_only": bool(confirmed_safety.get("one_task_only")),
            "scheduler_loop_started": bool(
                confirmed_safety.get("scheduler_loop_started")
            ),
            "background_worker_started": bool(
                confirmed_safety.get("background_worker_started")
            ),
            "automatic_task_picking_started": bool(
                confirmed_safety.get("automatic_task_picking_started")
            ),
            "multi_task_batch_started": bool(
                confirmed_safety.get("multi_task_batch_started")
            ),
            "approved": bool(confirmed_safety.get("approved")),
            "merged": bool(confirmed_safety.get("merged")),
            "cleanup_performed": bool(confirmed_safety.get("cleanup_performed")),
            "human_review_required": bool(
                confirmed_safety.get("human_review_required")
            ),
        },
        "forbidden_side_effect_counts": forbidden_counts_after_resume,
    }


def _assert_dry_run_safety(safety: dict[str, Any]) -> None:
    _require(safety.get("dry_run") is True, f"dry_run safety mismatch: {safety!r}")
    _require(safety.get("preview_only") is True, f"preview_only mismatch: {safety!r}")
    _require(
        safety.get("task_to_draft_pr_pipeline_called") is False,
        f"dry-run pipeline_called mismatch: {safety!r}",
    )
    _require(
        safety.get("approved_task_runner_called") is False,
        f"dry-run runner_called mismatch: {safety!r}",
    )
    for key in (
        "github_mutated",
        "branch_pushed",
        "draft_pr_created",
        "approved",
        "merged",
        "cleanup_performed",
        "scheduler_loop_started",
        "background_worker_started",
        "automatic_task_picking_started",
        "multi_task_batch_started",
    ):
        _require(safety.get(key) is False, f"dry-run {key} mismatch: {safety!r}")


def _assert_confirmed_no_forbidden(safety: dict[str, Any]) -> None:
    for key in (
        "approved",
        "merged",
        "cleanup_performed",
        "scheduler_loop_started",
        "background_worker_started",
        "automatic_task_picking_started",
        "multi_task_batch_started",
    ):
        _require(
            safety.get(key) is False,
            f"confirmed forbidden side effect {key}: {safety!r}",
        )
    _require(
        safety.get("one_task_only") is True,
        f"confirmed one_task_only mismatch: {safety!r}",
    )
    _require(
        safety.get("human_review_required") is True,
        f"confirmed human_review_required mismatch: {safety!r}",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Level 8B watcher one-task smoke."
    )
    parser.add_argument(
        "--workspace-root",
        help="Absolute workspace root. Defaults to a temporary directory.",
    )
    parser.add_argument("--keep-workspace", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cleanup_workspace = False
    workspace_root: Path | None = None
    try:
        if args.workspace_root:
            workspace_root = _require_absolute_path(
                args.workspace_root, "workspace_root"
            )
        else:
            workspace_root = Path(
                tempfile.mkdtemp(prefix="agent-taskflow-l8b-", dir="/tmp")
            )
            cleanup_workspace = not args.keep_workspace
        summary = run_smoke(workspace_root=workspace_root)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    finally:
        if cleanup_workspace and workspace_root is not None:
            shutil.rmtree(workspace_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
