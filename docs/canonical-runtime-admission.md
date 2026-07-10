# Canonical Runtime Admission Path

> Decision date: 2026-07-11  
> Scope: PR-4 explicit owner/token propagation across runtime entrypoints

## Status

```text
canonical_runtime_admission_path = implemented
explicit_owner_token_propagation = implemented
implicit_status_pickup = disabled_after_migration
executor_start_claim_binding = implemented
owned_terminal_release = implemented
runtime_heartbeat_supervisor = implemented
attempt_scoped_resources = not_implemented_in_this_pr
milestone_0 = open_blocked
level_2_eligible = false
```

PR-4 makes one Python admission path authoritative for the two direct executor
roots:

```text
Dispatcher.dispatch_task(...)
run_approved_task(...)
```

`queued_task_handoff` and the GitHub Issue scheduler import and delegate to the
same wrapped `run_approved_task(...)` function. They do not own a second claim,
lease, or heartbeat implementation.

## Canonical store adapter

`CanonicalRuntimeTaskStore` wraps the existing task mirror API while preserving
its public mutation surface. The first persisted transition to `preparing`:

1. applies the PR-4 migration;
2. creates a unique owner identity for the invocation;
3. calls the explicit `RuntimeAdmissionStore.claim(...)` API;
4. receives the raw lease token once;
5. keeps that token only in process memory; and
6. starts a daemon heartbeat supervisor.

The existing runner code then continues to use:

```text
update_task_status(...)
create_executor_run(...)
finish_executor_run(...)
record_validation_result(...)
```

The adapter binds every operation to the same `attempt_id`, `lease_id`,
`owner_id`, and raw token. A caller that only has a normal `TaskMirrorStore`
cannot reproduce this ownership context.

## Executor-start binding

A canonical executor-start event contains:

```text
runtime_attempt_id
runtime_lease_id
runtime_owner_id
```

It never contains the raw lease token.

SQLite trigger `runtime_executor_start_requires_canonical_claim` rejects the
event unless all three values match the task's active, unexpired token lease.
The canonical adapter validates the raw token with an owned heartbeat immediately
before recording the event.

A legacy caller cannot piggyback on another runtime's active lease by invoking
`TaskMirrorStore.create_executor_run(...)`; its event lacks the required claim
metadata and fails closed.

## Heartbeat ownership

The canonical store performs token-authenticated heartbeat in two ways:

- a daemon supervisor renews long-running work at a bounded interval; and
- persisted runtime boundaries heartbeat synchronously before status,
  executor, and validation mutations.

A heartbeat failure is retained on the claim state. Later runtime mutations fail
closed rather than continuing with stale ownership.

PR-3 event-driven heartbeat remains installed only as an `implicit_status`
compatibility definition. PR-4 migration refuses to start while an active
implicit lease exists, disables creation of new implicit leases, and does not
allow ordinary task events to renew token leases.

## Terminal release

For a token-owned runtime, terminal task statuses cannot be written directly.
SQLite trigger `runtime_token_terminal_requires_owned_release` requires the
suppression marker used by the token-authenticated release transaction.

The canonical adapter maps runner outcomes as follows:

| Task status | Attempt status | Execution result |
| --- | --- | --- |
| `waiting_approval` | `waiting_approval` | `completed` |
| `completed` | `completed` | `completed` |
| `blocked` | `blocked` | `blocked` |
| `canceled` | `canceled` | `canceled` |

The release transaction verifies owner and token, closes the Attempt, releases
the lease, clears `active_attempt_id`, updates task status, and appends lifecycle
evidence atomically.

## Bootstrap coverage

Package bootstrap installs:

- a claim-aware `Dispatcher` subclass; and
- a wrapped `run_approved_task(...)` that canonicalizes any supplied store by
  database path.

This is important because tests, scheduler adapters, runtime handoffs, and CLI
scripts frequently pass a pre-existing `TaskMirrorStore`. The wrapper replaces
that mutable object boundary with a canonical store connected to the same
SQLite database.

Regression tests assert that:

- Dispatcher is canonical;
- ApprovedTaskRunner is canonical;
- queued handoff imports the canonical wrapper;
- scheduler tick imports the canonical wrapper; and
- the older PR-3 `RuntimeAdmissionStore` symbol remains isolated for migration
  compatibility and historical tests.

## Migration

Run from the repository root:

```bash
python3 scripts/migrate_canonical_runtime_admission.py \
  --db-path "$HOME/.agent-taskflow/state.db"
```

The migration CLI bootstraps only the local database/model modules and does not
execute `agent_taskflow.__init__`. It therefore works from a source checkout with
the system Python even when FastAPI and Pydantic are not installed. A manual
`PYTHONPATH` prefix and editable package installation are not required.

The output verifies disabled implicit pickup by inspecting the retained
`runtime_pickup_claim_after_preparing` compatibility trigger definition. PR-4
intentionally preserves that trigger name with an inert `WHEN 0` body so an
older idempotent PR-3 migration cannot restore implicit pickup.

Migration `level2_canonical_runtime_admission_v1`:

- requires SQLite JSON functions;
- refuses active `implicit_status` leases;
- disables implicit `-> preparing` pickup;
- requires canonical claim metadata at executor start;
- requires owned release for token leases; and
- reserves PR-3 trigger names with safe definitions so rerunning the older
  idempotent migration cannot restore implicit behavior.

## Rollback

Application rollback must not silently restore implicit pickup. Reverting the
Python bootstrap while leaving canonical triggers installed causes legacy
runtime pickup to fail closed, which is the safe state.

Restoring PR-3 implicit behavior requires an explicit operator-approved schema
migration. Do not drop Attempt, lease, or lifecycle tables; they are audit
history.

## Explicitly out of scope

PR-4 does not introduce:

- attempt-scoped branch, worktree, lock, PID, or artifact resources;
- fresh-worktree retry/reset semantics;
- process-group termination or PID reaping;
- lifecycle transition graph enforcement; or
- approval, merge, or auto-merge behavior.
