# Semi-Automatic Scheduler Readiness Checkpoint (Phase F)

## 1. Purpose

This document is the Phase F **readiness checkpoint** for the question
"can agent-taskflow take the next step toward a semi-automatic
scheduler?" It is documentation-only.

This phase is explicitly **not**:

- a scheduler loop implementation
- a daemon, background worker, or `while True` loop
- a runtime automation layer
- an approval / merge / cleanup automation
- a Mission Control mutation surface
- a GitHub mutation surface
- a new DB schema migration

Phase F adds zero runtime behavior. Its only deliverable is a written
checkpoint describing what the merged Phase A–E chain has proven, what
"semi-automatic scheduler" means in this repo, what is safe to do next,
and what must remain operator-gated. Any future scheduler work will be
scoped as its own phase, with its own governance review, and will
inherit the gates spelled out below.

The core principle still holds:

> Manage work, not agents.

The scheduler, if and when it exists, is just one more deterministic
component that surfaces work for human operators. It is not a worker,
not a validator, and not an approver.

## 2. Current Proven Chain

Phase E (`docs/runtime-chain-dogfood-smoke.md`,
`scripts/run_runtime_chain_dogfood_smoke.py`,
`tests/test_runtime_chain_dogfood_smoke.py`) ran the following
end-to-end chain hermetically on a fresh queued task. Every link is
operator-triggered today; nothing on this path runs without an explicit
command.

```
GitHub / task mirror or seeded queued TaskRecord
  → task_execution_package artifact + event
    → scheduler_proposal artifact + event
      → scheduler_confirmation artifact + event
        → scheduler confirmation verifier report (persisted)
          → intake_runner_handoff artifact
            (binds verifier_run_id / verifier_report_path)
            → queued_task_handoff confirmed preflight
              (rechecks proposal_hash / item_hash / TTL,
               reopens handoff + verifier report)
              → runtime audit events + artifact
                  - runtime_preflight_finished
                  - runtime_execution_started
                  - runtime_execution_finished
                  - runtime_handoff_execution.v1 artifact
                → approved_task_runner
                  → executor run (executor_run_started /
                                  executor_run_finished)
                    → deterministic validators
                      (validation_result events)
                      → status = waiting_approval
                        → read-only API / Mission Control readback
                          - GET /api/tasks/{task_key}
                          - GET /api/tasks/{task_key}/runtime-audits
                          - GET /api/tasks/{task_key}/artifacts
                          - GET /api/tasks/{task_key}/validations
                          - Mission Control Runtime Audit panel
```

Phase markers backing each link:

- Phase A — `intake_runner_handoff` confirmed mode persists the verifier
  report sibling artifact and stamps `verifier_run_id` /
  `verifier_report_path` into the handoff payload.
- Phase B — `queued_task_handoff` confirmed mode requires
  `intake_runner_handoff_artifact_path`, reopens both the handoff and
  the verifier report, and rechecks `proposal_hash` / `item_hash` / TTL
  before invoking `approved_task_runner`.
- Phase C — `queued_task_handoff` confirmed mode writes the runtime
  audit evidence: `runtime_preflight_finished`,
  `runtime_execution_started`, `runtime_execution_finished`, and the
  `runtime_handoff_execution` artifact.
- Phase D — store / API / Mission Control read back runtime audit
  evidence via `TaskMirrorStore.list_runtime_audit_events`,
  `GET /api/tasks/{task_key}/runtime-audits`, and the Mission Control
  Runtime Audit panel.
- Phase E — runtime chain dogfood smoke ties Phases A–D together on a
  hermetic fresh queued task with no real Pi / OpenCode / network /
  GitHub access.

Properties this chain already has:

- explicit `--confirm-*` gates on every mutating step
- proof-of-work artifacts (handoff, verifier report, runtime audit,
  executor log, validation reports) per transition
- no self-approval anywhere on the chain
- no auto-merge anywhere on the chain
- no auto-cleanup anywhere on the chain
- runtime audit is **not** validation authority; `validation_result`
  remains authoritative
- Mission Control remains read-only

## 3. What "Semi-Automatic Scheduler" Means in This Repo

> **Semi-automatic scheduler** means the system may discover or propose
> eligible work, prepare deterministic proposal / handoff artifacts, and
> surface ready-to-run candidates, but it must not execute, approve,
> merge, or clean up without explicit operator confirmation.

In Chinese, for operator clarity: 半自動 scheduler 可以幫忙找任務、產生
proposal、產生 handoff candidate、顯示 ready-to-run 狀態，但不能自己執行、
不能自己批准、不能自己 merge、不能自己 cleanup。

Concretely, a "semi-automatic" scheduler in agent-taskflow:

- **may** read the SQLite mirror and on-disk artifacts
- **may** classify queued tasks as eligible / not-eligible candidates
  by a published deterministic policy
