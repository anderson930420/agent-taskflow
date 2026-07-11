# Milestone 0 Correctness Baseline Status

> Decision date: 2026-07-11  
> Scope: atomic permission, Task/Attempt schema, PR-3/PR-4 runtime admission, and PR-5 Attempt resources

## Decision

The atomic-write permission and orphan-audit slice is closed.

PR-2 implements the Task/Attempt/lifecycle persistence foundation and the
SQLite one-active-attempt constraint. PR-3 adds Atomic attempt claim,
execution ownership, runtime lease, heartbeat, and fail-closed executor-start
guards. PR-4 installs the canonical runtime admission path and propagates one
explicit owner/token claim through Dispatcher, `ApprovedTaskRunner`, queued
runtime handoff, and the scheduler delegation chain.

PR-5 implements Attempt-scoped branch, worktree, lock, PID, and artifact
resources plus fresh-worktree retry identity. The overall Level 2 Milestone 0 exit
gate is still **not complete** because dual-Attempt reset audit binding, concurrent
reset compare-and-set semantics, and process-group termination/recovery remain
open. This document must not be used as evidence that Milestone 0 has passed.

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

## Runtime ownership boundary: implemented after migration

PR-3 migration `level2_runtime_admission_v1` provides the lease table, partial
unique indexes, token-authenticated claim/heartbeat/release APIs, live-lease
transition guards, and stale-lease reaping.

PR-4 migration `level2_canonical_runtime_admission_v1` makes explicit ownership
the only new pickup path:

- ordinary `TaskMirrorStore` updates cannot transition a task to `preparing`;
- `CanonicalRuntimeTaskStore` performs the token-authenticated claim;
- the raw token remains only in process memory;
- a heartbeat supervisor renews long-running work;
- synchronous runtime boundaries also heartbeat with the same owner/token;
- executor-start evidence must match the active `attempt_id`, `lease_id`, and
  `owner_id`;
- terminal status requires token-authenticated owned release; and
- PR-3 trigger names remain occupied by canonical-safe definitions so rerunning
  the older migration cannot restore implicit pickup or token heartbeat.

The direct executor roots are `ApprovedTaskRunner` and `Dispatcher`. Package
bootstrap wraps both. `queued_task_handoff` and the GitHub Issue scheduler import
and delegate to the same canonical `run_approved_task(...)`; they cannot create
independent runtime ownership.

## Milestone 0 exit-gate reconciliation

| Exit-gate item | Status | Evidence or blocker |
| --- | --- | --- |
| New-file and overwrite permission tests pass | **Passed** | Atomic-write implementation plus mode regression tests. |
| Runtime concurrent pickup cannot create two active attempts | **Passed after migration** | One active Attempt and one active token lease are enforced transactionally. |
| Concurrent reset cannot create two active attempts | **Partial** | The one-active-attempt constraint and Atomic attempt claim are authoritative, but reset still does not allocate a retry Attempt. |
| Executor start requires active ownership | **Passed after migration** | SQLite requires matching canonical claim metadata and a live token lease. |
| Canonical runtime admission path is used by current executor roots | **Passed after migration** | Dispatcher and ApprovedTaskRunner use the claim-aware store; runtime handoff and scheduler delegate to the wrapped runner. |
| Reset can successfully rerun with correct retry identity | **Blocked** | Legacy `blocked -> queued` reset does not close the prior Attempt and allocate the next retry identity. |
| Retry uses fresh Attempt worktree semantics | **Passed after PR-5 migration** | Each claim allocates a unique Attempt branch/worktree; terminal history is retained and a retry cannot reuse the prior Attempt path. |
| Reset and atomic write have authoritative audit evidence | **Partial** | Atomic behavior, orphan audit, leases, and lifecycle storage exist; reset evidence is not yet bound to old and new Attempts. |
| Existing full test suite is green | **Required per PR** | GitHub Actions on the exact PR head is the authority. |

## Remaining blockers and ownership

Milestone 0 can be closed only after the following foundations land:

1. Process-group lifecycle and crash recovery tied to lease expiry and PID
   evidence.
2. Reset audit events bound to both the closed Attempt and newly created Attempt.
3. Concurrent reset compare-and-set coverage proving two simultaneous reset
   requests produce one accepted reset lineage and one fail-closed rejection.

Until those items pass their own regression tests, the canonical status is:

```text
atomic_permission_slice = closed
atomic_temp_policy = closed
task_attempt_schema = implemented
one_active_attempt_constraint = implemented
atomic_attempt_claim = implemented_after_migration
execution_ownership = implemented_after_migration
runtime_lease = implemented_after_migration
runtime_heartbeat = explicit_token_supervised_after_migration
executor_start_without_lease = denied_after_migration
runtime_attempt_admission = canonical_explicit_token_path
canonical_explicit_token_wiring = implemented_after_migration
implicit_status_pickup = disabled_after_migration
attempt_scoped_resources = implemented_after_pr5_migration
fresh_worktree_retry_identity = implemented
milestone_0 = open_blocked
level_2_eligible = false
```

## Migration and rollback

Deployments must apply both runtime migrations in order. PR-4's command applies
its prerequisites automatically:

```bash
python3 scripts/migrate_canonical_runtime_admission.py \
  --db-path /absolute/path/state.db
```

The PR-4 migration refuses to run while an active `implicit_status` lease exists.
Finish or reap that compatibility run first. Reverting application code while
canonical triggers remain installed causes legacy pickup to fail closed, which
is the safe rollback state.

Restoring implicit pickup requires an explicit operator-approved replacement
migration. Destructive rollback requires restoring a database backup or an
explicit rebuild; Attempt, lifecycle, and runtime-lease audit history must not be
dropped casually.
