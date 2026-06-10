# Scheduler-to-ExecutionEngine Migration Boundary (P5-a)

This is the **scheduler-execution-engine migration boundary** inventory
(`docs/scheduler-execution-engine-migration-boundary.md`). It is a
**documentation only** phase: it defines the boundary for a possible future
**scheduler-to-ExecutionEngine migration** without implementing any part of
that migration.

> **This phase adds no behavior.** The **active cron is not changed by P5-a**,
> the **active crontab is not modified by this phase**, and the **scheduler
> tick is not migrated to ExecutionEngine**. The live scheduler path described
> below stays exactly as it is.

See also:

- `docs/execution-engine-contract.md` — the P4 ExecutionEngine contract
  dataclasses / protocol.
- `docs/execution-engine-approved-task-adapter.md` — the
  `ApprovedTaskRunnerExecutionEngineAdapter`.
- `docs/execution-engine-manual-runtime-path.md` — the manual opt-in engine
  facade.
- `docs/execution-observability-summary.md` — the unified execution
  observability summary schema.
- `docs/real-scheduled-execution-observability.md` — the read-only dashboard /
  summarizer.
- `docs/active-cron-observability-post-rollout-validation.md` — the validated
  live cron observability state this boundary must preserve.
- `docs/scheduler-execution-engine-request-builder.md` — the P5-b
  request-builder contract, the first implemented stage of this plan.

## Purpose

This document exists to:

- define the boundary for a future **scheduler-to-ExecutionEngine migration**;
- preserve the stable active cron path — the **active cron remains stable**
  and is the live source of truth throughout any future migration;
- document how confirmed one-task scheduler execution would map onto the
  ExecutionEngine contract / facade / adapter if a later phase migrates it;
- define what remains legacy / fallback during migration.

## Current stable path (live today)

This is the path that must keep working unchanged:

- The scheduler tick
  (`agent_taskflow/github_issue_one_task_scheduler_tick.py`, driven by
  `scripts/run_github_issue_one_task_scheduler_tick.py`) discovers, ingests,
  and selects eligible confirmed one-task work.
- The **existing approved task runner path remains the live execution
  authority** for confirmed one-task work.
- The **real cron remains execution-only**: `publish_after_execution=False`,
  `mode=execution_only`.
- The **active cron remains stable** and validated: the active cron line
  includes the observability flag, and each live JSONL tick line carries a
  top-level `observability_summary`
  (`schema_version=execution_observability_summary.v1`,
  `source=scheduler_tick`). See
  `docs/active-cron-observability-post-rollout-validation.md`.
- The **dashboard reads the unified summary**:
  `scripts/summarize_real_scheduled_execution.py` reads
  `observability_summary` when present and falls back to the legacy scheduler
  tick payload when absent.
- **No ExecutionEngine-backed scheduler execution is active yet.** The
  scheduler tick does not construct, invoke, or depend on any ExecutionEngine
  component.

## Existing ExecutionEngine pieces (built in P4)

These pieces already exist and are the target surface a future migration would
map onto:

- **ExecutionEngine contract dataclasses / protocol**
  (`agent_taskflow/execution_engine_contract.py`): the frozen request / result
  / profile / safety dataclasses and the `ExecutionEngine` protocol
  (`execute(request) -> result`).
- **`ApprovedTaskRunnerExecutionEngineAdapter`**
  (`agent_taskflow/execution_engine_approved_task_adapter.py`): wraps the
  existing approved task runner behind the ExecutionEngine contract without
  changing runner behavior.
- **Manual opt-in engine facade**
  (`agent_taskflow/execution_engine_manual_runtime.py`,
  `scripts/run_execution_engine_approved_task.py`): the only current runtime
  entry into the engine path, and it is manual and opt-in only.
- **Unified execution observability summary**
  (`agent_taskflow/execution_observability.py`,
  `UnifiedExecutionSummary`, `schema_version=execution_observability_summary.v1`):
  one normalized summary shape for engine results, approved task runner
  payloads, and scheduler tick payloads.
- **Manual and scheduler observability output**: both the manual engine facade
  and the legacy scheduler tick can emit the unified
  `observability_summary`, so manual and scheduled execution are already
  comparable through the same schema.
- **Dashboard summary reader**
  (`scripts/summarize_real_scheduled_execution.py`): reads unified summaries
  when present, falls back to the legacy payload when absent, and safely
  skips malformed lines.

## Proposed future mapping (not implemented in P5-a)

