# Canonical ExecutionEngine Authority (M1-C)

Confirmed scheduler execution has one Level 2 authority:

```text
candidate discovery (Level 1, read-only)
  -> proposal / confirmation / verifier
  -> runtime handoff
  -> ExecutionEngine
  -> canonical runtime admission and Attempt resources
  -> managed executor and validators
  -> canonical Attempt-bound result
  -> waiting_approval / optional explicit downstream handoff
```

`github_issue_one_task_scheduler_tick` installs
`SchedulerExecutionEngineAuthority` as the runtime-handoff callback for every
confirmed tick. The callback builds a scheduled-tick `ExecutionEngineRequest`,
invokes one engine, and returns the engine decision to the existing pipeline.
The default engine uses `ApprovedTaskRunnerExecutionEngineAdapter`; the approved
runner is an implementation adapter behind the facade, not a second scheduler
authority.

Level 2 requests carry `execution_authority=execution_engine`,
`legacy_fallback_allowed=false`, the lifecycle database path, confirmation
state, and runtime-handoff evidence. The adapter rejects missing bindings
before execution. A successful result is accepted only when that invocation
created exactly one closed, non-legacy canonical Attempt with completed
execution and passed validation. The Attempt ID is propagated through runtime
evidence and downstream handoff summaries; engine-backed publication handoff
fails closed if that binding is absent.

Engine exceptions, invalid return types, task-key mismatches, and missing
Attempt bindings produce a deterministic blocked result. They do not invoke a
legacy scheduler fallback. Shadow comparison remains diagnostic and cannot
override the engine decision.

Compatibility remains deliberately narrower than authority:

- Level 1 candidate discovery remains read-only and does not execute.
- `tasks.is_legacy`, historical schema columns, and the legacy scheduler-log
  fallback reader remain available.
- Historical P5 opt-in/fallback helpers remain import-compatible for old
  evidence readers, but the scheduler no longer calls them as an authority
  decision.
- `--use-execution-engine` remains accepted for old command lines but is a
  compatibility no-op for confirmed work; ExecutionEngine authority does not
  depend on the flag.
- Human approval, merge, automatic publication, and cleanup remain outside
  ExecutionEngine.

Generate M1-C evidence without a real executor or production DB mutation:

```bash
python3 scripts/run_m1_canonical_execution_path_rehearsal.py \
  --output /absolute/evidence/canonical-execution-path.json
```

The rehearsal uses a disposable database and deterministic runner fixture. It
exercises successful canonical Attempt binding, engine-failure rejection with
no legacy callback, downstream bound/unbound checks, and legacy-reader
retention before atomically writing the audit artifact.
