# Milestone 0 Correctness Baseline Status

> Decision date: 2026-07-11  
> Scope: atomic permission, Task/Attempt schema, and PR-3 runtime admission reconciliation

## Decision

The atomic-write permission and orphan-audit slice is closed.

PR-2 implements the Task/Attempt/lifecycle persistence foundation and the
SQLite one-active-attempt constraint. PR-3 adds Atomic attempt claim,
execution ownership, runtime lease, heartbeat, and fail-closed executor-start
guards for every current persisted runtime pickup after migration.

The overall Level 2 Milestone 0 exit gate is **not complete** because reset does
not yet create a new Attempt, the canonical runtime admission path does not yet
propagate explicit owner tokens through every caller, and fresh-worktree retry
semantics are not implemented. This document must not be used as evidence that
Level 2 Milestone 0 has passed.

## Atomic-write slice: closed

The following behavior is implemented and protected by regression tests:

- Existing regular files preserve their complete permission bits when replaced.
- New files use `0o666` creation mode subject to the process umask.
- A standard `0o022` umask therefore produces a `0o644` file.
- Executable permission bits on an existing regular file are preserved.
- Temporary files are created in the target directory and replaced with
  `os.replace(...)`.
- File data is flushed and fsynced before replacement; directory fsync is
  best-effort after replacement.
- A symlink target is not followed. The symlink path itself is replaced and the
  former target remains unchanged.
- Normal exceptions attempt best-effort temporary-file cleanup.

The implementation authority is `agent_taskflow/atomic_write.py`. The primary
regression suite is `tests/test_atomic_write.py`; explicit closeout coverage is
in `tests/test_m0_correctness_baseline_status.py`.

## Atomic-temp policy: closed as a policy decision

Atomic-write orphan temp candidates are evidence, not validator noise.

Canonical handling:

- Never silently exclude atomic temp candidates from changed-files evidence.
- A candidate inside a task worktree remains an unexpected repository change
  and must fail closed until explicitly inspected and resolved.
- A candidate outside the repository working tree, within an attempt-scoped
  artifact root, is recorded by the read-only orphan audit and does not create
  a repository path-policy exclusion.
- Cleanup is a separate, explicit, human-confirmed, auditable operation and is
  never part of changed-files validation.

The policy authority is `docs/changed-files-no-exclusion-decision.md`. The
operator procedure is `docs/atomic-artifact-safety-runbook.md`.

## Task/Attempt persistence foundation: implemented

PR-2 adds:

- stable `task_id`, `task_class`, `active_attempt_id`, final-outcome, close-time,
  and legacy markers on mirrored tasks;
- `attempts` with unique `(task_id, attempt_number)` identity;
- partial unique index `ux_attempts_one_active_per_task`;
- append-only `lifecycle_events` with task/attempt integrity guards;
- additive legacy migration that does not synthesize historical attempts; and
- transactional create/close storage operations.

## Runtime pickup boundary: implemented after PR-3 migration

Migration `level2_runtime_admission_v1` adds:

- atomic `queued/blocked -> preparing` Attempt and lease creation;
- partial unique indexes allowing one active lease per task and Attempt;
- unique persisted execution ownership;
- token-authenticated explicit heartbeat and release APIs;
- compatibility heartbeat from persisted runtime/task events;
- a guard denying `implementing` and `validating` without a live lease;
- a guard denying `executor_run_started` without a live lease;
- automatic compatibility release at `blocked`, `waiting_approval`, `canceled`,
  or `completed`; and
- stale-lease reaping to `execution_aborted` plus task status `blocked`.

The current executor call sites are `ApprovedTaskRunner` and `Dispatcher`; both
persist `create_executor_run(...)` before `executor.run(...)` and are therefore
covered by the database guard. `queued_task_handoff` delegates to
`run_approved_task(...)` and cannot start an executor independently.

Existing callers use a database-enforced `implicit_status` ownership record.
The next canonical runtime admission path PR will propagate explicit owner/token
credentials through all runtime callers and remove reliance on compatibility
ownership. The bypass is closed after migration, but the Python call graph is
not yet unified.

## Milestone 0 exit-gate reconciliation

| Exit-gate item | Status | Evidence or blocker |
| --- | --- | --- |
| New-file and overwrite permission tests pass | **Passed** | Atomic-write implementation plus mode regression tests. |
| Runtime concurrent pickup cannot create two active attempts | **Passed after migration** | PR-3 concurrent pickup tests produce one winner and one fail-closed rejection. |
| Concurrent reset cannot create two active attempts | **Partial** | The one-active-attempt constraint and runtime claim are authoritative, but the reset command still does not create Attempts through the claim transaction. |
| Executor start requires active ownership | **Passed after migration** | SQLite rejects persisted `executor_run_started` without an active, unexpired lease. |
| Reset can successfully rerun with correct retry identity | **Blocked** | Legacy `blocked -> queued` status reset does not close the prior Attempt or create a new Attempt identity. |
| Retry uses destroy-and-recreate worktree semantics | **Blocked** | Current legacy behavior can retain/reuse the prior task worktree. |
| Reset and atomic write have authoritative audit evidence | **Partial** | Atomic behavior, orphan audit, runtime leases, and append-only lifecycle storage exist; reset evidence is not yet Attempt-scoped. |
| Existing full test suite is green | **Required per PR** | GitHub Actions on the exact PR head is the authority. |

## Remaining blockers and ownership

Milestone 0 can be closed only after the following foundations land:

1. A canonical runtime admission path that passes explicit owner identity and
   lease token through Dispatcher, `ApprovedTaskRunner`, scheduler/runtime
   handoff, and future execution entrypoints.
2. Attempt-scoped branch, worktree, lock, PID, and artifact resources.
3. Retry/reset semantics that close the prior attempt, create a new attempt, and
   create a fresh worktree without overwriting historical evidence.
4. Process-group lifecycle and crash recovery tied to lease expiry and PID
   evidence.
5. Reset audit events bound to both the closed Attempt and newly created Attempt.

Until those items pass their own regression tests, the canonical status is:

```text
atomic_permission_slice = closed
atomic_temp_policy = closed
task_attempt_schema = implemented
one_active_attempt_constraint = implemented
atomic_attempt_claim = implemented_after_migration
execution_ownership = implemented_after_migration
runtime_lease = implemented_after_migration
runtime_heartbeat = implemented_after_migration
executor_start_without_lease = denied_after_migration
runtime_attempt_admission = implemented_compatibility_boundary
canonical_explicit_token_wiring = open_blocked
milestone_0 = open_blocked
level_2_eligible = false
```

## Migration and rollback

PR-2 and PR-3 introduce additive forward-only migrations. A deployment must run
`python3 scripts/migrate_runtime_admission.py --db-path /absolute/path/state.db`
before claiming runtime admission enforcement.

Application rollback leaves the new tables and evidence intact. Removing the
runtime triggers without a replacement admission control would reopen executor
pickup bypass and therefore requires an explicit operator-approved migration.
Destructive rollback requires restoring a pre-migration database backup or an
explicit rebuild; Attempt, lifecycle, and runtime-lease audit history must not
be dropped casually.
