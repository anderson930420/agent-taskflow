# Validator Managed Process Lifecycle

> Decision date: 2026-07-11  
> Scope: selected Option A — validators join the managed process-group boundary

## Decision

Every canonical validator command that launches an external process must use the
same Attempt-scoped process identity, launch preflight, signal escalation, and
verified-exit contract as a primary executor.

The shared registry distinguishes:

```text
process_role = executor | validator
```

Only one managed runtime process may be active for an Attempt at a time. This is
intentional: implementation finishes before validation starts, and validators
run sequentially under the current dispatcher/runner contract.

## Covered validator subprocesses

```text
pytest
openspec
lint
typecheck
changed-files git status
```

Pure in-process validators do not create an artificial process record.

## Launch and termination contract

A bound validator receives the active Task, Attempt, lease, owner, worktree, and
artifact identity. Preflight requires those values to match persisted canonical
runtime ownership. The process starts with:

```text
shell = false
start_new_session = true
close_fds = true
cwd = exact Attempt worktree
artifacts = exact Attempt artifact root
```

Timeout, running kill switch, and explicit operator termination use:

```text
verify PID / PGID / session / Linux start ticks
-> SIGTERM process group
-> grace period
-> SIGKILL process group when still live
-> scan /proc for all PGID/session members
-> require verified_exit = true
```

A validator leader exiting while descendants remain is not considered success.
The descendants are terminated and the Attempt fails closed with auditable
validator process evidence.

## Compatibility boundary

A plain `ValidatorContext` without a canonical launch binding preserves the
historical direct-subprocess behavior for bounded local tools and unit fixtures.
Canonical Dispatcher and ApprovedTaskRunner validation always inject the managed
binding.

## Operator surface

The existing process status/termination command operates on the shared registry:

```bash
python3 scripts/terminate_executor_process.py status \
  --db-path "$HOME/.agent-taskflow/state.db"
```

Each returned record includes `process_role`. A process or Attempt selector may
therefore target either an executor or validator group.

## Deployment

```bash
python3 scripts/migrate_validator_process_lifecycle.py \
  --db-path "$HOME/.agent-taskflow/state.db"
```

## Security boundary

This closes the runtime process-lifecycle boundary; it is not a security sandbox.
It does not provide containers, cgroups, seccomp, user/mount namespaces, resource
quotas, credential isolation, or network isolation.
