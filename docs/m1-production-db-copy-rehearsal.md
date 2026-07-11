# M1-A Production Database-Copy Rehearsal

> Scope: Level 2 Roadmap v2, Milestone 1 production DB-copy migration,
> integrity, idempotency, and rollback rehearsal

## Purpose

This rehearsal proves that the current additive migration chain can run safely on
an online-consistent copy of the production SQLite database and that the copy can
be restored to its exact pre-migration logical state.

It does **not** migrate or restore the production database. The source database is
opened with SQLite URI `mode=ro` plus `PRAGMA query_only=ON`. Every write occurs
inside a fresh rehearsal output directory.

## Safety contract

The runner:

- uses `sqlite3.Connection.backup` rather than plain file copying;
- includes committed WAL content in the consistent SQLite snapshot;
- refuses a non-empty output directory;
- refuses a snapshot with an active Task Attempt, runtime lease, managed process,
  or Attempt resource;
- runs the current top-level migration entrypoint only on `migration-target.sqlite3`;
- runs that migration entrypoint twice and requires an identical logical result;
- performs `PRAGMA integrity_check` and `PRAGMA foreign_key_check` on the source
  snapshot, migrated target, and restored target;
- restores the source snapshot over a clone of the migrated database using the
  SQLite backup API;
- requires the restored database logical dump and schema/row inventory to match
  the pre-migration snapshot;
- writes `production-db-copy-rehearsal.json` atomically only after every gate
  passes.

A failed run does not produce passing evidence. Partial rehearsal databases may
remain for inspection and must not be reused; start the next run in a new empty
directory.

## Preconditions

1. Pull the exact merged `main` revision containing the M1-A runner.
2. Ensure no scheduler tick or manual runtime pickup will begin during the
   snapshot window.
3. Confirm the current process registry is empty.
4. Use a new output directory for each run.
5. Keep the rehearsal directory outside the repository and outside the production
   database path.

Read-only preflight:

```bash
cd ~/agent-taskflow

git switch main
git pull --ff-only origin main

python3 scripts/terminate_executor_process.py status \
  --db-path "$HOME/.agent-taskflow/state.db"
```

The process status should report `selected_count = 0` and
`all_verified_exit = true`.

## Run the rehearsal

```bash
cd ~/agent-taskflow

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
REHEARSAL_DIR="$HOME/.agent-taskflow/rehearsals/m1-a-$RUN_ID"

python3 scripts/run_m1_db_copy_rehearsal.py \
  --db-path "$HOME/.agent-taskflow/state.db" \
  --output-dir "$REHEARSAL_DIR" \
  --repo-root "$PWD" \
  --actor operator \
  --confirm-production-copy-rehearsal
```

The command is also supported from a source checkout under `python3 -S`; it does
not require runtime-only third-party dependencies.

## Successful CLI result

A successful summary contains:

```json
{
  "ok": true,
  "source_connection_mode": "read_only",
  "source_quiescent": true,
  "migration_dry_run": true,
  "migration_idempotent": true,
  "integrity_check": true,
  "foreign_key_check": true,
  "rollback_rehearsal": true,
  "production_database_modified": false
}
```

The rehearsal directory contains:

```text
source-snapshot.sqlite3
migration-target.sqlite3
restore-target.sqlite3
production-db-copy-rehearsal.json
```

Do not replace the production database with any of these files. They are
rehearsal evidence only.

## Evidence contract

The evidence file uses:

```text
schema_version = m1_production_db_copy_rehearsal.v1
```

The M1 audit requires these top-level fields to be true:

```text
migration_dry_run
integrity_check
rollback_rehearsal
```

The richer artifact additionally records:

- rehearsal ID, actor, and timestamps;
- source DB path and enforced read-only mode;
- active runtime counts from the consistent snapshot;
- backup and rollback methods;
- migration entrypoint and commands;
- migration names before and after;
- idempotency result;
- integrity and foreign-key output;
- file SHA-256 and logical-dump SHA-256 values;
- schema-object and row-count inventories;
- paths for all rehearsal databases and the evidence file;
- explicit safety flags proving that no production migration or restore occurred.

## Feed evidence into the M1 audit

```bash
python3 scripts/audit_m1_exit_gate.py \
  --db-path "$HOME/.agent-taskflow/state.db" \
  --repo-root "$PWD" \
  --evidence-dir "$REHEARSAL_DIR"
```

After a valid rehearsal, only this gate should change because of M1-A:

```text
production_db_copy_rehearsal = passed
```

M1 must remain blocked until the dual-write, runtime-drill, project/class-control,
and canonical-execution-path gates are independently satisfied.

## Failure handling

- `source snapshot is not quiescent`: stop runtime admission, resolve or terminate
  the active Attempt/process through the canonical control path, then use a new
  output directory.
- `integrity_check` or `foreign_key_check` failure: preserve the rehearsal
  directory, do not create or edit passing evidence, and investigate the copied
  database.
- `migration entrypoint is not idempotent`: treat as a migration defect; do not
  proceed to rollback certification.
- `restored target does not match`: treat as a rollback defect; never use that
  restore procedure on production.
- non-empty output directory: choose a new run ID. The runner never overwrites a
  previous rehearsal.

## Non-goals

M1-A does not:

- pause or resume production runtime controls;
- modify cron or systemd configuration;
- write to the production DB;
- establish dual-write consistency;
- create disposable Attempts;
- implement project or task-class controls;
- enforce the canonical ExecutionEngine path;
- authorize M2, Shadow Mode, or auto-merge.
