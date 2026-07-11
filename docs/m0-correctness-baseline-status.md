# Milestone 0 Correctness Baseline Status

> Decision date: 2026-07-11  
> Scope: atomic permission, Task/Attempt schema, and PR-3 through validator managed-process foundations

## Decision

The **Milestone 0 implementation gate is closed** once the exact PR head passes
the complete test suite and compile validation.

The **Milestone 0 deployment gate remains pending** until
`level2_validator_process_lifecycle_v1` is applied to the target runtime database
and its verification output confirms the role-aware process schema and zero
unexpected active validator processes.

Until that deployment verification is complete:

```text
milestone_0_implementation_gate = closed
milestone_0_deployment_gate = pending
level_2_eligible = false
```

This distinction is intentional. Repository implementation and CI evidence can
close the code gate, but they cannot prove that a specific VPS database has been
migrated.

## Implemented correctness baseline

The closed implementation baseline contains:

- atomic-write permission and orphan-evidence policy;
- stable Task identity and the one-active-attempt constraint;
- append-only lifecycle evidence;
- Atomic attempt claim, explicit execution ownership, runtime lease, and
  heartbeat;
- canonical runtime admission with implicit pickup disabled;
- Attempt-scoped branch, worktree, lock, PID, and artifact resources;
- fresh-worktree retry identity;
- legal forward-only Attempt lifecycle transitions;
- explicit timeout, abort, and validation-failure outcomes;
- persisted pause and kill controls;
- exact Attempt-bound process launch preflight;
- hard process-group termination and verified exit for executors and validators;
- atomic reset lineage, idempotency, and concurrent reset compare-and-set.

## Atomic-write slice

The following behavior is implemented and protected by regression tests:

- Existing regular files preserve their complete permission bits when replaced.
- New files use `0o666` creation mode subject to the process umask.
- A standard `0o022` umask therefore produces a `0o644` file.
- Executable permission bits on an existing regular file are preserved.
- Temporary files are created in the target directory and replaced with
  `os.replace(...)`.
- File data is flushed and fsynced before replacement; directory fsync is
  best-effort after replacement.
- A symlink target is not followed.
- Normal exceptions attempt best-effort temporary-file cleanup.

Canonical atomic-temp policy:

- Never silently exclude atomic temp candidates from changed-files evidence.
- A candidate inside a task worktree must fail closed until explicitly inspected.
- Cleanup is a separate, explicit, human-confirmed, auditable operation.

## Task, Attempt, and runtime ownership

The persistence and runtime authority now include:

- stable Task identity and one-active-attempt constraint;
- token-authenticated Atomic attempt claim, heartbeat, and owned release;
- canonical runtime admission path for Dispatcher and ApprovedTaskRunner;
- executor and validator start evidence bound to the active Attempt, lease, and
  owner;
- fail-closed SQLite transition and ownership guards; and
- append-only lifecycle, control, process, and reset evidence.

Queued handoff and the GitHub Issue scheduler delegate to the same canonical
runner and cannot create independent ownership.

## Attempt resources and fresh retry

Each Attempt receives immutable, unique resources:

```text
branch
worktree
artifact root
runtime lock
PID manifest
managed process records
```

A reset transaction reserves exactly one new Attempt. Canonical admission adopts
that reserved identity and can create a fresh worktree without creating a second
retry Attempt. Historical branches, worktrees, artifacts, manifests, process
records, and audit events are retained.

## Lifecycle and control plane

The active lifecycle is forward-only:

```text
created -> preparing -> implementing -> validating -> waiting_approval
```

Distinct terminal outcomes include:

```text
validation_failed
execution_timeout
execution_aborted
blocked
failed
completed
canceled
```

Pause denies new matching admission. Kill controls are hard for managed executor
and validator process groups: identity is verified, SIGTERM is attempted,
SIGKILL escalation is available, descendants are included, and success requires
verified group exit.

## Shared managed process boundary

Executors and external-command validators share one Attempt-scoped process
registry with:

- `process_role = executor | validator`;
- one active managed runtime process per Attempt;
- exact Attempt worktree and artifact binding;
- `shell=false`, `start_new_session=true`, and `close_fds=true`;
- PID, PGID, session ID, and Linux `/proc` start-tick identity;
- SIGTERM-to-SIGKILL escalation;
- descendant cleanup;
- whole-group verified exit; and
- append-only role-aware process events.

