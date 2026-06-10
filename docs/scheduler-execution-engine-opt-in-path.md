# Scheduler ExecutionEngine Opt-In Path (P5-d)

P5-d is the first **runtime wiring** stage of the staged
scheduler-to-ExecutionEngine migration plan defined by the P5-a boundary
document (`docs/scheduler-execution-engine-migration-boundary.md`). It adds an
explicit, off-by-default **opt-in execution path**: a *confirmed* scheduler tick
can route the one selected task through the ExecutionEngine facade / adapter for
runtime evidence, but only when the new `--use-execution-engine` flag is
provided.

> **`--use-execution-engine` is opt-in and off by default.** When the flag is
> not provided, the **default scheduler path remains legacy** and nothing in
> this stage runs. The **active cron is unchanged**: the active crontab, the
> cron example command lines, and the deploy cron examples are untouched, and
> `--use-execution-engine` is never part of any preset used by active cron.

See also:

- `docs/scheduler-execution-engine-migration-boundary.md` — the P5-a migration
  boundary this stage begins wiring at runtime.
- `docs/scheduler-execution-engine-request-builder.md` — the P5-b request
  builder that maps the selected task onto an `ExecutionEngineRequest`.
- `docs/scheduler-execution-engine-shadow-compare.md` — the P5-c shadow /
  compare layer that produces the diagnostic comparison this stage records.
- `docs/scheduler-execution-engine-fallback-hardening.md` — the P5-e fallback
  hardening layer that classifies the `execution_engine` evidence block this
  stage produces.
- `docs/execution-engine-contract.md` — the P4-b `ExecutionEngineRequest` /
  `ExecutionEngineResult` contract.

## Relationship to P5-a / P5-b / P5-c

- **P5-a** defined the scheduler-to-ExecutionEngine migration boundary.
- **P5-b** added a pure scheduler ExecutionEngine request builder
  (`agent_taskflow/scheduler_execution_engine_request_builder.py`).
- **P5-c** added a pure scheduler ExecutionEngine shadow / compare summary layer
  (`agent_taskflow/scheduler_execution_engine_shadow_compare.py`).
- **P5-d** (this stage) is the first stage that *runs* anything: it reuses the
  P5-b builder and the P5-c compare layer and, when explicitly opted in, routes
  one selected confirmed task through the ExecutionEngine facade.

## Purpose

P5-d answers "what does it look like to actually run the engine for one
scheduler-selected task?" without changing the default behavior. It is a narrow,
auditable first wiring step so later stages can build on a real, observed engine
invocation instead of a pure value.

## How to enable it

Module: `agent_taskflow/scheduler_execution_engine_opt_in.py`. CLI flag on
`scripts/run_github_issue_one_task_scheduler_tick.py`: `--use-execution-engine`.

The flag is **confirmed-mode only**. It requires `--confirmed`; a dry-run tick
with `--use-execution-engine` is rejected with a clear validation error
(`use_execution_engine requires confirmed mode`). The opt-in is never enabled by
default and is never wired into the active cron path.

When enabled, after the legacy automation selects and ingests one task, the
opt-in path:

1. builds an engine-shaped `ExecutionEngineRequest` (source `scheduled_tick`)
   via the P5-b builder;
2. produces the P5-c shadow / compare result against the legacy tick payload;
3. runs the ExecutionEngine facade **exactly once** for the one selected task
   (default facade: `ApprovedTaskRunnerExecutionEngineAdapter`); and
4. attaches an `execution_engine` evidence block to the tick payload, including
   `execution_engine_enabled` (`enabled`), the engine request and
   `request_summary`, the engine result and `result_summary`, the
   `shadow_compare` result, and an `observability_summary`. The block is
   JSON-compatible, and the existing observability summarizer still reads the
   legacy tick fields unchanged.

## What the opt-in path will not do

The opt-in path is **execution-only** by construction. The engine-shaped request
preserves `publish_after_execution=False` and `mode=execution_only`, and the
path adds:

- **no publish / PR publication / draft PR / branch push** — the engine path
  never publishes, never opens or drafts a pull request, and never pushes a
  branch;
- **no approval / merge / cleanup / archive / closeout** — the engine path never
  approves, merges, cleans up, archives, or closes out work;
- **no branch deletion / worktree deletion** — the engine path never deletes a
  branch or a worktree;
- **no daemon / webhook / background worker / scheduler loop / multi-task
  behavior** — it is one tick, one selected task, one engine invocation, and
  then it stops.

The legacy tick `ok` / `status` / publication / safety decision is never changed
by the engine path. The `execution_engine` block is additive evidence only.

## Evidence only, not approval authority

The `ExecutionEngineResult` recorded by this path is **runtime evidence only and
is not approval authority**. The P5-c **shadow compare** result is **diagnostic
only**. No engine result and no compare output can become approval evidence or a
substitute for human review: **deterministic validators and human review gates
remain the validation and approval authority**, exactly as the P5-a boundary
requires.

## Failure behavior

If the engine raises, or returns anything other than an `ExecutionEngineResult`,
the opt-in path records a **structured failure** in the `execution_engine` block
(`status: engine_error`) and the tick **does not fall through to any publish,
PR, merge, or cleanup behavior**. The shadow compare is produced *before* engine
execution, so the diagnostic comparison is present even when execution fails. The
non-overlap lock is acquired and released exactly as on the legacy path; the
engine evidence is attached after the lock is already released.

## Rollback

The rollback path is to **remove the opt-in flag**. Because the engine path only
runs when `--use-execution-engine` is explicitly provided, dropping the flag
returns the tick to the unchanged legacy path with no other change required. The
legacy scheduler tick path is never modified by opting out.

## Next stage

The **P5-e** stage hardens legacy-vs-engine **fallback** behavior: every
`execution_engine` evidence block carries a pure, machine-readable
`fallback_assessment` that pins `effective_authority="legacy_scheduler"`,
`engine_authority=False`, and `engine_result_accepted_as_authority=False`. See
`docs/scheduler-execution-engine-fallback-hardening.md`.
