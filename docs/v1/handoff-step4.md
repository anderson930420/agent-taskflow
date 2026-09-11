# Handoff — V1 Step 4: Concurrency Readiness

Branch: `task/v1-step4`
Draft PR: __PR_URL__
Base: `4552f4e` (`main`, after Steps 1 and 3 merged)
Spec: `~/agent-taskflow-ops/v1/SPEC.md` §19, §42 Step 4
Instructions: `~/agent-taskflow-ops/v1/step4.md`

Status: **implementation-complete, awaiting human review.** Stop conditions
were hit and are reported in §4, not repaired. The most important is §4.1:
enforcing a limit of 1 on every database would turn 3 pre-existing tests red,
so the limit is enforced only once an operator deploys it, and a ruling is
needed. Not approved, not merged. The PR is a draft.

Most Step 4 primitives already existed (M1 / Level 2). This branch adds proof
(a rehearsal plus tests), hardening (the ObservedStep guard and contention
observability), one reaper entry point, and the capacity gate.

---

## 1. Read-only inventory (what was extended vs. created)

Checked against the code before any edit. Corrections to the step4.md
"facts" are in §1.1.

| Module | Extend / Create | What |
|---|---|---|
| `agent_taskflow/runtime_admission.py` | extend | `RuntimeCapacityExceededError` and `assert_runtime_capacity_available(conn)`, called as the **first** statement after `BEGIN IMMEDIATE` in `claim()`. The status-check line F1 owns is untouched. |
| `agent_taskflow/reset_runtime_path.py` | extend | The same capacity call at the start of `_try_claim_reserved_retry`'s transaction. Adopting a reset-reserved Attempt creates a lease too, so without it the retry path could bypass the limit. |
| `agent_taskflow/store.py` | extend | `connect()` opens connections with `factory=ContentionObservingConnection`. Pragmas, row factory and timeout unchanged. |
| `agent_taskflow/runtime_progress.py` | extend | `RUNTIME_STEP_STATUS_RANK`, `is_step_status_regression()`, `ObservedStepRegressionError`. |
| `agent_taskflow/runtime_progress_store.py` | extend | `record_step` refuses a regression inside its existing `BEGIN IMMEDIATE`. **Step 3's table schema is unchanged.** |
| `agent_taskflow/lifecycle_control.py` | extend | `RuntimeControlStore.runtime_capacity()` and `.set_max_concurrent_tasks()`. |
| `scripts/runtime_control.py` | extend | New actions `capacity` and `set-capacity`. Existing actions and their output are byte-for-byte unchanged. |
| `agent_taskflow/sqlite_contention.py` | create | Contention counters and structured log. |
| `agent_taskflow/runtime_capacity_schema.py` | create | Additive capacity tables, explicit migration only. |
| `agent_taskflow/runtime_capacity.py` | create | Read, set (evidence-gated) and audit the global limit. |
| `agent_taskflow/concurrency_gate.py` | create | Read-only evidence gate, modelled on `m1_exit_gate.py`. |
| `agent_taskflow/runtime_reaper.py` | create | `reap_stale_runtime()`: one idempotent reaper call. |
| `agent_taskflow/concurrency_rehearsal.py` | create | §19.1–§19.3 rehearsal, fixtures, race harness, checkers. |
| `agent_taskflow/concurrency_rehearsal_worker.py` | create | Separate-process worker for the rehearsal and tests. |
| `scripts/reap_stale_runtime.py` | create | Reaper CLI. |
| `scripts/run_concurrency_rehearsal.py` | create | Rehearsal CLI; writes evidence and gates it. |
| 8 `tests/test_*.py` files | create | The acceptance gate (§3). No pre-existing test file was modified. |

Reused unchanged: `expire_stale_leases`, `AttemptResourceManager.reap_stale_resources`,
`ResetLineageStore.reserve_retry` / `scripts/reset_task_status.py`, the canonical
runtime store and its heartbeat thread, `atomic_write_json`.

No existing module was rewritten.

### 1.1 Corrections to the step4.md inventory facts

