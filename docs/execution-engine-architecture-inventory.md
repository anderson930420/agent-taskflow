# ExecutionEngine Architecture Inventory (P4-a)

This document is **documentation and tests only**. It is a read-only
architecture inventory of the current real scheduled execution path and a
boundary definition for a future `ExecutionEngine` abstraction.

This phase is **documentation-only and behavior-preserving**. It does **not**
add `ExecutionEngine` runtime code, and it does **not** refactor, modify, or
re-route the scheduler, automation, approved task runner, executor adapters,
validators, archive, closeout, retention, cron, DB, or Mission Control. Nothing
in this phase changes existing behavior.

The agent-taskflow principle still holds:

> Manage work, not agents.

AI coding tools are bounded implementation workers. They are not the
orchestrator, validator, reviewer, merger, or cleanup authority. A future
`ExecutionEngine` is an internal orchestration boundary, **not** a new
automation capability and **not** a relaxation of any existing human gate.

---

## A. Current execution flow

The real scheduled execution path, as it exists today, runs from a scheduled
cron tick down to a `waiting_approval` (or `blocked`) status awaiting human
review. The default scheduled tick is **execution-only**: it stops at
`waiting_approval` and never publishes.

```text
cron / systemd / manual operator
  -> scripts/run_github_issue_one_task_scheduler_tick.py        (CLI front end)
  -> agent_taskflow/github_issue_one_task_scheduler_tick.py     (non-overlap lock, one tick)
  -> agent_taskflow/github_issue_one_task_automation.py         (one issue -> one task)
       -> GitHub Issue discovery + ingestion-failure filter
       -> select first recommended issue (one issue only)
       -> ingest one GitHub Issue (mirrored TaskRecord + issue_spec artifact)
       -> one-shot task pipeline (gated runtime chain):
            -> proposal creation               (scheduler_proposal artifact/event)
            -> scheduler confirmation creation (scheduler_confirmation artifact/event)
            -> scheduler confirmation verifier report (dry-run, read-only verifier)
            -> intake runner handoff           (intake_runner_handoff artifact/event)
            -> runtime preflight + runtime handoff execution (audit evidence)
            -> agent_taskflow/approved_task_runner.py
                 -> executor adapter layer     (agent_taskflow/executors/*)
                 -> validator layer            (agent_taskflow/validators/*)
                 -> task status transitions
                 -> artifact recording
  -> final waiting_approval / blocked status -> human review gate
```

### Stage detail

1. **CLI front end — `scripts/run_github_issue_one_task_scheduler_tick.py`.**
   Parses operator/cron arguments, builds a
   `GitHubIssueOneTaskSchedulerTickRequest`, and prints a JSON result. Dry-run
   is the default. `--confirmed` applies the controlled lower-level confirmation
   preset, processes at most one issue/task, and stops. `--executor` wires the
   approved task runner configuration into runtime execution.
   `--publish-after-execution` is an explicit opt-in; omitting it keeps the tick
   execution-only.

2. **Scheduler tick — `agent_taskflow/github_issue_one_task_scheduler_tick.py`.**
   Acquires the shared non-overlap lock so cron, systemd timers, and manual
   invocations cannot overlap. It is **one tick only**: no daemon, scheduler
   loop, background worker, or multi-task batch is started. In confirmed mode
   with `--executor`, it builds the configured approved-task-runner wrapper and
   hands it to the automation, then releases the lock. It records a `safety`
   block on every response.

3. **One-task automation — `agent_taskflow/github_issue_one_task_automation.py`.**
   A thin outer loop over existing primitives: discover GitHub Issues, apply the
   ingestion-failure filter, select the **first** eligible recommended issue,
   ingest exactly that **one** issue, then run the one-shot task pipeline
   (execution-only) or the scheduler watcher (publication path) for that **one**
   task. It is not a daemon, scheduler loop, webhook, cron job, background
   worker, or multi-task queue.

