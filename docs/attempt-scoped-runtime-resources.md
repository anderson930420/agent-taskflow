# Attempt-Scoped Runtime Resources

> Decision date: 2026-07-11  
> Scope: PR-5 branch, worktree, lock, PID, artifact, reaper, and retry foundation

## Status

```text
attempt_resource_schema = implemented
attempt_scoped_branch = implemented
attempt_scoped_worktree = implemented
attempt_scoped_artifact_root = implemented
attempt_scoped_lock = implemented
attempt_scoped_pid_manifest = implemented
fresh_worktree_retry_identity = implemented (legacy tasks)
ticket_worktree_reuse = implemented (V1 Step 5, Tickets)
stale_process_marker_reaper = implemented
historical_resource_auto_cleanup = disabled
process_group_kill = not_implemented_in_this_pr
milestone_0 = open_blocked
level_2_eligible = false
```

## Resource identity

Every canonical token claim allocates one immutable resource record keyed by the
full `attempt_id`. The record binds:

- the task and Attempt identities;
- repository root and base SHA;
- a unique local branch;
- a unique worktree path;
- a unique artifact root;
- a process-scoped advisory lock path;
- a PID manifest path; and
- allocation, heartbeat, release, and reap timestamps.

The database retains the full Attempt identity even when an operational path or
branch uses a bounded readable suffix. Path/branch uniqueness is enforced by the
resource schema and the one-active-Attempt runtime admission boundary.

## Canonical paths

For a task `<task-key>` and Attempt `<attempt-id>`, the default layout is:

```text
branch:
  attempt/<task-slug>/<attempt-number>-<attempt-suffix>

worktree:
  <repo>/.worktrees/<task-slug>/<attempt-id>/

artifact root:
  <artifact-base>/<task-key>/<attempt-id>/

lock and PID:
  <artifact-root>/runtime.lock
  <artifact-root>/runtime.pid.json
```

The PID manifest is bound to the Attempt, lease, and owner identifiers. It is
evidence for crash recovery; it does not grant permission to kill a process.

## Input and output binding

Deterministic task inputs such as `issue_spec.md`, `implementation_prompt.md`,
and advisory evidence are snapshotted into the Attempt artifact root before
execution. Mission contracts, executor output, validator output, and review
evidence are written beneath the same Attempt root.

Dispatcher and ApprovedTaskRunner resolve mission-contract, executor, validator,
prompt, and evidence paths from the active Attempt record immediately before the
corresponding runtime boundary. A stale task-level `TaskRecord` or pre-dispatch
worktree object cannot redirect executor writes into an older workspace.

After a terminal runtime result, the task's review artifact pointer remains on
the latest Attempt root. Historical Attempt roots remain immutable evidence.

## Two worktree contracts

V1 Step 5 (ruling 26) splits Attempt resources into two contracts. SPEC §9 and
§44 (One Ticket = One Worktree) win where they conflict with the PR-5 fresh
retry contract below.

| | Legacy task | Ticket (a `tasks` row with Step 1's columns) |
|---|---|---|
| Branch | `attempt/<task-slug>/<attempt-number>-<attempt-suffix>`, new per Attempt | the Ticket's derived `tasks.branch`, the same for every Attempt |
| Worktree | `<repo>/.worktrees/<task-slug>/<attempt-id>/`, new per Attempt | the Ticket's derived `tasks.worktree_path`, the same for every Attempt |
| Created | at the claim, by `provision_workspace` | before the claim, by `ticket_worktree.ensure_ticket_worktree` (idempotent, audited) |
| Retry | a fresh branch and worktree (below) | the same worktree, **as the last Attempt left it**: a dirty tree is recorded in a `ticket_worktree_reused` event, never cleaned, reset or discarded |
| Mismatch | `blocked` | a path that is not this repository's worktree on the Ticket's branch, or a branch without its worktree, is refused, never recreated or deleted; the Ticket ends `failed` |
| Artifact root, lock, PID | per Attempt | per Attempt (unchanged) |

Sharing a worktree needs `attempt_resources.worktree_path` and `.branch_name`
without `UNIQUE`. Only the operator-run
`scripts/migrate_ticket_worktree_resources.py` rebuilds the table that way
(migration `v1_step5_ticket_worktree_attempt_resources`); every other
constraint, including `UNIQUE(task_id, attempt_number)` and the unique
artifact, lock and PID paths, is kept. Nothing applies it at startup: Ticket
dispatch and the scheduler tick refuse a Ticket until it has run, naming the
script. Legacy tasks run with or without it and keep unique paths, because the
allocator derives them from the Attempt identity.

## Fresh retry contract (legacy tasks)

A terminal Attempt releases its live lock and removes its live PID marker, but it
does not delete its branch, worktree, artifact root, manifest, or database row.

After an operator resets a blocked task to `queued`, the next canonical claim
creates a different `attempt_id`. The next Attempt therefore receives a new:

- branch;
- worktree;
- artifact root;
- lock path; and
- PID path.

The prior Attempt cannot be reused as the retry workspace. Existing input files
are copied into the new Attempt snapshot; execution output is never copied
forward as authoritative evidence.

## Reaper boundary

The PR-5 reaper may:

- expire stale runtime leases through the existing admission store;
- verify whether a recorded PID is no longer alive;
- verify that the advisory lock is no longer held;
- remove a dead PID marker; and
- mark the resource record reaped with audit timestamps and reason codes.

The reaper must refuse cleanup when the PID is still alive or the lock remains
held. It never deletes branches, worktrees, artifact roots, or historical
manifests. Destructive cleanup remains an explicit later policy and operator
action.

PR-5 also does not create process groups, send signals, terminate descendants,
or claim that PID liveness alone proves process ownership. Those controls belong
to the process-lifecycle PR.

## Deployment

Run from the repository root:

```bash
python3 scripts/migrate_attempt_resources.py \
  --db-path "$HOME/.agent-taskflow/state.db"
```

The command applies the Task/Attempt, runtime-admission, and canonical-admission
prerequisites before installing `level2_attempt_scoped_resources_v1`.

To expire stale leases and reap only dead process markers:

```bash
python3 scripts/migrate_attempt_resources.py \
  --db-path "$HOME/.agent-taskflow/state.db" \
  --reap
```

Review the JSON output. A successful migration is not permission to delete any
historical branch, worktree, or artifact directory.

## Remaining M0 blockers

PR-5 closes the Attempt resource and fresh-worktree retry foundation. Milestone 0
remains open for:

- process-group lifecycle, termination, and crash recovery;
- dual-Attempt reset audit lineage; and
- concurrent reset compare-and-set semantics and regression coverage.
