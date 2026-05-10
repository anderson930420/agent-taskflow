"""Manual/no-op executors for tasks that should not run an AI worker."""

from __future__ import annotations

from agent_taskflow.executors.base import (
    Executor,
    ExecutorContext,
    ExecutorResult,
    validate_executor_result_status,
)


class ManualExecutor(Executor):
    """Executor that intentionally performs no automated work."""

    def __init__(
        self,
        *,
        name: str = "manual",
        status: str = "skipped",
        summary: str | None = None,
    ) -> None:
        if status not in {"skipped", "blocked"}:
            raise ValueError("ManualExecutor status must be 'skipped' or 'blocked'")

        self.name = name.strip()
        if not self.name:
            raise ValueError("name must not be empty")

        self.status = validate_executor_result_status(status)
        self.summary = summary or "Task requires manual handling; no worker was run."

    def run(self, context: ExecutorContext) -> ExecutorResult:
        return ExecutorResult(
            executor=self.name,
            status=self.status,
            exit_code=None,
            log_path=None,
            summary=self.summary,
            artifacts={},
        )


class NoopExecutor(ManualExecutor):
    """Named no-op executor alias."""

    def __init__(self) -> None:
        super().__init__(
            name="noop",
            status="skipped",
            summary="No-op executor skipped task execution.",
        )


__all__ = ["ManualExecutor", "NoopExecutor"]