If a later phase migrates confirmed one-task scheduler execution to the
engine, the mapping would be:

| Scheduler concept | ExecutionEngine contract |
| --- | --- |
| Scheduler selected task / issue / candidate (confirmed one-task work) | `ExecutionEngineRequest.task_key` (+ a scheduler `source` marker) |
| Executor profile (executor + model + flags) | `ExecutionEngineExecutorProfile` |
| Validator profile (deterministic validators to run) | `ExecutionEngineValidatorProfile` |
| Workspace profile (worktree / branch / path policy) | `ExecutionEngineWorkspaceProfile` |
| Artifact refs (proof-of-work index entries) | `ExecutionEngineArtifactRef` |
| Safety flags (governance non-crossing evidence) | `ExecutionEngineSafety` |
| Execution request (one tick, one task) | `ExecutionEngineRequest` |
| Execution result (normalized outcome) | `ExecutionEngineResult` |
| Observability summary (tick JSONL output) | `UnifiedExecutionSummary` emitted as `observability_summary` |

The scheduler tick would build one execution request per selected confirmed
task, hand it to the engine facade backed by the
`ApprovedTaskRunnerExecutionEngineAdapter`, receive one execution result, and
summarize it into the same `observability_summary` shape the dashboard already
reads.

## Explicit non-mapping / deferred items

The following are **not** part of the scheduler-to-ExecutionEngine migration
boundary and stay outside the engine contract entirely:

- approval
- merge
- cleanup
- archive
- closeout
- PR publication
- issue close
- branch deletion
- worktree deletion
- multi-task behavior
- daemon / webhook / background worker
- automatic scheduler loop

These remain human-gated or explicitly out of scope; no future engine mapping
may absorb them implicitly.

## Migration constraints

Any future migration phase must respect all of the following:

- The **legacy scheduler path remains the default**; the legacy fallback
  remains default and readable throughout migration.
- The **future engine path must be opt-in** (explicit flag or profile, never
  implicit).
- The **active cron must not change in P5-a**; the active cron is not changed
  by P5-a.
- The engine path must preserve `publish_after_execution=False` unless a
  later human-reviewed phase explicitly changes it.
- Old JSONL / legacy payload fallback must remain readable: the dashboard must
  keep reading pre-migration lines via the legacy scheduler tick payload.
- Malformed log line skipping must remain safe (count and skip, never crash).
- **No ExecutionEngine result can become an approval authority.** An
  ExecutionEngine result cannot become approval authority, approval evidence,
  or a substitute for human review.
- **Validation authority remains the deterministic validators / human review
  gates**, not runtime audit output alone.

## Suggested future P5 stages

A staged plan, each stage separately reviewable:

- **P5-b** — request-builder contract only: a pure function mapping a selected
  confirmed task to an `ExecutionEngineRequest`, with tests, no runtime wiring.
- **P5-c** — shadow / compare summary only: build the engine-shaped summary
  alongside the legacy path and compare, without executing through the engine.
- **P5-d** — opt-in execution path, off by default: an explicit flag routes
  one confirmed task through the engine facade; default behavior unchanged.
- **P5-e** — legacy-vs-engine fallback hardening: failure-path tests proving
  the legacy path still works when the engine path errors or is disabled.
- **P5-f** — operator rollout runbook: a human-operated, reversible runbook
  (like the P4-j observability rollout) for enabling the opt-in flag.
- **P5-g** — post-rollout validation, only if the rollout actually happens.

Each stage is future work; none of them is implemented by P5-a.

## Safety boundary

P5-a adds:

- **no scheduler execution behavior**
- **no automation behavior**
- **no cron behavior**
- **no approved_task_runner behavior**
- **no executor behavior**
- **no validator behavior**
- **no DB behavior**
- **no GitHub mutation**
- **no approval**
- **no merge**
- **no cleanup**
- **no archive**
- **no closeout**
- **no PR publication**
- **no issue close**
- **no branch deletion**
- **no worktree deletion**
- **no daemon**
- **no webhook**
- **no background worker**
- **no scheduler loop**
- **no multi-task behavior**

This phase is documentation and documentation tests only.

## Conclusion

- P5-a only defines the migration boundaries: what maps to the engine
  contract, what is deferred, and what constraints any migration must hold.
- The scheduler tick remains the legacy, live-stable path; the active cron
  remains stable and the scheduler tick is not migrated to ExecutionEngine.
- ExecutionEngine-backed scheduler execution is **future work** (P5-b through
  P5-g), gated stage by stage on explicit opt-in and human review.
