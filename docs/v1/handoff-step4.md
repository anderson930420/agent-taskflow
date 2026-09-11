# Handoff — V1 Step 4: Concurrency Readiness

Branch: `task/v1-step4`
Draft PR: https://github.com/anderson930420/agent-taskflow/pull/200
Base: `4552f4e` (`main`, after Steps 1 and 3 merged). `origin/main`
(`79e568d`, F1 #199) was merged in as `70dd2fa`, a normal merge commit, never a
rebase (§4.9).
Spec: `~/agent-taskflow-ops/v1/SPEC.md` §19, §42 Step 4
Instructions: `~/agent-taskflow-ops/v1/step4.md`; human ruling 15 (§4.1)

Status: **implementation-complete, awaiting human review. No stop condition is
open.** The §4.1 stop condition (capacity scope) was ruled on (ruling 15) and
is implemented. The fixture changes the ruling authorizes are listed there as
stop conditions hit. Two items need a reviewer's eye: the fixture-only
capacity setter the ruling requires (§4.1, "How fixtures set their value") and
a pre-existing `claim()` side effect found while pinning the F1 interaction
(§4.8). Not approved, not merged. The PR is a draft.

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
| `agent_taskflow/runtime_capacity_schema.py` | create | Additive tables that store a chosen value. They are not needed for enforcement (ruling 15). |
| `agent_taskflow/runtime_capacity.py` | create | Read, set (evidence-gated), set for disposable fixtures (ungated, audited), and audit the global limit. |
| `agent_taskflow/concurrency_gate.py` | create | Read-only evidence gate, modelled on `m1_exit_gate.py`. |
| `agent_taskflow/runtime_reaper.py` | create | `reap_stale_runtime()`: one idempotent reaper call. |
| `agent_taskflow/concurrency_rehearsal.py` | create | §19.1–§19.3 rehearsal, fixtures, race harness, checkers. |
| `agent_taskflow/concurrency_rehearsal_worker.py` | create | Separate-process worker for the rehearsal and tests. |
| `scripts/reap_stale_runtime.py` | create | Reaper CLI. |
| `scripts/run_concurrency_rehearsal.py` | create | Rehearsal CLI; writes evidence and gates it. |
| 8 `tests/test_*.py` files | create | The acceptance gate (§3). |
| `agent_taskflow/m1_project_class_control_rehearsal.py` | extend (**ruling 15b collateral**) | Its disposable fixture sets `max_concurrent_tasks = 3` (§4.1). |
| `tests/test_project_class_controls.py` | extend (**ruling 15b collateral**) | One test sets `max_concurrent_tasks = 3` (§4.1). The only pre-existing test file changed. |

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
refusal. Each race runs on its own fresh database at the production default
`max_concurrent_tasks = 1`. The capacity check runs first in the claim
transaction, so at that default most losers are refused by it. Observed in the
final rehearsal:

- explicit API: `RuntimeCapacityExceededError` (the winner's lease fills the
  single slot). `RuntimeAdmissionError` ("not claimable from status preparing")
  and `ActiveAttemptExistsError` are also accepted.
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

- **Setting:** `max_concurrent_tasks`, **global**, **default 1 on every
  database** (ruling 15). A database that never stored a value needs no
  migration to be bounded. A chosen value is stored in the runtime-control
  database beside `runtime_controls`: one `runtime_capacity_controls` row
  (`global` scope only), plus an **append-only**
  `runtime_capacity_control_events` table whose UPDATE and DELETE are refused
  by triggers. It is reached through `RuntimeControlStore` and
  `scripts/runtime_control.py capacity|set-capacity`. No new ad-hoc file.
- **Enforced atomically at claim time:** `assert_runtime_capacity_available(conn)`
  is its own function, called first inside the claim's `BEGIN IMMEDIATE` in
  both lease-creating claim transactions. It counts **active executor leases**:
  every `runtime_leases.is_active = 1` row, so an expired but unreaped lease
  keeps its slot until the reaper runs. A claim that would exceed the limit
  raises `RuntimeCapacityExceededError(RuntimeAdmissionError)` with
  `reason_code = "runtime_capacity_exceeded"`, `max_concurrent_tasks` and
  `active_executor_leases`. The transaction rolls back: no Attempt, no lease,
  no event.
- **Gate:** `set-capacity` refuses a value above 1 unless
  `concurrency_gate.evaluate_concurrency_evidence()` passes the evidence file.
  That requires the schema version, `repo_sha` equal to the audited
  repository's `HEAD`, `disposable_database: true`,
  `production_database_touched: false`, and every one of the 16 §19.1–§19.3
  checks `true`. The gate is read-only and fails closed on a missing,
  unreadable or malformed file. A refusal writes nothing. An accepted value
  records the evidence path, SHA-256 and repo SHA on the row and the event. A
  `CHECK` constraint refuses any limit above 1 that has neither a 64-hex
  evidence hash nor the disposable-fixture reason code, even by direct SQL.
  Lowering to 1 never needs evidence.
- **Disposable fixtures** set their value with
  `runtime_capacity.set_disposable_fixture_capacity()` (§4.1).

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
| 19.4 / 43.9 / 43.10 | default 1; second claim refused at limit 1; raise without passing evidence refused; with evidence and limit K at most K active | `test_runtime_capacity` (32) |
| — | the real rehearsal evidence passes the gate and unlocks a limit above 1; tampered evidence is blocked | `test_concurrency_rehearsal` (11) |
| ruling 15 | every database bounded at 1 with no migration; disposable-fixture values labelled, audited, refused on the default DB; capacity check vs F1's status check | `test_runtime_capacity`: `EveryDatabaseIsBoundedTests`, `DisposableFixtureCapacityTests`, `CapacityAndF1StatusCheckTests` |

Test counts are unittest methods per file; `Ran` totals are in §5.

---

## 4. Stop conditions hit, and flags

Per step4.md: reported, **not repaired**.

### 4.1 Capacity scope — STOP CONDITION HIT, RESOLVED BY RULING 15

**What was reported.** SPEC §19 says `max_concurrent_tasks = 1` by default,
enforced inside the claim transaction. Existing code runs several claims at
once on one database. In an isolated scratch copy of `4552f4e` (`git archive`
into `/tmp/v1-step4-probe`, never this worktree), a strict "refuse when any
lease is active" check turned exactly 3 pre-existing tests red:

```
Ran 4776 tests in 531.320s
FAILED (errors=3, skipped=8)
ERROR: test_rehearsal_exercises_isolation_immediacy_and_append_only_controls (test_m1_project_class_control_rehearsal)
ERROR: test_cli_runs_without_site_packages (test_m1_project_class_control_rehearsal)
ERROR: test_project_pause_isolated_and_existing_attempt_remains_active (test_project_class_controls)
```

The first round therefore enforced the limit only once an operator deployed
it, and asked for a ruling.

**Ruling 15 (human):** enforce the limit on **every** database, default 1, no
opt-in. SPEC §19.4, §20 and §44 ("Concurrency is bounded") are authoritative.
`set-capacity` stays the way to change the value, and the rehearsal-evidence
gate stays for any value above 1. The tests and rehearsals that deliberately
hold several concurrent claims may set the value their scenario needs in their
fixture, and nothing else they assert may be weakened.

**Implemented (`8c8829a`).** `runtime_capacity_in_connection()` returns the
default of 1 when no value is stored, whether the capacity tables exist or not.
`assert_runtime_capacity_available()` always applies it; the `enforced` flag
and the "undeployed database is not gated" branch are gone.
`EveryDatabaseIsBoundedTests` pins the new behaviour: a database that never
stored a value refuses a second concurrent claim, and reading the setting or
refusing a claim never installs the tables.

**Consequence (ruling 15d).** Once this lands, **every existing database,
production included, is limited to 1 concurrent claim, with no migration and
no operator step.** Raising it requires `scripts/runtime_control.py
set-capacity --max-concurrent-tasks N --evidence-path <passing rehearsal
evidence for the deployed commit>`. The earlier advice to run `set-capacity
--max-concurrent-tasks 1` on production is obsolete.

**Authorized collateral: stop conditions hit (ruling 15b).** Each fixture
now sets `max_concurrent_tasks` to the peak number of claims its scenario
holds, and nothing else changed. Both scenarios hold A2, B1 and A1 at once.

| Pre-existing test or rehearsal | Change | Value |
|---|---|---|
| `agent_taskflow/m1_project_class_control_rehearsal.py` (the M1-D rehearsal; exercised by `test_m1_project_class_control_rehearsal.test_rehearsal_exercises_isolation_immediacy_and_append_only_controls` and `.test_cli_runs_without_site_packages`) | one call in its disposable fixture, right after `migrate_project_class_controls(db)`, before the first claim | **3** |
| `tests/test_project_class_controls.py::ProjectClassControlTests::test_project_pause_isolated_and_existing_attempt_remains_active` | one call at the top of that test only; the class's other tests keep the default of 1 | **3** |

All three pass afterwards, with every other assertion byte-identical. None
of them asserts anything incompatible with a bounded claim, so ruling 15c did
not fire. One ordering detail was checked. The M1-D rehearsal's "alternate
entry" claim (`RuntimeAdmissionStore(db).claim("AT-M1D-A1")` while A2 is
active) must fail with `RuntimePausedError`. With a value of 3 the capacity
check passes and the pause check still refuses it, so `alternate_denied` stays
`true`.

