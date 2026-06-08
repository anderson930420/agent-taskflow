# ExecutionEngine Manual Runtime Path (P4-d)

P4-d adds **one explicit manual runtime path** behind the ExecutionEngine
facade. It is the first phase where a runtime path actually flows through the
engine facade introduced by P4-a (architecture boundary), P4-b (contract), and
P4-c (`ApprovedTaskRunnerExecutionEngineAdapter`).

This is a **manual runtime path** and it is **opt-in**. Existing scheduler,
automation, and cron paths are **unchanged**. The scheduler tick, one-task
automation, dispatcher, and the live cron command do not call the engine facade
and continue to behave exactly as before. Nothing in P4-d migrates a scheduled
or automated path onto the facade.

## Components

- `agent_taskflow/execution_engine_manual_runtime.py` — helpers that build and
  run a manual request:
  - `build_manual_execution_engine_request(...)` constructs an
    `ExecutionEngineRequest` (source `manual`) from explicit inputs. It only
    builds contract dataclasses; it does not touch the filesystem, call git, call
    GitHub, or write the DB.
  - `run_manual_execution_engine_request(request)` instantiates
    `ApprovedTaskRunnerExecutionEngineAdapter` and returns the
    `ExecutionEngineResult` from `adapter.execute(request)`, with no extra side
    effect.
- `scripts/run_execution_engine_approved_task.py` — the opt-in CLI that uses the
  helpers above.

## Path

The manual runtime path is exactly:

1. CLI — `scripts/run_execution_engine_approved_task.py`
2. `build_manual_execution_engine_request` (in
   `agent_taskflow/execution_engine_manual_runtime.py`)
3. `ApprovedTaskRunnerExecutionEngineAdapter`
4. the existing `approved_task_runner.run_approved_task`
5. `ExecutionEngineResult`

The CLI serializes the `ExecutionEngineResult` with the existing contract JSON
helper (`to_json_dict`) when `--json`/`--pretty` is requested, and otherwise
prints a concise text summary.

## Safety

- **Dry-run is the default.** Without any execution flag the CLI previews the
  request through the adapter in dry-run mode.
- **Non-dry-run requires `--confirm-execution-engine-run`.** If a non-dry-run
  (`--no-dry-run`) is requested without `--confirm-execution-engine-run`, the CLI
  returns a blocked `ExecutionEngineResult` (`ok=false`, `status=blocked`) that
  explains the missing confirmation flag and keeps conservative safety evidence
  (`human_review_required=true` and no destructive action), and the adapter is
  not invoked.
- **Confirmation is narrow.** `--confirm-execution-engine-run` only allows
  invoking the existing approved task runner through the adapter. It does **not**
  mean approval, merge, cleanup, publication, closeout, archive, issue close,
  branch deletion, worktree deletion, or GitHub mutation.

The manual runtime path adds no approval, no merge, no cleanup, no archive, no
closeout, no issue close, no branch deletion, no worktree deletion, and no
GitHub mutation. It also adds no daemon, webhook, background worker, scheduler
loop, or multi-task batch behavior. Whatever `run_approved_task` already does
when called is the only behavior; the facade adds none. Human review remains the
final gate.

## P4-f: optional observability summary

P4-f adds an **opt-in** way for the manual CLI to additionally emit a
`UnifiedExecutionSummary` (the P4-e read-only observability shape) derived from
the `ExecutionEngineResult`. It is purely additive and **behavior-preserving by
default**.

- **Default output is unchanged.** Without either new flag the CLI emits exactly
  the same JSON (`to_json_dict(result)`) or text summary as P4-d.
- **`--include-observability-summary`** emits *both* the raw
  `ExecutionEngineResult` and the normalized summary. With JSON output
  (`--json` and/or `--pretty`) the payload becomes an object with two keys:
  - `execution_engine_result` — the unchanged contract serialization.
  - `observability_summary` — the normalized `UnifiedExecutionSummary`.
  With text output it prints the existing text summary plus a short read-only
  observability section (source, schema version, task key, status, ok).
- **`--observability-summary-only`** emits *only* the normalized
  `UnifiedExecutionSummary` JSON. It implies JSON output and works with
  `--pretty`. It is intended for future log / observability pipelines.

The summary is always produced with
`summarize_execution_engine_result(result, source="manual_engine_facade")`, so
its `source` is `manual_engine_facade` and its `schema_version` is
`execution_observability_summary.v1`.

### What P4-f does not change

The summary is **read-only observability**. The new flags do not change
execution semantics: confirmation defaults are unchanged, dry-run remains the
default, and a non-dry-run without `--confirm-execution-engine-run` still
returns a blocked result (now also summarizable) without invoking the adapter.

P4-f does **not** migrate the scheduler, automation, or cron paths onto the
facade; the scheduler tick, one-task automation, dispatcher, and cron command
are **unchanged**. It does not change `approved_task_runner`, executor, or
validator behavior, and it does not change the DB schema or Mission Control.

The new flags add **no approval, no merge, no cleanup, no archive, no closeout,
no PR publication, no issue close, no branch deletion, no worktree deletion, and
no GitHub mutation**. Human review remains the final gate.

## Future phases

- **P4-d:** one explicit, opt-in manual runtime path behind the engine facade,
  behavior-preserving.
- **P4-e:** read-only observability normalization (`UnifiedExecutionSummary`).
- **P4-f (this phase):** opt-in observability summary emission from the manual
  CLI, behavior-preserving.
- **Later:** may migrate a real scheduled path onto the facade, but only after
  this manual path is proven.