Managed validator commands are:

```text
pytest
OpenSpec
lint
typecheck
changed-files git status
```

Unbound local `ValidatorContext` callers retain the historical synchronous
subprocess path. Canonical Dispatcher and ApprovedTaskRunner contexts use the
managed boundary.

This is process-lifecycle isolation, not a security sandbox. It does not provide
containers, cgroups, seccomp, namespaces, credential isolation, resource quotas,
or network isolation.

## Reset lineage and concurrent retry reservation

The reset transaction provides:

- monotonic `tasks.reset_generation`;
- canonical enforcement that raw `blocked -> queued` updates require reset
  lineage;
- exact expected-generation compare-and-set;
- optional expected old-Attempt validation;
- exactly one reserved retry Attempt;
- append-only old/new Attempt lineage;
- stable request-ID idempotency;
- one-winner/one-rejection behavior for simultaneous reset requests; and
- runtime adoption of the reserved Attempt without a second retry identity.

## Exit-gate reconciliation

| Exit-gate item | Status | Evidence |
| --- | --- | --- |
| Atomic permission and orphan policy | **Passed** | Atomic-write and policy regressions. |
| One active Attempt and owned runtime admission | **Passed after migration** | Transactional claim, leases, owner/token guards. |
| Attempt-scoped resources and fresh worktree retry | **Passed after migration** | Unique resources and reserved retry adoption. |
| Legal lifecycle and distinct terminal outcomes | **Passed after migration** | SQLite graph and runtime outcome tests. |
| Hard executor termination with verified exit | **Passed after migration** | Identity-checked process-group tests. |
| Validator subprocesses join the same hard-termination boundary | **Passed in implementation** | Real pytest/OpenSpec/lint/typecheck/changed-files integration and process tests. |
| Concurrent reset creates only one retry reservation | **Passed after migration** | Reset-generation CAS and one-active-Attempt index. |
| Complete repository test suite and compile validation | **Required for merge** | Exact PR head GitHub Actions is authoritative. |
| Target VPS validator-process migration | **Pending deployment** | Must run and verify `migrate_validator_process_lifecycle.py`. |

## Canonical status

```text
atomic_permission_slice = closed
atomic_temp_policy = closed
task_attempt_schema = implemented
one_active_attempt_constraint = implemented
atomic_attempt_claim = implemented_after_migration
execution_ownership = implemented_after_migration
runtime_lease = implemented_after_migration
runtime_heartbeat = explicit_token_supervised_after_migration
implicit_status_pickup = disabled
attempt_scoped_resources = implemented_after_migration
fresh_worktree_retry_identity = implemented
attempt_transition_graph = implemented_after_migration
timeout_abort_validation_outcomes = implemented
runtime_pause = persisted_admission_only
runtime_kill_switch = hard_for_managed_runtime_processes
executor_process_group = implemented
validator_process_group = implemented
hard_process_group_kill = implemented
verified_runtime_process_exit = implemented
reset_generation = implemented_after_migration
reset_lineage = implemented
concurrent_reset_cas = implemented
reserved_retry_attempt_adoption = implemented
milestone_0_implementation_gate = closed
milestone_0_deployment_gate = pending
level_2_eligible = false
```

## Deployment and verification

Apply the latest additive migration from the repository root:

```bash
python3 scripts/migrate_validator_process_lifecycle.py \
  --db-path /absolute/path/state.db
```

Expected verification characteristics:

```text
migration = level2_validator_process_lifecycle_v1
migration_recorded = true
process_role_column_installed = true
active_validator_processes = 0
termination.verified_exit_required = true
termination.shared_registry = executor_processes
```

After this output is confirmed on the target VPS, the operational status becomes:

```text
milestone_0_implementation_gate = closed
milestone_0_deployment_gate = closed
level_2_eligible = true
```

Reverting application code while ownership, lifecycle, process, and reset
triggers remain installed causes unsupported legacy actions to fail closed. A
destructive rollback requires a database backup or explicit rebuild; audit
history must not be dropped casually.