**How fixtures set their value — for the reviewer.** `set-capacity` cannot
serve a disposable fixture above 1, because the gate needs rehearsal evidence
bound to `HEAD`. The M1-D module (production code, runnable under `python -S`)
would have to fabricate Step 4 evidence, which CLAUDE.md forbids. The Step 4
rehearsal is worse off still: §19.2 has to run N concurrent claims in order to
*produce* that evidence. So ruling 15b needed a mechanism, and I added
`runtime_capacity.set_disposable_fixture_capacity(db_path, value, *,
fixture)`:

- refuses the default state database (`default_db_path()`, compared by path
  only, never opened);
- is not exposed by any CLI (a test asserts `runtime_control.py` never names
  it);
- writes the same row and append-only event as `set-capacity`, but with
  `reason_code = disposable_fixture_capacity`, `requested_by = fixture:<name>`
  and no evidence. `capacity` reports it as `source: disposable_fixture`, so it
  can never pass for an evidence-backed limit;
- is accepted by the table `CHECK` through that reason code. Direct SQL could
  forge it, just as it could forge an evidence hash before; the governed path
  is the gated operator command.

Users: the two fixtures above, §19.2 of the Step 4 rehearsal (`writers`, 4 by
default; §19.1 and §19.3 run at the default of 1), and three of Step 4's own
tests (`test_concurrency_writes`: 4; `test_lease_contention`: 12 and 4). If
the reviewer prefers another mechanism, only those call sites change.

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