- **may** produce class-B proposal artifacts (per
  `docs/scheduler-automation-boundary.md` §2)
- **may** name the exact `--confirm-*` command the operator would type
- **must not** transition `TaskRecord.status` on its own
- **must not** write any class-C (action) evidence
- **must not** invoke `approved_task_runner` on its own
- **must not** merge, push, delete branches, delete worktrees, or close
  tasks
- **must not** run as a daemon, cron, webhook, or polling loop in this
  phase
- **must not** issue approval records — no self-approval, no
  auto-approval; **human review remains final**

## 4. Automation Levels

| Level | Name | What the system does | What stays operator-gated |
| --- | --- | --- | --- |
| 0 | Manual only | Operator runs every script; system only records artifacts and events. | Everything. |
| 1 | Read-only discovery | System lists eligible queued tasks and candidate command kinds from existing evidence. No proposal write, no confirmation write, no execution. | All mutation; all confirmation; all execution. |
| 2 | Proposal generation | Under an explicit operator command, system writes a `scheduler_proposal` artifact for a named task. No confirmation. No execution. | Confirmation; verifier report; handoff; runtime execution; approval; merge; cleanup. |
| 3 | Operator-confirmed handoff preparation | After operator confirmation, system writes scheduler confirmation + verifier report + `intake_runner_handoff` artifact. No runtime execution. | `queued_task_handoff` confirmed run; approved_task_runner; validators; approval; merge; cleanup. |
| 4 | Operator-confirmed runtime execution | Operator supplies the handoff artifact and confirms; `queued_task_handoff` performs the runtime preflight, writes runtime audit evidence, invokes `approved_task_runner`, validators run, task may reach `waiting_approval`. **This is what Phases A–E have already proven.** | Human approval; PR handoff; branch push; draft PR; merge; cleanup; closeout. |
| 5 | Background / daemon scheduler | Out of scope for this phase. Requires lease/lock, concurrency guards, replay protection, stale-candidate invalidation, operator queue policy, rate limiting, safe cancellation, and an explicit human review gate at any new action surface. | Everything Level 4 already gates, **plus** lease policy, concurrency policy, cancellation policy, replay policy. |

Levels 0–4 are all operator-triggered. The boundary between Level 4 and
Level 5 is "does the system pick the next task on its own?". Today the
answer is no, and Phase F does not change that.

## 5. Current Readiness Verdict

> **The repo is ready for Level 1–4 explicit-command semi-automation.**
> **The repo is NOT yet ready for Level 5 background/daemon scheduling.**

Why Levels 1–4 are reachable:

- handoff binding exists (`intake_runner_handoff` requires verifier
  report; `queued_task_handoff` requires `intake_runner_handoff`)
- verifier report persistence exists (Phase A)
- runtime preflight exists with `proposal_hash` / `item_hash` / TTL
  recheck (Phase B)
- runtime audit evidence exists
  (`runtime_preflight_finished`, `runtime_execution_started`,
  `runtime_execution_finished`, `runtime_handoff_execution` artifact)
  (Phase C)
- API / Mission Control readback exists
  (`GET /api/tasks/{task_key}/runtime-audits`, Runtime Audit panel)
  (Phase D)
- end-to-end dogfood smoke exists and passes hermetically (Phase E)
- task recommendations layer already produces a deterministic read-only
  candidate surface
  (`agent_taskflow/task_recommendations.py`,
  `scripts/list_task_recommendations.py`)
- runtime audit is **not** validation authority; `validation_result`
  remains authoritative

Why Level 5 is **not** yet reachable:

- no task claim / lease mechanism
- no concurrency protection across overlapping ticks or operators
- no idempotency / replay handling for scheduler candidates
- no stale-proposal / stale-confirmation cleanup or invalidation policy
- no operator queue / approval surface for batched proposals
- no failure retry policy
- no rate limiting
- no safe cancellation policy
- no branch / worktree lifecycle policy under concurrent runs
- no cleanup strategy for consumed or expired proposal artifacts
- no explicit human review gate in Mission Control for any new action
  affordances (Mission Control is, and must remain, read-only)

Until those exist, the system is allowed to *propose* and *prepare*, but
the *step* must always be an operator typing the next command.

## 6. Required Invariants Before Any Future Scheduler Execution

Any future scheduler that proposes a runtime step must verify each of
these invariants from the live mirror immediately before surfacing the
proposal. If any invariant is unmet, the proposal must be withheld and
the missing evidence must be reported.

- task exists in the mirror (`TaskRecord` row present)
- `task.status == "queued"`
- `task_execution_package` artifact exists and matches `task_key`
- `scheduler_proposal` exists and `proposal_hash` matches the package
- `scheduler_confirmation` exists and binds `proposal_hash` +
  `item_hash`
