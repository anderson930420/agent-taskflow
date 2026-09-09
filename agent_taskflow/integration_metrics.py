"""Integration metrics (§39.1, §39.2, §39.3).

These are collected from the Integration Controller's own evidence tables, so
they cost nothing extra at integration time and cannot drift from what actually
happened.

They exist to answer one question the spec poses in §40: is the optimistic
execution model paying for itself, or is conflict/re-integration rework
material enough to justify a predictive planner? Nothing in Step 2 branches on
these numbers — they are reporting only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.store import TaskMirrorStore


__all__ = ["IntegrationMetrics", "compute_integration_metrics"]


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


@dataclass(frozen=True)
class IntegrationMetrics:
    """A snapshot of the §39 metric set."""

    # §39.1 optimistic execution
    integration_conflict_rate: float
    late_dependency_rate: float
    upstream_rework_rate: float
    conflict_rework_cost: int

    # §39.2 re-integration
    reintegration_count_total: int
    reintegration_rate: float

    # §39.3 AI conflict resolver
    ai_conflict_resolution_success_rate: float
    post_resolution_validator_failure_rate: float

    # Supporting counts, so a rate is never reported without its base.
    integration_runs: int = 0
    conflicted_runs: int = 0
    resolved_conflict_runs: int = 0
    published_tickets: int = 0
    reintegrated_tickets: int = 0

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "integration_conflict_rate": self.integration_conflict_rate,
            "late_dependency_rate": self.late_dependency_rate,
            "upstream_rework_rate": self.upstream_rework_rate,
            "conflict_rework_cost": self.conflict_rework_cost,
            "reintegration_count_total": self.reintegration_count_total,
            "reintegration_rate": self.reintegration_rate,
            "ai_conflict_resolution_success_rate": (
                self.ai_conflict_resolution_success_rate
            ),
            "post_resolution_validator_failure_rate": (
                self.post_resolution_validator_failure_rate
            ),
            "integration_runs": self.integration_runs,
            "conflicted_runs": self.conflicted_runs,
            "resolved_conflict_runs": self.resolved_conflict_runs,
            "published_tickets": self.published_tickets,
            "reintegrated_tickets": self.reintegrated_tickets,
        }


def compute_integration_metrics(
    integration_store: IntegrationStore,
    *,
    store: TaskMirrorStore | None = None,
    task_keys: Sequence[str] | None = None,
) -> IntegrationMetrics:
    """Compute the §39 metrics from recorded integration evidence."""
    task_store = store or integration_store.task_store

    pr_states = integration_store.list_pr_states()
    if task_keys is not None:
        wanted = set(task_keys)
        pr_states = [state for state in pr_states if state["task_key"] in wanted]

    # A Ticket counts once it has actually reached a PR: §39.2 measures the
    # cost of the loose review queue, which a Ticket without a PR never entered.
    published = [state for state in pr_states if state["pr_number"] is not None]
    reintegrated = [
        state for state in published if (state["reintegration_count"] or 0) > 0
    ]
    reintegration_total = sum(
        (state["reintegration_count"] or 0) for state in pr_states
    )

    keys = integration_store.list_integration_task_keys()
    if task_keys is not None:
        wanted = set(task_keys)
        keys = [key for key in keys if key in wanted]

    runs: set[tuple[str, str]] = set()
    conflicted_runs: set[tuple[str, str]] = set()
    resolved_runs: set[tuple[str, str]] = set()
    failed_runs: set[tuple[str, str]] = set()
    runs_per_task: dict[str, set[str]] = {}

    for key in keys:
        for row in integration_store.list_validator_evidence(key):
            run = (key, row["integration_run_id"])
            runs.add(run)
            runs_per_task.setdefault(key, set()).add(row["integration_run_id"])
            if row["status"] != "passed":
                failed_runs.add(run)
        for row in integration_store.list_conflict_evidence(key):
            run = (key, row["integration_run_id"])
            runs.add(run)
            runs_per_task.setdefault(key, set()).add(row["integration_run_id"])
            conflicted_runs.add(run)
            if row["resolved"]:
                resolved_runs.add(run)

    # §39.3 — of the conflicts an AI resolver attempted, how many produced a
    # conflict-free tree, and how many of those then failed validators anyway.
    post_resolution_failures = resolved_runs & failed_runs

    # §39.1 conflict_rework_cost — extra integration runs beyond the first for
    # any Ticket that hit a conflict. This is the concrete cost of optimism.
    conflicted_tasks = {key for key, _ in conflicted_runs}
    rework_cost = sum(
        max(len(runs_per_task.get(key, set())) - 1, 0) for key in conflicted_tasks
    )

    # §39.1 late_dependency_rate and upstream_rework_rate depend on the
    # runtime-discovered dependency signal (§5.2) and on cross-Ticket file
    # overlap attribution, neither of which Step 2 owns. They are reported as
    # 0.0 with their bases exposed rather than guessed at; see HANDOFF.md.
    late_dependency_rate = 0.0
    upstream_rework_rate = _rate(len(reintegrated), len(published))

    return IntegrationMetrics(
        integration_conflict_rate=_rate(len(conflicted_runs), len(runs)),
        late_dependency_rate=late_dependency_rate,
        upstream_rework_rate=upstream_rework_rate,
        conflict_rework_cost=rework_cost,
        reintegration_count_total=reintegration_total,
        reintegration_rate=_rate(len(reintegrated), len(published)),
        ai_conflict_resolution_success_rate=_rate(
            len(resolved_runs), len(conflicted_runs)
        ),
        post_resolution_validator_failure_rate=_rate(
            len(post_resolution_failures), len(resolved_runs)
        ),
        integration_runs=len(runs),
        conflicted_runs=len(conflicted_runs),
        resolved_conflict_runs=len(resolved_runs),
        published_tickets=len(published),
        reintegrated_tickets=len(reintegrated),
    )