### 4.8 Flag — a refused `claim()` still backfills a legacy row's `task_id` (pre-existing)

Found while pinning the capacity/F1 interaction. `RuntimeAdmissionStore.claim()`
runs `self.init_db()` (the lazy `migrate_runtime_admission` →
`migrate_task_attempt_lifecycle` chain) **before** its transaction. That
migration backfills `task_id = 'task:<key>'` on legacy rows. So claiming a
legacy `blocked` or `paused` Ticket that has no `task_id` yet sets that column,
even though the claim is refused. The claim transaction itself changes nothing,
at either capacity. Reproduced on **pristine `origin/main`** (a `git archive`
copy, no Step 4 code): the `task_id` of a `paused` Ticket goes from `None` to
`task:AT-Y` after a refused claim. Neither Step 4 nor the merge causes it.
F1's "refusing a `blocked` or `paused` task leaves the row untouched" is about
the dispatcher's refusal path, which never reaches `claim()`, and F1's tests
for it pass. **Not repaired** (outside Step 4's layers).
`CapacityAndF1StatusCheckTests` runs `init_db()` before its snapshots and says
why.

### 4.9 Merge of `origin/main` (F1) and conflict resolution

`git fetch origin`, then `git merge --no-ff origin/main` → merge commit
`70dd2fa`, parents `8c8829a` (this branch, after ruling 15) and `79e568d`
(`origin/main`: Step 1 #195, Step 3 #197, F1 #199). No rebase.

**Textual conflicts: none.** Git auto-merged the only file both sides changed,
`agent_taskflow/runtime_admission.py`. The two changes sit about 10 lines apart
in `claim()`, so nothing needed resolving by hand. F1's other files
(`dispatcher.py`, `attempt_scoped_runtime_path.py`, `approved_task_runner.py`,
`runtime_progress_recorder.py`, `executors/base.py`, `validators/base.py`, and
its tests) are **byte-identical to `origin/main`** on the merged tree
(`git diff origin/main HEAD` on them is empty).

**Semantic check of the claim transaction after the merge:**
`BEGIN IMMEDIATE` → `assert_runtime_capacity_available(conn)` (Step 4) →
`_ensure_task_identity` → `assert_admission_allowed` → F1's
`if task["status"] not in {"created", "queued"}` → the existing ownership
checks. F1's behaviour is unchanged:

- `blocked` and `paused` stay out of the claimable set (F1's line is intact)
  and out of the dispatcher's `RUNNABLE_STATUSES`.
- Persisted `created` stays runnable and claimable.
- Refusing a `blocked` or `paused` Ticket leaves the row untouched, both in
  the dispatcher (F1's own tests) and in the claim transaction at either
  capacity (`CapacityAndF1StatusCheckTests`, added after the merge, `8766ef9`).
- The one visible interaction is **which** refusal a `blocked` or `paused`
  Ticket gets while capacity is full: `RuntimeCapacityExceededError` (a
  `RuntimeAdmissionError`) instead of F1's "not claimable". Both refuse and
  write nothing.

### Not hit

- **No test went red in the full suite after the merge (§5).** The only
  pre-existing test files touched are the ruling 15b collateral in §4.1.
- The new `CapacityAndF1StatusCheckTests` first failed on its own too-strict
  snapshot (§4.8, a pre-existing `init_db` side effect). It is a new Step 4
  test, not a pre-existing one, and it was corrected to measure the claim
  transaction alone. Nothing pre-existing was repaired.
- No push was rejected; the branch was never rebased or force-pushed.

---

## 5. Validation

Runner: `python -m unittest discover -s tests`. After the merge, the suite was
run in four parts so that each one finishes inside the 600 s tool limit. The
parts cover all 287 test files exactly once (`test_[a-f]*`: 78, `test_[g-q]*`:
66, `test_r*`: 63, `test_[s-z]*`: 80).

| Point | Tree | Result |
|---|---|---|
| Original baseline | `4552f4e` | Ran 4776 tests, OK (skipped=8) |
| BEFORE the merge (this branch) | `6d4e633` | Ran 4872 tests, OK (skipped=8) |
| BEFORE the merge (`main` after F1) | `79e568d` | 4804 tests, the figure the ruling gives (not re-run here) |
| AFTER (ruling 15 + merge + F1-interaction test) | `8766ef9` | **Ran 4906 tests, OK (skipped=8)** |

AFTER by part (on `70dd2fa`, with the `test_r*` part re-run on `8766ef9`
after `test_runtime_capacity.py` gained one test):

| Part | Result |
|---|---|
| `-p 'test_[a-f]*.py'` | Ran 1220 tests in 219.3s, OK |
| `-p 'test_[g-q]*.py'` | Ran 1267 tests in 68.6s, OK |
| `-p 'test_r*.py'` | Ran 900 tests in 242.9s, OK (skipped=8) |
| `-p 'test_[s-z]*.py'` | Ran 1519 tests in 71.7s, OK |
| **Total** | **4906, OK (skipped=8)** |

- **4906 = 4804 + 102.** Step 4's eight files now hold 102 methods (6 + 10 + 8
  + 14 + 9 + 12 + 32 + 11): the original 96, +5 for ruling 15 (3 bounded-DB
  tests replace the 3 opt-in ones, plus 5 fixture-setter tests), and +1 for
  the F1 interaction.
- **No test went red.** Skips unchanged at 8.
- Step 4 acceptance tests + F1's `test_runtime_progress_wiring` +
  `test_dispatcher` + the two ruling 15b test files on the merged tree
  (`70dd2fa`): Ran 193 tests, OK.
- Superseded evidence, kept for history: the first round's strict-capacity
  probe (§4.1) ran 4776 tests with errors=3, and the pre-merge F1 scratch
  combination ran 205 tests, OK.

| Command | Result |
|---|---|
| `python -m compileall -q agent_taskflow scripts tests` (from the repo root) | OK |
| `scripts/validate_workflow_contract.py` | `status: passed` |
| `scripts/validate_workflow_policy.py` | `status: passed` |
| `scripts/run_concurrency_rehearsal.py --output-dir <fresh>` on `70dd2fa` | exit 0, **16/16 checks**, gate `passed` |
| the same on `8766ef9` | exit 0, **16/16 checks**, gate `passed` |
| `cd mission-control && npm run build` | **not run**: no `mission-control/` file changed on this branch |

Rehearsal on the merged commit (`70dd2fa`):

```
ok True gate passed checks 16 / 16 repo_sha 70dd2fa
max_concurrent_tasks {'19.1': 1, '19.2': 4, '19.3': 1}
refusals {'processes_dispatcher': {'RuntimeCapacityExceededError': 3},
          'processes_explicit': {'RuntimeCapacityExceededError': 3},
          'threads_dispatcher': {'RuntimeCapacityExceededError': 5, 'ValueError': 2},
          'threads_explicit': {'RuntimeCapacityExceededError': 7}}
contention busy_waits 106 timeouts 0 acq 412
```

---

## 6. Exact commands to verify

Run from the worktree root, `/home/ubuntu/agent-taskflow/.worktrees/v1-step4`.
Use the repo virtualenv; the system `python3` lacks `pydantic`:

```bash
PY=/home/ubuntu/agent-taskflow/.venv/bin/python
```

Full suite (~10 min), in one run or in the four parts used in §5, then byte-compile:

```bash
$PY -m unittest discover -s tests
# or, each part under 600 s:
for p in 'test_[a-f]*.py' 'test_[g-q]*.py' 'test_r*.py' 'test_[s-z]*.py'; do
  $PY -m unittest discover -s tests -p "$p"
done
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

Ruling 15b collateral and F1 on the merged tree:

```bash
$PY -m unittest tests.test_project_class_controls \
  tests.test_m1_project_class_control_rehearsal \
  tests.test_runtime_progress_wiring tests.test_dispatcher
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
$PY scripts/runtime_control.py capacity --db-path "$DB"            # 1, source default (no migration needed)
$PY scripts/runtime_control.py set-capacity --db-path "$DB" --actor me \
  --max-concurrent-tasks 2; echo "exit=$?"                         # exit 2, gate blocked
$PY scripts/runtime_control.py set-capacity --db-path "$DB" --actor me \
  --max-concurrent-tasks 2 --evidence-path "$OUT/concurrency-rehearsal.json"   # ok
$PY scripts/reap_stale_runtime.py --db-path "$OUT/crash-recovery/state.db"     # idempotent: nothing left
```

Observed on `8766ef9`, in `/tmp/v1-step4-smoke2`, with rehearsal evidence
produced at that commit:

```
capacity: 1 default counted active_executor_leases
set-capacity 2, no evidence: exit=2
set-capacity 2, evidence at HEAD: exit=0
True 2 configured 8766ef9
set-capacity 3, evidence from 70dd2fa: exit=2
['repo_sha must match the audited repository HEAD']
reap: [] [] []
```

The last `set-capacity` shows why evidence must be re-made for the commit you
deploy (§4.7.5). The handoff commit that follows `8766ef9` changes only this
file, but it still moves `HEAD`. Re-run the rehearsal at the commit you want
to deploy.

---

## 7. Governance

- Only `task/v1-step4` was pushed, with normal pushes. `origin/main` was merged
  **into** the branch with a normal merge commit (`70dd2fa`). Nothing was
  pushed to or merged into `main`, nothing was force-pushed, and the branch was
  never rebased.
- The PR is a **draft**.
- The production database (`~/.agent-taskflow/state.db`) was never read or
  written. Every test and rehearsal uses a `TemporaryDirectory` or a fresh
  `--output-dir`, and the default-database path is tested to stay unused.
- No scheduler tick or scheduler entry point was run. No daemon, cron,
  systemd, nginx or deployment configuration was added or touched.
- No migration runs at process startup. The capacity tables are created only
  when a value is written (`set-capacity` or a disposable fixture). Enforcement
  needs no migration.
- No task was approved, closed, or marked complete. Human review remains the
  final gate.
