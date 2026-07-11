# Lifecycle Transition and Runtime Control Plane

> Decision date: 2026-07-11  
> Scope: PR-6 Attempt lifecycle graph, outcome classification, pause, cooperative kill, and reason codes

## Status

```text
attempt_transition_graph = implemented
attempt_transition_guard = implemented
timeout_outcome = execution_timeout
abort_outcome = execution_aborted
validation_failure_outcome = validation_failed
pause_switch = persisted_admission_only
kill_switch = persisted_cooperative
reason_code_taxonomy = closed_for_pr6_runtime_outcomes
os_signal_kill = not_implemented_in_this_pr
process_group_termination = not_implemented_in_this_pr
milestone_0 = open_blocked
level_2_eligible = false
```

## Attempt transition graph

The authoritative active path is:

```text
created -> preparing -> implementing -> validating -> waiting_approval
```

The runtime claim normally creates an Attempt directly in `preparing`. The
`created` node remains supported for lower-level AttemptStore callers and tests.
The graph allows a forward skip only where an existing bounded workflow already
performs more than one phase atomically. It never permits a backward edge such
as `validating -> implementing`, and terminal Attempts cannot be reopened.

Every active phase may close into an appropriate terminal outcome:

```text
waiting_approval
validation_failed
execution_timeout
execution_aborted
blocked
failed
completed
canceled
```

The migration installs the graph in `lifecycle_allowed_transitions` and a SQLite
trigger rejects any status update outside that graph, including direct SQL that
bypasses the Python API.

## Task and Attempt projection

The Attempt is the execution authority. Task status is a projection used by the
local mirror:

| Attempt status | Task status |
| --- | --- |
| `preparing` | `preparing` |
| `implementing` | `implementing` |
| `validating` | `validating` |
| `waiting_approval` | `waiting_approval` |
| `validation_failed` | `blocked` |
| `execution_timeout` | `blocked` |
| `execution_aborted` | `blocked` |
| `failed` / `blocked` | `blocked` |
| `completed` | `completed` |
| `canceled` | `canceled` |

An active transition updates the Attempt, projected Task, lifecycle event, and
task event in one transaction under the same owner/token lease.

## Outcome classification

Executor and validator results remain constrained by their existing public
result vocabularies. PR-6 classifies those results at the canonical runtime
boundary:

- executor timeout evidence closes the Attempt as `execution_timeout` with
  `execution_result=timed_out`;
- executor failure closes as `failed`;
- validator failure closes as `validation_failed` with
  `validation_result=failed`;
- expired ownership and an observed cooperative kill close as
  `execution_aborted`;
- successful implementation and validation close as `waiting_approval` with
  execution `completed` and validation `passed`.

Timeout detection includes the existing Claude Code artifact/result contract,
which reports a constrained executor status of `failed` while retaining an
explicit `timed_out` artifact and timeout summary.

## Reason codes

`reason_code` is a stable machine identifier. Human explanation belongs in the
message or metadata and must not be substituted for the reason code.

PR-6 runtime outcomes use a closed taxonomy including:

```text
runtime_preparing
runtime_implementing
runtime_validating
runtime_waiting_approval
runtime_completed
runtime_canceled
executor_failed
executor_timeout
executor_aborted
executor_blocked
validator_failed
validator_timeout
validator_blocked
operator_pause_requested
operator_pause_cleared
operator_kill_requested
operator_kill_cleared
runtime_lease_expired
runtime_internal_error
runtime_governance_blocked
attempt_resource_allocation_failed
```

Every control change is append-only in `runtime_control_events`, while
`runtime_controls` stores the current value and monotonic generation for each
scope.

## Control scopes

Controls may target:

```text
global  -> *
task    -> <task-key>
attempt -> <attempt-id>
```

Effective precedence is severity-based rather than last-writer-wins:
`kill_requested` overrides `paused`, which overrides `running`. Global, task,
and Attempt controls are all evaluated.

## Pause semantics

Pause is admission-only:

- new claims matching the paused scope are denied;
- an already active Attempt is not suspended or aborted;
- the heartbeat supervisor continues;
- clearing pause permits future claims.

This deliberately avoids claiming that arbitrary subprocess execution can be
safely frozen and resumed.

## Kill semantics

Kill is cooperative:

- a new matching claim is denied;
- active runtimes inspect the switch immediately before and after executor and
  validator calls;
- an observed kill records `operator_kill_requested` and closes the Attempt as
  `execution_aborted` at the next boundary;
- if observed before a worker call, the worker is not invoked.

PR-6 does not send a signal, create or terminate a process group, kill child
processes, or prove that a process has exited. CLI output and lifecycle metadata
explicitly state `os_signals_sent=false`. Hard termination belongs to the later
process-lifecycle change.

## Deployment

Run from the repository root:

```bash
python3 scripts/migrate_lifecycle_control.py \
  --db-path "$HOME/.agent-taskflow/state.db"
```

The command applies the PR-2 through PR-5 prerequisites before installing
`level2_lifecycle_control_v1`.

Inspect the effective global control:

```bash
python3 scripts/runtime_control.py status \
  --db-path "$HOME/.agent-taskflow/state.db"
```

Pause new global admission:

```bash
python3 scripts/runtime_control.py pause \
  --db-path "$HOME/.agent-taskflow/state.db" \
  --actor operator
```

Request cooperative termination for one task:

```bash
python3 scripts/runtime_control.py kill \
  --db-path "$HOME/.agent-taskflow/state.db" \
  --scope-kind task \
  --scope-id AT-EXAMPLE-1 \
  --actor operator
```

Clear the same control:

```bash
python3 scripts/runtime_control.py clear \
  --db-path "$HOME/.agent-taskflow/state.db" \
  --scope-kind task \
  --scope-id AT-EXAMPLE-1 \
  --actor operator
```

## Remaining M0 blockers

PR-6 closes the legal lifecycle graph and cooperative stop-control foundation.
Milestone 0 remains open for:

- process-group creation, signal escalation, descendant termination, and
  post-termination verification;
- crash recovery that correlates process-group evidence with lease and PID
  manifests;
- reset audit events bound to both the closed and newly created Attempt; and
- concurrent reset compare-and-set semantics and regression coverage.
