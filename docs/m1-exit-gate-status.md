# Milestone 1 Exit-Gate Reconciliation

> Decision date: 2026-07-12  
> Authority: `agent-taskflow-shortest-level2-roadmap-v2.md`  
> Scope: Attempt Model, canonical execution path, lifecycle correctness, and M1 operational rehearsals

## Decision

Milestone 0 is closed and deployed. Milestone 1 is **substantially implemented but not closed**.

The current repository has strong Attempt identity, lifecycle, resource isolation,
process termination, reset lineage, and append-only audit foundations. Those
foundations do not by themselves satisfy every M1 exit gate in the Level 2
Roadmap. In particular, M1 also requires production-copy migration/rollback
evidence, a zero-mismatch dual-write observation window, deployed cleanup and
pause rehearsals, project/class controls, and a canonical ExecutionEngine path
for Level 2-eligible work.

Canonical status:

```text
m0_exit_gate = passed
m1_exit_gate = blocked
m2_entry_allowed = false
shadow_mode_ready = false
auto_merge_eligible = false
```

This document must not be used to claim that Roadmap PR-9, M2, Shadow Mode, or
auto-merge is ready.

## Machine-readable audit

Run the read-only audit from the repository root:

```bash
python3 scripts/audit_m1_exit_gate.py \
  --db-path "$HOME/.agent-taskflow/state.db" \
  --repo-root "$PWD"
```

The command:

- opens the SQLite database in read-only mode;
- does not apply migrations or modify runtime state;
- reports each gate as `passed`, `partial`, `blocked`, or `not_applicable`;
- keeps `m2_entry_allowed=false` unless all gates pass;
- always keeps `shadow_mode_ready=false` and `auto_merge_eligible=false`, because
  those are later milestone decisions.

Use `--require-passed` in CI or a promotion check when a non-zero exit is desired
for any incomplete gate.

## Reconciled gate matrix

| M1 exit gate | Current status | Current authority | Required closeout evidence |
| --- | --- | --- | --- |
| Production DB-copy migration dry-run, integrity check, rollback rehearsal | **Blocked pending evidence** | Additive migrations exist, but production-copy rehearsal evidence is not stored | `production-db-copy-rehearsal.json` |
| Dual-write consistency audit has zero mismatch in a bounded window | **Blocked pending evidence** | Attempt/event model exists; no authoritative observation-window evidence | `dual-write-consistency.json` |
| One task produces at least three non-overwriting Attempts | **Partial until deployed rehearsal** | Unique Attempt, branch, worktree, and artifact constraints | Three distinct rows in `attempt_resources` for one task |
| Timeout/abort clears PID, releases lock, applies worktree cleanup policy, and verifies exit | **Blocked pending drill** | Managed process groups and resource release exist | `timeout-abort-cleanup.json` |
| Lifecycle timeline can be reconstructed from events | **Partial or passed from DB** | `lifecycle_events` are append-only | Every Attempt has a continuous event chain ending at persisted status |
| Illegal lifecycle transition is rejected | **Passed after migration** | `lifecycle_attempt_transition_guard` | Trigger and allowlist table remain installed |
| Pause prevents new pickup | **Partial pending deployed rehearsal** | Global/task/Attempt pause controls exist | `pause-admission-rehearsal.json` |
| Project admission pause and task-class auto-merge eligibility can be disabled immediately | **Blocked in production; code/rehearsal implementation pending deployment review** | M1-D adds independent project and class-global scopes without enabling auto-merge | Deploy `level2_project_class_controls_v1`, then provide DB-/SHA-bound `project-class-control-rehearsal.json` |
| ExecutionEngine parity passes and it is the repository-wide Level 2 authority | **Passed only with v2 M1-C evidence for the audited SHA** | Scheduler, direct scripts, dispatcher/API, queued/runtime/one-shot callbacks, downstream pipelines, and PR handoff share the Level 2 guard and exact-Attempt verifier | `canonical-execution-path.json` produced by `scripts/run_m1_canonical_execution_path_rehearsal.py` |
| Legacy schema and reader remain available until M1 closes | **Passed** | `tasks.is_legacy` and legacy observability fallback reader | Keep both until final M1 closeout |

## External evidence contracts

Evidence files are operator-produced facts. The audit does not invent them and
does not infer them from prose.

