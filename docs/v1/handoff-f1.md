# Handoff — V1 FOLLOWUPS F1: Wire runtime progress into the execution loop

Branch: `task/v1-stepf1`, stacked on `task/v1-step3` (PR #197) at `35e6b02`,
which already contains Step 1.
Draft PR: https://github.com/anderson930420/agent-taskflow/pull/199 (draft, base `main`)
Instructions: `~/agent-taskflow-ops/v1/stepf1.md`. Spec: `~/agent-taskflow-ops/v1/SPEC.md`.
Follow-up definition: `~/agent-taskflow-ops/v1/FOLLOWUPS.md` F1.

**Status: implemented and ready for human review. No stop condition is open.**
The first round hit two stop conditions (§4.1, §4.2). Human rulings 10 and 11
resolved both, and this round applied them. No new stop condition fired. One
finding about Ruling 10c is flagged for the reviewer in §4.3. Every item in the
acceptance gate passes, none is skipped, and no pre-existing test went red.

The root `HANDOFF.md` belongs to Step 3 and was not modified.

---

## 1. Read-only inventory, and every file changed

No existing module was rewritten.

| File | Extend / Create | Change |
|---|---|---|
| `agent_taskflow/dispatcher.py` | extend | `RUNNABLE_STATUSES` is now `{created, queued, preparing}`: `created` added, `blocked` removed (Ruling 10a). A new `UNTOUCHED_REFUSAL_STATUSES = {blocked, paused}` makes their refusal write nothing (Ruling 10c). The Attempt id is captured right after the `preparing` claim and passed to both contexts. Prepare / Implementer / Validator progress is recorded. New optional `progress_store=` constructor argument. |
| `agent_taskflow/approved_task_runner.py` | extend | The same Attempt-id capture and progress writes on the Level 2 ExecutionEngine path. |
| `agent_taskflow/runtime_admission.py` | extend | `RuntimeAdmissionStore.claim()` accepts `{created, queued}` (line 299). It was `{queued, blocked}` at line 296. |
| `agent_taskflow/attempt_scoped_runtime_path.py` | extend | `AttemptScopedDispatcher` stages Attempt resources for `{created, queued}` (line 581). It was `{queued, blocked}`. |
| `agent_taskflow/executors/base.py` | extend | `ExecutorContext.attempt_id: str \| None = None`. |
| `agent_taskflow/validators/base.py` | extend | `ValidatorContext.attempt_id: str \| None = None`. |
| `agent_taskflow/runtime_progress_recorder.py` | **create** | A best-effort producer that writes only through Step 3's `RuntimeProgressStore`. |
| `mission-control/components/ActionPanel.tsx` | extend (Ruling 10b) | `STARTABLE_STATUSES` becomes `["queued", "preparing"]`. |
| `mission-control/components/StartDispatchPanel.tsx` | extend (Ruling 10b) | `canStart` becomes `queued \|\| preparing`. |
| `docs/mission-control-ui-state-model.md` | extend (Ruling 10b) | Lines 128 and 317: Start applies to `queued` and `preparing` only. |
| `tests/test_dispatcher.py` | extend (Ruling 10b) | Line 288 only: `test_successful_task_has_no_blocked_reason` seeds `status="queued"` instead of `"blocked"`. That test seeded no stale `blocked_reason`, so nothing else needed keeping. No other line in the file changed. |
| `tests/test_runtime_progress_wiring.py` | **create** | The F1 acceptance gate: 28 tests. |
| `docs/v1/handoff-f1.md` | **create** | This file. |

`created` was **not** added to either Mission Control startable set (Ruling 11).

Read but not modified: `canonical_runtime_path.py`, `canonical_runtime_schema.py`,
`runtime_admission_schema.py`, `lifecycle_runtime_path.py`,
`lifecycle_entrypoint_controls.py`, `lifecycle_control.py`, `attempt_schema.py`,
`attempt_scoped_runtime_compat.py`, `executor_process_runtime_path.py`,
`validator_process_runtime_path.py`, `reset_runtime_path.py`,
`execution_engine_approved_task_adapter.py`, `runtime_progress.py`,
`runtime_progress_store.py`, `runtime_progress_schema.py`,
`realtime_projection.py`, `status_vocab.py`, `task_status_reset.py`,
`api/main.py`, `mission-control/lib/taskState.ts`.

### Prior inventory facts, checked

| Fact given in stepf1.md | Result |
|---|---|
| `dispatcher.py:40` `RUNNABLE_STATUSES = {queued, blocked, preparing}` | Verified. Now `{created, queued, preparing}` at line 48. |
| Claim is `update_task_status("preparing")` at `dispatcher.py:215`, reaching `CanonicalRuntimeTaskStore._claim` | Verified. At runtime the store is the fully layered `ValidatorProcessRuntimeTaskStore`, and the admission store is `ResetAwareRuntimeAdmissionStore`. Both end in `RuntimeAdmissionStore.claim()`. |
| …or the SQL trigger `runtime_pickup_claim_after_preparing` | **Corrected.** After the canonical migration that trigger is an inert `WHEN 0` placeholder (`canonical_runtime_schema.py:184`), and `runtime_preparing_requires_canonical_claim` aborts any `preparing` transition that did not go through the claim. No task-status check lives in SQL. The Python check in `RuntimeAdmissionStore.claim()` is the only claimable-status gate. |
| `AttemptScopedDispatcher` has its own `{queued, blocked}` check | Verified (line 581). It only stages Attempt resource configuration. The claim is still the gate. |
| `Dispatcher` is patched by layered subclasses at import time | Verified: Canonical → AttemptScoped → LifecycleControlled. The changes live in the base `dispatch_task`, which every layer calls through `super()`. |
| Attempt id readable from `runtime_claim` / `reserved_runtime_claim` / `AttemptStore.get_active_attempt` | Verified. F1 uses **`runtime_claim`** (the live claim) on purpose. `reserved_runtime_claim` outlives the lease, so on a second run through the same store it could name the previous run's Attempt. |
| `ExecutorContext` / `ValidatorContext` have no `attempt_id` | Verified. All 64 constructions in `agent_taskflow/`, `scripts/` and `tests/` use keyword arguments only. Nothing subclasses them or calls `fields()`/`asdict()` on them, so a trailing optional field is safe. |
| `record_step` does not require an active Attempt | Verified. Progress written after the lease is released still lands. |
| The Level 2 path runs through the ExecutionEngine and reads the reserved Attempt | Verified. The approved runner's `preparing` claim *is* the reservation the adapter reports. |
| Display ↔ persisted vocabulary is bridged only by `status_vocab.py` | Verified. F1 adds no copy of the mapping; it only names the persisted `created`. |

One extra fact: `run_approved_task` itself accepts only `queued`
(`approved_task_runner.py:270`). F1 leaves that unchanged. The Level 2 runner
serves approved GitHub-issue tasks, and nothing in F1 asks it to take Tickets.

---

## 2. What was implemented

### 2.1 Runnable and claimable statuses (Ruling 10a)

| Status | Dispatcher runnable | Claim accepts | Dispatch refusal writes | Changed by F1 |
|---|---|---|---|---|
| `created` (display `ready`) | yes | yes | n/a | **added** |
| `queued` | yes | yes | n/a | unchanged (legacy GitHub-issue path) |
| `preparing` | yes | n/a | n/a | unchanged |
| `blocked` | **no** | **no** | **nothing** | **removed** (SPEC §44) |
| `paused` | no | no | **nothing** | refusal no longer rewrites it to `blocked` |
| any other non-runnable status | no | no | `blocked`, reason `Task status is not runnable: <status>` | unchanged |

**Refusing `blocked` or `paused` (Ruling 10c).** `dispatch_task` returns the same
refusal result as before: `status="blocked"`, and
`summary` = `blocked_reason` = `Task status is not runnable: <status>`. It skips
`_block_task`, so nothing is written to `status` or `blocked_reason`, no task
event is recorded, and the claim is never reached, so no Attempt and no lease
are created. Every other non-runnable status keeps the existing branch
unchanged.

**Retry path for a `blocked` task (Ruling 10d).** After this change, the only
way to retry a `blocked` task is the operator reset CLI,
`scripts/reset_task_status.py` (`agent_taskflow/task_status_reset.py`). It
performs an operator-confirmed `blocked → queued` reset with a reserved retry
Attempt, and the task is then dispatched from `queued`. Mission Control has no
Start or Retry action for `blocked` any more, and the reset has no API route.
This is **accepted for V1** until Step 5 remaps failures to `failed` /
`needs_decision`. The reset flow does not depend on claiming `blocked`; its
reserved-Attempt adoption requires `tasks.status = 'queued'`
(`reset_runtime_path.py:123`).

### 2.2 Attempt id at claim

Right after `update_task_status("preparing")` returns, the dispatcher and the
approved runner read the live claim's Attempt id. They put it on
`ExecutorContext.attempt_id` and on every `ValidatorContext.attempt_id`.

- **Plain dispatcher path:** set when the contexts are constructed.
- **Level 2 path:** set with `dataclasses.replace` after `_build_executor_context`.
  `_build_executor_context` is wrapped by `attempt_scoped_runtime_path` with a
  fixed signature, so changing that signature would break the wrapper.
- Both contexts reject an `attempt_id` that disagrees with an attached
  `launch_binding.attempt_id`. This follows the existing task-key / worktree /
  artifact-root consistency checks. The two always agree today because both come
  from the same claim.
- `attempt_id` is `None` when no claim exists, for example when an executor is
  called directly in a unit fixture.

### 2.3 Runtime progress writes

`agent_taskflow/runtime_progress_recorder.py` binds to the claimed Attempt and
writes only through `RuntimeProgressStore.record_step` and
`.set_current_activity`. Every step transition also sets `current_phase` to
that step and `current_activity` to the same factual text.

| When | Step → status | `current_activity` |
|---|---|---|
| Right after the claim | Prepare → `running` | `Task claimed; preparing executor <executor>` |
| Executor context built | Prepare → `passed` | `Executor <executor> context prepared` |
| Executor unavailable (dispatcher) | Prepare → `failed` | `Executor <executor> is unavailable` |
| Workspace provisioning failed inside or after the claim | Prepare → `failed` | `Workspace preparation failed` |
| Model or prompt missing (Level 2) | Prepare → `failed` | `Executor <executor> requires a model` / `Implementation prompt is unavailable` |
| Just before `executor.run` | Implementer → `running` | `Running executor <executor>` |
| Executor returned | Implementer → `passed` / `failed` | `Executor <executor> returned <status>` |
| Executor raised | Implementer → `failed` | `Executor <executor> raised <ExceptionClass>` |
| Before the validator loop | Validator → `running` | `Running validators: <names>` |
| Before each validator | activity only | `Running validator <name>` |
| Validator failed / blocked / raised | Validator → `failed` | `Validator <name> returned <status>` / `raised <ExceptionClass>` |
| All validators done | Validator → `passed` | `Validators finished: <name> <status>, …` |

- **`passed` vs `failed` follows the lifecycle decision already in the code.**
  The step is `failed` exactly when the dispatcher or runner treats the result as
  a failure (`failed` or `blocked`). `completed` and `skipped` count as `passed`.
  F1 asks for `passed`/`failed` only, so an executor `blocked` result, including
  a cooperative operator kill, is recorded as step `failed`, not as §14.1
  `blocked`.
- **§14.2.** Text is built only from step names, executor and validator names,
  result statuses and exception class names. Executor and validator summaries
  are free text, so they are never copied in. A test feeds an executor summary
  of `85% done, ETA 2 minutes remaining` and asserts that no progress payload
  contains an estimate. Step 3's write-time guard (`assert_no_progress_estimate`)
  still applies to every write.
- **Progress is not lifecycle.** Every write is wrapped. A failure is logged on
  the `agent_taskflow.runtime_progress_recorder` logger and swallowed. The first
  failure in a run logs at `WARNING` and later ones at `DEBUG`, because a
  database without Step 3's tables fails every write the same way. No lifecycle
  call was moved, reordered or made conditional.
- **Nothing migrates at dispatch time.** The recorder never calls
  `migrate_runtime_progress` or `init_db`. On a database where the operator has
  not run `scripts/migrate_runtime_progress.py`, dispatch runs exactly as before,
  logs one warning per run, and creates no table. A test pins this.
- **Scout, Planner, Reviewer: left `pending`.** No executor exposes those phases
  deterministically. `executors/pi_orchestrator.py` names scout / planner /
  implementer / reviewer roles, but only as sections of one rendered prompt, and
  all of them run inside a single `executor.run()`. Writing them would fabricate
  progress.
- **Integration: not written.** It is out of scope (Step 2 code, Step 5 wiring).
- **No projection code was added.** The Step 3 board and Ticket projections
  already select the latest Attempt by `attempt_number`, so they show the new
  writes as they are.

---

## 3. Acceptance gate → tests

All tests are in `tests/test_runtime_progress_wiring.py`: 28 tests, all passing,
none skipped.

| # | Requirement | Test class | Result |
|---|---|---|---|
| 1 | A Step 1 Ticket can start | `CreatedTicketStartsTests` | pass, at the claim and dispatch level with a worktree-bearing fixture (Ruling 11; §4.2) |
| 2 | Progress is recorded; board and Ticket projection show it | `ProgressIsRecordedTests` | pass |
| 3 | Failure is visible | `FailureIsVisibleTests` | pass |
| 4 | §44 Blocked Ticket cannot execute | `BlockedTicketCannotExecuteTests` | pass: skip removed, the three tests unedited |
| 5 | §44 Paused Ticket cannot acquire work | `PausedTicketCannotAcquireWorkTests` | pass |
| 6 | Legacy `queued` regression | `LegacyQueuedRegressionTests` | pass |
| 7 | Progress is not lifecycle | `ProgressIsNotLifecycleTests`, `Level2ExecutionEnginePathTests.test_raising_progress_store_does_not_change_level2_outcome` | pass |
| 8 | Level 2 path | `Level2ExecutionEnginePathTests` | pass |
| 9 | §14.2 negative | `NoProgressEstimateTests` | pass |
| R10c | Refusing `blocked` / `paused` leaves the row untouched | `RefusalLeavesRowUntouchedTests` (added this round) | pass |

What the tests assert, beyond the table in stepf1.md:

- **1:** one claim (lifecycle events into `preparing`), one lease, one Attempt, and
  that Attempt's id on the executor context and on both validator contexts. It
  also calls `RuntimeAdmissionStore.claim()` directly on a `created` Ticket.
- **2:** a recording wrapper around the real store captures the status held
  *before* each write. Each wired step therefore shows exactly two recorded
  transitions, `pending → running` then `running → passed`, and the six
  transitions arrive in order. Inside the fake executor the live DB reads
  Prepare `passed`, Implementer `running`, Validator `pending`, and the Step 3
  board puts the Ticket in `RUNNING` with Implementer `running`. Inside the
  validator, Validator is `running`. Afterwards `build_board_projection` and
  `build_ticket_projection` show the three steps `passed` and the other four
  `pending`.
- **3:** also covers a raising executor (Implementer `failed`), a failing
  validator (Validator `failed`), and an unavailable executor (Prepare `failed`).
- **4 / 5:** shared assertions. Dispatch refuses and neither executor nor
  validators run. `RuntimeAdmissionStore.claim()` and the installed
  `CanonicalRuntimeAdmissionStore.claim()` both raise `not claimable` and leave
  the status as it was. The layered runtime store's
  `update_task_status("preparing")` raises. Zero Attempts and zero leases exist
  afterwards.
- **R10c:**
  - `test_dispatching_blocked_keeps_the_original_blocked_reason`: a `blocked`
    task with `blocked_reason="tests failed on AT-0101"` keeps it.
  - `test_dispatching_paused_leaves_it_paused`: a `paused` task stays `paused`.
  - In both, the whole `tasks` row is identical before and after dispatch, the
    task-event count is unchanged, and no Attempt or lease exists. The refusal
    result text is today's `Task status is not runnable: <status>`.
  - `test_other_non_runnable_statuses_keep_the_blocking_refusal`: an `unknown`
    task is still rewritten to `blocked` with the existing reason.
  - See §4.3 for one fixture step these tests need.
- **6:** a `queued` task gives the same `DispatcherResult` values, the same
  `status_changed` sequence
  (`preparing → implementing → validating → waiting_approval`) and the same
  evidence rows as before. A second test does this on a database without Step 3's
  tables: same outcome, exactly one warning, and no table created.
- **7:** a store that raises on every call leaves the success outcome
  (`waiting_approval`, closed Attempt with `validation_result=passed`) and the
  failure outcome (`blocked`, same reason) unchanged. On the Level 2 path the
  recorder's `RuntimeProgressStore` is patched to raise, and the run still binds
  its canonical Attempt.
- **8:** runs the real `ApprovedTaskRunnerExecutionEngineAdapter` over the real,
  fully wrapped `run_approved_task`, on a real Git repository. The adapter's
  `canonical_attempt_id` (the reserved Attempt) equals the executor's and the
  validator's `attempt_id`. That Attempt's progress shows the three steps
  `passed`, and it is the Ticket's latest Attempt. A failing-executor variant
  shows Implementer `failed` and Validator `pending` on the reserved Attempt.

Tests changed outside the new file: only `tests/test_dispatcher.py:288`
(Ruling 10b).

---

## 4. Stop conditions

### 4.1 Removing `blocked` breaks an operator flow and a pre-existing test — HIT in round 1, RESOLVED by Ruling 10

**Round 1.** stepf1.md's watchlist said to stop if removing `blocked` broke an
operator flow. It did:

- Mission Control enabled Start for `blocked`: `ActionPanel.tsx:20` and
  `StartDispatchPanel.tsx:43-46`, calling `POST /api/tasks/{key}/start`.
- The documented contract said so: `docs/mission-control-ui-state-model.md:128`
  and `:317`.
- `tests/test_dispatcher.py:287` dispatched a `blocked` task and expected it to
  run.

`blocked` was left in place, and acceptance 4's tests were written and skipped.

**Ruling 10.** `blocked` is removed, because SPEC §44 is authoritative. Applied
this round:

- (a) `blocked` was dropped from the three sets, and the skip was removed. The
  three tests pass unedited.
- (b) The collateral outside F1's layers was authorized for this change only:
  both frontend startable sets, the two doc lines, and one line of
  `tests/test_dispatcher.py`.
- (c) Refusing `blocked` / `paused` writes nothing (§2.1).
- (d) The reset CLI is the only retry path for `blocked` (§2.1).

**Ruling 10c follow-on check.** Ruling 10c says to list as a stop condition any
pre-existing test that asserted the old rewrite for `blocked` or `paused`.
**None exists.** The full suite passed with no test changed for this reason. An
earlier sweep had found no test asserting the `Task status is not runnable`
message and no test dispatching a `paused` task. No test was changed under this
clause.

### 4.2 A real Step 1 Ticket cannot start: no worktree record — HIT in round 1, RESOLVED by Ruling 11

**Ruling 11.** Step 5 (worktree-per-Ticket) owns creating a Ticket's worktree
record before dispatch. F1's acceptance item 1 is met at the claim and dispatch
level with a worktree-bearing fixture. Mission Control does not offer Start for
`ready` Tickets until Step 5. No code was written for this ruling.

**Known gaps handed to Step 5:**

1. **A Ticket made by Step 1's own creation path cannot start.** Step 1 stores
   the derived branch and worktree path on `tasks` only and writes no
   `task_worktrees` row (Step 3 HANDOFF §4.9). The dispatcher checks governance
   *before* the claim and refuses. Round 1 ran the real `create_ticket` and then
   `Dispatcher.dispatch_task` against a scratch database under `/tmp`:

   ```
   created: AT-0001 created worktree row: None
   dispatch: blocked | Task worktree not found: AT-0001
   after: blocked attempts: 0
   ```

2. **`created → blocked` governance side effect.** That refusal goes through the
   existing governance branch (`_block_task`), which is not the untouched
   refusal of Ruling 10c. The Ticket is written `created → blocked` with
   `blocked_reason = "Task worktree not found: <key>"`. After Ruling 10 that
   `blocked` Ticket is no longer runnable, so a second `/start` is refused
   without a write. The only way back is the reset CLI (`blocked → queued`),
   which then fails the same governance check until a worktree record exists.
   Mission Control cannot trigger this path, because Start is not offered for
   `created`. It is still reachable through `POST /api/tasks/{key}/start` and
   `scripts/run_dispatcher.py`.

### 4.3 Finding for the reviewer (not a stop condition): the dispatch entry backfills `task_id`

While writing the Ruling 10c tests, a whole-row before/after comparison showed one
column changing on a refused `blocked` / `paused` task: `task_id`, from `NULL` to
`task:<key>`.

- **Cause.** The refusal does not write it; it is a pre-existing migration on
  the dispatch entry. `LifecycleControlledDispatcher.dispatch_task` calls
  `RuntimeControlStore.assert_admission_allowed` *before* the status is read.
  That calls `init_db()` (`lifecycle_control.py:581`), which runs
  `migrate_task_attempt_lifecycle`. That migration backfills a missing `task_id`
  on **every** `tasks` row, whatever its status (`attempt_schema.py:66-73`).
- **Scope.** It only affects rows inserted without a `task_id` since the last
  migration run. It is identity, not lifecycle: `status`, `blocked_reason`,
  events, Attempts and leases are untouched. Every item Ruling 10c enumerates
  holds.
- **How the tests handle it.** They apply that one-time migration before the
  "before" snapshot and then compare the whole row. A comment in the fixture
  records why.
- **Not repaired.** Changing when the lifecycle migration runs is outside F1's
  layers, and "no migrations at process startup" is already an open repo-wide
  question (Step 3 HANDOFF §4.8).
- **Why not a stop condition.** Ruling 10c's enumerated requirements are all
  met. If the reviewer reads "row untouched" as covering identity columns too,
  this is where that reading breaks.

### Not hit this round

- **No pre-existing test went red.** Baseline at `35e6b02`: Ran 4776 tests, OK
  (skipped=8). This branch: Ran 4804 tests, OK (skipped=8). The difference is
  exactly the 28 new tests in `tests/test_runtime_progress_wiring.py`, and the 8
  skips are the same pre-existing ones.
- No forbidden layer was entered. Nothing changed in the scheduler loop,
  capacity, priority, `blocked_by`, worktree creation, integration handoff,
  failure or recovery target statuses, the `RuntimeProgressStore` schema, the
  board, SSE, or startup migrations.

---

## 5. Semantic gaps recorded (not repaired, by instruction)

1. **Failure and recovery targets are unchanged.** Executor failure, validator
   failure, governance refusal and lease expiry still write `blocked`. They are
   not remapped to §29.2 `failed` or §29.1 `needs_decision`; that is Step 5.
   Combined with Ruling 10, every such task now needs the reset CLI to retry
   (§2.1).
2. **An executor `blocked` result is recorded as Implementer `failed`**, because
   F1 specifies passed/failed. §14.1's `blocked` step status is unused.
3. **Unexpected exceptions between claim and executor start** (for example an
   `OSError` writing the mission contract, or a DB error recording the manifest
   artifact on the Level 2 path) leave Prepare at `running` on the closed
   Attempt. The lifecycle outcome is unchanged. Only the explicit failure
   branches and a raising `preparing` transition record Prepare `failed`.
   Wrapping the whole preparation block would have meant re-indenting a large
   part of `run_approved_task`.
4. **An Attempt that fails resource allocation inside the claim shows all steps
   `pending`.** `preclaim_runtime` releases that claim itself before raising, so
   no live claim remains to bind progress to.
5. **Level 2: the Codex advisory evidence gate runs after Validator `passed`.**
   A run it blocks shows Validator `passed` with the task `blocked`. The gate is
   not a §14.1 step, and Reviewer is not written (§2.3).
6. **`mission-control/lib/taskState.ts` still describes `blocked` as terminal
   with no allowed actions**, and the Reject action is still enabled for
   `blocked` (`ActionPanel.tsx` `REJECTABLE_STATUSES`). Neither was in Ruling
   10b's scope, and both are consistent with Start no longer being offered.

---

## 6. Validation (run for this handoff)

All Python commands used the repo venv from the worktree root. Test runs set
`HOME` to a scratch directory so nothing could resolve the default
`~/.agent-taskflow/state.db`. Every command below was run in the foreground and
had finished before this file was written.

| Command | Result |
|---|---|
| **BEFORE:** full suite at `35e6b02`, before any F1 change | `Ran 4776 tests in 594.571s` — `OK (skipped=8)` |
| F1 gate: `python -m unittest tests.test_runtime_progress_wiring -v` | `Ran 28 tests in 20.177s` — `OK` |
| F1 gate + `tests.test_dispatcher` | `Ran 79 tests in 35.264s` — `OK` |
| **AFTER:** full suite in six parts, run with `unittest discover -s tests -p <pattern>` | total **Ran 4804 tests — OK (skipped=8)** |
|  part `test_[a-b]*.py` (23 files) | `Ran 420 tests in 51.606s` — `OK` |
|  part `test_[c-e]*.py` (50 files) | `Ran 750 tests in 98.235s` — `OK` |
|  part `test_[f-l]*.py` (26 files) | `Ran 281 tests in 28.532s` — `OK` |
|  part `test_[m-q]*.py` (39 files) | `Ran 974 tests in 27.413s` — `OK` |
|  part `test_r*.py` (62 files) | `Ran 868 tests in 222.367s` — `OK (skipped=8)` |
|  part `test_[s-z]*.py` (79 files) | `Ran 1511 tests in 66.678s` — `OK` |
| `python -m compileall -q agent_taskflow scripts tests` | exit 0 |
| `python scripts/validate_workflow_contract.py` | exit 0, `status: passed` (`WORKFLOW.md`) |
| `python scripts/validate_workflow_policy.py` | exit 0, `status: passed` (`examples/workflow-policy.example.json`) |
| `cd mission-control && npm ci --prefer-offline` | exit 0. Installed from the lockfile; `package.json` and `package-lock.json` unchanged. |
| `npm run typecheck` (`tsc --noEmit`) | exit 0 |
| `npm run build` (`next build`) | exit 0, `✓ Compiled successfully`, all 7 routes generated |

The six patterns cover all 279 `tests/test_*.py` files exactly once
(23 + 50 + 26 + 39 + 62 + 79). The part counts add up to 4804 = 4776 + 28.

No test went red at any point this round. The full suite prints one
`Runtime progress write … no such table` line per dispatch in existing fixtures.
Those fixtures never install Step 3's tables, so this is the intended single
warning, and no test fails because of it.

---

## 7. Exact commands to verify

From the worktree root:

```bash
PY=/home/ubuntu/agent-taskflow/.venv/bin/python
export HOME=$(mktemp -d)   # optional: keeps every run off ~/.agent-taskflow
export GIT_AUTHOR_NAME=Test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=Test GIT_COMMITTER_EMAIL=test@example.invalid

# F1 acceptance gate (28 tests)
PYTHONPATH=. $PY -m unittest tests.test_runtime_progress_wiring -v

# Full suite in parts, each well under 10 minutes (4804 tests in total)
for p in 'test_[a-b]*.py' 'test_[c-e]*.py' 'test_[f-l]*.py' \
         'test_[m-q]*.py' 'test_r*.py' 'test_[s-z]*.py'; do
  PYTHONPATH=. $PY -m unittest discover -s tests -p "$p"
done

# Byte-compile and repo validators
PYTHONPATH=. $PY -m compileall -q agent_taskflow scripts tests
$PY scripts/validate_workflow_contract.py
$PY scripts/validate_workflow_policy.py

# Mission Control
cd mission-control
npm ci --prefer-offline
npm run typecheck
npm run build
```

---

## 8. Governance

- Commits are only on `task/v1-stepf1`. Only that branch was pushed, with a
  normal `git push -u origin task/v1-stepf1`; the draft PR targets `main`.
  Nothing was pushed to `main`, merged, rebased or force-pushed.
- No scheduler tick or scheduler entry point was run.
- `~/.agent-taskflow/state` and `~/.agent-taskflow/state.db` were never read or
  written. Every database was a temporary one or a scratch one under `/tmp`.
- Nothing was approved, closed, cleaned up or deleted.
- The collateral edits outside F1's layers are exactly the four Ruling 10b
  authorized, and nothing else.
