# Runtime Admission, Execution Ownership, Lease, and Heartbeat

> Decision date: 2026-07-11  
> Scope: PR-3 atomic runtime pickup and fail-closed executor-start enforcement

## Status

```text
atomic_attempt_claim = implemented
execution_ownership = implemented
runtime_lease = implemented
runtime_heartbeat = implemented
executor_start_without_lease = denied
legacy_runtime_pickup_bypass = denied_after_migration
canonical_explicit_token_wiring = next_pr
milestone_0 = open_blocked
level_2_eligible = false
```

PR-3 converts the PR-2 Task/Attempt constraint into a runtime admission boundary.
After migration, the existing Dispatcher and `ApprovedTaskRunner` cannot begin an
executor run without an active, unexpired lease bound to the task's active
Attempt. `queued_task_handoff` delegates execution to `run_approved_task` and
therefore crosses the same boundary.

This PR does not yet refactor every caller to invoke one Python admission service
explicitly. Existing callers are protected through persisted SQLite triggers;
the explicit token API is provided for the canonical runtime path in the next
PR.

## Atomic pickup

The current runtime paths already persist a transition to `preparing` before
executor side effects. Migration `level2_runtime_admission_v1` turns that
persisted transition into an atomic database claim:

1. Ensure the task has a stable `task_id`.
2. Reject the transition if the task already has an active Attempt or lease.
3. Allocate the next monotonic `attempt_number`.
4. Insert one active Attempt.
5. Set `tasks.active_attempt_id`.
6. Insert one active runtime lease.
7. Append lifecycle evidence.
8. Commit all records in the same SQLite transaction as the status mutation.

A second `preparing -> preparing` pickup is rejected with
`runtime pickup already claimed`. Two concurrent SQLite connections therefore
cannot both win the same task.

## Execution ownership modes

### Explicit token ownership

`RuntimeAdmissionStore.claim(...)` returns:

```text
task_id
attempt_id
attempt_number
lease_id
owner_id
lease_token
acquired_at
heartbeat_at
expires_at
```

Only a SHA-256 token fingerprint is stored in SQLite. The raw token is returned
once to the claimant and is required with the matching `owner_id` for explicit
heartbeat and release operations.

### Persisted compatibility ownership

The existing Dispatcher and `ApprovedTaskRunner` do not yet carry a lease token
through their call stacks. Their persisted `preparing` transition creates an
`implicit_status` compatibility lease. The subsequent `status_changed` event
binds the audit owner to a unique value such as:

```text
approved_task_runner:event-418
```

This mode prevents duplicate pickup and executor-start bypass now. PR-4 will
replace compatibility ownership with explicit token propagation through the
canonical admission service.

## Lease schema

`runtime_leases` records:

```text
lease_id
task_id
attempt_id
owner_id
token_hash
auth_mode
ttl_seconds
acquired_at
heartbeat_at
expires_at
released_at
release_reason
is_active
```

SQLite partial unique indexes enforce:

- at most one active lease per task;
- at most one active lease per Attempt; and
- the existing PR-2 at-most-one-active-Attempt constraint remains authoritative.

## Heartbeat

There are two heartbeat paths:

1. `RuntimeAdmissionStore.heartbeat(...)` verifies the explicit owner and token,
   extends expiry, updates Attempt freshness, and appends lifecycle evidence.
2. Existing runtime paths refresh their compatibility lease whenever they write
   persisted task/runtime evidence, including status changes, executor-run
   boundaries, and validation records.

An expired lease cannot be revived by moving the task to `implementing` or
`validating`. Those transitions fail closed when no active, unexpired lease is
present.

## Executor-start guard

The strongest compatibility boundary is the
`runtime_executor_start_requires_live_lease` trigger. A persisted
`executor_run_started` event is rejected unless:

- the task has an active Attempt;
- the active Attempt owns an active runtime lease; and
- the lease is unexpired.

The current production executor call sites are limited to:

```text
agent_taskflow/approved_task_runner.py
agent_taskflow/dispatcher.py
```

Both persist `create_executor_run(...)` before calling `executor.run(...)`.
Regression tests scan the package and fail if a new executor call site appears
without this persisted boundary. `queued_task_handoff.py` does not call an
executor directly; it delegates to `run_approved_task(...)`.

## Terminal release

For compatibility-owned runs, transitions to these task statuses automatically
release the lease and close the Attempt:

```text
blocked
waiting_approval
canceled
completed
```

Explicit token owners use `RuntimeAdmissionStore.release(...)`, which verifies
the owner/token pair and atomically records the final Attempt status, task
status, release reason, and lifecycle event.

## Stale lease recovery

`RuntimeAdmissionStore.expire_stale_leases()` performs a deterministic reaper
pass. Each expired active lease is:

- released with reason `runtime_lease_expired`;
- closed as `execution_aborted`;
- recorded in append-only lifecycle events; and
- moved to task status `blocked`.

The reaper does not reset the task, create a retry Attempt, reuse a worktree, or
authorize cleanup. Attempt-safe reset and fresh-worktree retry remain separate
M0 work.

## Migration

Run:

```bash
python3 scripts/migrate_runtime_admission.py \
  --db-path /absolute/path/to/state.db
```

To also reap leases already expired at invocation time:

```bash
python3 scripts/migrate_runtime_admission.py \
  --db-path /absolute/path/to/state.db \
  --reap-expired
```

The migration first applies the PR-2 Task/Attempt schema, then installs the
lease tables, unique indexes, pickup/transition/executor guards, heartbeat
trigger, and terminal-release trigger. It is additive and idempotent.

Deployments must run this migration before claiming PR-3 runtime enforcement.
A database that has not recorded `level2_runtime_admission_v1` is not covered by
this admission boundary.

## Rollback

Application rollback can stop using the explicit API while leaving lease and
Attempt evidence intact. Removing the SQLite triggers would reopen the bypass
that this PR closes and therefore requires an explicit operator-approved
migration and a replacement admission control.

Do not drop `runtime_leases`, `attempts`, or `lifecycle_events` as an informal
rollback; that would destroy execution ownership and audit history.

## Acceptance criteria

- Two concurrent pickups produce exactly one active Attempt and lease.
- A repeated pickup of a task already in `preparing` fails closed.
- An executor-start event without a live lease is rejected by SQLite.
- `implementing` and `validating` transitions require a live lease.
- Explicit heartbeat requires the matching owner and token.
- The raw lease token is never stored.
- Persisted runtime events refresh compatibility heartbeat.
- Terminal task status releases the compatibility lease.
- Expired leases are closed as `execution_aborted` and block the task.
- Current executor call sites remain covered by persisted admission.
- The existing full test suite remains green.
