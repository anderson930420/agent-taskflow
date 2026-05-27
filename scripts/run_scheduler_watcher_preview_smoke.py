#!/usr/bin/env python3
"""Smoke test for the Level 8A scheduler watcher dry-run preview."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_taskflow.models import TaskRecord  # noqa: E402
from agent_taskflow.scheduler_watcher_preview import (  # noqa: E402
    SchedulerWatcherPreviewRequest,
    build_scheduler_watcher_preview,
)
from agent_taskflow.store import TaskMirrorStore  # noqa: E402


ELIGIBLE_TASK_KEY = "AT-L8A-WATCHER-ELIGIBLE"
BLOCKED_TASK_KEY = "AT-L8A-WATCHER-BLOCKED"
WAITING_TASK_KEY = "AT-L8A-WATCHER-WAITING"
COMPLETED_TASK_KEY = "AT-L8A-WATCHER-COMPLETED"


def main() -> int:
    try:
        summary = run_smoke()
    except AssertionError as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                indent=2,
                sort_keys=True,
            )
        )
        return 1

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def run_smoke() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="agent-taskflow-l8a-") as tmp:
        root = Path(tmp)
        db_path = root / "state.db"
        repo_path = root / "repo"
        artifact_root = root / "artifacts"
        repo_path.mkdir()
        artifact_root.mkdir()

        store = TaskMirrorStore(db_path)
        store.init_db()
        _seed_tasks(store, repo_path=repo_path, artifact_root=artifact_root)

        counts_before = _db_counts(db_path)
        statuses_before = _task_statuses(store)

        preview = build_scheduler_watcher_preview(
            SchedulerWatcherPreviewRequest(db_path=db_path)
        )

        counts_after = _db_counts(db_path)
        statuses_after = _task_statuses(store)

        candidate_keys = {item["task_key"] for item in preview["candidates"]}
        skipped_by_key = {item["task_key"]: item for item in preview["skipped"]}

        eligible_task_seen = ELIGIBLE_TASK_KEY in candidate_keys
        blocked_task_skipped = (
            skipped_by_key.get(BLOCKED_TASK_KEY, {}).get("reason") == "blocked"
        )
        waiting_task_skipped = (
            skipped_by_key.get(WAITING_TASK_KEY, {}).get("reason")
            == "waiting_approval"
        )
        completed_task_skipped = (
            skipped_by_key.get(COMPLETED_TASK_KEY, {}).get("reason") == "completed"
        )
        db_counts_unchanged = counts_before == counts_after
        task_statuses_unchanged = statuses_before == statuses_after

        include_waiting = build_scheduler_watcher_preview(
            SchedulerWatcherPreviewRequest(
                db_path=db_path,
                include_waiting_approval=True,
            )
        )
        include_completed = build_scheduler_watcher_preview(
            SchedulerWatcherPreviewRequest(
                db_path=db_path,
                include_completed=True,
                include_no_action=True,
            )
        )

        assert preview["candidate_count"] == 1, preview
        assert preview["skipped_count"] == 3, preview
        assert eligible_task_seen, preview
        assert blocked_task_skipped, preview
        assert waiting_task_skipped, preview
        assert completed_task_skipped, preview
        assert db_counts_unchanged, (counts_before, counts_after)
        assert task_statuses_unchanged, (statuses_before, statuses_after)
        assert include_waiting["candidate_count"] >= 1, include_waiting
        assert include_completed["skipped_count"] >= 1, include_completed
        _assert_safety(preview["safety"])

        return {
            "ok": True,
            "candidate_count": preview["candidate_count"],
            "skipped_count": preview["skipped_count"],
            "eligible_task_seen": eligible_task_seen,
            "blocked_task_skipped": blocked_task_skipped,
            "waiting_task_skipped": waiting_task_skipped,
            "completed_task_skipped": completed_task_skipped,
            "db_counts_unchanged": db_counts_unchanged,
            "task_statuses_unchanged": task_statuses_unchanged,
            "safety": {
                "dry_run_preview": preview["safety"]["dry_run_preview"],
                "read_only": preview["safety"]["read_only"],
                "task_execution_started": preview["safety"]["task_execution_started"],
                "one_shot_pipeline_called": preview["safety"][
                    "one_shot_pipeline_called"
                ],
                "task_to_draft_pr_pipeline_called": preview["safety"][
                    "task_to_draft_pr_pipeline_called"
                ],
                "approved_task_runner_called": preview["safety"][
                    "approved_task_runner_called"
                ],
                "github_mutated": preview["safety"]["github_mutated"],
                "branch_pushed": preview["safety"]["branch_pushed"],
                "draft_pr_created": preview["safety"]["draft_pr_created"],
                "approved": preview["safety"]["approved"],
                "merged": preview["safety"]["merged"],
                "cleanup_performed": preview["safety"]["cleanup_performed"],
                "scheduler_loop_started": preview["safety"]["scheduler_loop_started"],
                "background_worker_started": preview["safety"][
                    "background_worker_started"
                ],
                "automatic_task_picking_started": preview["safety"][
                    "automatic_task_picking_started"
                ],
            },
        }


def _seed_tasks(
    store: TaskMirrorStore,
    *,
    repo_path: Path,
    artifact_root: Path,
) -> None:
    _seed_task(
        store,
        task_key=ELIGIBLE_TASK_KEY,
        status="queued",
        title="Eligible watcher preview task",
        repo_path=repo_path,
        artifact_root=artifact_root,
    )
    _seed_task(
        store,
        task_key=BLOCKED_TASK_KEY,
        status="blocked",
        title="Blocked watcher preview task",
        repo_path=repo_path,
        artifact_root=artifact_root,
        blocked_reason="waiting on human decision",
    )
    _seed_task(
        store,
        task_key=WAITING_TASK_KEY,
        status="waiting_approval",
        title="Waiting approval watcher preview task",
        repo_path=repo_path,
        artifact_root=artifact_root,
    )
    _record_executor_and_validation_success(store, WAITING_TASK_KEY)
    _seed_task(
        store,
        task_key=COMPLETED_TASK_KEY,
        status="accepted",
        title="Accepted watcher preview task",
        repo_path=repo_path,
        artifact_root=artifact_root,
    )


def _seed_task(
    store: TaskMirrorStore,
    *,
    task_key: str,
    status: str,
    title: str,
    repo_path: Path,
    artifact_root: Path,
    blocked_reason: str | None = None,
) -> None:
    artifact_dir = artifact_root / task_key
    artifact_dir.mkdir()
    store.upsert_task(
        TaskRecord(
            task_key=task_key,
            project="agent-taskflow",
            board="agent-taskflow",
            title=title,
            status=status,
            repo_path=repo_path,
            artifact_dir=artifact_dir,
            blocked_reason=blocked_reason,
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-01T00:00:00Z",
        )
    )


def _record_executor_and_validation_success(
    store: TaskMirrorStore,
    task_key: str,
) -> None:
    run_id = store.create_executor_run(task_key, "manual")
    store.finish_executor_run(
        task_key,
        run_id,
        executor="manual",
        status="completed",
        exit_code=0,
        summary="done",
    )
    store.record_validation_result(
        task_key,
        "pytest",
        status="passed",
        exit_code=0,
        summary="passed",
    )


def _db_counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        return {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "artifacts": conn.execute(
                "SELECT COUNT(*) FROM task_artifacts"
            ).fetchone()[0],
        }


def _task_statuses(store: TaskMirrorStore) -> dict[str, str]:
    return {
        key: store.get_task(key).status  # type: ignore[union-attr]
        for key in (
            ELIGIBLE_TASK_KEY,
            BLOCKED_TASK_KEY,
            WAITING_TASK_KEY,
            COMPLETED_TASK_KEY,
        )
    }


def _assert_safety(safety: dict[str, Any]) -> None:
    assert safety["dry_run_preview"] is True, safety
    assert safety["read_only"] is True, safety
    for key in (
        "task_execution_started",
        "one_shot_pipeline_called",
        "task_to_draft_pr_pipeline_called",
        "approved_task_runner_called",
        "github_mutated",
        "branch_pushed",
        "draft_pr_created",
        "approved",
        "merged",
        "cleanup_performed",
        "scheduler_loop_started",
        "background_worker_started",
        "automatic_task_picking_started",
    ):
        assert safety[key] is False, (key, safety)


if __name__ == "__main__":
    raise SystemExit(main())
