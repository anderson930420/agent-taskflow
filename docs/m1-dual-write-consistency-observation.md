# M1-B Dual-Write Consistency Observation

> Scope: Level 2 Roadmap v2, Milestone 1 bounded dual-write observation

## Purpose

M1-B proves that the canonical runtime admission path writes equivalent status
transitions to both persistence models:

```text
legacy:    task_events(event_type = status_changed)
canonical: lifecycle_events
```

The observed seam is the real `RuntimeAdmissionStore.claim()` and
`RuntimeAdmissionStore.release()` implementation. The runner does not fabricate
rows or compare hand-written fixtures.

## Safety boundary

The production SQLite database is never used for the disposable workload. The
runner:

- opens production read-only and creates a consistent snapshot with SQLite's
  online backup API;
- refuses a non-quiescent source snapshot;
- creates a separate `observation-target.sqlite3`;
- creates disposable tasks only inside that observation target;
- runs the real canonical claim/release code against the target;
- requires the production database file hash to remain unchanged throughout the
  observation;
- writes passing evidence only when mismatch and silent-failure counts are zero;
- refuses a non-empty output directory.

M1-B is not a live-production traffic experiment and does not enable scheduler,
Shadow Mode, or auto-merge behavior.

## Compared fields

For each disposable task, the runner observes two transition pairs:

```text
queued -> preparing
preparing -> completed
```

For each pair it requires:

- exactly one legacy event and exactly one canonical event;
- matching status;
- matching task key;
- matching legacy source and canonical actor;
- matching timestamp;
- a non-empty canonical Attempt identity.

It also verifies terminal postconditions:

- Task status is `completed`;
- Task has no active Attempt;
- Attempt is completed, inactive, and has an end timestamp;
- lease is inactive and has a release timestamp.

A missing counterpart increments `silent_failure_count`. Duplicates, malformed
payloads, field disagreements, or terminal-state disagreements increment
`mismatch_count`.

## Preconditions

M1-A must already have produced passing evidence. Keep its directory available.
For the current deployment that directory is expected to be similar to:

```text
$HOME/.agent-taskflow/rehearsals/m1-a-<RUN_ID>
```

Before the run, confirm the process registry is empty:

```bash
cd ~/agent-taskflow

git switch main
git pull --ff-only origin main

python3 scripts/terminate_executor_process.py status \
  --db-path "$HOME/.agent-taskflow/state.db"
```

Expected idle state:

```text
selected_count = 0
all_verified_exit = true
```

## Run the observation

Set `M1A_EVIDENCE_DIR` to the successful M1-A directory:

```bash
cd ~/agent-taskflow

M1A_EVIDENCE_DIR="$HOME/.agent-taskflow/rehearsals/m1-a-20260711T210437Z"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
M1B_EVIDENCE_DIR="$HOME/.agent-taskflow/rehearsals/m1-b-$RUN_ID"

python3 scripts/run_m1_dual_write_observation.py \
  --db-path "$HOME/.agent-taskflow/state.db" \
  --output-dir "$M1B_EVIDENCE_DIR" \
  --prior-evidence-dir "$M1A_EVIDENCE_DIR" \
  --repo-root "$PWD" \
  --actor operator \
  --workload-tasks 3 \
  --confirm-production-copy-observation
```

The CLI validates the M1-A evidence before creating the new output directory. It
then carries the unmodified M1-A JSON into the M1-B directory so that the M1
audit can evaluate both gates from one consolidated directory.

The command is supported under `python3 -S` from a source checkout.

## Successful result

For the default three-task workload:

```json
{
  "ok": true,
  "observation_scope": "production-copy-disposable-workload",
  "workload_task_count": 3,
  "records_compared": 6,
  "mismatch_count": 0,
  "silent_failure_count": 0,
  "source_connection_mode": "read_only",
  "source_quiescent": true,
  "production_database_modified": false
}
```

The new directory contains:

```text
source-snapshot.sqlite3
observation-target.sqlite3
dual-write-consistency.json
production-db-copy-rehearsal.json
```

The SQLite files and disposable tasks are evidence artifacts only. Do not copy
any of them over production.

## Evidence contract

`dual-write-consistency.json` uses:

```text
schema_version = m1_dual_write_consistency.v1
```

The M1 audit requires:

```text
observation_window_started_at = non-empty
observation_window_ended_at = non-empty
records_compared >= 1
mismatch_count = 0
silent_failure_count = 0
```

The richer artifact also records task keys, every comparison, terminal
postconditions, source and observation database reports, hashes, safety flags,
and the exact dual-write seam.

## Feed consolidated evidence into the M1 audit

```bash
python3 scripts/audit_m1_exit_gate.py \
  --db-path "$HOME/.agent-taskflow/state.db" \
  --repo-root "$PWD" \
  --evidence-dir "$M1B_EVIDENCE_DIR"
```

After a passing M1-B run, both of these gates should be passed:

```text
production_db_copy_rehearsal = passed
dual_write_consistency = passed
```

M1 must remain blocked until the disposable Attempt/runtime drills,
project/task-class controls, and canonical ExecutionEngine gate independently
pass.

## Failure handling

- `source snapshot is not quiescent`: stop and resolve active runtime ownership
  through canonical controls; use a new output directory for the next run.
- `source database file changed during observation window`: treat the observation
  as non-authoritative; confirm no scheduler or operator write occurred and rerun.
- `dual-write observation failed`: preserve the observation directory and inspect
  `observation-target.sqlite3`; do not create or edit passing evidence manually.
- invalid M1-A evidence: rerun or repair M1-A through its operator procedure; do
  not bypass the prerequisite.
- non-empty output directory: choose a new run ID.

## Non-goals

M1-B does not:

- write disposable tasks into production;
- establish ExecutionEngine parity;
- implement project or task-class controls;
- perform timeout, abort, or pause drills;
- close M1;
- authorize M2, Shadow Mode, or auto-merge.
