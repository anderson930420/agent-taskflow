# Task / Attempt / Lifecycle Schema

> Decision date: 2026-07-11  
> Scope: PR-2 schema, migration, and one-active-attempt constraint

## Status

```text
task_attempt_schema = implemented
legacy_migration = implemented
one_active_attempt_constraint = implemented
append_only_lifecycle_events = implemented
runtime_attempt_admission = not_implemented_in_this_pr
attempt_scoped_worktree_lock_pid_artifact = not_implemented_in_this_pr
milestone_0 = open_blocked
level_2_eligible = false
```

This PR creates the persistence foundation only. It does not route the existing
Dispatcher, `ApprovedTaskRunner`, scheduler, reset command, worktree manager, or
executor launch path through Attempt admission. Those integrations remain
blocked on the following PRs.

## Task identity

The existing `tasks` table remains the compatibility table used by the local
Hermes/Kanban mirror. The existing `status` column is the current task status;
it is the compatibility equivalent of the roadmap field `current_status`.

The migration adds:

| Column | Meaning |
| --- | --- |
| `task_id` | Stable execution-model identity. Migrated rows use deterministic `task:<task-key>` values. |
| `task_class` | Deterministic task class. Existing rows are marked `legacy`. |
| `active_attempt_id` | Pointer to the task's current active Attempt, or `NULL`. |
| `final_outcome` | Final task outcome when the task is closed. |
| `closed_at` | Task close timestamp. |
| `is_legacy` | `1` when pre-migration history cannot be reconstructed safely. |

`task_id` has a unique index. Existing tasks are marked legacy; the migration
does not infer or manufacture historical attempts from task status, task
events, worktrees, or artifacts.

`AttemptStore.register_task_identity(...)` can explicitly classify future work
and clear the legacy marker. It refuses to replace an already-established
`task_id`; the API changes classification metadata, not historical identity.

## Attempt schema

Each actual executor run must eventually use one immutable `attempt_id`.
PR-2 introduces the table and storage API with these fields:

```text
attempt_id
task_id
attempt_number
status
is_active
is_legacy
executor
model
base_commit
policy_version
config_snapshot_hash
prompt_template_version
permission_profile
worktree_path
artifact_root
started_at
ended_at
execution_result
validation_result
merge_recommendation
created_at
updated_at
```

The following constraints are authoritative at the SQLite layer:

- `attempt_id` is globally unique.
- `(task_id, attempt_number)` is unique.
- `attempt_number >= 1`.
- An active Attempt has `ended_at IS NULL`.
- An inactive Attempt has a non-null `ended_at`.
- Partial unique index `ux_attempts_one_active_per_task` permits at most one
  row with `is_active = 1` for each task.
- `tasks.active_attempt_id` may reference only an active Attempt belonging to
  the same `task_id`.

`AttemptStore.create_attempt(...)` uses `BEGIN IMMEDIATE`, calculates the next
attempt number, inserts the Attempt, updates the task pointer, and appends the
creation lifecycle event in one transaction. This method allocates identity;
it is not yet the canonical runtime pickup/lease operation.

`AttemptStore.close_attempt(...)` closes the Attempt, clears the task pointer,
and appends the close event in one transaction. A transition graph is not
introduced in this PR; illegal lifecycle transition enforcement remains a
separate work item.

## Lifecycle event schema

`lifecycle_events` contains:

```text
event_id
task_id
attempt_id
from_status
to_status
reason_code
actor
timestamp
metadata_json
```

Properties:

- Events are append-only.
- SQLite triggers reject `UPDATE` and `DELETE`.
- If `attempt_id` is present, it must belong to the same `task_id`.
- `reason_code` and `actor` are mandatory in the storage API.
- Metadata is stored as deterministic JSON.

PR-2 makes these statuses representable without yet defining the full legal
transition graph:

```text
validation_failed
execution_timeout
execution_aborted
```

## Migration behavior

Run:

```bash
python3 scripts/migrate_task_attempt_lifecycle.py \
  --db-path /absolute/path/to/state.db
```

The migration is additive and idempotent:

1. Initialize the existing task mirror schema.
2. Add missing Task identity columns.
3. Backfill deterministic `task_id` values.
4. Mark pre-existing rows `task_class = legacy` and `is_legacy = 1`.
5. Create `attempts`, indexes, integrity guards, and lifecycle tables/triggers.
6. Record `level2_task_attempt_lifecycle_v1` in `schema_migrations`.

The command reports counts and explicitly reports:

```text
historical_attempts_synthesized = false
```

A task inserted through the legacy `TaskMirrorStore` after migration may still
have a null `task_id`; the Attempt storage API fills the deterministic identity
before creating its first Attempt and keeps the task marked legacy.

## Rollback

This is a forward-only additive SQLite migration.

Application rollback is safe: reverting the Python integration leaves the new
columns, tables, indexes, and triggers inert while legacy task-mirror code
continues using its original columns and tables.

A destructive schema rollback must not use ad-hoc `DROP COLUMN` operations.
Restore a pre-migration database backup or rebuild a new legacy database and
copy only the original mirror tables. Dropping Attempt/lifecycle data would
destroy audit history and therefore requires an explicit operator-approved
migration.

## Acceptance criteria

- Existing task rows receive unique stable `task_id` values.
- Existing rows are marked legacy without synthetic Attempt or lifecycle rows.
- The migration is idempotent and recorded once.
- The same task can create and close at least three sequential attempts with
  monotonically increasing attempt numbers.
- Concurrent creators produce exactly one active Attempt.
- Direct SQL cannot insert a second active Attempt for the same task.
- A task cannot point to another task's Attempt.
- Lifecycle events cannot be updated or deleted.
- Lifecycle events cannot bind an Attempt from another task.
- Existing test suite remains green.
