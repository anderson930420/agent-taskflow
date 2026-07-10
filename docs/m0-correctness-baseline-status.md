# Milestone 0 Correctness Baseline Status

> Decision date: 2026-07-11  
> Scope: atomic permission closeout plus PR-2 Task/Attempt schema reconciliation

## Decision

The atomic-write permission and orphan-audit slice is closed.

PR-2 implements the Task/Attempt/lifecycle persistence foundation and the
SQLite one-active-attempt constraint. The overall Level 2 Milestone 0 exit gate
is **not complete** because reset and runtime pickup do not yet use that
foundation, and fresh-worktree retry semantics are not implemented. This
document must not be used as evidence that Level 2 Milestone 0 has passed.

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

## Task/Attempt persistence foundation: implemented, not yet adopted by runtime

PR-2 adds:

- stable `task_id`, `task_class`, `active_attempt_id`, final-outcome, close-time,
  and legacy markers on mirrored tasks;
- `attempts` with unique `(task_id, attempt_number)` identity;
- partial unique index `ux_attempts_one_active_per_task`;
- append-only `lifecycle_events` with task/attempt integrity guards;
- additive legacy migration that does not synthesize historical attempts; and
- transactional create/close storage operations.

This foundation does not yet make reset or executor pickup Attempt-safe. The
legacy execution paths can still bypass it until the canonical admission and
claim PRs land.

## Milestone 0 exit-gate reconciliation

| Exit-gate item | Status | Evidence or blocker |
| --- | --- | --- |
| New-file and overwrite permission tests pass | **Passed** | Atomic-write implementation plus mode regression tests. |
| Concurrent reset cannot create two active attempts | **Partial** | The SQLite one-active-attempt constraint and concurrent create tests now exist, but the reset command does not yet create Attempts through that transaction. |
| Reset can successfully rerun with correct retry identity | **Blocked** | Legacy `blocked -> queued` status reset does not close the prior Attempt or create a new Attempt identity. |
| Retry uses destroy-and-recreate worktree semantics | **Blocked** | Current legacy behavior can retain/reuse the prior task worktree. |
| Reset and atomic write have authoritative audit evidence | **Partial** | Atomic behavior, orphan audit, and append-only lifecycle storage exist; reset evidence is not yet Attempt-scoped. |
| Existing full test suite is green | **Required per PR** | GitHub Actions on the exact PR head is the authority. |

## Remaining blockers and ownership

Milestone 0 can be closed only after the following runtime foundations land:

1. Atomic attempt claim using the new one-active-attempt constraint and explicit
   execution ownership.
2. A canonical runtime admission path that all runners use.
3. Attempt-scoped branch, worktree, lock, PID, and artifact resources.
4. Retry/reset semantics that close the prior attempt, create a new attempt, and
   create a fresh worktree without overwriting historical evidence.
5. Migration of legacy executor entrypoints so every executor run obtains a
   unique `attempt_id` before side effects.

Until those items pass their own regression tests, the canonical status is:

```text
atomic_permission_slice = closed
atomic_temp_policy = closed
task_attempt_schema = implemented
one_active_attempt_constraint = implemented
runtime_attempt_admission = open_blocked
milestone_0 = open_blocked
level_2_eligible = false
```

## Migration and rollback

PR-2 introduces an additive forward-only schema migration. Application rollback
leaves the new tables and columns inert while legacy mirror code continues to
use its original schema. Destructive rollback requires restoring a pre-migration
database backup or an explicit operator-approved rebuild; Attempt and lifecycle
audit history must not be dropped casually.
