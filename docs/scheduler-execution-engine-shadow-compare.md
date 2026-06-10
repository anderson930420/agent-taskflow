# Scheduler ExecutionEngine Shadow / Compare (P5-c)

P5-c is the **shadow / compare summary only** stage of the staged
scheduler-to-ExecutionEngine migration plan defined by the P5-a boundary
document (`docs/scheduler-execution-engine-migration-boundary.md`). It adds a
**pure, behavior-free** compare layer that takes a **legacy scheduler tick
payload** and an **engine-shaped request** produced by the P5-b request builder
(`docs/scheduler-execution-engine-request-builder.md`) and reports a diagnostic
comparison summary — and nothing else.

> **This phase adds no runtime behavior.** The compare layer **does not execute
> the engine**, adds **no scheduler runtime wiring**, and makes **no active cron
> change**. The live scheduler tick path described in the P5-a boundary document
> stays exactly as it is.

See also:

- `docs/scheduler-execution-engine-migration-boundary.md` — the P5-a migration
  boundary this stage implements one diagnostic piece of.
- `docs/scheduler-execution-engine-request-builder.md` — the P5-b request
  builder that produces the engine-shaped request this layer compares against.
- `docs/scheduler-execution-engine-opt-in-path.md` — the P5-d opt-in execution
  path that uses this compare layer before routing one confirmed task through
  the engine.
- `docs/execution-engine-contract.md` — the P4-b `ExecutionEngineRequest`
  contract the engine-shaped request conforms to.

## Purpose

P5-c lets a later stage answer "does the request the scheduler *would* have
built line up with the legacy scheduler tick payload?" without running anything.
It compares the **legacy scheduler tick payload** against the **engine-shaped
request** and produces a summary of matches, mismatches, and warnings. The
comparison is read-only and side-effect-free.

## API

Module: `agent_taskflow/scheduler_execution_engine_shadow_compare.py`

- `SCHEDULER_EXECUTION_ENGINE_SHADOW_COMPARE_SCHEMA_VERSION`
  (`scheduler_execution_engine_shadow_compare.v1`) and
  `SCHEDULER_EXECUTION_ENGINE_SHADOW_COMPARE_SOURCE`
  (`scheduler_execution_engine_shadow_compare`) identify the compare layer.
- `SchedulerExecutionEngineShadowCompareInput` is a frozen input dataclass: a
  legacy scheduler tick payload (`legacy_scheduler_tick`), an engine-shaped
  `engine_request`, and caller `metadata`. Construction copies the dict-like
  inputs defensively and requires `engine_request.source` to be
  `REQUEST_SOURCE_SCHEDULED_TICK`.
- `SchedulerExecutionEngineShadowCompareResult` is a frozen, JSON-compatible
  result dataclass carrying `ok`, `matched`, `mismatches`, `warnings`, the
  legacy / engine task identity, and a diagnostic `summary`.
- `compare_scheduler_tick_to_engine_request(input)` is a pure function that
  returns the comparison result.
- `scheduler_execution_engine_shadow_compare_to_json_dict(result)` serializes
  the result to a JSON-compatible dict via the contract codec.

## What is compared

- **Task identity** — the legacy `selected_task_key` (falling back to the
  nested `automation.selected_task_key`) against the engine request `task_key`.
- **Scheduler / source identity** — the engine request source must be
  `REQUEST_SOURCE_SCHEDULED_TICK`. The legacy source is recorded but not
  required to match, because the legacy source marker is
  `github_issue_one_task_scheduler_tick`.
- **Repo / project** — the legacy `repo` against the engine request `project`.
- **Execution-only publication** — the engine request metadata must preserve
  `publish_after_execution=False`, `mode=execution_only`, and
  `execution_only=True`. If legacy publication markers are present, they must
  also be `publish_after_execution=False` and `mode=execution_only`.
- **Safety** — the engine request metadata must mark `one_task_only` and
  `scheduler_tick` true. Legacy safety markers must not indicate a scheduler
  loop, background worker, or multi-task batch, and must not indicate GitHub
  mutation, approval, or merge.
- **Executor / validator / workspace observability** — the engine executor,
  model, validators, and workspace paths are recorded in the summary, and
  legacy runner config is compared where it is safely available.

## Publication checks

The compare layer verifies the request stays execution-only:

- `publish_after_execution=False`
- `mode=execution_only`
- `execution_only=True`

## Safety checks

The compare layer verifies the request stays a single, non-crossing tick:

- `one_task_only`
- `scheduler_tick`
- no scheduler loop / background worker / multi-task batch
- no GitHub mutation / approval / merge

## Missing legacy fields become warnings

The compare layer does not overfit to one exact legacy payload shape. Missing
legacy fields become warnings: when legacy publication markers, safety markers,
or runner config are **absent**, the compare layer records a **warning** rather
than a hard mismatch, so a sparse or older legacy payload is not treated as a
contradiction.

## Safety boundary

The compare layer is **pure and behavior-free**. P5-c adds:

- **no engine execution** — nothing calls `ExecutionEngine.execute`, the engine
  facade, or the adapter; the engine-shaped request is a value, not an action.
- **no scheduler runtime wiring** — the scheduler tick does not import,
  construct, or call this compare layer; the legacy scheduler path remains the
  default live path.
- **no active cron change** — the active crontab and the cron example command
  lines are untouched.
- **no approved_task_runner call**
- **no executor behavior**
- **no validator behavior**
- **no DB behavior** — no DB reads or writes.
- **no GitHub mutation** — no GitHub reads or writes.
- **no directory or artifact creation**
- **no subprocess execution**

The compare layer never touches the filesystem: path inputs are read as values
only.

## Mismatches are diagnostic only

A mismatch reported by this layer is **diagnostic only and is not approval
authority**. An `ExecutionEngineRequest`, and any comparison of it, carries no
authority: **deterministic validators and human review gates remain the
validation and approval authority**, exactly as the P5-a boundary requires. No
compare output can become approval evidence or a substitute for human review.

## Next stage

A future **P5-d** stage may use this compare layer before enabling an **opt-in
execution path** (off by default, explicit flag): comparing the legacy tick to
the engine-shaped request first, then routing one confirmed task through the
engine facade only when explicitly opted in. That stage is future work and is
not implemented by P5-c.
