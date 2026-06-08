# Unified Execution Summary / Observability Record (P4-e)

P4-e adds a **unified execution summary** — a single, JSON-safe observability
record — and the **read-only normalization** helpers that produce it from the
execution-result payloads the system already emits.

It is implemented in `agent_taskflow/execution_observability.py` with a small
read-only CLI in `scripts/summarize_execution_observability_payload.py`.

## Why a unified shape

Today the system produces several different execution-result shapes:

1. `ExecutionEngineResult` — the P4-b/P4-c/P4-d engine facade output.
2. `approved_task_runner` result payloads — the one-shot approved runner output.
3. Scheduler tick payloads — the real, cron-driven one-task scheduler tick JSON.

Each shape is correct for its own caller, but future observability surfaces
(Mission Control, CLI readbacks) should not have to special-case three payload
layouts. P4-e introduces **one** normalized shape so that reasoning over
execution records is uniform. Establishing this shared shape *before* any future
migration of the scheduler behind the engine facade means the observability
contract is stable independent of which runtime produced the record.

## What it is, and is not

- It is **read-only normalization**. The normalizers only inspect values handed
  to them and return new dataclasses. They never mutate the source payload, read
  or write files, touch the DB, call git or GitHub, or run executors/validators.
- It does **not** migrate the live scheduler or one-task automation onto the
  engine facade. **No live scheduler migration** happens in P4-e.
- It does **not** change the cron command. **No cron change** happens in P4-e.
- It introduces **no behavior change** to the scheduler tick, one-task
  automation, `approved_task_runner`, executors, validators, the DB schema, or
  Mission Control. It adds no merge, approval, cleanup, archive, closeout, PR
  publication, issue close, branch push/deletion, worktree deletion, daemon,
  webhook, background worker, scheduler loop, or multi-task behavior.

## Supported inputs

The module provides three summarizers, each returning a
`UnifiedExecutionSummary`:

- `summarize_execution_engine_result(result, source="manual_engine_facade")`
  normalizes an `ExecutionEngineResult`.
- `summarize_approved_task_runner_payload(payload, source="approved_task_runner")`
  normalizes an `approved_task_runner` payload. It accepts both mapping-like and
  attribute-like payloads.
- `summarize_scheduler_tick_payload(payload)` normalizes a scheduler tick
  payload; its `source` is always `scheduler_tick`.

## Fields

`UnifiedExecutionSummary` carries:

- `schema_version` — always `execution_observability_summary.v1`.
- `source` — one of `manual_engine_facade`, `approved_task_runner`,
  `scheduler_tick`, or `unknown`.
- `ok` — whether the underlying execution reported success.
- `task_key` — the task identifier when known.
- `status` / `raw_status` — the normalized and original status strings.
- `dry_run` — whether the underlying execution was a dry run, when known.
- `mode` — scheduler mode, when applicable.
- `publication_mode` — `execution_only` vs `publication`, when applicable.
- `next_operator_action` — the next human action, when the payload advertises one.
- `profile` — an `ExecutionObserverProfile` (executor, model, provider, tools,
  validators).
- `safety` — an `ExecutionObservedSafety` record of high-level governance flags.
- `steps` — observed execution steps (`ExecutionObservedStep`).
- `artifacts` — observed proof-of-work references (`ExecutionObservedArtifact`).
- `metadata` — a JSON-safe mapping that always includes `result_type`.

`to_observability_dict(value)` returns a recursively JSON-safe copy: `Path`
becomes `str`, dataclasses become dicts, tuples/lists become lists, mappings
become dicts, and primitives pass through. It never mutates its input.

## Safety defaults

`ExecutionObservedSafety` defaults are deliberately **conservative**: human
review is required and nothing destructive or expansive has happened.
Specifically, `human_review_required=True`, `one_task_only=True`, and
`execution_only=True`, while every governance-mutation flag (`approved`,
`merged`, `github_mutated`, `issue_closed`, `branch_pushed`, `branch_deleted`,
`worktree_deleted`, `cleanup_performed`, `cron_modified`, `daemon_started`,
`webhook_started`, `background_worker_started`, `scheduler_loop_started`,
`multi_task_batch_started`) defaults to `False`. A summarizer only overrides a
default when the source payload explicitly reports that flag, so a payload that
omits a flag stays at its conservative default.

## CLI

`scripts/summarize_execution_observability_payload.py` reads exactly one JSON
object from `--input <file>` or stdin, normalizes it with the summarizer chosen
by `--source` (`manual_engine_facade`, `approved_task_runner`, or
`scheduler_tick`), and prints the unified summary. JSON is the default output;
`--pretty` pretty-prints and `--text` prints a concise human-readable summary.
The CLI exposes no mutation flags.

## Future use

This shape is intended as the stable substrate for later work, none of which is
part of P4-e:

- **Mission Control** may read the unified summary later to render execution
  records uniformly across sources.
- The **scheduler tick** may emit this shape later, once normalization is proven.
- The **manual engine facade** may emit this shape later from its CLI.

Until then, P4-e is purely additive, read-only normalization with **no behavior
change** and **no runtime migration**.