### `production-db-copy-rehearsal.json`

```json
{
  "schema_version": "m1_production_db_copy_rehearsal.v1",
  "migration_dry_run": true,
  "integrity_check": true,
  "rollback_rehearsal": true
}
```

The real artifact should also retain source/copy identifiers, timestamps,
commands, migration list, integrity output, rollback steps, actor, and any
backup/checksum references.

### `dual-write-consistency.json`

```json
{
  "schema_version": "m1_dual_write_consistency.v1",
  "observation_window_started_at": "...",
  "observation_window_ended_at": "...",
  "records_compared": 1,
  "mismatch_count": 0,
  "silent_failure_count": 0
}
```

### `timeout-abort-cleanup.json`

```json
{
  "schema_version": "m1_timeout_abort_cleanup.v1",
  "timeout_pid_cleared": true,
  "timeout_lock_released": true,
  "timeout_worktree_cleanup_verified": true,
  "timeout_verified_exit": true,
  "abort_pid_cleared": true,
  "abort_lock_released": true,
  "abort_worktree_cleanup_verified": true,
  "abort_verified_exit": true
}
```

`worktree_cleanup_verified` means the documented retention/cleanup policy was
applied and verified. It does not silently authorize deletion of historical
evidence.

### `pause-admission-rehearsal.json`

```json
{
  "schema_version": "m1_pause_admission_rehearsal.v1",
  "new_pickup_denied": true,
  "existing_attempt_not_suspended": true,
  "pause_cleared": true
}
```

### `canonical-execution-path.json`

```json
{
  "schema_version": "m1_canonical_execution_path.v2",
  "canonical_path": "ExecutionEngine",
  "repo_sha": "<audited git HEAD>",
  "deterministic_fixture": true,
  "production_db_mutated": false,
  "real_executor_invoked": false,
  "canonical_attempt_id": "<verified Attempt>",
  "scheduler_level2_engine_authoritative": true,
  "direct_legacy_level2_entry_blocked": true,
  "alternate_level2_entrypoints_engine_or_fail_closed": true,
  "injected_runner_level2_bypass_blocked": true,
  "engine_canonical_attempt_verified_in_store": true,
  "downstream_exact_attempt_binding_verified": true,
  "pr_handoff_exact_attempt_binding_verified": true,
  "engine_failure_legacy_fallback_blocked": true,
  "legacy_reader_compatibility_retained": true
}
```

All semantic booleans must also be present and true in the artifact's
`checks` object. The artifact must also prove rejection of nonexistent,
wrong-Task, and nonterminal Attempt claims in `adversarial_attempt_checks`.
The audit rejects the old v1 scheduler-only contract and any artifact whose
`repo_sha` does not equal the audited checkout's HEAD.

### `project-class-control-rehearsal.json`

This artifact uses `m1_project_class_controls.v1` and binds the rehearsal to the
audited repository SHA and its disposable database path. It must prove project-pause denial,
active-Attempt non-abort, project/class isolation, immediate class-global
disable, clear behavior, operator attribution, append-only events, and an
alternate-entry denial. The audit separately requires the target database and
the disposable evidence database to contain the deployed migration. Schema
presence without evidence is only partial, while evidence cannot substitute
for production schema deployment.
Automatic merge remains disabled regardless of this control-plane result.

## What this PR closes

This reconciliation PR closes ambiguity, not Milestone 1 itself. It provides:

- one authoritative M1 gate list aligned with Roadmap v2;
- deterministic read-only inspection of database/schema facts;
- explicit evidence contracts for operational rehearsals;
- a fail-closed overall decision;
- long-term regressions preventing M1, M2, Shadow Mode, or auto-merge from being
  declared ready without evidence.

## Required implementation sequence after reconciliation

The audit is expected to identify work in these slices:

```text
M1-A  production DB-copy migration, integrity, and rollback rehearsal
M1-B  dual-write consistency audit and bounded observation window
M1-C  ExecutionEngine parity and Level 2 canonical-path enforcement
M1-D  project pause, task-class kill switch, and remaining deployed drills
M1 Final Closeout
```

Only `M1 Final Closeout` may set `m1_exit_gate=passed`. After that, work may
proceed to Roadmap PR-9: evidence writer and validation summary.
