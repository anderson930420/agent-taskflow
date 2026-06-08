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

## Future phases

- **P4-d (this phase):** one explicit, opt-in manual runtime path behind the
  engine facade, behavior-preserving.
- **P4-e or later:** may migrate a real scheduled path onto the facade, but only
  after this manual path is proven.