4. **Proposal creation, scheduler confirmation creation, scheduler confirmation
   verifier report, intake runner handoff, runtime handoff execution.**
   The one-shot task pipeline (`agent_taskflow/one_shot_task_pipeline.py`) walks
   one `task_key` through the existing gated chain without bypassing any dry-run
   default, confirm flag, hash/binding check, or duplicate detection:
   - **proposal creation** — `scheduler_proposals.py` records the
     `scheduler_proposal` artifact/event. A proposal is not action evidence.
   - **scheduler confirmation creation** — `scheduler_confirmations.py` records
     the `scheduler_confirmation` artifact/event. A confirmation is not runtime
     consumption.
   - **scheduler confirmation verifier report** —
     `scheduler_confirmation_verifier.py` /
     `scheduler_confirmation_verifier_report.py` produce a **dry-run-only,
     read-only** verifier report. The verifier never writes execution evidence
     and never authorizes a runtime to start.
   - **intake runner handoff** —
     `intake_runner_handoff_from_verifier_report.py` records the
     `intake_runner_handoff` artifact/event. The handoff is handoff-only: it is
     not execution permission.
   - **runtime preflight + runtime handoff execution** —
     `runtime_handoff_execution_from_handoff.py` re-validates the handoff at
     execution time, invokes the approved task runner, and records
     `runtime_handoff_execution` audit evidence. The audit record is evidence
     only; it is not approval, merge, or cleanup.

5. **Approved task runner — `agent_taskflow/approved_task_runner.py`.**
   The single point that actually runs one queued task. In order it:
   validates executor/validator selection; requires `--confirm-approved-task`
   for non-dry-run; loads the task and requires `queued` status; validates the
   repo and base ref; resolves the **effective executor profile** (request
   overrides over recorded `TaskRecord` profile); runs preflight; prepares an
   isolated worktree workspace; writes the mission contract and records it;
   resolves the executor and builds the executor context; dispatches the
   executor; records executor evidence; runs the validators; records validation
   evidence; and updates task status. It stops at `waiting_approval` on success
   or `blocked` on any failure. It never approves, pushes, creates PRs, merges,
   or cleans up.

6. **Executor adapter layer — `agent_taskflow/executors/*`.**
   Deterministic CLI wrappers and result normalizers (`manual`, `shell`,
   `opencode`, `pi`). They construct commands, select the workspace, capture
   logs, route artifacts, and return a standardized `ExecutorResult`. The
   external AI coding tool invoked by an adapter is the bounded worker.

7. **Validator layer — `agent_taskflow/validators/*`.**
   Deterministic proof-of-work gates (`pytest`, `openspec`, `policy`,
   `changed-files`, `typecheck`, `lint`). Each returns a `ValidatorResult` that
   the runner records. A failed or blocked validator blocks the task.

8. **Task status transitions.** The approved task runner drives:

   ```text
   queued -> preparing -> implementing -> validating -> waiting_approval
   ```

   Any failure routes to `blocked`. No status beyond `waiting_approval` is set
   by automation; `approved` / `rejected` remain human decisions.

9. **Artifact recording.** Mission contract (`manifest`), implementation prompt,
   executor log (`worker_log`), validator logs (`review_log`), and other
   executor/validator artifacts are recorded in the store as a proof-of-work
   index.

10. **Final `waiting_approval` / `blocked` status.** The terminal automated
    state. Human review is the final gate for approval, publication, and
    cleanup.

---

## B. Responsibility map

For each layer below: **input**, **output**, **artifact ownership**, **status
ownership**, **safety ownership**, and **what it must not do**.

### Scheduler tick (`github_issue_one_task_scheduler_tick.py`)

- **Input:** scheduler tick request (repo, db, local repo, artifact root,
  confirmed flag, executor config, publication flag, lock path).
- **Output:** one tick JSON result with `lock`, `runner_config`,
  `publication_config`, `automation`, and `safety` blocks.
- **Artifact ownership:** none of its own; surfaces nested automation evidence.
- **Status ownership:** none; does not transition task status.
- **Safety ownership:** non-overlap lock, one-tick-only enforcement, dry-run vs
  confirmed mode, execution-only vs publication mode.
- **Must not:** start a daemon/loop/background worker/multi-task batch, approve,
  merge, push, clean up, delete branches or worktrees, or run multiple ticks.

### One-task automation (`github_issue_one_task_automation.py`)

- **Input:** automation request (repo, paths, confirmation flags, label
  filters, executor profile metadata, publication flag).
