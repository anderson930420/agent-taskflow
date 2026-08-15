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

## Task identity is settled at admission

A Task is classified once, when it is created. `github_issue_ingestion`,
`github_issue_intake`, and `github_issue_intake_gate` persist the task row and
its canonical non-legacy `TaskIdentityRecord` in the same SQLite transaction
(`TaskMirrorStore.upsert_task_with_level2_identity`), so no observer and no
crash can see a newly created task classified legacy. Classification is
therefore already settled when the runtime handoff decides whether Level 2
enforcement applies, and a task's *first* run is protected by the same guards
as every later run.

Re-ingesting an existing task never changes its identity: historical tasks
recorded as `is_legacy=1` keep their historical execution path.
`ensure_level2_task_identity` remains an idempotent assertion for runtime
callers and accepts a caller-supplied connection so admission can promote
inside its own transaction.

## The canonical Attempt is reserved before the executor starts

The engine does not infer its Attempt after the fact. The adapter builds the
installed canonical runtime store (`canonical_runtime_task_store`) and hands it
to the runner, so the runtime-admission claim taken on the
`queued -> preparing` transition *is* the reservation: the Attempt id, and the
executor-start claim credentials that go with it, exist before any executor
runs. `CanonicalRuntimeTaskStore.reserved_runtime_claim` keeps that identity
readable after the lease is released.

Identification is not authorization. Every terminal status — including a run
blocked at the advisory-evidence gate — reports `canonical_attempt_id` in the
engine metadata and in the persisted runtime evidence, so the evidence always
says which Attempt the run belonged to. `canonical_attempt_bound=true` is still
reserved for a reserved Attempt that is closed, non-legacy, owned by this Task,
created by this invocation, with `execution_result=completed` and
`validation_result=passed`. A reserved Attempt that fails any of those
conditions is rejected, and the independent post-execution store verification
in `SchedulerExecutionEngineAuthority` is unchanged.

Direct `run_approved_task`, queued handoff, dispatcher CLI/API, runtime
handoff, and one-shot entry points consult the same `tasks.is_legacy`-based
authority policy. Explicit non-legacy tasks either cross the canonical bound
ExecutionEngine callback or fail closed before an injected runner or executor
can start. That non-legacy identity is persisted at task admission, and the
confirmed scheduler re-asserts it before engine invocation. The one-task
automation path builds the same bound `SchedulerExecutionEngineAuthority`
callback for a Level 2 task; when classification itself fails it refuses to
execute rather than falling back to the caller's runner. The old post-legacy
shadow executor and a manual facade
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
