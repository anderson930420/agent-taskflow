#!/usr/bin/env python3
"""Run the Level 7D task-to-draft-PR smoke with fake mutations."""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
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
from agent_taskflow.store import TaskMirrorStore  # noqa: E402
from agent_taskflow.task_to_draft_pr_pipeline import (  # noqa: E402
    TaskToDraftPRPipelineRequest,
    run_task_to_draft_pr_pipeline,
)
from agent_taskflow.tasks import normalize_task_key  # noqa: E402


DEFAULT_TASK_KEY = "AT-L7D-TASK-TO-DRAFT-PR-SMOKE"
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
        store = TaskMirrorStore(db_path)
        store.update_task_status(
            task_key,
            "waiting_approval",
            source="level-7d-smoke-runner",
            message="fake approved task runner completed for Level 7D smoke",
        )
        return {
            "ok": True,
            "status": "waiting_approval",
            "phase": "level-7d-smoke-runner",
            "summary": "fake approved task runner completed",
            "artifacts": {},
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
            "title": f"{task_key}: task to draft PR smoke",
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


def _require_absolute_path(path: str | Path, field_name: str) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        raise ValueError(f"{field_name} must be absolute: {path}")
    return resolved


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


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


def _init_repo(repo_path: Path, task_key: str) -> tuple[str, str]:
    repo_path.mkdir(parents=True)
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "user.email", "agent-taskflow@example.invalid")
    _git(repo_path, "config", "user.name", "Agent Taskflow")
    (repo_path / "README.md").write_text("# Task to draft PR smoke\n", encoding="utf-8")
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
            title="Task to draft PR smoke",
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
        goal="Run task_key to draft PR smoke",
        repo_path=repo_path,
        worktree_path=repo_path,
        artifact_dir=artifact_dir,
        executor="noop",
        required_validators=("pytest",),
    )
    write_mission_contract(contract, artifact_dir=artifact_dir)

    executor_log = artifact_dir / "executor.log"
    executor_log.write_text("executor log\n", encoding="utf-8")
    run_id = store.create_executor_run(task_key, "noop")
    store.finish_executor_run(
        task_key,
        run_id,
        executor="noop",
        status="completed",
        exit_code=0,
        summary="executor summary",
        log_path=executor_log,
        artifacts={"log": executor_log},
    )
    store.record_task_artifact(task_key, "worker_log", executor_log)

    validator_log = artifact_dir / "pytest.log"
    validator_log.write_text("validator log\n", encoding="utf-8")
    store.record_validation_result(
        task_key,
        "pytest",
        status="passed",
        exit_code=0,
        summary="validator summary",
        log_path=validator_log,
        artifacts={"log": validator_log},
    )
    store.record_task_artifact(task_key, "review_log", validator_log)


def _issue_spec_text(task_key: str) -> str:
    return "\n".join(
        [
            "# Offline Issue Spec",
            "",
            f"- Repository: {DEFAULT_REPO}",
            "- Issue number: 707",
            "- Issue URL: https://github.com/anderson930420/agent-taskflow/issues/707",
            "- Issue state: open",
            "- Title: Task to draft PR smoke",
            "- Labels: smoke",
            "- Author: octocat",
            "- Created at: 2026-05-01T00:00:00Z",
            "- Updated at: 2026-05-02T00:00:00Z",
            "- Ingested at: 2026-05-03T00:00:00Z",
            f"- Task key: {task_key}",
            "",
            "Offline issue fixture for Level 7D smoke.",
            "",
        ]
    )


