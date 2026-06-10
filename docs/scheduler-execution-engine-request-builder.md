# Scheduler ExecutionEngine Request Builder (P5-b)

P5-b is the **request-builder contract only** stage of the staged
scheduler-to-ExecutionEngine migration plan defined by the P5-a boundary
document (`docs/scheduler-execution-engine-migration-boundary.md`). It adds a
**pure and behavior-free** builder that maps one scheduler-selected confirmed
task onto an `ExecutionEngineRequest` value — and nothing else.

> **This phase adds no runtime behavior.** The builder is value mapping only:
> there is **no scheduler runtime wiring**, **no engine execution**, and
> **no active cron change**. The live scheduler tick path described in the
> P5-a boundary document stays exactly as it is.

See also:

- `docs/scheduler-execution-engine-migration-boundary.md` — the P5-a
  migration boundary this stage implements one contract piece of.
- `docs/execution-engine-contract.md` — the P4-b `ExecutionEngineRequest`
  contract the builder targets.
- `docs/execution-engine-approved-task-adapter.md` — the adapter a future
  engine-backed path would delegate to.
- `docs/execution-engine-manual-runtime-path.md` — the manual opt-in engine
  facade, still the only runtime entry into the engine path.
- `docs/scheduler-execution-engine-shadow-compare.md` — the P5-c shadow /
  compare layer that consumes the request this builder produces.

## Purpose

P5-b ships exactly one piece of the migration boundary: a deterministic,
side-effect-free mapping from scheduler-selected confirmed one-task work to
the existing ExecutionEngine request contract. It lets later stages reason
about "what request would the scheduler have built?" without executing
anything.

## API

Module: `agent_taskflow/scheduler_execution_engine_request_builder.py`

- `SCHEDULER_EXECUTION_ENGINE_REQUEST_BUILDER_SCHEMA_VERSION`
  (`scheduler_execution_engine_request_builder.v1`) and
  `SCHEDULER_EXECUTION_ENGINE_REQUEST_BUILDER_SOURCE`
  (`scheduler_execution_engine_request_builder`) identify the builder in
  request metadata.
- `SchedulerExecutionEngineRequestBuildInput` is a frozen input dataclass
  describing one scheduler-selected confirmed task: task key, `owner/name`
  repo, absolute local repo path, absolute artifact dir, executor profile
  fields (executor / model / provider / tools / pi_bin), validators,
  optional worktree paths, dry-run / confirmed / preflight flags, operator
  fields, selected issue / candidate markers, optional evidence paths, and
  caller metadata.
- `build_scheduler_execution_engine_request(input)` is a pure function that
  returns an `ExecutionEngineRequest` with
  `source=REQUEST_SOURCE_SCHEDULED_TICK`, the mapped executor / validator /
  workspace profiles, and a stable JSON-compatible metadata mapping.
- `scheduler_execution_engine_request_to_json_dict(request)` serializes the
  built request through the contract's `to_json_dict` codec and asserts the
  result is a dict.

## Validation rules

Input construction validates shape only — paths are not required to exist
and the filesystem is never touched:

- `task_key` and `executor` must be non-empty.
- `repo` must be in `owner/name` form.
- `local_repo_path` and `artifact_dir` must be absolute paths.
- `tools` and `validators` are normalized to tuples.
- `operator`, `operator_note`, and `selected_candidate_key` are stripped to
  `None` when blank.
- `publish_after_execution=True` is rejected: the built request is always
  `publish_after_execution=False`.
- `execution_only=False` is rejected: the built request is always
  `mode=execution_only`.

## Request metadata

The built request's metadata always includes the builder schema version, the
builder source, the repo, the `confirmed` flag, and the safety markers
`publish_after_execution=False`, `mode=execution_only`,
`execution_only=True`, `one_task_only=True` (the builder maps **one selected
task only**), and `scheduler_tick=True`. When provided,
`selected_issue_number`, `selected_candidate_key`, `operator`, and
`operator_note` are included. Caller metadata is copied defensively (a deep,
JSON-compatible copy under `caller_metadata`), so mutating the caller's
mapping after the build cannot mutate the built request.

## Safety boundary

The builder is **pure and behavior-free**. P5-b adds:

- **no scheduler runtime wiring** — the scheduler tick does not import,
  construct, or call this builder; the legacy scheduler path remains the
  default live path.
- **no engine execution** — nothing calls `ExecutionEngine.execute`, the
  engine facade, or the adapter; the built request is a value, not an action.
- **no active cron change** — the active crontab and the cron example
  command lines are untouched.
- **no approved_task_runner behavior change**
- **no executor behavior change**
- **no validator behavior change**
- **no DB reads or writes**
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

An `ExecutionEngineRequest` built here carries no authority: deterministic
validators and human review gates remain the validation and approval
authority, exactly as the P5-a boundary requires.

## Next stage

A future **P5-c** stage may use this builder for a **shadow / compare
summary** only: building the engine-shaped request alongside the legacy
scheduler path and comparing summaries, still without executing through the
engine. That stage is future work and is not implemented by P5-b.
