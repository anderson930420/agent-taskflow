# Milestone 0 Correctness Baseline Status

> Decision date: 2026-07-11  
> Scope: atomic permission, Task/Attempt schema, and PR-3 through PR-8 runtime foundations

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
PR-7 adds exact Attempt-bound ExecutorLaunchSpec preflight, isolated POSIX
sessions/process groups, PID/PGID/session/start-tick identity, SIGTERM/SIGKILL
escalation, descendant cleanup, external hard termination, and verified exit.
PR-8 adds an atomic reset transaction that binds the latest closed Attempt to
exactly one reserved retry Attempt, a monotonic reset generation, append-only
reset audit, idempotent request IDs, and concurrent compare-and-set rejection.

The overall Level 2 Milestone 0 exit gate is **not complete** because the final
scope decision for validator subprocess hard termination remains open. Reset
lineage and concurrent reset correctness are no longer blockers after the PR-8
migration. This document must not be used as evidence that Milestone 0 has
passed or that Level 2 is eligible.

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

## Task/Attempt persistence and ownership: implemented

The persisted foundation now includes:

- stable Task identity and one-active-attempt constraint;
- append-only lifecycle events;
- token-authenticated Atomic attempt claim, lease, heartbeat, and owned release;
- canonical runtime admission with implicit pickup disabled;
- executor-start evidence bound to the active Attempt/lease/owner; and
- fail-closed transition guards at the SQLite boundary.

The direct executor roots are `ApprovedTaskRunner` and `Dispatcher`. Package
bootstrap wraps both. Queued handoff and the GitHub Issue scheduler delegate to
the same canonical runner and cannot create independent ownership.

## Attempt resources, controls, and executor processes: implemented

PR-5 allocates immutable Attempt-scoped branch, worktree, artifact, lock, and PID
resources. Each new claim can create a fresh worktree and cannot overwrite a
prior Attempt's runtime evidence.

PR-6 migration `level2_lifecycle_control_v1` adds:

- a forward-only Attempt transition graph enforced by SQLite;
- explicit `execution_timeout`, `execution_aborted`, and `validation_failed`
  terminal outcomes;
- persisted global/task/Attempt pause and kill controls; and
- stable machine-readable reason codes separate from human explanation.

PR-7 migration `level2_executor_process_lifecycle_v1` adds:

- exact Attempt/lease/owner launch binding and preflight;
- `shell=false`, `start_new_session=true`, and `close_fds=true`;
- one active managed executor process per Attempt;
- Linux PID, PGID, session ID, and `/proc` start-tick identity;
- append-only process events and legal process-state transitions;
- SIGTERM grace followed by SIGKILL escalation;
- descendant cleanup; and
- verified exit only when no live process remains in the stored PGID/session.

PR-7 is process-lifecycle isolation, not a container or network sandbox.

## Reset lineage and concurrent retry reservation: implemented after migration

PR-8 migration `level2_reset_lineage_v1` adds:

- monotonic `tasks.reset_generation`;
- canonical enforcement that raw `blocked -> queued` updates require a reset
  lineage reservation;
- one transaction that verifies the blocked task, absence of active ownership,
  expected generation, and optional expected old Attempt;
- insertion of exactly one new active Attempt in `created` state;
- compare-and-set of the task to `queued`, binding the new Attempt and
  incrementing the reset generation;
- append-only `reset_lineages` and `reset_lineage_events` evidence;
- stable request-ID idempotency;
- one-winner/one-rejection behavior for simultaneous reset requests; and
- canonical runtime adoption of the reserved Attempt through
  `created -> preparing`, without creating a second retry identity.

The next runtime allocation still creates a fresh worktree, branch, artifact
root, lock, PID evidence, and managed executor process for the reserved Attempt.
No historical branch, worktree, artifact, process record, or audit event is
deleted by reset.

## Milestone 0 exit-gate reconciliation