- **Output:** automation JSON result with discovery, selected issue, ingestion,
  execution/watcher, `selected_task_key`, and `safety`.
- **Artifact ownership:** delegates; owns no execution artifacts directly.
- **Status ownership:** none directly; downstream helpers transition status.
- **Safety ownership:** one issue only, one task only, confirmation-flag gate,
  ingestion-failure quarantine, execution-only default.
- **Must not:** select more than one issue/task, approve, merge, push (unless
  publication explicitly opted in), clean up, or run as a background worker.

### Gated runtime chain (proposal -> confirmation -> verifier report -> intake runner handoff -> runtime handoff execution)

- **Input:** one `task_key` plus the prior stage's recorded artifact/binding.
- **Output:** stage artifacts/events (`scheduler_proposal`,
  `scheduler_confirmation`, verifier report, `intake_runner_handoff`,
  `runtime_handoff_execution`).
- **Artifact ownership:** each stage owns its own audit artifact/event.
- **Status ownership:** none; these stages are evidence/binding, not lifecycle.
- **Safety ownership:** dry-run defaults, confirm flags, hash/binding checks,
  duplicate detection, time-of-check/time-of-use re-validation at runtime
  preflight.
- **Must not:** treat a proposal/confirmation/verifier report/handoff as
  execution permission; the verifier stays dry-run-only and read-only.

### Approved task runner (`approved_task_runner.py`)

- **Input:** `ApprovedTaskRunRequest` (task key, executor, repo path, db,
  artifact root, worktree root, base branch, validators, confirm flag,
  preflight flag, command, executor profile overrides).
- **Output:** `ApprovedTaskRunResult` (ok, status, phase, preflight, workspace,
  executor_run, validators, artifacts, summary, safety).
- **Artifact ownership:** records mission contract, implementation prompt,
  executor logs, validator logs, and other run artifacts.
- **Status ownership:** owns `queued -> preparing -> implementing ->
  validating -> waiting_approval` and `blocked` on failure.
- **Safety ownership:** confirm-gate, queued precondition, repo/base validation,
  preflight, isolated workspace, deterministic executor/validator dispatch.
- **Must not:** approve, push, create PRs, merge, clean up, delete branches or
  worktrees, auto-select tasks, or move status past `waiting_approval`.

### Executor adapter layer (`agent_taskflow/executors/*`)

- **Input:** `ExecutorContext` (task key, project, worktree path, artifact dir,
  prompt path, model).
- **Output:** `ExecutorResult` (executor, status, exit code, summary, log path,
  artifacts).
- **Artifact ownership:** produces executor log and executor artifacts; the
  runner records them.
- **Status ownership:** none; reports a run status the runner interprets.
- **Safety ownership:** command construction, workspace confinement, env setup,
  log capture, artifact routing.
- **Must not:** select tasks, transition lifecycle status, approve, push, merge,
  clean up, or act as the governance layer.

### Validator layer (`agent_taskflow/validators/*`)

- **Input:** `ValidatorContext` (task key, project, worktree path, artifact
  dir).
- **Output:** `ValidatorResult` (validator, status, exit code, summary, log
  path, artifacts).
- **Artifact ownership:** produces validation logs/artifacts; the runner records
  them.
- **Status ownership:** none; a failed/blocked result blocks the task via the
  runner.
- **Safety ownership:** deterministic proof-of-work checking; AI worker claims
  alone are insufficient.
- **Must not:** be replaced by AI review, be skipped, approve, or mutate
  lifecycle status directly.

### Store / artifact + approval metadata

- **Input:** task records, status updates, artifact/event records.
- **Output:** persisted orchestrator state and a proof-of-work index.
- **Artifact ownership:** the canonical artifact/event index.
- **Status ownership:** persists status; does not decide transitions.
- **Safety ownership:** durable evidence for human review.
- **Must not:** auto-approve, infer approval, or imply human approval.

---

## C. Current safety boundaries (must be preserved)

The current scheduled execution path is constrained by the following
invariants. P4 must preserve every one of them:

- **no auto-approval** — automation never sets `approved`.
- **no auto-merge** — automation never merges.
- **no auto-cleanup** — automation never runs cleanup/delete.
- **no automatic GitHub issue close** — issues are never closed by automation.
- **no branch deletion** — automation never deletes branches.
- **no worktree deletion** — automation never deletes worktrees.
- **no daemon** — there is no long-running process.
- **no webhook** — there is no inbound event listener.
- **no multi-task batch** — at most one task is processed.
- **one issue / one task / one tick** — exactly one issue selected, one task
  executed, one tick per invocation, under a non-overlap lock.
- **human confirmation remains required** for archive, closeout, cleanup, PR
  publication, merge, and destructive actions.
- **scheduled execution remains execution-only** unless explicitly opted into
  publication via `--publish-after-execution`.

The scheduled tick's `safety` block makes these explicit on every run
(`approved=false`, `merged=false`, `cleanup_performed=false`,
`branch_deleted=false`, `worktree_deleted=false`,
`scheduler_loop_started=false`, `background_worker_started=false`,
`multi_task_batch_started=false`, `human_review_required=true`).

---

## D. Proposed ExecutionEngine responsibility

A future `ExecutionEngine` is the internal boundary that owns **execution of one
already-approved task**, factoring the responsibilities that
`approved_task_runner.py` performs today behind a stable contract. It is
responsible for, and only for:

- consuming an approved task / runtime handoff / execution request;
- resolving the effective executor profile (request overrides over recorded
  `TaskRecord` profile);
- enforcing preflight inputs;
- preparing or resolving workspace context;
- dispatching the executor;
- capturing executor artifacts;
- dispatching deterministic validators;
- recording the execution / validation result;
- producing a proof-of-work summary;
- returning the next operator action.

The `ExecutionEngine` is an execution boundary, not a new capability. Its
existence must not change what the system is allowed to do.

---

## E. What ExecutionEngine must NOT own

The `ExecutionEngine` must explicitly **not** own any of the following. These
remain with the surrounding orchestration layers, scheduler chain, explicit
operator commands, and human review:

- GitHub issue discovery;
- GitHub issue ingestion;
- scheduler candidate selection;
- proposal creation;
- human confirmation creation;
- confirmation verifier authority;
- PR creation / publication policy;
- merge;
- cleanup;
- archive disposition;
- task closeout disposition;
- cron scheduling;
- Mission Control UI mutation.

---

## F. Refactor roadmap (future P4 subphases)

- **P4-a: architecture inventory and boundary doc.** This document and its
  tests. Documentation-only and behavior-preserving. No runtime code.
- **P4-b: ExecutionEngine contract dataclasses / protocol only, no runtime
  migration.** Define request/result dataclasses and a protocol that mirror the
  current `approved_task_runner` contract. Nothing calls them yet.
- **P4-c: adapter that delegates to existing approved_task_runner, no behavior
  change.** A thin `ExecutionEngine` implementation that delegates straight to
  `run_approved_task`, producing identical results and artifacts.
- **P4-d: migrate one execution path behind engine facade while keeping tests
  identical.** Route a single existing call site through the facade; all
  existing tests must continue to pass unchanged.
- **P4-e: unified execution summary / observability record.** A consolidated,
  additive execution summary record for observability, without changing
  existing JSON shapes unless explicitly versioned.
- **P4-f: optional executor-profile normalization cleanup.** Optional internal
  cleanup of executor-profile resolution, behavior-preserving.

---

## G. Migration constraints

Every future P4 migration step must preserve all of the following:

- **existing tests** — the current test suite must keep passing;
- **current CLI behavior** — flags, defaults, and exit codes are unchanged;
- **current JSON shapes** unless explicitly versioned;
- **current safety flags** — every `safety` block field and its meaning;
- **current artifact semantics** — artifact kinds, paths, and proof-of-work
  meaning;
- **dry-run semantics** — dry-run remains the default and writes nothing;
- **confirmed flag semantics** — `--confirm-*` / `confirmed` gates remain
  required for any non-dry-run action;
- **no new automation capability without a separate explicit phase** — the
  engine refactor never adds approval, merge, push, publication, cleanup, or
  background behavior.

If any migration step cannot preserve one of these, it is out of scope for the
engine refactor and requires its own explicitly approved phase.