- **"Some writers bypass `connect()`, e.g. `scheduler_proposal_review.py`" — not
  true.** Every `sqlite3.connect(` outside `store.py` in `agent_taskflow/` and
  `scripts/` was checked. The five in `scheduler_proposal_review.py`, the two in
  `scheduler_confirmation_verifier.py` and all the `scripts/migrate_*.py` ones
  are `SELECT`-only. The rest are `mode=ro` URIs or the M1-A backup target.
  The only raw writers are smoke-script fixture seeds, which are test harnesses,
  not runtimes. See §4.2.
- **The dispatcher's `preparing` path does not use the PR-3 trigger in the real
  runtime.** `agent_taskflow/__init__.py` installs the canonical runtime path,
  so `Dispatcher(...).store` is `ValidatorProcessRuntimeTaskStore`. Its
  `update_task_status("preparing")` calls the layered admission store's
  `claim()` (`ResetAwareRuntimeAdmissionStore` → `LifecycleRuntimeAdmissionStore`
  → `RuntimeAdmissionStore.claim`) and then allocates Attempt resources: branch,
  git worktree, lock and PID file. The canonical migration disables
  `runtime_pickup_claim_after_preparing` (`WHEN 0`) and refuses a raw
  `preparing` write. The trigger path only survives on databases that never ran
  the canonical migration; the pre-existing
  `test_concurrent_runtime_pickup_has_exactly_one_winner` covers it.
- **There are three lease-creating paths, not one:** `RuntimeAdmissionStore.claim`,
  the reset-retry adoption in `reset_runtime_path.py`, and the PR-3 trigger.
  The capacity check guards the first two (§4.5 for the third).

---

## 2. What was implemented

### §19.1 Atomic claim rehearsal

Four races, each on its own Ticket: explicit `claim()` from N threads and from
N separate processes, and the dispatcher's `preparing` transition from N
threads and N processes. Each race asserts exactly one winner, exactly one
Attempt and one lease (both active), the Ticket in `preparing` pointing at that
Attempt, and **exactly one** audited claim event. Every loser must get a typed
refusal. Observed refusals:

- explicit API: `RuntimeAdmissionError` ("not claimable from status preparing");
  `RuntimeCapacityExceededError` when capacity is deployed and full;
  `ActiveAttemptExistsError` is also accepted.
- dispatcher path: the above plus the canonical store's pre-claim
  compare-and-set `ValueError` (§4.4).

A `sqlite3.OperationalError` or `IntegrityError` from a loser counts as a
failure, not a refusal. Processes are real (`python -m
agent_taskflow.concurrency_rehearsal_worker`, distinct PIDs asserted) and are
released together by a file barrier after they have all imported.

### §19.2 Concurrent writes and contention observability

N writer processes, each on its own Ticket, run the full write mix at once:
claim, H heartbeats, `transition` to `implementing`, all seven ObservedSteps
(`running` → `passed`) plus `current_phase`/`current_activity`, V validator
results and V artifacts (evidence), `transition` to `validating`, and
`release` to `waiting_approval`. They start behind a separate process that
already holds the write lock, so contention is guaranteed, not incidental.

Afterwards the tests assert that every expected row is present, row by row:
Ticket, Attempt, lease, lifecycle events (exact reason sequence), steps,
progress, evidence, and the Ticket's status history. `PRAGMA integrity_check`
must be `ok`. `lifecycle_log_errors()` replays every Attempt's
`lifecycle_events` against the forward-only Attempt graph in
`lifecycle_control_schema.ATTEMPT_TRANSITIONS`: continuity, legal edges,
ending at the persisted status. A dedicated test forges an illegal event to
prove the checker catches it.

**Contention observability** (`sqlite_contention.py`): every `store.connect()`
connection counts

- *busy waits*: an explicit `BEGIN IMMEDIATE`/`EXCLUSIVE` that took ≥ 1 ms.
  Uncontended it returns in microseconds, and SQLite's busy handler sleeps at
  least 1 ms before its first retry, so a slower acquisition means a wait.
- *busy timeouts*: any statement failing with `SQLITE_BUSY`/`SQLITE_LOCKED`.
  The error is still raised.

