# Atomic Reset Lineage and Retry Reservation

> Decision date: 2026-07-11  
> Scope: PR-8 old/new Attempt binding, reset audit, idempotency, and concurrent compare-and-set

## Status

```text
reset_generation = implemented
reset_old_attempt_binding = implemented
reset_new_attempt_reservation = implemented
reset_lineage_events = append_only
canonical_blocked_to_queued_guard = implemented
concurrent_reset_compare_and_set = implemented
reset_request_idempotency = implemented
reserved_attempt_runtime_adoption = implemented
second_retry_identity_on_claim = denied
historical_attempt_resources_deleted = false
automatic_retry_policy = not_implemented_in_this_pr
milestone_0 = open_pending_validator_scope_decision
level_2_eligible = false
```

## Atomic reset contract

The only supported reset remains:

```text
blocked -> queued
```

A confirmed reset now executes in one SQLite `BEGIN IMMEDIATE` transaction. It:

1. verifies that the task is still `blocked`;
2. verifies that `active_attempt_id` is null;
3. verifies that no active Attempt, runtime lease, or managed executor process remains;
4. compares the current `reset_generation` with the caller's expected value;
5. optionally compares the latest closed Attempt with `expected_old_attempt_id`;
6. creates exactly one new active Attempt in `created` state;
7. inserts a transaction-local suppression bound to that Task and new Attempt;
8. compare-and-sets the task to `queued`, binds the new Attempt, clears the blocked reason, and increments `reset_generation`;
9. inserts the old/new lineage and immutable reset event;
10. records the task status event; and
11. removes the suppression before committing.

Any failure rolls back the new Attempt, suppression, lineage, and task update
together.

The `reset_lineage_required_for_retry` SQLite trigger rejects every raw
`blocked -> queued` task update unless the same transaction contains the exact
suppression for `task_id + new_attempt_id`. This makes the reset service the
canonical mutation path; direct `TaskMirrorStore` or SQL updates cannot bypass
old/new Attempt lineage or generation CAS.

## Reset lineage

`reset_lineages` binds:

```text
reset_id
request_id
task_id / task_key
old_attempt_id
new_attempt_id
expected_generation
committed_generation
reason / actor
state = reserved | claimed | canceled
created_at / claimed_at
```

The old Attempt is the latest closed Attempt at the time of reset. Legacy tasks
may have `old_attempt_id = null`; the new Attempt is always explicit.

The new Attempt starts as:

```text
status = created
is_active = true
lease = none
worktree/artifact resources = not allocated yet
```

This is a retry identity reservation, not runtime ownership.

## Concurrent compare-and-set

Every task has a monotonic `reset_generation`, initialized to zero.

The task update requires all of these predicates:

```text
status = blocked
active_attempt_id IS NULL
reset_generation = expected_generation
```

The successful transaction increments the generation and binds the new Attempt.
Two callers using the same observed generation cannot both succeed. The loser
fails closed and appends a `reset_compare_and_set_rejected` event to the winning
lineage when one is available.

The existing partial unique index on active Attempts remains a second independent
constraint. The generation CAS is not a replacement for the one-active-Attempt
constraint.

## Request idempotency

`request_id` is a stable idempotency key. Repeating the same request ID with the
same task, reason, actor, expected generation, and expected old Attempt returns
the already committed lineage without creating another Attempt or incrementing
the generation.

Reusing a request ID for different reset inputs fails closed.

## Runtime adoption

A canonical runtime claim first checks for a reset-reserved Attempt matching:

```text
task.status = queued
task.active_attempt_id = attempt.attempt_id
attempt.status = created
attempt.is_active = true
reset_lineage.state = reserved
no active runtime lease
```

When found, admission atomically:

```text
created -> preparing
creates the token lease
updates task queued -> preparing
marks reset lineage reserved -> claimed
appends lifecycle and reset-lineage events
```

The runtime uses the reserved `new_attempt_id`; it does not create a third
identity. Attempt-scoped branch, worktree, artifact, lock, PID, and executor
process resources are allocated afterward through the existing PR-5/PR-7 path.

## Audit evidence

`reset_lineage_events` is append-only. SQLite rejects UPDATE and DELETE. Events
include:

```text
reserved
claimed
compare_and_set_rejected
artifact_failed
```

The database is the reset authority. A supplementary JSON artifact is written to:

```text
<task artifact base>/reset-audit/<reset-id>.json
```

It is deliberately outside a released Attempt artifact root, so recording the
reset does not mutate historical Attempt evidence. Artifact failure is audited
but does not roll back an already committed database transaction.

## Safety boundaries

A reset does not:

- run an executor or validator;
- approve or merge work;
- delete branches, worktrees, artifacts, process records, or audit events;
- reuse the previous Attempt worktree;
- bypass pause or kill controls; or
- schedule an automatic retry.

The next runtime pickup still crosses canonical admission, lifecycle, resource,
and process preflight boundaries.

## Deployment

Run from the repository root:

```bash
python3 scripts/migrate_reset_lineage.py \
  --db-path "$HOME/.agent-taskflow/state.db"
```

Preview a reset and read the current generation/old Attempt:

```bash
python3 scripts/reset_task_status.py \
  --task-key AT-EXAMPLE-1 \
  --db-path "$HOME/.agent-taskflow/state.db" \
  --from-status blocked \
  --reason "retry after operator inspection" \
  --dry-run
```

Commit with explicit compare-and-set inputs:

```bash
python3 scripts/reset_task_status.py \
  --task-key AT-EXAMPLE-1 \
  --db-path "$HOME/.agent-taskflow/state.db" \
  --from-status blocked \
  --reason "retry after operator inspection" \
  --request-id reset-request-example-1 \
  --expected-reset-generation 0 \
  --expected-old-attempt-id attempt-EXAMPLE \
  --actor operator \
  --confirm-reset
```

The output reports the reset ID, old Attempt, new Attempt, expected/committed
generations, idempotent replay status, and audit artifact path.

## Remaining decision

PR-8 closes the reset-lineage and concurrent-reset correctness blockers. The
Milestone 0 document remains open only for the explicit scope decision on whether
validator subprocesses must join the PR-7 managed process-group hard-termination
boundary before Level 2 eligibility is declared.
