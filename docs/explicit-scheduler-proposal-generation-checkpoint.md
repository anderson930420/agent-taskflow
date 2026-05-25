# Explicit Scheduler Proposal Generation Checkpoint (Phase J0)

## 1. Purpose

This is the Level 2 explicit proposal generation checkpoint for the
agent-taskflow scheduler path.

This checkpoint defines the boundary for a future operator-gated proposal
creation step. It is documentation and test coverage only. It does not add
runtime behavior.

This phase is explicitly:

- not a scheduler loop
- not runtime execution
- not confirmation automation
- not handoff automation
- not merge automation

Level 2 explicit proposal generation must preserve the project rule:

> Manage work, not agents.

## 2. Current Level 1 foundation

Level 1 read-only scheduler candidate discovery is already the foundation for
this next boundary:

- Phase G CLI/module candidate discovery added read-only scheduler candidate
  discovery.
- Phase H API scheduler candidate readback added read-only candidate readback
  endpoints.
- Phase I Mission Control read-only scheduler candidate visibility added
  read-only UI visibility.

candidate visibility is not execution permission. A candidate shown by the
CLI, API, or Mission Control is review material only. It does not authorize
proposal creation, confirmation, handoff, runtime execution, approval, merge,
or cleanup.

## 3. Level 2 definition

Level 2 means the operator explicitly requests proposal generation for a
read-only scheduler candidate.

The system may generate a scheduler_proposal artifact only after operator
intent is explicit and the live task state is rechecked. A proposal is not
confirmation; proposal is not confirmation is a protected boundary. A proposal
is not execution permission; proposal is not execution permission is a
protected boundary. A proposal is only auditable planning evidence for the
next human/operator gate.

Level 2 requires explicit operator command. It is not automatic task picking,
not a queue runner, and not a runtime executor.

## 4. Required input

Any future Level 2 proposal generation entrypoint must receive or derive the
following inputs:

- candidate task_key
- candidate recommended_command_kind
- current task status
- current recommendation snapshot
- operator intent / explicit confirm flag
- db_path
- artifact root

The command-time implementation must treat the current database and artifacts
as authority. User-provided candidate JSON is only an input hint and must not
be trusted as authority.

## 5. Output

Confirmed Level 2 proposal generation may write only proposal evidence:

- scheduler_proposal artifact
- scheduler_proposal event
- proposal_hash
- proposal_item_id
- item_hash
- safety block
- no runtime side effects

The safety block must state that downstream action did not happen. In
particular, proposal evidence must not be interpreted as confirmation,
handoff, runtime execution, approval, merge, or cleanup evidence.

## 6. Safety boundary

Level 2 proposal generation has a strict safety boundary:

- no confirmation
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
- no background worker
- no scheduler loop

The implementation must not create scheduler confirmation evidence, verifier
reports, `intake_runner_handoff` artifacts, runtime audit events, executor
runs, validation results, GitHub side effects, approvals, merges, branch
deletions, worktree deletions, or cleanup evidence.

## 7. Proposed phases

The proposed sequence after this checkpoint is:

- J1 CLI explicit proposal generation
- J2 API readback for proposal candidates/proposals
- J3 Mission Control read-only proposal visibility
- J4 optional operator-gated proposal creation hardening smoke
- only later: confirmation preparation

These phases must remain incremental. J1 is the first phase that may add a
write path, and that write path must be limited to proposal artifact/event
evidence when explicitly confirmed.

## 8. Required invariants before proposal generation

Before any proposal generation write, the implementation must verify:

- candidate exists
- `candidate_ready` is true
- task still exists
- task status still matches candidate snapshot
- `recommended_command_kind` still matches current recommendation
- no stale candidate trust
- proposal generation recomputes current recommendation at command time
- user-provided candidate JSON must not be trusted as authority

If any invariant fails, proposal generation must not write proposal evidence.
The operator should receive a clear stale-candidate or mismatch result.

## 9. Acceptance criteria for J1

J1 must satisfy these acceptance criteria:

- explicit CLI command
- requires `--confirm-create-proposal` for writes
- requires explicit operator command
- dry-run default
- dry-run writes nothing
- confirmed mode writes proposal artifact/event only
- no confirmation
- no verifier report
- no handoff
- no runtime execution
- no approved_task_runner

Dry-run output may show what would be proposed, but it must not write
artifacts, record events, mutate task state, invoke executors, start
validators, or call downstream helpers.

## 10. Non-goals

This checkpoint and the immediate Level 2 path do not include:

- no scheduler loop
- no background worker
- no automatic task picking
- no batch execution
- no runtime execution
- no approved_task_runner
- no GitHub mutation
- no approval
- no merge
- no cleanup
- no approval / merge / cleanup

They also do not include confirmation creation, verifier report creation,
handoff creation, runtime audit generation, executor invocation, validator
invocation, Mission Control write controls, API mutation endpoints, branch
push, draft PR creation, PR merge, branch deletion, worktree deletion, task
closeout, or any other GitHub mutation.
