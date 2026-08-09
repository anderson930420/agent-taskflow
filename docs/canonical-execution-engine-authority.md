# Canonical ExecutionEngine Authority (M1-C)

Every executable Level 2 path has one authority:

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
execution and passed validation. The scheduler independently re-opens the
lifecycle store and proves that the returned Attempt belongs to the expected
Task, was created by that engine invocation, is closed, and is valid for
handoff. The exact Attempt ID is then persisted through runtime evidence,
one-shot/task pipelines, PR preparation, and PR handoff. Level 2 PR handoff
never reselects the latest Attempt.

Direct `run_approved_task`, queued handoff, dispatcher CLI/API, runtime
handoff, and one-shot entry points consult the same `tasks.is_legacy`-based
authority policy. Explicit non-legacy tasks either cross the canonical bound
ExecutionEngine callback or fail closed before an injected runner or executor
can start. The confirmed scheduler persists that non-legacy identity before
engine invocation. The old post-legacy shadow executor and a manual facade
request without the Level 2 contract also fail closed for Level 2 tasks.
Legacy and historical records retain their supported behavior.

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

The v2 rehearsal uses a disposable database and deterministic runner fixture.
It exercises the direct script, queued handoff, dispatcher, injected callback,
fake/mismatched/nonterminal Attempt, exact A-over-newer-B downstream and PR
handoff, engine-failure/no-fallback, and legacy-compatibility cases before
atomically writing SHA-bound audit evidence.
