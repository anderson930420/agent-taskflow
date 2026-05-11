"""FastAPI app for Agent Taskflow Mission Control."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from agent_taskflow.api.schemas import (
    ActionResponse,
    ApprovalRequest,
    BlockTaskRequest,
    CreateTaskRequest,
    RejectRequest,
    StartTaskRequest,
    ValidateTaskRequest,
    action_response,
    approval_decision_to_dict,
    artifact_to_dict,
    detail_response,
    dispatcher_result_to_dict,
    executor_run_to_dict,
    list_response,
    project_to_dict,
    task_to_dict,
    validation_result_to_dict,
)
from agent_taskflow.dispatcher import DEFAULT_VALIDATORS, Dispatcher
from agent_taskflow.governance import (
    assert_not_main_repo_write,
    assert_worktree_inside_repo_worktrees,
)
from agent_taskflow.models import TaskRecord, TaskWorktreeRecord, require_absolute_path
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key


SERVICE_NAME = "agent-taskflow-api"

DispatcherFactory = Callable[[TaskMirrorStore, Sequence[str]], Any]


def create_app(
    db_path: str | Path | None = None,
    *,
    dispatcher_factory: DispatcherFactory | None = None,
) -> FastAPI:
    """Create the Mission Control API app.

    db_path is injectable for tests. The default app uses the standard local
    mirror database path. Action routes mutate only the local mirror state and
    route execution through the dispatcher abstraction.
    """
    store = TaskMirrorStore(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store.init_db()
        yield

    app = FastAPI(title="Agent Taskflow Mission Control API", lifespan=lifespan)

    def get_store() -> TaskMirrorStore:
        return store

    def make_dispatcher(
        current_store: TaskMirrorStore,
        validators: Sequence[str],
    ) -> Any:
        if dispatcher_factory is not None:
            return dispatcher_factory(current_store, validators)
        return Dispatcher(current_store, validators=validators)

    def task_or_404(task_key: str, current_store: TaskMirrorStore) -> TaskRecord:
        try:
            normalized_task_key = normalize_task_key(task_key)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_key}") from exc

        task = current_store.get_task(normalized_task_key)
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {normalized_task_key}")
        return task

    def conflict(action: str, task: TaskRecord, message: str) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=action_response(
                ok=False,
                action=action,
                task_key=task.task_key,
                status=task.status,
                message=message,
                item=task_to_dict(task),
            ),
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": SERVICE_NAME}

    @app.get("/api/projects")
    def list_projects(
        current_store: TaskMirrorStore = Depends(get_store),
    ) -> dict[str, object]:
        items = [project_to_dict(project) for project in current_store.list_projects()]
        return list_response(items)

    @app.get("/api/tasks")
    def list_tasks(
        status: str | None = Query(default=None),
        project: str | None = Query(default=None),
        current_store: TaskMirrorStore = Depends(get_store),
    ) -> dict[str, object]:
        try:
            tasks = current_store.list_tasks(project=project, status=status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return list_response([task_to_dict(task) for task in tasks])

    @app.post("/api/tasks", response_model=ActionResponse)
    def create_task(
        request: CreateTaskRequest,
        current_store: TaskMirrorStore = Depends(get_store),
    ) -> dict[str, object]:
        try:
            task_key = normalize_task_key(request.task_key)
            repo_path = require_absolute_path(request.repo_path, "repo_path")
            worktree_path = require_absolute_path(request.worktree_path, "worktree_path")
            artifact_dir = require_absolute_path(request.artifact_dir, "artifact_dir")
            assert_not_main_repo_write(worktree_path, repo_path)
            assert_worktree_inside_repo_worktrees(worktree_path, repo_path)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if current_store.get_task(task_key) is not None:
            return JSONResponse(
                status_code=409,
                content=action_response(
                    ok=False,
                    action="create",
                    task_key=task_key,
                    status="queued",
                    message=f"Task already exists: {task_key}",
                ),
            )

        task = TaskRecord(
            task_key=task_key,
            project=request.project,
            board=request.board or request.project,
            hermes_task_id=request.hermes_task_id,
            title=request.title or f"Task {task_key}",
            status="queued",
            repo_path=repo_path,
            artifact_dir=artifact_dir,
        )
        worktree = TaskWorktreeRecord(
            task_key=task_key,
            repo_path=repo_path,
            worktree_path=worktree_path,
            branch=request.branch or f"task/{task_key}",
            base_branch=request.base_branch,
            status="active",
        )

        current_store.upsert_task(task)
        current_store.upsert_task_worktree(worktree)
        current_store.record_task_event(
            task_key,
            "created",
            "api",
            message="Task created through action API",
            payload={
                "kind": "task_created",
                "executor": request.executor,
                "model": request.model,
                "validator": request.validator,
                "pr_url": request.pr_url,
                "pr_number": request.pr_number,
            },
        )

        created = current_store.get_task(task_key)
        assert created is not None
        return action_response(
            ok=True,
            action="create",
            task_key=task_key,
            status=created.status,
            message="Task created",
            item=task_to_dict(created),
        )

    @app.get("/api/tasks/{task_key}")
    def get_task(
        task_key: str,
        current_store: TaskMirrorStore = Depends(get_store),
    ) -> dict[str, object]:
        task = task_or_404(task_key, current_store)
        return detail_response(task_to_dict(task))

    @app.post("/api/tasks/{task_key}/start", response_model=ActionResponse)
    def start_task(
        task_key: str,
        request: StartTaskRequest | None = None,
        current_store: TaskMirrorStore = Depends(get_store),
    ) -> dict[str, object] | JSONResponse:
        task = task_or_404(task_key, current_store)

        if task.status in {
            "waiting_approval",
            "waiting_for_review",
            "accepted",
            "rejected",
            "cleaned",
            "completed",
            "canceled",
        }:
            return conflict(
                "start",
                task,
                f"Task cannot be started from status: {task.status}",
            )

        request = request or StartTaskRequest()
        validators = tuple(request.validators) if request.validators is not None else DEFAULT_VALIDATORS
        dispatcher = make_dispatcher(current_store, validators)
        result = dispatcher.dispatch_task(
            task.task_key,
            executor_name=request.executor,
            model=request.model,
            dry_run=request.dry_run,
        )

        ok = result.status not in {"blocked"}
        return action_response(
            ok=ok,
            action="start",
            task_key=result.task_key,
            status=result.status,
            message=result.summary,
            item=dispatcher_result_to_dict(result),
        )

    @app.post("/api/tasks/{task_key}/validate", response_model=ActionResponse)
    def validate_task(
        task_key: str,
        request: ValidateTaskRequest | None = None,
        current_store: TaskMirrorStore = Depends(get_store),
    ) -> JSONResponse:
        task = task_or_404(task_key, current_store)
        _ = request
        return JSONResponse(
            status_code=501,
            content=action_response(
                ok=False,
                action="validate",
                task_key=task.task_key,
                status=task.status,
                message="validation-only endpoint is not implemented yet",
                item=task_to_dict(task),
            ),
        )

    @app.post("/api/tasks/{task_key}/approve", response_model=ActionResponse)
    def approve_task(
        task_key: str,
        request: ApprovalRequest,
        current_store: TaskMirrorStore = Depends(get_store),
    ) -> dict[str, object] | JSONResponse:
        task = task_or_404(task_key, current_store)
        if task.status != "waiting_approval":
            return conflict(
                "approve",
                task,
                f"Task must be waiting_approval before approve; current status: {task.status}",
            )

        try:
            current_store.record_approval_decision(
                task.task_key,
                "accepted",
                decided_by=request.decided_by,
                notes=request.notes,
                source="api",
            )
            current_store.update_task_status(
                task.task_key,
                "accepted",
                source="api",
                message="Task accepted through action API",
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        updated = task_or_404(task.task_key, current_store)
        return action_response(
            ok=True,
            action="approve",
            task_key=updated.task_key,
            status=updated.status,
            message="Task accepted",
            item=task_to_dict(updated),
        )

    @app.post("/api/tasks/{task_key}/reject", response_model=ActionResponse)
    def reject_task(
        task_key: str,
        request: RejectRequest,
        current_store: TaskMirrorStore = Depends(get_store),
    ) -> dict[str, object] | JSONResponse:
        task = task_or_404(task_key, current_store)
        if task.status not in {"waiting_approval", "blocked"}:
            return conflict(
                "reject",
                task,
                f"Task must be waiting_approval or blocked before reject; current status: {task.status}",
            )

        try:
            current_store.record_approval_decision(
                task.task_key,
                "rejected",
                decided_by=request.decided_by,
                notes=request.notes,
                source="api",
            )
            current_store.update_task_status(
                task.task_key,
                "rejected",
                source="api",
                message="Task rejected through action API",
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        updated = task_or_404(task.task_key, current_store)
        return action_response(
            ok=True,
            action="reject",
            task_key=updated.task_key,
            status=updated.status,
            message="Task rejected",
            item=task_to_dict(updated),
        )

    @app.post("/api/tasks/{task_key}/block", response_model=ActionResponse)
    def block_task(
        task_key: str,
        request: BlockTaskRequest,
        current_store: TaskMirrorStore = Depends(get_store),
    ) -> dict[str, object]:
        task = task_or_404(task_key, current_store)
        reason = request.blocked_reason.strip()
        if not reason:
            raise HTTPException(status_code=422, detail="blocked_reason must not be empty")

        current_store.update_task_status(
            task.task_key,
            "blocked",
            source="api",
            message=reason,
            blocked_reason=reason,
        )

        updated = task_or_404(task.task_key, current_store)
        return action_response(
            ok=True,
            action="block",
            task_key=updated.task_key,
            status=updated.status,
            message=reason,
            item=task_to_dict(updated),
        )

    @app.get("/api/tasks/{task_key}/runs")
    def list_executor_runs(
        task_key: str,
        current_store: TaskMirrorStore = Depends(get_store),
    ) -> dict[str, object]:
        task = task_or_404(task_key, current_store)
        runs = current_store.list_executor_runs(task.task_key)
        return list_response([executor_run_to_dict(run) for run in runs])

    @app.get("/api/tasks/{task_key}/artifacts")
    def list_artifacts(
        task_key: str,
        current_store: TaskMirrorStore = Depends(get_store),
    ) -> dict[str, object]:
        task = task_or_404(task_key, current_store)
        artifacts = current_store.list_task_artifacts(task.task_key)
        return list_response([artifact_to_dict(artifact) for artifact in artifacts])

    @app.get("/api/tasks/{task_key}/validations")
    def list_validations(
        task_key: str,
        current_store: TaskMirrorStore = Depends(get_store),
    ) -> dict[str, object]:
        task = task_or_404(task_key, current_store)
        results = current_store.list_validation_results(task.task_key)
        return list_response([validation_result_to_dict(result) for result in results])

    @app.get("/api/tasks/{task_key}/approvals")
    def list_approvals(
        task_key: str,
        current_store: TaskMirrorStore = Depends(get_store),
    ) -> dict[str, object]:
        task = task_or_404(task_key, current_store)
        decisions = current_store.list_approval_decisions(task.task_key)
        return list_response([approval_decision_to_dict(decision) for decision in decisions])

    return app


app = create_app()
