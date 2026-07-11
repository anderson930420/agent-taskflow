# Managed Executor Launch and Process-Group Lifecycle

> Decision date: 2026-07-11  
> Scope: PR-7 `ExecutorLaunchSpec`, launch preflight, process groups, hard termination, and verified exit

## Status

```text
executor_launch_spec = implemented
canonical_launch_binding = implemented
launch_preflight = implemented
shell_false = enforced
start_new_session = enforced
close_fds = enforced
process_group_identity = pid_pgid_session_start_ticks
sigterm_sigkill_escalation = implemented
verified_group_exit = implemented
external_hard_termination = implemented
stale_process_recovery_cli = implemented
validator_process_groups = not_implemented_in_this_pr
container_isolation = not_implemented_in_this_pr
network_isolation = not_implemented_in_this_pr
milestone_0 = open_blocked
level_2_eligible = false
```

## Canonical versus compatibility execution

PR-7 adds `ExecutorLaunchBinding` to `ExecutorContext`.

Only a canonical Attempt runtime may create this binding. It includes the exact:

- database path;
- Task and Attempt identities;
- runtime lease and owner identities;
- Attempt worktree path; and
- Attempt artifact root.

A bound executor must use the managed launch path. A context without a binding
retains the historical synchronous subprocess path for local utilities and
unit-test fixtures. This compatibility path is not evidence of Level 2 runtime
isolation.

## ExecutorLaunchSpec

Every managed launch writes a redacted `executor_launch_spec.v1` artifact beneath
the Attempt artifact root. It records:

- executor name and redacted argv;
- exact cwd and artifact root;
- timeout and signal grace periods;
- stdin mode;
- environment key names, never values;
- `shell=false`;
- `start_new_session=true`;
- `close_fds=true`; and
- the explicit absence of network isolation.

Prompt or mission text passed through argv by OpenCode and Pi is replaced with
`<redacted>` in persisted launch evidence. Claude Code continues to receive its
prompt over stdin rather than argv.

## Preflight

A process is not started unless preflight proves all of the following:

1. The platform is POSIX and exposes Linux `/proc` plus `os.killpg`.
2. The executable is an absolute executable file or resolves on `PATH`.
3. The launch cwd exactly matches the active Attempt worktree.
4. The artifact directory exactly matches the active Attempt artifact root.
5. The Attempt and runtime lease are active.
6. Task, Attempt, lease, and owner identities all match.
7. The persisted Attempt resource paths match the launch binding.
8. No other active executor process exists for the Attempt.

Preflight failure creates an auditable `preflight_failed` process record with no
PID and sends no signal.

## Process-group identity

Managed executors are launched with `start_new_session=True`. The leader is
therefore expected to satisfy:

```text
pid == pgid == session_id
```

The runtime persists:

- leader PID;
- process-group ID;
- session ID;
- Linux `/proc/<pid>/stat` start ticks;
- process state and timestamps;
- signal escalation evidence; and
- verified-exit status.

Before any external signal is sent, the stored identity is compared with `/proc`.
A reused PID, mismatched PGID/session, or mismatched start tick fails closed and
creates an `executor_process_identity_mismatch` audit event.

## Timeout and hard termination

Timeout and operator kill use the same deterministic escalation:

```text
verify identity
  -> SIGTERM to the process group
  -> wait terminate grace
  -> SIGKILL to the process group when still live
  -> wait kill grace
  -> verify no live process remains in the stored PGID/session
```

A zombie leader does not count as a live process, but live descendants do. The
leader is reaped by the launcher when it is the direct child. A successful
termination requires `verified_exit=true`; merely sending a signal is not
success.

If the leader exits normally while descendants remain, PR-7 treats that as a
lifecycle violation, terminates the remaining group, and reports executor
failure rather than silently leaving orphan work.

## PR-6 kill-switch integration

The managed launcher polls the persisted PR-6 global, Task, and Attempt controls
while the executor is running. A matching `kill_requested` value triggers hard
process-group termination. The surrounding lifecycle runtime then closes the
Attempt as `execution_aborted` with `operator_kill_requested`.

The external termination CLI also writes the Attempt-scoped kill control before
sending signals. This ensures the returning executor result cannot be
misclassified as an ordinary nonzero exit.

## Persistence

`executor_processes` stores the current process lifecycle record. At most one
record may be active for an Attempt.

`executor_process_events` is append-only. SQLite triggers reject UPDATE and
DELETE. The legal process states are:

```text
allocated
preflight_failed
start_failed
running
term_sent
kill_sent
exited
exit_unverified
```

A SQLite transition guard rejects illegal backward or terminal-reopen state
changes.

## Covered executors

Canonical managed launch is implemented for the primary worker process of:

- Shell;
- OpenCode;
- Pi; and
- Claude Code real invocation.

Short-lived Git evidence capture commands remain outside the managed worker
record. Validator subprocess migration is explicitly deferred.

## Deployment

Run from the repository root:

```bash
python3 scripts/migrate_executor_process_lifecycle.py \
  --db-path "$HOME/.agent-taskflow/state.db"
```

Inspect active managed processes:

```bash
python3 scripts/terminate_executor_process.py status \
  --db-path "$HOME/.agent-taskflow/state.db"
```

Hard-terminate one active Attempt process group:

```bash
python3 scripts/terminate_executor_process.py terminate \
  --db-path "$HOME/.agent-taskflow/state.db" \
  --attempt-id attempt-EXAMPLE \
  --actor operator
```

Terminate only process records whose lease is inactive or expired:

```bash
python3 scripts/terminate_executor_process.py reap-stale \
  --db-path "$HOME/.agent-taskflow/state.db" \
  --actor reaper
```

The command exits nonzero when identity cannot be proven or group exit cannot be
verified.

## Isolation boundary

PR-7 provides process-lifecycle isolation, not a security sandbox. It does not
provide:

- containers or mount namespaces;
- cgroups or resource quotas;
- seccomp;
- user namespaces;
- network isolation; or
- credential isolation from inherited environment variables.

Environment values are never persisted in launch artifacts, but executors still
inherit the environment required for configured credentials.

## Remaining M0 blockers

PR-7 closes the executor process-group creation, hard termination, descendant
cleanup, and verified-exit foundation. Milestone 0 remains open for:

- reset audit events that bind the closed Attempt and new retry Attempt;
- concurrent reset compare-and-set semantics; and
- validator process lifecycle if validators are included in the final M0 hard
  termination requirement.
