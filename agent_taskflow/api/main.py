"""Read-only FastAPI app for Agent Taskflow Mission Control."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Query

from agent_taskflow.api.schemas import (
    approval_decision_to_dict,
    artifact_to_dict,
    detail_response,
    executor_run_to_dict,
    list_response,
    project_to_dict,
    task_to_dict,
    validation_result_to_dict,
)
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key


SERVICE_NAME = "agent-taskflow-api"


def create_app(db_path: str | Path | None = None) -> FastAPI:
    """Create the read-only API app.

    db_path is injectable for tests. The default app uses the standard local
    mirror database path. Initialization only ensures the SQLite schema exists;
    routes do not mutate task state or call dispatchers/workers.
    """
    store = TaskMirrorStore(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store.init_db()
        yield

    app = FastAPI(title="Agent Taskflow Mission Control API", lifespan=lifespan)

    def get_store() -> TaskMirrorStore:
        return store

    def task_or_404(task_key: str, current_store: TaskMirrorStore) -> object:
        try:
            normalized_task_key = normalize_task_key(task_key)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_key}") from exc

        task = current_store.get_task(normalized_task_key)
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {normalized_task_key}")
        return task

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

    @app.get("/api/tasks/{task_key}")
    def get_task(
        task_key: str,
        current_store: TaskMirrorStore = Depends(get_store),
    ) -> dict[str, object]:
        task = task_or_404(task_key, current_store)
        return detail_response(task_to_dict(task))

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