| Exit-gate item | Status | Evidence or blocker |
| --- | --- | --- |
| New-file and overwrite permission tests pass | **Passed** | Atomic-write implementation plus mode regression tests. |
| Runtime concurrent pickup cannot create two active attempts | **Passed after migration** | One active Attempt and one active token lease are enforced transactionally. |
| Concurrent reset cannot create two active attempts | **Passed after PR-8 migration** | Reset generation CAS and the one-active-Attempt index permit one reservation winner; the loser fails closed and is audited. |
| Executor start requires active ownership | **Passed after migration** | SQLite requires matching canonical claim metadata and a live token lease. |
| Canonical runtime admission path is used by current executor roots | **Passed after migration** | Dispatcher and ApprovedTaskRunner use the claim-aware store; handoff and scheduler delegate to the wrapped runner. |
| Attempt lifecycle uses a legal forward-only transition graph | **Passed after PR-6 migration** | SQLite rejects backward and terminal-reopen Attempt status edges. |
| Timeout, abort, and validation failure remain distinguishable | **Passed after PR-6 migration** | Canonical runtime release persists explicit Attempt outcomes and reason codes. |
| Operator can pause new admission | **Passed after PR-6 migration** | Persisted pause controls deny new matching claims while active Attempts continue. |
| Operator can request hard executor termination | **Passed after PR-7 migration** | Managed process groups use identity-checked SIGTERM/SIGKILL escalation and require verified exit. |
| Executor descendants cannot survive leader completion unnoticed | **Passed after PR-7 migration** | Live members in the stored PGID/session are terminated and reported as executor failure. |
| Reset can successfully rerun with correct retry identity | **Passed after PR-8 migration** | The reset transaction reserves the new Attempt and canonical admission adopts that exact identity before allocating fresh resources. |
| Retry uses fresh Attempt worktree semantics | **Passed after PR-5/PR-8 migrations** | The reserved retry identity receives a new worktree and cannot reuse the prior Attempt path. |
| Reset and atomic write have authoritative audit evidence | **Passed after PR-8 migration** | Old/new Attempt lineage, generation, actor, reason, idempotency key, CAS rejection, and supplemental JSON evidence are retained. |
| Validator subprocess hard termination is within the final M0 boundary | **Open scope decision** | Primary executors are managed; validator subprocesses still use their historical execution paths. |
| Existing full test suite is green | **Required per PR** | GitHub Actions on the exact PR head is the authority. |

## Remaining blocker and ownership

Milestone 0 can be closed only after one explicit decision is recorded and, when
required, implemented:

1. Decide whether validator subprocesses must join the PR-7 managed
   process-group hard-termination boundary before Level 2 eligibility.
2. If yes, implement validator launch binding, signal escalation, descendant
   cleanup, and verified exit with the same fail-closed evidence model.
3. If no, document and test why validators are outside the Milestone 0 process
   boundary.

Until that decision is closed, the canonical status is:

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
runtime_kill_switch = hard_for_managed_executors_after_pr7
executor_launch_spec = implemented_after_pr7_migration
executor_process_group = implemented
hard_process_group_kill = implemented_for_managed_executors
verified_executor_exit = implemented
reset_generation = implemented_after_pr8_migration
reset_lineage = implemented
concurrent_reset_cas = implemented
reserved_retry_attempt_adoption = implemented
validator_process_group = scope_open
milestone_0 = open_blocked
level_2_eligible = false
```

## Migration and rollback

Deployments apply the latest additive migration command from the repository root:

```bash
python3 scripts/migrate_reset_lineage.py \
  --db-path /absolute/path/state.db
```

The command installs the Task/Attempt, runtime admission, canonical admission,
Attempt resource, lifecycle-control, executor-process, and reset-lineage
prerequisites in order.

Reverting application code while canonical reset and runtime triggers remain
installed causes raw legacy resets and unsupported transitions to fail closed,
which is the safe rollback state. Destructive rollback requires a database
backup or an explicit rebuild; Attempt, lifecycle, lease, resource, control,
process, reset lineage, and CAS rejection audit history must not be dropped.