Counters are process-local (`contention_snapshot()`). Each event is logged on
`agent_taskflow.sqlite_contention` as `"<event> <json>"`: waits at INFO,
timeouts at WARNING. The rehearsal records the aggregate in its evidence.

### §19.3 Crash recovery and the reaper

A worker claims through the dispatcher path with a 2 s lease, which provisions
a real worktree, lock and PID file. It moves to `implementing`, records
`Prepare: passed` and `Implementer: running`, and keeps running. The parent
waits **longer than one TTL** and checks the lease is still live, which only the
worker's heartbeat thread can explain. Then it SIGKILLs the worker and checks:

- **lease expires correctly:** a reap right after the kill reaps nothing (the
  lease is still live); after the TTL, one reap expires exactly that Attempt;
  a second reap changes nothing.
- **dead-process markers:** the resource reaper takes the dead PID's lock, removes
  its PID file, and marks the resource `reaped`. Worktree and artifacts are kept.
- **recoverable through the existing retry:** `scripts/reset_task_status.py
  --from-status blocked --confirm-reset` (run as a real subprocess) reserves a
  retry Attempt, and the dispatcher path adopts it.
- **nothing stays running forever:** `running_without_live_lease()` reports the
  Ticket after expiry and nothing after the reap or the retry.
- **no double ownership:** `ownership_violations()` is empty at every
  checkpoint, exactly one lease is active afterwards (the new owner's), and
  the killed owner's token is refused for both heartbeat and release.
- **the old Attempt stays auditable:** `execution_aborted`, `lease_expired`,
  its lifecycle events from `canonical_runtime_pickup_claimed` to
  `runtime_lease_expired` by `runtime_lease_reaper`, and its two ObservedSteps.

**The reaper** is `runtime_reaper.reap_stale_runtime(db_path)`: it runs
`expire_stale_leases()` then `reap_stale_resources()`, returns a
`RuntimeReapResult`, and also reports (read-only) Tickets in a running status
that no live lease owns. It is idempotent and **installs no schema**: a
database without the admission tables is reported as skipped, and a test
pins the schema byte-for-byte. `scripts/reap_stale_runtime.py --db-path`
exposes it; `--db-path` is required, so there is no default database. No daemon
or cron was added. Step 5's loop is expected to call it.

### Lease contention

- **Heartbeat vs reaper,** 12 rounds each. On an expired lease the heartbeat is
  always refused (`LeaseExpiredError` or `LeaseOwnershipError`) and the reaper
  reaps it exactly once, so an expired lease is never resurrected. On a live lease
  the heartbeat always wins and the reaper leaves it alone.
- **Racing reapers:** six concurrent reapers expire four leases, each exactly
  once, and write one `runtime_lease_expired` event per lease.
- **Stale owner after reclaim:** after expiry, reap, `reserve_retry` and a new
  claim, the old owner's heartbeat and release are refused, and the new lease row
  stays byte-identical. The old token cannot act on the new Attempt either, and
  the new owner still can.
- **N-owner contention:** eight callers heartbeat one lease (impostors with and
  without the real token) and only the owner succeeds. Eight contenders reclaim
  after a reap and exactly one wins.

### ObservedStep lost-write guard

`record_step` reads the current status and refuses a lower-rank write in the
same `BEGIN IMMEDIATE` transaction. The ranks are `pending` (0) < `running`
(1) < `passed`/`failed`/`blocked` (2). A refused write raises
`ObservedStepRegressionError` (a `RuntimeProgressError`) and changes no row.
Per-Attempt: a retry's new Attempt starts over. Tests:

- a late `running` after `passed` is refused and the `passed` row, summary
  included, survives;
- 7 threads writing 7 steps and 7 processes writing 7 steps lose nothing;
- 10 rounds of 8 threads racing one `passed` against seven `running` on one
  step always end `passed`.

No column was added. See §4.3 for what "regress" is taken to mean.

### Capacity limit and gate (§19.4, §20)

- **Setting:** `max_concurrent_tasks`, **global**, **default 1**. It is stored in
  the runtime-control database beside `runtime_controls`: a
  `runtime_capacity_controls` row (one per scope, `global` only), plus an
  **append-only** `runtime_capacity_control_events` table, since UPDATE and
  DELETE are refused by triggers. It is reached through `RuntimeControlStore`
  and `scripts/runtime_control.py capacity|set-capacity`. No new ad-hoc file.
- **Enforced atomically at claim time:** `assert_runtime_capacity_available(conn)`
  is its own function, called first inside the claim's `BEGIN IMMEDIATE`, in both
  lease-creating claim transactions. It counts **active executor leases** (every
  `runtime_leases.is_active = 1` row, so an expired but unreaped lease keeps its
  slot until the reaper runs). A claim that would exceed the limit raises
  `RuntimeCapacityExceededError(RuntimeAdmissionError)`, with
  `reason_code = "runtime_capacity_exceeded"`, `max_concurrent_tasks` and
  `active_executor_leases`, and the transaction rolls back. No Attempt, no
  lease, no event.
- **Gate:** a value above 1 is refused unless
  `concurrency_gate.evaluate_concurrency_evidence()` passes the evidence file.
  That requires the schema version, `repo_sha` equal to the audited repository's
  `HEAD`, `disposable_database: true`, `production_database_touched: false`,
  and every one of the 16 §19.1–§19.3 checks `true`. The gate is read-only and
  fails closed on a missing, unreadable or malformed file. A refusal writes
  nothing and does not install the control. An accepted value records the
  evidence path, SHA-256 and repo SHA on the row and the event. A `CHECK`
  constraint refuses any limit above 1 without a 64-hex evidence hash, even
  by direct SQL. Lowering to 1 never needs evidence.

### Step 4 rehearsal

`scripts/run_concurrency_rehearsal.py --output-dir DIR` runs §19.1–§19.3
against fresh disposable databases created inside `DIR`. DIR must be new or
empty, and every database path is checked to be inside it. It writes
`DIR/concurrency-rehearsal.json`, gates it, and exits 0 only if the gate passes.
It never touches the default state database (tested with `HOME` redirected).
A failing section is recorded with its error and `false` checks, never
omitted.

---

## 3. Acceptance gate

| # | Requirement | Tests |
|---|---|---|
| 19.1 | N threads + N processes, explicit API + dispatcher path: one winner, one Attempt, one lease, typed refusals | `test_concurrency_atomic_claim` (6 tests) |
| 19.2 | no lost write, `integrity_check` ok, valid lifecycle log, contention observable | `test_concurrency_writes` (10), `test_sqlite_contention` (8) |
| 19.3 | SIGKILLed holder: lease expires, retry recovers, nothing running forever, no double ownership, old Attempt auditable | `test_concurrency_crash_recovery` (14, incl. reaper CLI) |
| — | heartbeat vs reaper; stale owner refused after reclaim; N-owner contention | `test_lease_contention` (9) |
| — | late `running` never overwrites `passed`; concurrent `record_step` loses nothing | `test_observed_step_guard` (12) |
| 19.4 / 43.9 / 43.10 | default 1; second claim refused at limit 1; raise without passing evidence refused; with evidence and limit K at most K active | `test_runtime_capacity` (26) |
| — | the real rehearsal evidence passes the gate and unlocks a limit above 1; tampered evidence is blocked | `test_concurrency_rehearsal` (11) |

Test counts are unittest methods per file; `Ran` totals are in §5.

---

## 4. Stop conditions hit, and flags

Per step4.md: reported, **not repaired**.

### 4.1 STOP CONDITION HIT — "default 1, enforced at claim time" contradicts existing code and turns 3 pre-existing tests red

**What conflicts.** SPEC §19 says `max_concurrent_tasks = 1` by default, and
step4.md says to enforce it inside the claim transaction. Existing code runs
several claims at once on one database, and pre-existing tests pin that:

- `agent_taskflow/m1_project_class_control_rehearsal.py` holds claims A2, B1
  and A1 active simultaneously;
- `tests/test_project_class_controls.py::test_project_pause_isolated_and_existing_attempt_remains_active`
  claims a second task while the first is active.

**Measured, not guessed.** In an isolated scratch copy of `4552f4e`
(`git archive` into `/tmp/v1-step4-probe`, never this worktree), I added a strict
"refuse when any lease is active" check to both claim transactions and ran the
full suite:

```
Ran 4776 tests in 531.320s
FAILED (errors=3, skipped=8)
ERROR: test_rehearsal_exercises_isolation_immediacy_and_append_only_controls (test_m1_project_class_control_rehearsal)
ERROR: test_cli_runs_without_site_packages (test_m1_project_class_control_rehearsal)
ERROR: test_project_pause_isolated_and_existing_attempt_remains_active (test_project_class_controls)
```

All three fail with the probe's capacity refusal. The same tree without the
probe was green (`Ran 4776 tests … OK (skipped=8)`).

**What this branch does instead (reversible, and not a repair).** The capacity
control is enforced from the moment it is **deployed**, with the default 1
applying at once. Deployment is an explicit operator action
(`runtime_control.py set-capacity`, which installs
`runtime_capacity_schema`). An undeployed database keeps its pre-Step-4
admission behaviour. This is the same compatibility rule `runtime_controls`
already applies ("Historical PR-3 databases predate the optional
lifecycle-control plane…"), and the same explicit-migration pattern as the
M1-D project/class controls. `UndeployedDatabaseTests` pins it, with a pointer
to this section. No pre-existing test or fixture was touched, and none goes red.

**Consequence you must know about.** Until an operator deploys the control,
the production database is **not** capacity-gated, which is today's behaviour.
To enforce the default of 1 there, run (not run by me; I never touch the
production DB):

```bash
$PY scripts/runtime_control.py set-capacity --db-path <prod-db> \
  --actor <operator> --max-concurrent-tasks 1
```

**Decision needed from the human:**

- **(a) Keep explicit deployment** (as implemented), and add the command above
  to the deploy runbook; or
- **(b) Enforce the default on every database.** In
  `runtime_capacity.runtime_capacity_in_connection`, return `enforced=True`
  when the table is absent. Then rule on the fixture changes the three tests
  above need: deploy capacity with passing evidence, or release their earlier
  claims. That is a change to pre-existing tests, which I may not make without
  a ruling. `UndeployedDatabaseTests` would flip accordingly.

### 4.2 Premise did not hold — "move writers that bypass `store.connect()`"

No runtime writer bypasses `connect()` (§1.1). Nothing was moved. I did not
move the read-only callers either. `connect()` sets `journal_mode = WAL` and
creates parent directories, which would add a write and a mkdir to paths that
only read today. That would change what they do, which the brief forbids.
Reported because the requirement names an example the code contradicts.
**Decision needed:** none, unless the reviewer wants the read-only callers moved
anyway.

### 4.3 Ambiguity — what counts as an ObservedStep "regression"

step4.md's example is `passed → running`. Implemented as **"never lower the
rank"** (`pending < running < outcome`). A change between outcomes
(`passed ↔ failed ↔ blocked`) is therefore still accepted. Making outcomes
final would turn Step 3's pre-existing
`test_record_step_supports_every_spec_status` red: it writes `pending, running,
passed, failed, blocked` in sequence on one step. `blocked` is ranked as an
outcome, so a `blocked` step cannot go back to `running` in the same Attempt;
a retry is a new Attempt (§33.3). `set_current_activity` has no ordering guard
(the latest write wins); step4.md asks only about `record_step`.
**Decision needed:** whether outcomes should be final within an Attempt (that
needs a ruling on the Step 3 test), and whether `blocked` is an outcome.

### 4.4 Flag — the dispatcher path's loser refusal is a bare `ValueError`

When a dispatcher loses the race after the winner committed, the canonical
store's pre-claim compare (`CanonicalRuntimeTaskStore._claim`, "status is
'preparing'; expected 'queued'") raises a plain `ValueError`. It is a clean
refusal that writes nothing, but it is not an admission-typed error. That is
existing behaviour of a module outside Step 4's layers. The rehearsal accepts
it explicitly (`DISPATCHER_PATH_REFUSALS`) and still fails on any SQLite
error. Not changed.

### 4.5 Flag — the PR-3 implicit trigger claim is not capacity-gated

On a database that never ran the canonical migration, a raw
`update_task_status("preparing")` still claims through
`runtime_pickup_claim_after_preparing`, which no Python code guards. Gating it
would take a new trigger in the runtime-admission schema and would change
legacy dispatcher behaviour. On canonical databases the trigger is inert and a
raw `preparing` write is refused, so the gate covers every path the installed
runtime uses. Not changed.

### 4.6 Flag — ambiguity watchlist, resolved by RULINGS #8

step4.md flags (global vs per project) and (whether `integrating` counts).
Ruling 8 in `~/agent-taskflow-ops/v1/RULINGS.md` already decides both:
**global**, and **executor leases only**. Implemented that way. Step 2
integration holds no executor lease, so it is not counted.

### 4.7 Flags for Step 5 (not repaired; outside Step 4's layers)

1. **Managed executor processes are not reaped.** If a SIGKILLed runtime had
   registered an `executor_processes` row in an active state, `reserve_retry`
   refuses ("still has active runtime ownership") until an operator runs
   `scripts/terminate_executor_process.py`. step4.md scopes the reaper to
   leases plus resources; the rehearsal's crash holder spawns no managed
   executor process.
2. **An expired, unreaped lease keeps its capacity slot** (conservative: its
   process may still be alive). Step 5's loop should call
   `reap_stale_runtime()` before claiming.
3. **ObservedSteps have no ownership check.** A stale owner can still write
   progress on its own, already-aborted Attempt. It cannot touch the new
   Attempt's lease. That is Step 3's design (progress is observation), unchanged.
4. **Contention counters are process-local**, and implicit deferred writes
   (`with conn:` + bare DML) are observed only when they time out. Waits at INFO
   are silent unless the process configures logging.
5. **Evidence is bound to `HEAD`, not to a clean tree.** As with M1, a
   rehearsal run on uncommitted changes records the committed SHA. Any new
   commit requires a fresh rehearsal before the limit can go above 1.
6. **Rehearsal Tickets are legacy-class tasks.** The dispatcher entry refuses
   Level 2 tasks, so the dispatcher-path races use legacy tasks. The explicit
   `claim()` transaction is the same code for both.

### Not hit

- No pre-existing test went red on this branch (§5).
- No push was rejected; the branch was never rebased or force-pushed.

---

## 5. Validation

All counts use `python -m unittest discover -s tests`, the repo's documented
runner.

| Point | Tree | Result |
|---|---|---|
| BEFORE any change | `4552f4e` | Ran 4776 tests in 526.9s, OK (skipped=8) |
| AFTER | `4552f4e` + this branch's changes | Ran 4872 tests in 504.7s, OK (skipped=8) |

- Delta **+96** is exactly the eight new test files (6 + 10 + 8 + 14 + 9 + 12 +
  26 + 11 methods). **No pre-existing test went red**, and no pre-existing test
  file was modified. Skips unchanged at 8.
- Focused pre-existing suites around the touched code (admission, canonical
  runtime, reset lineage, lifecycle control and CLI, project/class controls,
  M1 rehearsals and exit gate, dispatcher, store, Step 3 progress, schema diff
  and negative scope; 29 files): Ran 353 tests, OK.
- The §4.1 strict-capacity probe (scratch copy, not this branch): Ran 4776
  tests, FAILED (errors=3). This is evidence for the stop condition, not a
  result of this branch.

| Command | Result |
|---|---|
| `python -m compileall -q agent_taskflow scripts tests` (from the repo root) | OK |
| `scripts/validate_workflow_contract.py` | passed |
| `scripts/validate_workflow_policy.py` | passed |
| `scripts/run_concurrency_rehearsal.py --output-dir <fresh>` | exit 0, 16/16 checks, gate `passed` |
| `cd mission-control && npm run build` | **not run**: no `mission-control/` file changed |

**F1 compatibility (scratch tree only).** This branch plus F1's
`agent_taskflow/` and `tests/` diff (`4552f4e..origin/task/v1-stepf1`) applies
cleanly: `git apply --3way` onto a commit of this branch reports every file
clean, including `runtime_admission.py`, where the capacity call sits apart
from F1's status-check line. On that combined tree, F1's
`test_runtime_progress_wiring` plus `test_dispatcher`, `test_runtime_admission`,
`test_runtime_progress_store` and all eight Step 4 files ran **205 tests, OK**.
F1's claimable-status change (no `blocked`) does not affect Step 4's tests,
which never claim from `blocked` and always recover through the reset path.

---

## 6. Exact commands to verify

Run from the worktree root, `/home/ubuntu/agent-taskflow/.worktrees/v1-step4`.
Use the repo virtualenv; the system `python3` lacks `pydantic`:

```bash
PY=/home/ubuntu/agent-taskflow/.venv/bin/python
```

Full suite (~9 min) and byte-compile:

```bash
$PY -m unittest discover -s tests
$PY -m compileall -q agent_taskflow scripts tests
```

Step 4 acceptance gate only (~1 min):

```bash
$PY -m unittest \
  tests.test_concurrency_atomic_claim \
  tests.test_concurrency_writes \
  tests.test_sqlite_contention \
  tests.test_concurrency_crash_recovery \
  tests.test_lease_contention \
  tests.test_observed_step_guard \
  tests.test_runtime_capacity \
  tests.test_concurrency_rehearsal
```

Repo validators:

```bash
$PY scripts/validate_workflow_contract.py
$PY scripts/validate_workflow_policy.py
```

Rehearsal, gate, capacity and reaper, against scratch paths only:

```bash
OUT=/tmp/step4-rehearsal-$(date +%s)
$PY scripts/run_concurrency_rehearsal.py --output-dir "$OUT"      # exit 0, gate "passed"

DB=/tmp/step4-capacity-$(date +%s).db
$PY -c "from agent_taskflow.store import TaskMirrorStore; TaskMirrorStore('$DB').init_db()"
$PY scripts/runtime_control.py capacity --db-path "$DB"            # 1, source default, enforced false
$PY scripts/runtime_control.py set-capacity --db-path "$DB" --actor me \
  --max-concurrent-tasks 2; echo "exit=$?"                         # exit 2, gate blocked
$PY scripts/runtime_control.py set-capacity --db-path "$DB" --actor me \
  --max-concurrent-tasks 2 --evidence-path "$OUT/concurrency-rehearsal.json"   # ok
$PY scripts/reap_stale_runtime.py --db-path "$OUT/crash-recovery/state.db"     # idempotent: nothing left
```

Observed on this tree, in `/tmp/v1-step4-smoke` (evidence bound to `4552f4e`):

```
rehearsal exit=0
ok True gate passed checks 16 / 16
contention {'busy_timeouts': 0, 'busy_wait_seconds_max': 1.23, 'busy_waits': 130, 'lock_acquisitions': 412, 'lock_holder_seconds': 0.4}
refusals {'processes_dispatcher': {'RuntimeAdmissionError': 2, 'ValueError': 1},
          'processes_explicit': {'RuntimeAdmissionError': 3},
          'threads_dispatcher': {'RuntimeAdmissionError': 6, 'ValueError': 1},
          'threads_explicit': {'RuntimeAdmissionError': 7}}
capacity: 1 default enforced False
set-capacity no evidence exit=2
False blocked ['evidence_path is required to raise max_concurrent_tasks above 1']
set-capacity with evidence exit=0
True 2 configured enforced True 7fa509f0bbd0
reap: [] [] []
```

Once this branch is committed, `HEAD` moves, so that evidence no longer passes
the gate (§4.7.5). Re-run the rehearsal at the commit you want to deploy.

---

## 7. Governance

- Only `task/v1-step4` was pushed, with normal pushes. Nothing was pushed to
  or merged into `main`, nothing was force-pushed, and the branch was never
  rebased.
- The PR is a **draft**.
- The production database (`~/.agent-taskflow/state.db`) was never read or
  written. Every test and rehearsal uses a `TemporaryDirectory` or a fresh
  `--output-dir`, and the default-database path is tested to stay unused.
- No scheduler tick or scheduler entry point was run. No daemon, cron,
  systemd, nginx or deployment configuration was added or touched.
- No migration runs at process startup. The capacity migration runs only from
  `set-capacity`.
- No task was approved, closed, or marked complete. Human review remains the
  final gate.
