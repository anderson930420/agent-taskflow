# Milestone 0 Correctness Baseline Status

> Decision date: 2026-07-11  
> Scope: PR-1 atomic permission status confirmation and Milestone 0 reconciliation

## Decision

The atomic-write permission and orphan-audit slice is closed.

The overall Level 2 Milestone 0 exit gate is **not complete**. It remains blocked
on the Task/Attempt model, atomic execution ownership, and fresh-worktree retry
semantics. This document must not be used as evidence that Level 2 Milestone 0
has passed.

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
regression suite is `tests/test_atomic_write.py`; PR-1 adds explicit closeout
coverage in `tests/test_m0_correctness_baseline_status.py`.

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

## Milestone 0 exit-gate reconciliation

| Exit-gate item | Status | Evidence or blocker |
| --- | --- | --- |
| New-file and overwrite permission tests pass | **Passed** | Atomic-write implementation plus mode regression tests. |
| Concurrent reset cannot create two active attempts | **Blocked** | The current data model has no canonical Attempt entity or one-active-attempt constraint. |
| Reset can successfully rerun with correct retry identity | **Blocked** | Legacy `blocked -> queued` status reset does not create a new attempt identity. |
| Retry uses destroy-and-recreate worktree semantics | **Blocked** | Current legacy behavior can retain/reuse the prior task worktree. |
| Reset and atomic write have authoritative audit evidence | **Partial** | Atomic behavior and orphan audit are documented; reset evidence is not yet attempt-scoped. |
| Existing full test suite is green | **Required per PR** | GitHub Actions on the exact PR head is the authority. |

## Remaining blockers and ownership

Milestone 0 can be closed only after the following foundations land:

1. Task/Attempt/lifecycle schema, migration, and a one-active-attempt constraint.
2. Atomic attempt claim with explicit execution ownership.
3. A canonical runtime admission path that all runners use.
4. Attempt-scoped branch, worktree, lock, PID, and artifact resources.
5. Retry/reset semantics that close the prior attempt, create a new attempt, and
   create a fresh worktree without overwriting historical evidence.

Until those items pass their own regression tests, the canonical status is:

```text
atomic_permission_slice = closed
atomic_temp_policy = closed
milestone_0 = open_blocked
level_2_eligible = false
```

## Migration and rollback

This PR introduces no schema migration and changes no runtime behavior.

Rollback consists of reverting this status document and its regression test.
Reverting it must not be interpreted as reverting the atomic-write safety
implementation or the no-exclusion policy, which were established by earlier
merged changes.
