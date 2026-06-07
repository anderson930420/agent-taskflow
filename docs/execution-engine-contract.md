# ExecutionEngine Contract (P4-b)

P4-b is **contract-only** and includes **no runtime migration**. It adds typed
values and an `ExecutionEngine` protocol, but no scheduler, automation,
`approved_task_runner`, executor, validator, cron, store, API, or Mission
Control path imports, instantiates, or calls that protocol yet.

The contract prepares the stable boundary needed for the P4-c adapter and the
P4-d migration. It preserves the ownership and safety boundaries documented by
the P4-a architecture inventory; it does not add an execution capability.

## Contract values

- `ExecutionEngineExecutorProfile` describes the selected executor plus its
  optional model, provider, tool names, and Pi binary override.
- `ExecutionEngineValidatorProfile` lists the deterministic validators that
  must produce proof-of-work.
- `ExecutionEngineWorkspaceProfile` identifies the repository, artifact
  directory, and optional worktree locations. Construction validates path
  shape only and never reads or writes the filesystem.
- `ExecutionEngineArtifactRef` identifies an artifact by type and path, with an
  optional human-readable description.
- `ExecutionEngineStepResult` records the status, summary, artifacts, and
  metadata for one execution or validation step.
- `ExecutionEngineRequest` is the future input for exactly one execution. It
  carries task identity, request source, dry-run and preflight choices, the
  executor and validator profiles, workspace context, optional handoff
  evidence paths, and metadata.
- `ExecutionEngineResult` is the future output. It reports success, execution
  status, a proof-of-work summary, the next operator action, step and artifact
  evidence, metadata, and `ExecutionEngineSafety`.
- `ExecutionEngineSafety` makes governance-relevant observations explicit,
  including whether an executor or validator started and whether prohibited or
  out-of-boundary actions occurred.
- The `ExecutionEngine` protocol defines only
  `execute(request: ExecutionEngineRequest) -> ExecutionEngineResult`. It has
  no implementation behavior.

The module also defines stable request-source, execution-status, and
step-status strings. `to_json_dict()` recursively copies contract values into
JSON-compatible data: paths become strings, tuples become lists, mappings
become plain dictionaries, and nested dataclasses are serialized recursively.
Serialization does not mutate the source values.

## Future engine boundary

The future engine may consume one already-authorized execution request,
resolve an effective executor profile, enforce preflight inputs, prepare or
resolve workspace context, dispatch one bounded executor, capture artifacts,
run deterministic validators, and return a proof-of-work summary and next
operator action.

The engine remains execution-only. Task discovery, task selection, scheduler
ticks, proposal and confirmation authority, human review decisions, persistent
lifecycle policy, and Mission Control review remain outside this contract.

Merge, cleanup, archive, closeout, and PR publication are outside the contract.
They are intentionally absent from `ExecutionEngineRequest` because
they occur after or around bounded execution and remain explicit operator or
orchestrator responsibilities. The request also provides no approval, issue
close, branch deletion, worktree deletion, GitHub mutation, daemon, webhook,
background worker, cron modification, or multi-task controls.

## Safety interpretation

`ExecutionEngineSafety` is evidence, not permission. Conservative defaults
state that human review is required; approval, merge, GitHub mutation, issue
close, push, deletion, cleanup, cron modification, and long-running or
multi-task activity did not occur; and the contract remains one-task-only and
execution-only. `executor_started` and `validator_started` may describe what an
implementation actually attempted in a later phase, but they do not authorize
execution by themselves.

These fields preserve the P4-a boundaries: no auto-approval, auto-merge,
auto-cleanup, issue close, branch or worktree deletion, daemon, webhook,
background worker, scheduler loop, or multi-task batch is introduced here.

## Future phases

- **P4-c:** approved_task_runner adapter that implements the protocol by
  delegating to existing behavior.
- **P4-d:** migrate one path behind the facade without changing behavior.
- **P4-e:** unified execution summary / observability record.
- **P4-f:** optional executor profile normalization cleanup.

Until those phases are separately reviewed, this module remains a
behavior-free type contract with no runtime callers.
