# Explicit Scheduler Confirmation Preparation Checkpoint (Phase K0)

## 1. Purpose

This is the Level 3 confirmation preparation checkpoint for the agent-taskflow scheduler path.

It follows completed Level 1 candidate discovery and completed Level 2 explicit proposal generation. This checkpoint defines the scheduler_proposal → scheduler_confirmation preparation boundary for the next explicit operator gate.

This phase is documentation and test coverage only. It does not add runtime behavior.

This phase is explicitly:

- not verifier report creation
- not handoff creation
- not runtime execution
- not confirmation creation implementation
- not CLI, API, UI, or Mission Control implementation

Level 3 confirmation preparation must preserve the project rule:

> Manage work, not agents.

## 2. Completed Level 1 and Level 2 foundation

Level 1 observe/classify/list candidates is complete. It provides read-only scheduler candidate discovery and visibility. Candidate visibility is not execution permission.

Level 2 explicit proposal generation is complete. The completed Level 2 foundation includes:

- J1 explicit proposal CLI exists.
- J2 proposal readback API exists.
- J3 Mission Control proposal visibility exists.
- J4 proposal creation hardening smoke exists.

The completed Level 2 chain is:

scheduler candidate discovery → explicit proposal generation CLI → scheduler_proposal artifact/event → proposal readback API → Mission Control read-only proposal visibility → proposal creation hardening smoke.

A scheduler_proposal is not confirmation. A scheduler_proposal is not execution permission. It is auditable planning evidence that can be considered at the next human/operator gate, but it does not authorize confirmation, verification, handoff, runtime execution, approval, merge, or cleanup.

Mission Control remains read-only for proposal visibility.

## 3. Level 3 definition

Level 3 defines explicit scheduler confirmation preparation.

The operator explicitly selects a scheduler_proposal item. The system may prepare a scheduler_confirmation artifact/event only after explicit operator intent and after rereading current stored proposal evidence.

A scheduler_confirmation is not execution permission. A scheduler_confirmation is not verifier report. A scheduler_confirmation is not handoff. A scheduler_confirmation is not runtime execution.

A scheduler_confirmation is auditable evidence for the next gate only. It records that an operator intentionally selected a specific proposal item for downstream verification/handoff preparation work. It must not start runtime and must not be interpreted as approval to execute the task.

Level 3 confirmation preparation is still not a scheduler loop, not automatic task picking, and not a runtime executor.

## 4. Required input

Any future Level 3 confirmation preparation entrypoint must receive or derive the following inputs:

- task_key
- proposal_id or proposal_hash
- proposal_item_id
- item_hash
- recommended_command_kind
- proposal_artifact_path
- current task status
- expected task status
- operator intent / explicit confirm flag
- db_path
- artifact root

The command-time implementation must treat the current database and stored artifacts as authority. User-provided proposal JSON is only an input hint and must not be trusted as authority.

## 5. Output

Confirmed Level 3 confirmation preparation may write only confirmation evidence:

- scheduler_confirmation artifact
- scheduler_confirmation_created event
- confirmation_id
- proposal_hash
- proposal_item_id
- item_hash
- recommended_command_kind
- proposal_artifact_path
- safety block
- no runtime side effects

The safety block must state that downstream action did not happen. In particular, scheduler_confirmation evidence must not be interpreted as verifier report creation, handoff creation, runtime execution, approval, merge, cleanup, or execution permission.

## 6. Safety boundary

Level 3 confirmation preparation has a strict safety boundary:

- no verifier report
- no handoff
- no runtime execution
- no approved_task_runner call
- no executor started
- no validators started
- no GitHub mutation
- no approval
- no merge
- no cleanup
- no approval / merge / cleanup
- no scheduler loop
- no background worker
- no automatic task picking

The implementation must not create verifier reports, `intake_runner_handoff` artifacts, runtime audit events, executor runs, validation results, GitHub side effects, approvals, merges, branch deletions, worktree deletions, or cleanup evidence.

Mission Control remains read-only. Phase K0 does not add Mission Control confirmation visibility, and later Mission Control confirmation visibility must remain read-only with no action controls.

## 7. Required invariants before confirmation preparation

Before any scheduler_confirmation write, the implementation must verify:

- proposal exists
- proposal artifact exists
- proposal_hash matches proposal artifact contents
- proposal_item_id exists in proposal artifact
- item_hash matches selected proposal item
- task still exists
- task status still matches expected status
- recommended_command_kind still matches selected item
- proposal is not stale
- user-provided proposal JSON must not be trusted as authority
- confirmation preparation recomputes / rereads current stored proposal evidence
- duplicate active confirmation for same proposal item should block or be explicitly handled

If any invariant fails, confirmation preparation must not write scheduler_confirmation evidence. The operator should receive a clear stale-proposal, missing-evidence, binding-mismatch, status-mismatch, command-kind-mismatch, or duplicate-confirmation result.

## 8. Proposed phases

The proposed sequence after this checkpoint is:

- K1 confirmation eligibility / binding read-only helper
- K2 explicit confirmation creation CLI
- K3 confirmation readback API
- K4 Mission Control read-only confirmation visibility
- K5 confirmation preparation hardening smoke
- only later: verifier report preparation

These phases must remain incremental. K1 is read-only and writes nothing. K2 is the first Level 3 phase that may add a write path, and that write path must be limited to scheduler_confirmation artifact/event evidence when explicitly confirmed.

## 9. Acceptance criteria for K2

K2 must satisfy these acceptance criteria:

- explicit CLI command
- dry-run default
- dry-run writes nothing
- confirmed mode requires --confirm-create-confirmation
- confirmed mode writes scheduler_confirmation artifact/event only
- no verifier report
- no handoff
- no runtime execution
- no approved_task_runner
- no executor
- no validators
- no GitHub mutation

Dry-run output may show what would be confirmed, but it must not write artifacts, record events, mutate task state, invoke executors, start validators, create verifier reports, create handoffs, call downstream helpers, or mutate GitHub.

## 10. Non-goals

This checkpoint and the immediate K0 work do not include:

- no verifier report creation
- no handoff creation
- no runtime execution
- no approved_task_runner
- no executor
- no validators
- no proposal creation API
- no confirmation creation API in K0
- no Mission Control action UI
- no scheduler loop
- no background worker
- no automatic task picking
- no GitHub mutation
- no approval / merge / cleanup

They also do not include confirmation creation implementation, verifier report preparation implementation, handoff artifact generation, runtime audit generation, executor invocation, validator invocation, Mission Control write controls, API mutation endpoints, branch push, draft PR creation, PR merge, branch deletion, worktree deletion, task closeout, or any other GitHub mutation.
