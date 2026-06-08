# ExecutionEngine Approved Task Adapter (P4-c)

P4-c is **adapter-only** and includes **no runtime migration**. It adds
`ApprovedTaskRunnerExecutionEngineAdapter`, which implements the P4-b
`ExecutionEngine` protocol by delegating to the existing
`approved_task_runner.run_approved_task`. It adds no execution capability of its
own and changes no existing behavior.

The existing scheduler and automation runtime do not use the adapter yet. No
scheduler tick, one-task automation, dispatcher, executor, validator, cron,
store, API, or Mission Control path imports, instantiates, or calls the adapter.
Only tests and docs reference it. The first phase that may place one runtime path
behind the engine facade is **P4-d**, and that step is intended to remain
behavior-preserving.

## What the adapter does

`ApprovedTaskRunnerExecutionEngineAdapter.execute(request)` accepts an
`ExecutionEngineRequest`, constructs an `ApprovedTaskRunRequest` from it, calls
`run_approved_task(...)`, and converts the returned result into an
`ExecutionEngineResult`.

The adapter does not change the approved runner. It performs no extra filesystem,
database, or GitHub action beyond what `run_approved_task` already does when it
is called. The adapter never approves, merges, cleans up, archives, closes out,
publishes a PR, deletes a branch or worktree, starts a daemon, webhook, or
background worker, runs a scheduler loop, or batches multiple tasks.

## Request mapping

`ExecutionEngineRequest` maps to `ApprovedTaskRunRequest` as follows:

| `ExecutionEngineRequest` field            | `ApprovedTaskRunRequest` field |
| ----------------------------------------- | ------------------------------ |
| `task_key`                                | `task_key`                     |
| `dry_run`                                 | `dry_run`                      |
| `preflight`                               | `preflight`                    |
| `executor_profile.executor`               | `executor`                     |
| `executor_profile.model`                  | `model`                        |
| `executor_profile.provider`               | `provider`                     |
| `executor_profile.tools`                  | `tools`                        |
| `executor_profile.pi_bin`                 | `pi_bin`                       |
| `validator_profile.validators`            | `validators`                   |
| `workspace.repo_path`                     | `repo_path`                    |
| `workspace.artifact_dir`                  | `artifact_root`                |
| `workspace.worktree_root`                 | `worktree_root`                |

Fields that `ApprovedTaskRunRequest` defines but `ExecutionEngineRequest` does
not expose (for example `confirm_approved_task`, `db_path`, `base_branch`, and
`command`) keep their existing `ApprovedTaskRunRequest` defaults. The adapter
does not invent values for them.

## Result mapping

The result from `run_approved_task` (an `ApprovedTaskRunResult`, or any
dict-like or attribute-bearing result) maps to `ExecutionEngineResult`:

- `ok`: from the runner result `ok`, defaulting to `False`.
- `task_key`: the original `ExecutionEngineRequest.task_key`.
- `status`: from the runner result `status` (or `task_status`), defaulting to
  `blocked`.
- `summary`: the runner `summary` when it is a string, otherwise a short
  generated summary describing the returned status.
- `next_operator_action`: the first item of the runner result
  `next_allowed_actions` when present, otherwise `None`.
- `steps`: deterministic `ExecutionEngineStepResult` entries derived only from
  the runner sections that are present — `preflight`, `workspace`, `executor`,
  `validators`, and a final `status_transition`. Absent sections are omitted
  rather than reported as successful.
- `artifacts`: `ExecutionEngineArtifactRef` entries built from the runner
  artifacts. The adapter accepts a mapping of `artifact_type -> path`, a list of
  dicts, or records exposing `artifact_type`/`path` attributes. Missing or
  invalid paths are skipped instead of failing the whole conversion.
- `metadata`: selected runner result fields copied in JSON-safe form. The
  adapter never mutates the original runner result.

Small internal helpers read either mapping keys or attributes so the adapter
works whether the runner returns a dataclass or a dict-like result.

## Safety mapping

The adapter builds an `ExecutionEngineSafety` with conservative interpretation:

- Conservative defaults are kept: `human_review_required=True`, `approved=False`,
  `merged=False`, `github_mutated=False`, `issue_closed=False`,
  `cleanup_performed=False`, `branch_deleted=False`, `worktree_deleted=False`,
  `daemon_started=False`, `webhook_started=False`,
  `background_worker_started=False`, `scheduler_loop_started=False`,
  `multi_task_batch_started=False`, `one_task_only=True`, `execution_only=True`.
- If the runner result reports that the executor started,
  `executor_started=True`.
- If the runner result reports that validators started,
  `validator_started=True`.
- Other governance evidence is only set when it is explicitly present in the
  runner safety payload. The adapter never infers approval, merge, or cleanup.

## Error handling

If `run_approved_task` raises, the adapter does not swallow the error silently.
It returns an `ExecutionEngineResult` with `ok=False`, `status=blocked`, a
summary describing the adapter failure, conservative safety defaults, a single
step named `approved_task_runner` with status `failed`, and metadata containing
the error type and message.

## Why destructive operations remain outside ExecutionEngine

Merge, approval, cleanup, archive, closeout, PR publication, branch deletion,
worktree deletion, issue close, and GitHub mutation occur after or around
bounded execution and remain explicit operator or orchestrator responsibilities.
Keeping them outside the ExecutionEngine boundary preserves the P4-a ownership
and safety boundaries: AI coding tools are bounded implementation workers, and
human review remains the final gate. The adapter therefore exposes only
execution and proof-of-work, not governance authority.

## Future phases

- **P4-c (this phase):** adapter only, behavior-free over `run_approved_task`.
- **P4-d:** the first phase that may place one runtime path behind the engine
  facade, still behavior-preserving.
