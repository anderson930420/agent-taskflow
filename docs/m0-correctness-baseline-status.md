# Milestone 0 Correctness Baseline Status

> Decision date: 2026-07-11  
> Scope: atomic permission, Task/Attempt schema, PR-3 through PR-6 runtime foundations

## Decision

The atomic-write permission and orphan-audit slice is closed.

PR-2 implements the Task/Attempt/lifecycle persistence foundation and the
SQLite one-active-attempt constraint. PR-3 adds Atomic attempt claim,
execution ownership, runtime lease, heartbeat, and fail-closed executor-start
guards. PR-4 installs the canonical runtime admission path and propagates one
explicit owner/token claim through Dispatcher, `ApprovedTaskRunner`, queued
runtime handoff, and the scheduler delegation chain.

PR-5 implements Attempt-scoped branch, worktree, lock, PID, and artifact
resources plus fresh-worktree retry identity. PR-6 implements a persisted legal
Attempt transition graph, explicit timeout/abort/validation-failed outcomes,
admission pause, cooperative kill switches, and stable runtime reason codes.

The overall Level 2 Milestone 0 exit gate is **not complete** because hard
process-group termination and verified crash recovery, dual-Attempt reset audit
binding, and concurrent reset compare-and-set semantics remain open. This
document must not be used as evidence that Milestone 0 has passed.

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

## Attempt resources and lifecycle controls: implemented after migration

PR-5 allocates immutable Attempt-scoped branch, worktree, artifact, lock, and PID
resources. Each new claim can create a fresh worktree and cannot overwrite a
prior Attempt's runtime evidence.

PR-6 migration `level2_lifecycle_control_v1` adds:

- a forward-only Attempt transition graph enforced by SQLite;
- atomic Attempt/Task projection for `preparing`, `implementing`, and
  `validating`;
- explicit `execution_timeout`, `execution_aborted`, and `validation_failed`
  terminal outcomes;
- persisted global/task/Attempt controls with append-only control events;
- pause semantics that deny new admission without suspending active work;
- cooperative kill checks at executor and validator boundaries; and
- stable machine-readable reason codes separate from human explanation.

A cooperative kill is not hard process termination. PR-6 sends no OS signal and
does not claim process-group or descendant termination authority.

## Milestone 0 exit-gate reconciliation

| Exit-gate item | Status | Evidence or blocker |
| --- | --- | --- |
| New-file and overwrite permission tests pass | **Passed** | Atomic-write implementation plus mode regression tests. |
| Runtime concurrent pickup cannot create two active attempts | **Passed after migration** | One active Attempt and one active token lease are enforced transactionally. |
| Concurrent reset cannot create two active attempts | **Partial** | The one-active-attempt constraint and Atomic attempt claim are authoritative, but reset lineage and concurrent reset CAS remain incomplete. |
| Executor start requires active ownership | **Passed after migration** | SQLite requires matching canonical claim metadata and a live token lease. |
| Canonical runtime admission path is used by current executor roots | **Passed after migration** | Dispatcher and ApprovedTaskRunner use the claim-aware store; runtime handoff and scheduler delegate to the wrapped runner. |
| Attempt lifecycle uses a legal forward-only transition graph | **Passed after PR-6 migration** | SQLite rejects backward and terminal-reopen Attempt status edges. |
| Timeout, abort, and validation failure remain distinguishable | **Passed after PR-6 migration** | Canonical runtime release persists explicit Attempt outcomes and reason codes. |
| Operator can pause new admission | **Passed after PR-6 migration** | Persisted pause controls deny new matching claims while active Attempts continue. |
| Operator can request hard process termination | **Blocked** | PR-6 kill is cooperative only; process groups, signals, descendants, and exit verification are not implemented. |
| Reset can successfully rerun with correct retry identity | **Blocked** | Legacy `blocked -> queued` reset does not yet bind the closed Attempt and new retry Attempt in one audited reset transaction. |
| Retry uses fresh Attempt worktree semantics | **Passed after PR-5 migration** | Each new claim can create a fresh worktree on a unique Attempt branch; terminal history is retained and a retry cannot reuse the prior Attempt path. |
| Reset and atomic write have authoritative audit evidence | **Partial** | Atomic behavior, orphan audit, leases, lifecycle storage, resources, and control events exist; reset evidence is not yet bound to old and new Attempts. |
| Existing full test suite is green | **Required per PR** | GitHub Actions on the exact PR head is the authority. |

## Remaining blockers and ownership

Milestone 0 can be closed only after the following foundations land:

1. Process-group creation, signal escalation, descendant termination, verified
   process exit, and crash recovery tied to lease/PID evidence.
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
attempt_transition_graph = implemented_after_pr6_migration
timeout_abort_validation_outcomes = implemented
runtime_pause = persisted_admission_only
runtime_kill_switch = persisted_cooperative
hard_process_group_kill = blocked
milestone_0 = open_blocked
level_2_eligible = false
```

## Migration and rollback

Deployments apply the latest additive migration command from the repository root:

```bash
python3 scripts/migrate_lifecycle_control.py \
  --db-path /absolute/path/state.db
```

The command installs the Task/Attempt, runtime admission, canonical admission,
Attempt resource, and lifecycle-control prerequisites in order.

Reverting application code while canonical ownership and lifecycle triggers
remain installed causes unsupported legacy transitions to fail closed, which is
the safe rollback state. Destructive rollback requires a database backup or an
explicit rebuild; Attempt, lifecycle, lease, resource, and control audit history
must not be dropped casually.