- `verifier_report_path` exists on disk and the report is valid
- `intake_runner_handoff` artifact exists and binds `verifier_run_id`
  and `verifier_report_path`
- `queued_task_handoff` preflight has rechecked TTL and binding before
  any executor invocation
- runtime audit evidence is written
  (`runtime_preflight_finished`, `runtime_execution_started`,
  `runtime_execution_finished`, `runtime_handoff_execution` artifact)
- validators produce `validation_result` events; `validation_result`
  remains authoritative
- final status may reach `waiting_approval`
- **human review remains final**; no scheduler-issued approval

The invariants are a *read* contract for any future scheduler. They are
not a license for the scheduler to write downstream evidence on the
assumption that an upstream step "must have happened".

## 7. What Remains Missing Before Daemon / Background Loop

These are the gaps a Level 5 design would have to close, each of which
is its own scoping discussion and almost certainly its own phase:

- task claim / lease mechanism (so two ticks cannot pick the same task)
- concurrency protection (so overlapping operators or ticks cannot
  interleave handoff steps for the same task)
- idempotency / replay handling for scheduler candidates (so a re-run
  produces the same proposal, not a duplicate side effect)
- stale proposal / stale confirmation cleanup or invalidation policy
  (so a stale `scheduler_proposal` cannot be reused after the task
  state has drifted)
- operator queue / approval surface (so batched proposals have an
  auditable accept/reject record)
- failure retry policy (with explicit bounds and an operator-readable
  failure trail)
- rate limiting (so a buggy tick cannot saturate executors, GitHub, or
  validators)
- safe cancellation policy (so an in-flight run can be stopped without
  corrupting the runtime audit chain)
- branch / worktree lifecycle policy under concurrent runs (so worktree
  reuse, branch reuse, and cleanup races are all defined)
- clearer cleanup strategy for consumed / expired artifacts (so the
  artifact tree does not accumulate stale proposals indefinitely)
- explicit human review gate in Mission Control if any future action
  affordances are added (today Mission Control is read-only; that gate
  is "do not add buttons")

None of this is being designed in Phase F. The list exists so that the
next phase boundary is unambiguous.

## 8. Recommended Next Implementation Phase

The recommended next phase is **not** a daemon. It is the strictly
read-only step that makes a future daemon design discussion possible
without compromising any gate.

> **Phase G — Read-only scheduler candidate discovery**

Goals:

- list eligible queued tasks and their candidate command kinds, derived
  from existing evidence
- expose the listing as a CLI, and optionally as a read-only API
  endpoint that surfaces the same data already in the mirror
- **no** proposal creation unless an operator explicitly confirms a
  separate command
- **no** runtime execution
- **no** DB mutation in default mode
- **no** GitHub mutation, ever, in this layer

Phase G earns its name only by being safer than the existing
`list_task_recommendations` surface in one specific way: it speaks the
scheduler vocabulary (`current_status`, `recommended_command_kind`,
`missing_evidence`, `required_next_gate`) and refuses to act on it.

## 9. Non-Goals

Phase F, and Phase G as scoped above, explicitly do **not** include:

- no scheduler loop
- no background worker
- no automatic task picking
- no automatic confirmation
- no automatic runtime execution
- no approval / merge / cleanup
- no GitHub mutation
- no Mission Control action buttons
- no new DB schema migration
- no new dependency
- no change to executor or validator behavior
- no change to runtime audit semantics
- no change to `validation_result` authority
- no self-approval by any worker, executor, validator, or scheduler

Any of the above requires a separate phase, separate scoping, and
separate operator approval.

## 10. Acceptance Checklist for Future Phase G

The following acceptance criteria apply to Phase G (read-only
candidate discovery) when it is scoped and implemented. They are listed
here so the gate is fixed before code is written.

- read-only candidate listing returns a deterministic candidate list
  (stable order, stable fields, stable hashes across re-runs on the
  same mirror state)
- each candidate record includes:
  - `task_key`
  - `current_status`
  - `recommended_command_kind`
  - `missing_evidence` (list, empty if none)
  - `required_next_gate` (named operator command + `--confirm-*` flag)
- candidates do **not** write to the DB or to artifact storage by
  default
- candidate output explicitly states that being listed is **not**
  execution permission
- tests prove no DB mutation occurs from the listing command
- tests prove no `approved_task_runner` call occurs from the listing
  command
- tests prove no `gh` / GitHub mutation occurs from the listing command
- tests prove the listing refuses to act on tasks with non-empty
  `consistency_warnings` (it must surface them, not hide them)
- documentation states that human review remains final and that
  `validation_result` remains authoritative

When all of these are met, Phase G is implementation-complete. None of
this allows Level 5 daemon mode; that remains a future, separately
authorized discussion.