def run_smoke(
    *,
    workspace_root: Path,
    task_key: str = DEFAULT_TASK_KEY,
) -> dict[str, Any]:
    normalized_task_key = normalize_task_key(task_key)
    workspace_root = _require_absolute_path(workspace_root, "workspace_root")
    db_path = workspace_root / "task-to-draft-pr-pipeline-smoke.db"
    repo_path = workspace_root / "repo"
    artifact_root = workspace_root / "artifacts"
    workspace_root.mkdir(parents=True, exist_ok=True)
    artifact_root.mkdir(parents=True, exist_ok=True)

    store = TaskMirrorStore(db_path)
    store.init_db()
    base_sha, branch = _init_repo(repo_path, normalized_task_key)
    _seed_queued_task(
        store=store,
        task_key=normalized_task_key,
        repo_path=repo_path,
        artifact_root=artifact_root,
        base_sha=base_sha,
        branch=branch,
    )

    fake_runner = _FakeApprovedTaskRunner()
    fake_branch_push = _FakeBranchPush()
    fake_draft_pr = _FakeDraftPR()

    dry_run = run_task_to_draft_pr_pipeline(
        TaskToDraftPRPipelineRequest(
            db_path=db_path,
            artifact_root=artifact_root,
            task_key=normalized_task_key,
            dry_run=True,
        ),
        approved_task_runner_fn=fake_runner,
        branch_push_fn=fake_branch_push,
        draft_pr_fn=fake_draft_pr,
    )
    _require(dry_run.get("ok") is True, f"dry-run not ok: {dry_run!r}")
    _require(fake_runner.call_count == 0, "dry-run called fake runner")
    _require(fake_branch_push.call_count == 0, "dry-run called fake branch push")
    _require(fake_draft_pr.call_count == 0, "dry-run called fake draft PR")

    confirmed = run_task_to_draft_pr_pipeline(
        TaskToDraftPRPipelineRequest(
            db_path=db_path,
            artifact_root=artifact_root,
            task_key=normalized_task_key,
            dry_run=False,
            confirm_run_one_shot_pipeline=True,
            confirm_prepare_pr=True,
            confirm_github_mutations=True,
            confirm_branch_push=True,
            confirm_draft_pr=True,
            operator="level-7d-smoke",
            operator_note="Level 7D task to draft PR smoke",
        ),
        approved_task_runner_fn=fake_runner,
        branch_push_fn=fake_branch_push,
        draft_pr_fn=fake_draft_pr,
    )
    _require(confirmed.get("ok") is True, f"confirmed not ok: {confirmed!r}")
    _require(confirmed.get("status") == "draft_pr_created", "draft PR not created")
    _require(fake_runner.call_count == 1, "fake runner not called once")
    _require(fake_branch_push.call_count == 1, "fake branch push not called once")
    _require(fake_draft_pr.call_count == 1, "fake draft PR not called once")
    evidence_counts_after_confirmed = _evidence_counts(db_path)
    final_task = TaskMirrorStore(db_path).get_task(normalized_task_key)
    final_task_status = final_task.status if final_task is not None else None
    _require(
        final_task_status == "waiting_approval",
        f"final task status mismatch: {final_task_status!r}",
    )

    safety = confirmed.get("safety") or {}
    _require(safety.get("approved") is False, "approved side effect present")
    _require(safety.get("merged") is False, "merged side effect present")
    _require(safety.get("cleanup_performed") is False, "cleanup side effect present")
    _require(
        safety.get("scheduler_loop_started") is False,
        "scheduler loop side effect present",
    )
    _require(
        safety.get("background_worker_started") is False,
        "background worker side effect present",
    )
    _require(
        safety.get("automatic_task_picking_started") is False,
        "automatic picking side effect present",
    )
    forbidden_counts = _forbidden_side_effect_counts(db_path)
    _require(
        forbidden_counts == {"artifacts": 0, "events": 0, "payload_markers": 0},
        f"forbidden side effects found: {forbidden_counts}",
    )

    resume_confirmed = run_task_to_draft_pr_pipeline(
        TaskToDraftPRPipelineRequest(
            db_path=db_path,
            artifact_root=artifact_root,
            task_key=normalized_task_key,
            dry_run=False,
            resume_existing=True,
            resume_pr_preparation=True,
            confirm_run_one_shot_pipeline=True,
            confirm_prepare_pr=True,
            confirm_github_mutations=True,
            confirm_branch_push=True,
            confirm_draft_pr=True,
            operator="level-7e-smoke",
            operator_note="Level 7E task to draft PR resume smoke",
        ),
        approved_task_runner_fn=fake_runner,
        branch_push_fn=fake_branch_push,
        draft_pr_fn=fake_draft_pr,
    )
    _require(
        resume_confirmed.get("ok") is True,
        f"resume confirmed not ok: {resume_confirmed!r}",
    )
    _require(
        resume_confirmed.get("status") == "draft_pr_already_created",
        "resume did not report existing draft PR",
    )
    _require(fake_runner.call_count == 1, "resume called fake runner again")
    _require(
        fake_branch_push.call_count == 1,
        "resume called fake branch push again",
    )
    _require(fake_draft_pr.call_count == 1, "resume called fake draft PR again")
    _require(
        evidence_counts_after_confirmed == _evidence_counts(db_path),
        "resume changed evidence counts",
    )
    resume_safety = resume_confirmed.get("safety") or {}
    _require(
        resume_safety.get("approved_task_runner_called") is False,
        "resume reported runner call",
    )
    _require(
        resume_safety.get("branch_pushed") is False,
        "resume pushed branch",
    )
    _require(
        resume_safety.get("draft_pr_created") is False,
        "resume created draft PR",
    )
    forbidden_counts_after_resume = _forbidden_side_effect_counts(db_path)
    _require(
        forbidden_counts_after_resume == {"artifacts": 0, "events": 0, "payload_markers": 0},
        f"forbidden side effects after resume: {forbidden_counts_after_resume}",
    )

    pr_stage = (confirmed.get("stages") or {}).get("pr_preparation") or {}
    resume_pr_stage = (
        (resume_confirmed.get("stages") or {}).get("pr_preparation") or {}
    )
    return {
        "ok": True,
        "task_key": normalized_task_key,
        "db_path": str(db_path),
        "workspace_root": str(workspace_root),
        "artifact_root": str(artifact_root),
        "dry_run": {
            "ok": dry_run.get("ok") is True,
            "runner_call_count": 0,
            "branch_push_call_count": 0,
            "draft_pr_call_count": 0,
        },
        "confirmed": {
            "ok": confirmed.get("ok") is True,
            "status": confirmed.get("status"),
            "runner_call_count": fake_runner.call_count,
            "branch_push_call_count": fake_branch_push.call_count,
            "draft_pr_call_count": fake_draft_pr.call_count,
            "final_task_status": final_task_status,
            "pr_url": pr_stage.get("pr_url"),
            "pr_number": pr_stage.get("pr_number"),
        },
        "resume_confirmed": {
            "ok": resume_confirmed.get("ok") is True,
            "status": resume_confirmed.get("status"),
            "runner_call_count": fake_runner.call_count,
            "branch_push_call_count": fake_branch_push.call_count,
            "draft_pr_call_count": fake_draft_pr.call_count,
            "branch_pushed": resume_safety.get("branch_pushed"),
            "draft_pr_created": resume_safety.get("draft_pr_created"),
            "branch_push_reused": resume_pr_stage.get("branch_push_reused"),
            "draft_pr_reused": resume_pr_stage.get("draft_pr_reused"),
            "draft_pr_already_created": resume_pr_stage.get(
                "draft_pr_already_created"
            ),
            "evidence_counts_unchanged": evidence_counts_after_confirmed
            == _evidence_counts(db_path),
        },
        "safety": {
            "approved": safety.get("approved"),
            "merged": safety.get("merged"),
            "cleanup_performed": safety.get("cleanup_performed"),
            "scheduler_loop_started": safety.get("scheduler_loop_started"),
            "background_worker_started": safety.get("background_worker_started"),
            "automatic_task_picking_started": safety.get(
                "automatic_task_picking_started"
            ),
            "human_review_required": safety.get("human_review_required"),
        },
        "forbidden_side_effect_counts": forbidden_counts_after_resume,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Level 7D task-to-draft-PR pipeline smoke."
    )
    parser.add_argument("--task-key", default=DEFAULT_TASK_KEY)
    parser.add_argument(
        "--workspace-root",
        help="Absolute workspace root. Defaults to a temporary directory under /tmp.",
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
                tempfile.mkdtemp(prefix="agent-taskflow-l7d-task-pr-", dir="/tmp")
            )
            cleanup_workspace = not args.keep_workspace
        summary = run_smoke(workspace_root=workspace_root, task_key=args.task_key)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Task-to-draft-PR pipeline smoke failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if cleanup_workspace and workspace_root is not None:
            shutil.rmtree(workspace_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
