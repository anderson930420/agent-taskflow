# Handoff — V1 Step 5: Parallel Scheduler

Branch: `task/v1-step5`
Draft PR: https://github.com/anderson930420/agent-taskflow/pull/201 (draft, base `main`)
Base: `a5fa8e2` (`main`, after Steps 1, 3, 4 and F1 merged)
Spec: `~/agent-taskflow-ops/v1/SPEC.md` §5, §7, §9, §20, §21, §29, §33.3, §42 Step 5, §43, §44
Instructions: `~/agent-taskflow-ops/v1/step5.md`; rulings 21 and 26–28 (`RULINGS.md`)

**Status: implemented and ready for human review. No stop condition is open.**
Round 3 (ruling 32) closed the independent review's single FAIL item: the
dependency gate now lives at runtime admission (§2.6).
Round 1 stopped during the read-only inventory on two stop conditions (S1, S2;
§3.1). Rulings 26, 27 and 28 resolved them and decided D1–D6. This round
implemented Step 5 under those rulings, with the acceptance-gate tests written
first. No new stop condition fired, and no pre-existing test went red or was
edited. Three interpretations of the rulings are flagged for the reviewer in
§3.2; none of them is a stop condition. An independent read-only review
subagent checked the implementation; its findings are fixed and listed in §2.5.
The PR is a draft. Nothing is approved or merged.

---

## 0. Round history

| Round | Commit(s) | What happened |
|---|---|---|
| 1 | `a18057f`, `f339d54` | Read-only inventory hit S1 and S2 (§3.1). Stopped, wrote this file, opened draft PR #201. Nothing implemented. |
| 1b | `2c77cc3` | A second builder run with the same prompt re-verified S1 and S2 against the code and changed nothing else (§10). |
| 2 | `d7b51c0` | Rulings 26–28 applied. Step 5 implemented with the acceptance-gate tests written first (§2, §5). |
| 2 | `ff7becd` | An independent read-only review of `d7b51c0` found 2 blocking and 5 should-fix issues plus 5 nits; all fixed with tests (§2.5). |
| 2 | `a12d708` | Handoff for round 2. |
| 3 | `68f9f58` | Independent review of PR #201: FAIL, one blocking item (§2.6). Ruling 32 applied: the dependency gate at runtime admission, 13 new tests. |
| 3 | this commit | This handoff. Validation in §7 is on `68f9f58`'s code. |

---

## 1. Inventory

### 1.1 Facts handed to Step 5 (verified in round 1, still true at `a5fa8e2`)

- A Step 1 Ticket had no `task_worktrees` row, so dispatch refused it with
  "Task worktree not found" and wrote `created → blocked`.
- Executor failure, validator failure, governance refusal and lease expiry all
  wrote `blocked`.
- The claim checks capacity first, then `{created, queued}`
  (`runtime_admission.py` `claim()`); `max_concurrent_tasks` is global, default 1.
- The reaper is `runtime_reaper.reap_stale_runtime` plus
  `scripts/reap_stale_runtime.py`.
- The installed runtime (`AttemptScopedRuntimeTaskStore`) gave every Attempt
  a new branch and worktree and overwrote `task_worktrees` with it (§3.1).
- Step 1 already stores one `tasks.blocked_by` and persists a Ticket created
  with it as `blocked`; nothing could set, remove or release it afterwards.
- `failed` and `needs_decision` already exist in `TASK_STATUSES` and
  `status_vocab.py`; §12 `completed` persists as `cleaned` (aliases
  `completed`, `done`).
- No preferred-order field exists.

### 1.2 Every file touched: extend or create

No existing module was rewritten.

| File | Extend / Create | Change |
|---|---|---|
| `agent_taskflow/ticket_lifecycle.py` | **create** | Ticket identity (`prompt IS NOT NULL`, safe on legacy schemas) and the §29 failure vocabulary. |
| `agent_taskflow/ticket_worktree_schema.py` | **create** | The ruling 26d rebuild of `attempt_resources`, its fail-closed gate and precondition check. |
| `scripts/migrate_ticket_worktree_resources.py` | **create** | The only thing that applies it (§2.1). |
| `agent_taskflow/ticket_worktree.py` | **create** | Idempotent, audited, fail-closed creation of the Ticket's one worktree and row (ruling 26b, e, f). |
| `agent_taskflow/attempt_resources.py` | extend | `allocate()` binds a Ticket's Attempt to the Ticket's worktree and branch; `_provision_ticket_workspace()` reuses it as left and audits it; `_activate()` gains optional `worktree_base_sha` and `summary`. Legacy allocation unchanged. |
| `agent_taskflow/canonical_runtime_path.py` | extend | `RUNTIME_RELEASE_TASK_STATUSES` adds `failed`, `needs_decision`; `_terminal_attempt_status` maps them; the post-release reason rewrite covers them. |
| `agent_taskflow/attempt_scoped_runtime_path.py` | extend | Uses that release set; a Ticket's in-claim allocation failure and workspace-preparation failure write `failed`. |
| `agent_taskflow/lifecycle_runtime_path.py` | extend | `_release` closes a `failed`/`needs_decision` with no pending executor/validator outcome as Attempt `failed` / `runtime_failed`; the reason rewrite covers them. |
| `agent_taskflow/lifecycle_control.py` | extend | One new reason code, `runtime_failed`. |
| `agent_taskflow/dispatcher.py` | extend | Ticket detection; round 3: an unreleased dependency is refused before worktree preparation, writing nothing; refusing to start a Ticket never writes; missing Step 5 migration refused untouched, naming the script; worktree ensured before the claim; every failure through `_fail()` (legacy → unchanged `_block_task`; a Ticket's pre-claim failure is a compare-and-set). |
| `agent_taskflow/runtime_admission.py` | extend | `expire_stale_leases` writes `failed` for a Ticket, `blocked` (unchanged) for a legacy task. Round 3: `RuntimeDependencyUnreleasedError` and `assert_dependency_released`, called in `claim()` after the capacity and status checks (§2.6). |
| `agent_taskflow/reset_runtime_path.py` | extend (round 3) | The reserved-retry adoption claim calls `assert_dependency_released` (§2.6). |
| `agent_taskflow/runtime_reaper.py` | extend | Docstring only. |
| `agent_taskflow/ticket_retry.py` | **create** | The Ticket retry (§33.3): `failed`/`needs_decision → created`, one audited compare-and-set. |
| `agent_taskflow/task_status_reset.py`, `scripts/reset_task_status.py` | extend | Accept `failed` and `needs_decision` for a Ticket, routed to `ticket_retry`; `to_status` is derived; the legacy `blocked → queued` path is unchanged. |
| `agent_taskflow/ticket_dependencies.py` | **create** | Set / replace / remove `blocked_by`, cycle detection, release, failed/cancelled-blocker propagation (§2.3). |
| `scripts/ticket_dependency.py` | **create** | The D6 operator CLI. |
| `agent_taskflow/ready_queue.py` | **create** | Eligibility and D3 ordering. |
| `agent_taskflow/parallel_scheduler.py` | **create** | The tick. |
| `agent_taskflow/scheduler_worker.py` | **create** | The one-Ticket worker process a tick starts. |
| `scripts/run_parallel_scheduler_tick.py` | **create** | The tick CLI. |
| `agent_taskflow/api/main.py` | extend | One line: `/start` reports `ok: false` for `failed` and `needs_decision` too (it already did for `blocked`). |
| `docs/attempt-scoped-runtime-resources.md` | extend | Both worktree contracts (ruling 26g). |
| `tests/step5_support.py`, `tests/step5_scheduler_worker.py` | **create** | Test fixtures and the test-only scheduler worker (not test modules). |
| `tests/test_step5_*.py` (7 files) | **create** | The acceptance gate, 105 tests (§5); `test_step5_dependency_admission.py` is round 3's. |
| `docs/v1/handoff-step5.md` | extend | This file. |

Not touched: `approved_task_runner.py` (ruling 27f), Step 2's modules (ruling
21), `RuntimeProgressStore` and its schema, the SSE endpoint, the board and
Ticket projections, `mission-control/`, `~/agent-taskflow-ops/` (ruling 27g).

---

## 2. What was implemented, ruling by ruling

### 2.1 Ruling 26 — One Ticket = One Worktree (step5.md layer 1)

- **26a.** `AttemptResourceManager.allocate()` looks the task up with
  `ticket_worktree_for()`. For a Ticket, the Attempt's `branch_name` is
  `tasks.branch` and its `worktree_path` is `tasks.worktree_path`; its
  artifact root, lock and PID paths stay per Attempt (`<artifact>/<attempt-id>/`).
- **26b.** `ticket_worktree.ensure_ticket_worktree()` creates the real worktree
  (`git worktree add <tasks.worktree_path> -b <tasks.branch> <tasks.base_branch>`)
  and the Ticket's single `task_worktrees` row, before any claim. It runs from
  the dispatcher (`_prepare_ticket_worktree`, before governance and the claim)
  and from the scheduler tick (before it starts a worker). It is idempotent: a
  worktree already registered on the Ticket's branch is left as is and writes
  no event, and one created concurrently by another process is treated as
  existing. Creation writes a `worktree_recorded` event
  (`kind: ticket_worktree_created`, with the base SHA). Provisioning at claim
  time never creates it: a Ticket worktree missing then is refused.
- **26c.** Legacy tasks never take the Ticket branch; they keep
  `attempt/<slug>/<n>-<suffix>` and `.worktrees/<slug>/<attempt-id>`.
  `test_retry_gets_new_branch_worktree_and_artifact_root` is unedited and green;
  `LegacyFreshWorktreeContractTests` shows the same on a migrated database.
- **26d.** `scripts/migrate_ticket_worktree_resources.py` →
  `migrate_ticket_worktree_resources()` (migration
  `v1_step5_ticket_worktree_attempt_resources`):
  - takes the stored `CREATE TABLE attempt_resources` SQL and removes exactly
    `UNIQUE` from `branch_name TEXT NOT NULL UNIQUE,` and
    `worktree_path TEXT NOT NULL UNIQUE,`; any other shape is refused;
  - in one `BEGIN IMMEDIATE` with foreign keys off: renames the old table aside,
    creates the new one from the relaxed SQL, copies every column of every row,
    drops the old table, restores its index and both triggers verbatim, refuses
    if the rebuild would introduce a foreign-key violation, records the migration;
  - is idempotent (a recorded migration returns without touching the schema), and
    the lazy `migrate_attempt_resources` (all `IF NOT EXISTS`) does not restore
    `UNIQUE` afterwards;
  - needs the task-mirror schema and never creates it (exit 2, names the fix);
  - **nothing applies it at startup.** `require_ticket_worktree_resources()`
    fails closed and names the script. It is called by Ticket dispatch (a
    refusal that writes nothing), by `ensure_ticket_worktree`, by
    `allocate()` for a Ticket, and at the start of the tick CLI (exit 2).
    Legacy tasks run with or without it. The API process does not gate on it:
    ~15 existing API test files and 9 smoke scripts enter the API lifespan with
    only Step 1's migration (Step 3 set no startup gate either); a Ticket
    started through `/start` without it is refused at dispatch, untouched,
    naming the script.
  - The schema-diff test (`test_schema_diff_is_exactly_the_two_unique_keywords`)
    asserts: only `attempt_resources`' SQL changed, by exactly those two
    keywords; every other table, named index, trigger and view is identical;
    the unique indexes lost are exactly `(branch_name)` and `(worktree_path)`,
    and `artifact_root`, `lock_path`, `pid_path` and `(task_id,
    attempt_number)` stay unique; the only new migration row is this one.
- **26e.** `_provision_ticket_workspace()` runs the Attempt in the worktree as
  the last Attempt left it. It never cleans, resets, checks out or discards.
  Each Attempt writes a `note` (`kind: ticket_worktree_reused`) with
  `attempt_number`, `dirty`, `head_sha` and `cleaned: false`. The Attempt's
  `base_commit` is the worktree `HEAD` it started from; the `task_worktrees`
  row keeps the base the worktree was created from.
- **26f.** A path that exists but is not registered in this repository, is
  registered on another branch, is registered but missing on disk, or a Ticket
  branch that exists without its worktree, is refused: nothing is deleted,
  recreated or reattached. A `note` (`kind: ticket_worktree_refused`,
  `deleted: false`, `recreated: false`) is written and the Ticket ends
  `failed` with the reason in its `status_changed` event.
- **26g.** `docs/attempt-scoped-runtime-resources.md` now has a "Two worktree
  contracts" table (legacy vs Ticket) and names the migration; the old section
  is retitled "Fresh retry contract (legacy tasks)".

### 2.2 Ruling 27 — the §29 failure vocabulary, Tickets only (layer 6)

| Failure (Ticket) | Ends | Where |
|---|---|---|
| governance refusal (`_validate_governance`, the opencode-prompt check) | `failed` | `dispatcher._fail(FAILURE_GOVERNANCE)` |
| worktree preparation (refused worktree, in-claim allocation or provisioning failure) | `failed` | `_prepare_ticket_worktree`, `attempt_scoped_runtime_path.py` |
| executor unavailable, raised, returned `failed`, returned `blocked` (a cooperative operator kill included) | `failed` | `dispatcher._fail(FAILURE_EXECUTOR)` |
| validator returned `failed` (red) | `needs_decision` | `dispatcher._fail(FAILURE_VALIDATOR_RED)` |
| validator unavailable, raised, returned `blocked` | `failed` | `dispatcher._fail(FAILURE_VALIDATOR_ERROR)` |
| runtime lease expired | `failed` | `RuntimeAdmissionStore.expire_stale_leases` |

- **27a.** No pre-existing test needed to change: every test in round 1's
  §3.2 list uses a legacy row, so under 27f its target stays `blocked` (§4).
- **27b.** `expire_stale_leases` decides per lease, inside its existing
  transaction: a Ticket gets `status = 'failed'`, `blocked_reason` NULL, and a
  `status_changed` event from `runtime_lease_reaper` whose message names
  `runtime_lease_expired`; a legacy task gets exactly what it got before. Still
  idempotent, no schema change, same return value, one lifecycle event and one
  status event per lease. The Step 4 rehearsal still passes 16/16 (§7).
- **27c.** `reset_task_status` accepts `--from-status failed` and
  `needs_decision` for a Ticket (a dependency-held `blocked` Ticket is sent to
  `scripts/ticket_dependency.py` instead); `to_status` is derived (`created`) and an
  explicit mismatch is refused. The Ticket retry (`ticket_retry.retry_ticket`)
  is one `BEGIN IMMEDIATE` compare-and-set `failed|needs_decision → created`
  that also writes a `status_changed` event and a `note`
  (`kind: ticket_retry_reset`, the full audit record) plus a JSON audit
  artifact under the Ticket's `reset-audit/`. It honours `--dry-run`,
  `--confirm-reset`, `--request-id` (replay), `--expected-old-attempt-id` and
  `--expected-reset-generation`, refuses a Ticket with an active Attempt or
  lease, with a previous Attempt's process possibly still alive (an active
  executor process, or Attempt resources `allocated`/`active`/
  `reap_blocked_live_pid`), and one whose `blocked_by` is unreleased. The next claim
  creates the new Attempt, in the same worktree. A legacy row cannot reset
  from `failed`/`needs_decision`; `--from-status queued` and
  `--to-status blocked` stay invalid choices. Step 4's rehearsal and
  `test_concurrency_crash_recovery` use legacy rows, so their
  `--from-status blocked` did not change (§3.2).
- **27d.** `failed` and `needs_decision` release the lease and the Attempt's
  resources in all three layers (the shared `RUNTIME_RELEASE_TASK_STATUSES`,
  `LifecycleRuntimeTaskStore._release`, `_terminal_attempt_status`). A pending
  executor/validator outcome still decides the Attempt status
  (`failed`/`executor_failed`, `validation_failed`/`validator_failed`,
  `blocked`, `execution_aborted`); with none, the Attempt closes `failed` /
  `runtime_failed`. `FailureVocabularyTestCase.assert_ends` checks every row
  of the table above leaves no active lease and no active Attempt, and
  `test_capacity_slot_is_free_right_after_a_failure` starts a second Ticket at
  capacity 1 immediately after a failure.
- **27e.** As the table. A cooperative operator kill is signalled by the
  lifecycle proxies as an executor or validator `blocked` result, so **it lands
  in `failed` and is retried through the reset path** (`--from-status failed`).
- **27f.** Legacy tasks are untouched: `_fail()` delegates to the unchanged
  `_block_task` with the same result fields, `expire_stale_leases` keeps
  `blocked`, and `approved_task_runner.py` is not modified.
  `LegacyTasksKeepBlockedTests` and the unedited pre-existing suites pin it.
- **27g.** `~/agent-taskflow-ops/taskflow_dc_notify.py` was not touched. **Failure
  alerts for Tickets depend on it being updated at deploy time:** it alerts on
  `blocked` but not on `failed` or `needs_decision`.
- Refusing to start a Ticket writes nothing, whatever its status (like
  `blocked`/`paused` after F1): a `failed` or `needs_decision` Ticket is
  retried through the reset path, and `/start` can no longer overwrite a
  Ticket, Step 2's `ready_for_integration`/`integrating` included, with
  `blocked`. Legacy tasks keep the existing blocking refusal.
- A Ticket's pre-claim failure (governance, worktree) is a compare-and-set on
  the status the dispatch read, so it can never overwrite a Ticket another
  process has claimed since.

### 2.3 Ruling 28 — D3 to D6 (layers 2 and 3)

- **D3.** `ready_queue.eligible_tickets()` orders by priority rank
  (`critical 0, high 1, normal 2, low 3`), then `created_at`, then
  `task_key`. No preferred-order column.
- **Eligibility.** A Ticket row with its derived worktree path and branch;
  status `created` (or a legacy-reset `queued`); no `blocked_by`, or one whose
  blocker is in a §12 `completed` status; no active runtime lease; no pause or
  kill control in force at the global, project (`tasks.project`) or task scope.
  That check reads `runtime_controls` on the ready queue's read-only
  connection. It matches the project directly because a never-claimed Ticket
  has no task identity yet; the claim re-checks every control anyway. Legacy
  `queued` tasks are not scheduled here (27f).
- **D4.** A running dependent (`preparing`, `implementing`, `validating`) whose
  blocker fails is never interrupted: `maintain_dependencies` reports it in
  `deferred_running` and leaves it. It is not claimable while it runs, and once
  its Attempt has ended the next maintenance moves it to `needs_decision`.
- **D5.** Release happens only for a `blocked` row that has `blocked_by` and a
  `blocked_reason` that is empty (Step 1 creation) or written by
  `ticket_dependencies` (`blocked_by <KEY>: …`). It moves `blocked → created`,
  clears `blocked_by` (SPEC §5.3 "解除"), and is audited
  (`kind: ticket_dependency_released`). A `blocked_reason` that records a
  failure is never released. Setting `blocked_by` on a `created` Ticket moves
  it to `blocked` with the dependency reason.
- **Failed or cancelled blocker (§5.4, §43.8).** A blocker in `failed`,
  `canceled` or `archived` sends each dependent that is not running, deciding,
  paused, completed, cancelled or already `blocked` by a failure to
  `needs_decision`, audited (`kind: ticket_dependency_blocker_stopped`; the
  message names the blocker and the choices). Nothing is released. The event
  records whether the dependent was only waiting on its dependency
  (`dependency_owned`). Only such a Ticket returns to ready when the
  dependency is removed or replaced; one that had failed or finished first
  stays `needs_decision` and needs the audited retry (§2.5 B2).
- **Validation (§5.2, §43.6).** `set_blocked_by` refuses a self-dependency, an
  unknown blocker, a non-Ticket dependent and any cycle (it walks the blocker's
  chain), inside one transaction, so a refusal writes nothing.
- **D6.** `scripts/ticket_dependency.py --db-path … {set,remove,retry}`:
  previews unless `--confirm`; `set` also replaces; `remove` returns a
  dependency-held Ticket (`blocked`, or `needs_decision` put there by a
  dependency) to `created`; `retry` runs the §33.3 retry through
  `reset_task_status`; refusals exit 2 and write nothing. No API route and no
  Mission Control control.

### 2.4 The scheduler loop (layers 4 and 5)

`parallel_scheduler.run_scheduler_tick(db_path, *, launcher=None, wait=True,
claim_timeout_seconds=60)` and `scripts/run_parallel_scheduler_tick.py
--db-path DB [--no-wait]`:

1. `reap_stale_runtime()` (Step 4's reaper, first).
2. `maintain_dependencies()` (release and stop).
3. Read `max_concurrent_tasks` and the eligible Tickets.
4. While the active executor leases are below the limit: take the next
   eligible Ticket, `ensure_ticket_worktree()` (a refusal ends it `failed` and
   the loop moves on), start one worker process for it, and wait until that
   worker's claim is observed (a new or adopted Attempt) or it exits without
   claiming. A worker refused for capacity ends the loop. A worker that has not
   claimed within the timeout is left running (never killed) and the tick
   starts nothing more; a candidate whose preparation raises is recorded and
   skipped.
5. With `wait` (the CLI default), wait for the started workers to finish and
   report their results; `--no-wait` returns once each is claimed.

- **Atomic claim, bounded concurrency.** The worker
  (`agent_taskflow.scheduler_worker`) runs the installed Dispatcher, so the
  Step 4 claim transaction, capacity check first, is the authority. The tick
  only picks; it never writes a claim itself. Several ticks, or a tick racing
  a manual `/start`, cannot exceed the limit or double-claim.
- **Several executors at once, each in its own worktree, one owner per
  Attempt** (§21): each worker is its own process holding its own lease.
- **No daemon, no cron, no background thread.** A worker handles one Ticket and
  exits. Production workers log to `<artifact base>/scheduler-worker-<utc>-<id>.log`,
  where the artifact base is the latest `attempt_resources.artifact_base_root`
  (the Ticket's own artifact directory), falling back to `tasks.artifact_dir`
  before the Ticket's first claim.
- **Integration handoff is not wired** (ruling 21, FOLLOWUPS F4).

### 2.5 Independent review of `d7b51c0`, and the fixes in `ff7becd`

A read-only review subagent (it wrote nothing and touched no database) read
the whole diff against rulings 26–28 and step5.md. I verified each finding
against the code before fixing it. Fixes with a dedicated test name it in
brackets; S3 and N1–N4 have none (their paths are races or internals the
existing tests exercise only indirectly).

| # | Finding | Fix |
|---|---|---|
| B1 (blocking) | The Ticket retry lacked the legacy reset's live-process guard; with a shared worktree, a retry could start while a previous Attempt's executor still wrote to it. | `ticket_retry` refuses while an executor process is `allocated`/`running`/`term_sent`/`kill_sent`, or an Attempt resource is `allocated`/`active`/`reap_blocked_live_pid` (run the reaper first). [`ReviewFixRetryGuardTests`] |
| B2 (blocking) | A failed/cancelled blocker turned a failure-`blocked` (or `failed`) dependent into a `needs_decision` that `remove`/`replace` then released without a retry, erasing the failure reason — a D5 bypass. | A failure-`blocked` dependent is left untouched (its reason kept). Each stop records `dependency_owned`; only a dependency wait (ready, or dependency-`blocked`) returns to ready when its dependency is removed or replaced. Anything else stays `needs_decision` for the audited retry. [`ReviewFixDependencyTests`] |
| S1 | `set_blocked_by` on a `queued` Ticket holding a legacy reserved retry Attempt stranded the reservation. | Refused; release also skips a row with an active Attempt. [`test_a_reserved_retry_attempt_blocks_setting_a_dependency`] |
| S2 | A Ticket in another non-runnable status (`ready_for_integration`, `integrating`, `archived`, `unknown`, …) was still rewritten `blocked` by a refused dispatch. | Refusing to start a Ticket never writes; legacy tasks unchanged. [`ReviewFixRefusalTests`] |
| S3 | A race could mark a healthy Ticket `failed`: an unconditional pre-claim `failed` write, and a lost concurrent `git worktree add` reported as a refusal. | Pre-claim Ticket failures are a compare-and-set on the status the dispatch read; `ensure_ticket_worktree` re-inspects before refusing and treats a worktree created meanwhile as existing. |
| S4 | A claim timeout killed the worker, which could orphan a lease committed a moment later. | The worker is left running and kept (a `wait` tick waits for it); the tick starts nothing more. [`test_a_slow_claimer_is_left_running_not_killed`] |
| S5 | One raising candidate aborted the whole tick, every tick. | Per-candidate errors are recorded in `not_started` and the loop continues; eligibility needs the derived worktree and branch. [`test_one_raising_candidate_does_not_abort_the_tick`] |
| N1 | The rebuild renames the old table first; a rename could rewrite another object's reference to the staging name. | `legacy_alter_table = ON` during the rebuild; docstring corrected. Nothing references `attempt_resources` today. |
| N2 | `is_ticket()` inside the in-claim release call could skip the release. | Decided before allocating. |
| N3 | The retry audit artifact and the worker log landed inside the previous Attempt's immutable artifact root (`tasks.artifact_dir` after a claim). | Both use the Ticket's artifact base (the latest `attempt_resources.artifact_base_root`), as the legacy reset audit does; worker logs are one file per launch. |
| N4 | The ready queue's pause check ran the lifecycle-control `init_db` per candidate. | It reads `runtime_controls` on its read-only connection. |
| N5 | Provisioning created a missing Ticket worktree after the claim (26b says before). | Refused at claim time; the dispatcher and the tick create it before the claim. [`ReviewFixClaimTimeWorktreeTests`] |

The reviewer also confirmed: legacy behaviour unchanged, `failed` /
`needs_decision` release the lease in every layer, the reaper contract is
unchanged apart from the Ticket status, the rebuild SQL copies every column
and restores the index and triggers, the retry compare-and-set is atomic, cycle
detection is correct, and D3 ordering is as ruled.

### 2.6 Ruling 32 — the dependency gate at runtime admission (round 3)

**The review's blocking item.** Neither `RuntimeAdmissionStore.claim()` nor
`Dispatcher.dispatch_task` read `blocked_by`, so SPEC §44 "Dependency releases
only after blocker completed" (§43.7) held only in the scheduler's selection.
The reviewer reproduced it:

1. A Ticket B ran once and failed.
2. An operator held it through the `/block` route, making it `blocked` with a
   free-text reason.
3. `set_blocked_by(B, A)` kept it `blocked` with that reason.
4. `reset_task_status --from-status blocked` passed, because its guard only
   refused a reason written by the dependency module. B became `queued` with
   a reserved retry Attempt and `blocked_by=A`.
5. `dispatch_task(B)` ran it while A was still `created`.

**Every path that can start a Ticket.** A read-only sweep of
`agent_taskflow/` and `scripts/`, which I checked against the code, found exactly
two transactions that create a lease and move a task to `preparing`:
`RuntimeAdmissionStore.claim()` (`runtime_admission.py`) and the reset-reserved
retry adoption `ResetAwareRuntimeAdmissionStore._try_claim_reserved_retry`
(`reset_runtime_path.py`). The installed admission class is
`ResetAwareRuntimeAdmissionStore`: its `claim()` runs the adoption first and
`RuntimeAdmissionStore.claim()` otherwise. Every entry point reaches one of the
two through the installed store:

- `POST /api/tasks/{key}/start` and `scripts/run_dispatcher.py` → the
  installed `Dispatcher` → `update_task_status("preparing")` → `_claim`
  → `ResetAware.claim`;
- `scheduler_worker` (started by the tick) → the same Dispatcher;
- `run_approved_task` and its wrappers (`scripts/run_approved_task.py`,
  the manual ExecutionEngine, queued handoff, the GitHub-issue one-task tick
  and automation) → `approved_task_runner` → the same claim. They accept only
  `queued`, which a Ticket reaches only through a legacy reset;
- the concurrency and M1 rehearsals → `claim()` directly, on disposable
  databases.

The runtime-handoff, one-shot and scheduler-watcher paths never execute (no
runner is injected). No executor context is built and no executor starts before
a claim. No production code writes a raw `preparing`.

Two paths outside the two transactions, neither reachable by a Ticket from
code:

- the legacy PR-3 trigger `runtime_pickup_claim_after_preparing`. It claims
  on a raw status write to `preparing`, but only on a database that never ran
  the canonical migration. That migration makes it `WHEN 0` and adds
  `runtime_preparing_requires_canonical_claim`, which refuses a raw
  `preparing`. Every runtime store applies it inside `_claim` before
  claiming, and Ticket dispatch requires the Step 5 migration, which
  installs it. Only an external raw SQL write, or a hand-written
  `TaskMirrorStore.update_task_status(key, 'preparing')`, on a Step-1-only
  database could fire it;
- `AttemptStore.create_attempt`, which creates an Attempt with no lease and
  no execution, and is called only by an M1 rehearsal.

**No path needed rerouting and no stop condition fired** (ruling 32f).

**The fix (`68f9f58`):**

- **32a.** `runtime_admission.assert_dependency_released(conn, task_key)`
  runs inside `claim()`'s transaction **after** Step 4's capacity check
  (still the first statement) and **after** F1's claimable-status check,
  before any write. It refuses a task whose `blocked_by` blocker is not in a
  §12 `completed` status (`cleaned`, `completed`, `done`). The same call runs
  in the adoption transaction, after its capacity check, its `queued` row
  match and its admission-control check. Both claim transactions are gated,
  as ruling 32 intends ("the dependency gate belongs at admission"). The
  adoption is the other half of the installed admission store's `claim()`,
  so this gates it rather than rerouting it.
- **32b.** The refusal is typed:
  `RuntimeDependencyUnreleasedError(RuntimeAdmissionError)`, with
  `reason_code = "runtime_dependency_unreleased"` and the task, blocker and
  blocker status. The transaction rolls back, so nothing is written: no
  status, no `blocked_reason`, no Attempt, no lease, no task event, and a
  reserved lineage stays `reserved`. The pre-existing `task_id` backfill runs
  before the transaction (ruling 14). An unknown blocker is refused with
  `blocker_status = None`.
- **32c.** `reset_task_status` refuses any Ticket with an unreleased or
  unknown blocker, on both the legacy `blocked → queued` branch and the
  Ticket retry. The message tells the operator to remove or replace the
  dependency with `scripts/ticket_dependency.py`. It can no longer make such
  a Ticket runnable.
- **32d.** The scheduler's selection filter (`ready_queue`) is kept. The
  dispatcher also refuses such a Ticket *before* worktree preparation. This
  earlier refusal exists so that a direct or API start writes nothing at all,
  not even a worktree audit event. The claim gate stays the authority.
- Shared helpers: `ticket_dependencies.unreleased_dependency_in_connection`,
  `unreleased_dependency`, and `unreleased_dependency_message`.

**Tests (`tests/test_step5_dependency_admission.py`, 13, written first):**

- `ResetRefusesTests`: the reviewer's Ticket (steps 1–3) cannot be reset,
  and the refusal names the blocker and the D6 CLI. An unknown blocker is
  refused too. Once the blocker is `cleaned`, the reset proceeds.
- `StartPathsRefuseTests`: the reviewer's post-reset state is built with
  `ResetLineageStore.reserve_retry`, the lower-level call a pre-fix reset
  made, so it is `queued` with a reserved retry Attempt and `blocked_by=A`.
  Then:
  - **(i)** direct `Dispatcher.dispatch_task`,
  - **(ii)** `POST /api/tasks/{key}/start` (`ok: false`, the blocker named),
  - **(iii)** a scheduler tick (B is not a candidate and is not started;
    the blocker itself may run),
  - and the installed adoption claim,

  are each refused. The row, events, Attempts and leases are identical
  before and after, the lineage stays `reserved`, and there is no active
  lease. Once A is `cleaned`, dispatch adopts the reserved Attempt and runs
  B, and the lineage becomes `claimed`.
- `ClaimRefusesTests`: `RuntimeAdmissionStore.claim()` refuses for six
  non-completed blocker statuses and for an unknown blocker, writing
  nothing. The capacity check still fires first. `cleaned`, `completed` and
  `done` each allow the claim, and a Ticket without `blocked_by` is
  unaffected.

---

## 3. Stop conditions

### 3.1 Round 1: S1 and S2 — HIT, RESOLVED by rulings 26 and 27

- **S1.** "One worktree per Ticket, a retry reuses it" contradicted the
  installed Attempt-scoped fresh-retry contract (documented in
  `docs/attempt-scoped-runtime-resources.md`, enforced by `UNIQUE` on
  `attempt_resources.worktree_path` / `branch_name`, pinned by
  `test_retry_gets_new_branch_worktree_and_artifact_root`). The Appendix A
  probe showed one Step 1 Ticket with three git worktrees after one retry.
  **Ruling 26** chose Option A; applied as §2.1.
- **S2.** The §29 remap would have turned pre-existing tests red (the 7
  `test_dispatcher` tests, the admission lease-expiry test and the F1
  progress-wiring failure tests), and remapping lease expiry needed a change to
  the Step 4 reaper, which step5.md forbade. **Ruling 27** authorized the
  remap for Tickets only; applied as §2.2.

### 3.2 This round: none hit. Interpretations flagged for the reviewer

1. **Lease expiry is remapped for Tickets only.** Ruling 27's header and 27f
   scope the remap to Tickets, while 27a–c authorize updating the admission
   test and the Step 4 rehearsal's `--from-status`. Those three use legacy
   rows (`TaskMirrorStore.upsert_task`, no `prompt`), so under 27f their
   targets stay `blocked`, and the authorizations went unused. Step 4's
   evidence path is therefore unchanged. If a global lease-expiry remap was
   intended, it is the per-row `ticket` choice in `expire_stale_leases` plus
   the authorized test updates.
2. **A validator that returns `blocked` ends `failed`, not `needs_decision`.**
   27e names validator `failed` → `needs_decision` and validator raised or
   unavailable → `failed`. A `blocked` result is how the lifecycle proxies
   report an operator kill during validation, and 27e requires a kill to land
   in `failed`. So only a red validator stops for a decision.
3. **The Ticket retry does not write a `reset_lineages` row.** That table's
   SQL `CHECK`s allow only `blocked → queued`, and reserved-Attempt adoption
   only looks at `queued` rows, so a lineage-based Ticket retry would need a
   second table rebuild and a change inside the adoption claim transaction.
   Neither is authorized. The Ticket retry is an audited compare-and-set to
   `created`, and the next ordinary claim creates the new Attempt, which is
   what §33.3 describes.

Not hit: no pre-existing test went red (§7), no forbidden layer was entered,
nothing migrates at startup, and no push was rejected.

### 3.3 Round 3: none hit

The review's FAIL item was a defect in this branch, not a stop condition;
ruling 32 decided the fix. The ruling 32f sweep found no path that starts a
Ticket outside the two admission transactions (§2.6), so nothing had to be
rerouted and nothing is left open. No pre-existing test went red, and none
was edited.

---

## 4. Pre-existing tests changed

**None.** Every pre-existing test file is byte-identical to `a5fa8e2`
(`git diff --name-status a5fa8e2 -- tests/` lists only `A` entries). Ruling 27a authorized
updating the round-1 §3.2 tests; none needed it, because each uses a legacy
row and keeps `blocked` under 27f:

| Test | Row | Result |
|---|---|---|
| `test_dispatcher.py`: `test_executor_failed_blocks_task`, `test_executor_blocked_blocks_task`, `test_validator_failed_blocks_task`, `test_validator_blocked_blocks_task`, `test_unknown_executor_blocks_task`, `test_unknown_validator_blocks_task`, `test_unknown_executor_still_blocks_task` | legacy (`upsert_task(TaskRecord)`) | unedited, green |
| `test_runtime_admission.py::test_stale_lease_reaper_aborts_attempt_and_blocks_task` | legacy | unedited, green |
| `test_runtime_progress_wiring.py` failure tests (F1) | legacy (no `prompt` column in its DB) | unedited, green |
| `test_concurrency_crash_recovery.py`, Step 4 rehearsal (`--from-status blocked`) | legacy | unedited, green; rehearsal 16/16 |

---

## 5. Acceptance gate → tests

105 new tests in 7 files, all passing: `test_step5_ticket_worktree` 12,
`test_step5_ticket_worktree_schema` 10, `test_step5_failure_vocabulary` 24,
`test_step5_dependencies` 25, `test_step5_ready_queue` 6,
`test_step5_parallel_scheduler` 15, `test_step5_dependency_admission` 13
(round 3).

| # | Requirement | Tests |
|---|---|---|
| §43.4 / §9 | One worktree per Ticket | `test_step5_ticket_worktree`: `test_step1_ticket_gets_one_worktree_one_row_and_dispatch_runs` (a real `create_ticket` Ticket, dispatched; exactly one git worktree on its branch, one row, the executor ran there); `test_retry_reuses_the_same_worktree_as_the_last_attempt_left_it` (fail with a dirty file, retry to `created`, the second Attempt sees the file, same path and branch, per-Attempt artifact/lock paths, reuse events `dirty: false, true`); idempotent ensure; fail-closed cases; migration gate; legacy contract kept; a worktree missing at claim time is refused, not created (`ReviewFixClaimTimeWorktreeTests`). `test_step5_ticket_worktree_schema`: schema diff, idempotency, row preservation, sharing only after the rebuild, script CLI. |
| §43.5 | Blocked / paused never execute | Unchanged F1 behaviour (F1's tests, unedited); `test_step5_ready_queue` shows neither is eligible, and `IdempotentLoopTests.test_a_tick_with_no_eligible_ticket_is_a_no_op` shows a tick leaves a paused Ticket's row and events untouched. |
| §43.6 | Invalid dependencies | `InvalidDependencyTests`: self, unknown blocker, unknown dependent, 2-cycle, 3-cycle, creation-time blocker; the rows and events of every Ticket involved are identical before and after. |
| §43.7 / §43.34 / §44 on every start path | Release after `completed` | Round 3, `test_step5_dependency_admission` (§2.6): `claim()`, the adoption claim, direct dispatch, `POST /start` and a tick all refuse a Ticket whose blocker is not completed, writing nothing; `reset_task_status` refuses it; a completed blocker makes it claimable. Round 2, `ReleaseOnlyAfterCompletedTests`: 12 non-completed blocker statuses plus a claimed (running) blocker release nothing; `cleaned`, `completed` and `done` each release, audited; a failure-`blocked` row is never released; idempotent. |
| §43.8 | Failed / cancelled blocker | `FailedOrCancelledBlockerTests`: `failed`, `canceled`, `archived` → dependent `needs_decision`, audited, no Attempt; D4 running dependent deferred, then stopped after its Attempt ends; the stopped dependent is neither eligible nor dispatchable. `ReviewFixDependencyTests`: a failure-`blocked` dependent keeps its reason; a failed dependent needs the audited retry, not just a removal; a dependency wait returns to ready when removed; a reserved retry Attempt blocks setting a dependency. |
| §43.10 | Capacity | `CapacityTests`: limit 1 with 3 eligible starts exactly the highest-priority one; limit 2 (disposable-fixture setting, ruling 17) with 3 eligible starts 2; a full slot starts nothing. |
| §21 | Parallel implementation | `test_three_tickets_run_at_once_in_their_own_worktrees`: at limit 3, three worker processes run the real Dispatcher; a barrier opens only when all three executors are running; three PIDs, three worktrees equal to the derived paths, one Attempt and one lease per Ticket with matching ids, three distinct owners. |
| §29.1 / §29.2 | Failure vocabulary | `test_step5_failure_vocabulary`: every row of §2.2's table, each audited, each leaving no lease or active Attempt; lease expiry for a Ticket (`failed`) vs legacy (`blocked`); stopped Tickets refused untouched; legacy failures still `blocked`; the retry path (API, dry run, confirmation, mismatch, unreleased dependency, legacy refusal, CLI, a possibly-live previous process: `ReviewFixRetryGuardTests`); a Ticket in any non-runnable status is refused untouched (`ReviewFixRefusalTests`). |
| — | Loop is idempotent | `IdempotentLoopTests`: two no-wait ticks at a full limit start nothing twice (one Attempt each); the tick calls the reaper then dependency maintenance first; an expired slot is reaped and refilled in the same tick; a tick with no eligible Ticket is a no-op. Plus `PreparationTests`, `TickCliTests` and `ReviewFixSchedulerTests` (a slow claimer is left running, one raising candidate does not abort the tick). |

---

## 6. Known limitations and follow-ups (not repaired)

1. **Integration handoff (FOLLOWUPS F4).** A Ticket whose implementation
   succeeds ends `waiting_approval`, as before; it is not queued for Step 2.
2. **Failure alerts (27g).** Update `taskflow_dc_notify.py` to alert on
   `failed` and `needs_decision` when deploying.
3. **Split vocabulary (FOLLOWUPS F5).** Legacy tasks keep `blocked`.
4. **Deploy runbook addition (FOLLOWUPS F3).** Run
   `scripts/migrate_ticket_worktree_resources.py --db-path <db>` after the
   Step 1 and Step 3 migrations; until then Ticket dispatch and the tick refuse
   Tickets, naming the script. Rehearsal evidence must be produced for the
   deployed commit before `set-capacity` above 1 (Step 4 §4.7.5).
5. **Mission Control** still offers no Start for `ready` Tickets (ruling 11)
   and shows `needs_decision` as "Unknown task status" in `taskState.ts`
   (cosmetic; Step 6 owns §18's UX).
6. **M1 exit gate.** `m1_exit_gate._audit_three_attempts` looks for a task with
   three Attempts on distinct worktrees; Ticket Attempts share one, so only
   legacy tasks can provide that evidence.
7. **A paused dependent** of a failed blocker keeps `paused`; it is not moved to
   `needs_decision` over the user's pause.
8. **The pre-existing `task_id` backfill** (F1 §4.3, ruling 14) also runs on a
   refused Ticket dispatch and on the reaper's lazy migration; two new tests
   apply it before their snapshots and say why.

---

## 7. Validation (run for this handoff)

All Python ran with `/home/ubuntu/agent-taskflow/.venv/bin/python`,
`PYTHONPATH` set to the worktree root, a throwaway git identity, and `HOME` set
to a fresh `mktemp -d` directory; every one was empty afterwards. Every command
ran in the foreground and finished before this file was written.

| Command | Result |
|---|---|
| **BEFORE round 3**: full suite at `a12d708` (measured on `ff7becd`'s code, which `a12d708` changes only in this file; the independent reviewer re-ran it at `a12d708` and got the same 4998) | Ran 4998 tests, OK (skipped=8) |
| **BEFORE round 2**: full suite at `a5fa8e2` (round 1) | Ran 4906 tests, OK (skipped=8) |
| **AFTER round 3**, on `68f9f58`'s code: `test_[a-f]` 1220, `test_[g-q]` 1267, `test_r` 900 (skipped=8), `test_[s-z]` 1624 | **Ran 5011 tests — OK (skipped=8).** 5011 = 4998 + 13 new. No test went red. |
| Round 3: Step 5 gate, the seven `tests.test_step5_*` modules, `-W error::ResourceWarning` | Ran 105 tests in 56.7 s, OK |
| Round 3: pre-existing admission/reset suites (`test_runtime_admission`, `test_reset_lineage`, `test_reset_lineage_cli`, `test_task_status_reset`, `test_runtime_capacity`, `test_lease_contention`, `test_concurrency_atomic_claim`, `test_concurrency_crash_recovery`, `test_dispatcher`, `test_runtime_progress_wiring`, `test_canonical_runtime_admission`, `test_api_actions`) | Ran 225 tests in 69.5 s, OK, unedited |
| Round 3: compileall, both validators, the two operator CLIs under `python3 -S` with no pydantic | exit 0; `status: passed` twice; `--help` exit 0 each |
| Round 3: Step 4 rehearsal at `68f9f58` | exit 0, **16/16 checks**, gate `passed`, evidence `repo_sha` `68f9f58` |
| *Round 2 results follow (on `ff7becd`'s code).* | |
| Step 5 acceptance gate: the six `tests.test_step5_*` modules, with `-W error::ResourceWarning` (on `ff7becd`'s code) | Ran 92 tests in 46.5 s, OK |
| Pre-existing suites nearest the change (on `d7b51c0`'s code, before the review fixes; the full suite below re-covers them): `test_dispatcher`, `test_runtime_progress_wiring`, `test_attempt_resources`, `test_attempt_resources_cli`, `test_runtime_admission`, `test_task_status_reset`, `test_reset_lineage`, `test_reset_lineage_cli`, `test_concurrency_crash_recovery`, `test_lease_contention`, `test_runtime_capacity`, `test_lifecycle_control`, `test_canonical_runtime_admission`, `test_api_actions`, `test_api`, `test_realtime_negative_scope`, `test_docs_maps`, `test_m1_exit_gate` | Ran 293 tests in 89.1 s, OK |
| **AFTER** part `-p 'test_[a-f]*.py'` (78 files), on `ff7becd`'s code | `Ran 1220 tests in 212.403s` — `OK` |
| **AFTER** part `-p 'test_[g-q]*.py'` (66 files) | `Ran 1267 tests in 66.941s` — `OK` |
| **AFTER** part `-p 'test_r*.py'` (63 files) | `Ran 900 tests in 229.144s` — `OK (skipped=8)` |
| **AFTER** part `-p 'test_[s-z]*.py'` (86 files) | `Ran 1611 tests in 125.639s` — `OK` |
| **AFTER total** | **Ran 4998 tests — OK (skipped=8).** 4998 = 4906 + 92 new; the 8 skips are the same pre-existing ones. No test went red. (The same four parts on `d7b51c0`, before the review fixes, gave 4989 = 4906 + 83, also OK.) |
| `python -m compileall -q agent_taskflow scripts tests` (on `ff7becd`'s code) | exit 0 |
| `python scripts/validate_workflow_contract.py` | exit 0, `status: passed` |
| `python scripts/validate_workflow_policy.py` | exit 0, `status: passed` |
| `python scripts/run_concurrency_rehearsal.py --output-dir <fresh /tmp dir>` | exit 0, **16/16 checks**, gate `passed`, three times: on the uncommitted tree (evidence `repo_sha` `2c77cc3`), at `d7b51c0`, and at `ff7becd` (evidence `repo_sha` `ff7becd`; `max_concurrent_tasks` 19.1: 1, 19.2: 4, 19.3: 1). This handoff commit moves HEAD, so re-run it for the commit you deploy (Step 4 §4.7.5). |
| Real tick smoke: a disposable DB, one Ticket, `scripts/run_parallel_scheduler_tick.py --db-path` with the production launcher and worker (manual executor, default validators) | exit 0; one Ticket started and claimed, its worktree created on its branch; the default `pytest` validator went red (exit code 5, no tests in the scratch repo), so the Ticket ended `needs_decision` (§29.1); the worker result was read from its own log; a second tick started nothing |
| The three operator CLIs (`reset_task_status.py`, `ticket_dependency.py`, `migrate_ticket_worktree_resources.py`) `--help` under the system `python3 -S`, which has no pydantic | exit 0 each: they still run without the app's dependencies |
| The §8 scratch-database commands | as documented: the tick exits 2 naming the Step 5 script; the migration reports `rebuilt: true`, then `already_installed: true`; the no-op tick reports `started: []` |
| `cd mission-control && npm run build` | **not run**: no `mission-control/` file changed |

The four parts cover all 293 `tests/test_*.py` files exactly once
(78 + 66 + 63 + 86).

---

## 8. Exact commands to verify

From the worktree root:

```bash
PY=/home/ubuntu/agent-taskflow/.venv/bin/python
export HOME=$(mktemp -d) PYTHONPATH=$PWD
export GIT_AUTHOR_NAME=T GIT_AUTHOR_EMAIL=t@example.invalid
export GIT_COMMITTER_NAME=T GIT_COMMITTER_EMAIL=t@example.invalid

# No pre-existing test file changed: the first prints nothing, the second lists
# only added (A) files
git diff --name-status --diff-filter=M a5fa8e2 -- tests/
git diff --name-status a5fa8e2 -- tests/

# Step 5 acceptance gate (105 tests, ~60 s)
$PY -m unittest tests.test_step5_dependency_admission \
  tests.test_step5_ticket_worktree tests.test_step5_ticket_worktree_schema \
  tests.test_step5_failure_vocabulary tests.test_step5_dependencies \
  tests.test_step5_ready_queue tests.test_step5_parallel_scheduler

# Full suite in four parts, each under 600 s
for p in 'test_[a-f]*.py' 'test_[g-q]*.py' 'test_r*.py' 'test_[s-z]*.py'; do
  $PY -m unittest discover -s tests -p "$p"
done

# Byte-compile and repo validators
$PY -m compileall -q agent_taskflow scripts tests
$PY scripts/validate_workflow_contract.py
$PY scripts/validate_workflow_policy.py

# Step 4 rehearsal: must print 16/16 and exit 0
OUT=/tmp/step5-rehearsal-$(date +%s)
$PY scripts/run_concurrency_rehearsal.py --output-dir "$OUT"

# The migration and the tick against a scratch database only
DB=/tmp/step5-scratch-$(date +%s).db
$PY -c "from agent_taskflow.store import TaskMirrorStore; TaskMirrorStore('$DB').init_db()"
$PY scripts/migrate_ticket_fields.py --db-path "$DB"
$PY scripts/run_parallel_scheduler_tick.py --db-path "$DB"; echo "exit=$?"   # 2, names the Step 5 script
$PY scripts/migrate_ticket_worktree_resources.py --db-path "$DB"              # rebuilt: true
$PY scripts/migrate_ticket_worktree_resources.py --db-path "$DB"              # already_installed: true
$PY scripts/run_parallel_scheduler_tick.py --db-path "$DB"                    # no-op tick, started: []
```

---

## 9. Governance

- **Git.** Commits on `task/v1-step5` only (`d7b51c0`, `ff7becd`, `a12d708`,
  `68f9f58`, then this handoff), each pushed with a normal fast-forward push;
  PR #201 stays a draft against `main`. Nothing was pushed to or merged into `main`,
  nothing was force-pushed, nothing was rebased.
- **State.** `~/.agent-taskflow/state` and the production database were never
  read or written. Every database was a temporary one under `/tmp`.
- **Scheduler.** Scheduler ticks ran only against disposable databases under
  `/tmp`: in the tests and in the §7 smoke.
- **Ops.** Nothing under `~/agent-taskflow-ops/` was changed (27g).
- **Subagents.** Read-only subagents did the inventory research and an
  independent review of the diff (§2.5); every file, test run, commit and push
  was done by the main session.
- **Approvals.** Nothing was approved, closed, cleaned up or deleted; no config,
  cron, systemd, nginx or deployment file was touched. No migration runs at
  process startup.

---

## 10. Round 1b — re-verification by a second builder run (2026-09-11, HEAD `f339d54`)

*Historical record, kept verbatim. Its section references (§3, §7, §8) point
at round 1's layout of this file, which §0–§3.1 now summarize.*

A second builder session was launched with the same prompt and the same
`step5.md`. No ruling after ruling 25 settles D1-D6, and `step5.md` has not
changed since this handoff was written, so the stop still stands. The session
checked S1 and S2 against the code itself instead of trusting §3. It
implemented nothing and changed no file other than this one.

- **S1 still holds.** `attempt_resources_schema.py:31,33` still declares
  `branch_name` and `worktree_path` `NOT NULL UNIQUE`, and `:79` still defines
  the `attempt_resources_immutable_paths` trigger. `attempt_resources.py:282-283`
  still derives the branch and worktree from the Attempt. `:415-417` still
  refuses a dirty reused worktree, and `:471-481` still overwrites
  `task_worktrees` with the Attempt's path. `test_attempt_resources.py:166-168`
  still asserts `assertNotEqual` on branch, worktree and artifact root. The
  "Fresh retry contract" in `docs/attempt-scoped-runtime-resources.md:78` is
  unchanged.
- **S2 still holds.** `runtime_admission.py:742-757` still writes
  `status = 'blocked'` inside `expire_stale_leases`, and `runtime_reaper.py:101`
  still calls it. The seven `test_dispatcher` tests in §3.2 still assert
  `blocked`, and `test_runtime_admission.py:309-310` still asserts `blocked`
  with `runtime_lease_expired`. `task_status_reset.py:24` still accepts
  `blocked` only.
- **Commands run** (with `HOME` set to a fresh `mktemp -d`, which was empty
  afterwards):
  - the pinned-test command in §8 printed `Ran 36 tests in 13.742s`, `OK`
  - `python -m compileall -q agent_taskflow scripts tests` exited 0
  - `scripts/validate_workflow_contract.py` exited 0
  - `scripts/validate_workflow_policy.py` exited 0
- **Not re-run:** the full suite and the Appendix A probe. The only change
  since §7 is to this file, so neither result can have changed.

---

## Appendix A — S1 probe (`/tmp/probe_s1.py`, not committed)

```python
"""Read-only probe for the Step 5 handoff (stop condition S1).

Runs entirely under a scratch directory. Creates a Ticket through Step 1's real
path, gives it the per-Ticket worktree Step 5 is asked to create (git worktree
at the Step 1 derived path/branch + a task_worktrees row), then claims it twice
through the installed runtime store exactly as AttemptScopedDispatcher does,
with a reset in between. No executor, validator or scheduler runs.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import agent_taskflow  # noqa: F401  installs the layered runtime path
from agent_taskflow.dispatcher import Dispatcher
from agent_taskflow.models import TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore, connect
from agent_taskflow.task_status_reset import TaskStatusResetRequest, reset_task_status
from agent_taskflow.ticket_creation import TicketCreationRequest, create_ticket
from agent_taskflow.ticket_fields_schema import migrate_ticket_fields
from agent_taskflow.ticket_repositories import TicketRepository
from agent_taskflow.ticket_store import TicketStore


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def worktrees(repo: Path) -> list[str]:
    out = git(repo, "worktree", "list", "--porcelain")
    return [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")]


def claim(db: Path, key: str):
    dispatcher = Dispatcher(db_path=db)
    store = dispatcher.store
    print("  store class:", type(store).__name__, "| dispatcher class:", type(dispatcher).__mro__[0].__qualname__)
    task = store.get_task(key)
    previous = store.get_task_worktree(key)
    latest = store._attempt_resources.latest_for_task(key)
    # Same staging as AttemptScopedDispatcher.dispatch_task (attempt_scoped_runtime_path.py:581-598).
    store.configure_attempt_resources(
        key,
        base_branch=latest.base_branch if latest else (previous.base_branch if previous else "main"),
        worktree_root=latest.worktree_root if latest else (previous.worktree_path.parent if previous else None),
        artifact_base_root=latest.artifact_base_root if latest else task.artifact_dir,
    )
    store.update_task_status(key, "preparing", source="probe", message="probe claim")
    resource = store.attempt_resource(key)
    row = store.get_task_worktree(key)
    print("  attempt:", resource.attempt_id, "number", resource.attempt_number)
    print("  attempt worktree:", resource.worktree_path)
    print("  attempt branch:  ", resource.branch_name)
    print("  task_worktrees row now:", row.worktree_path, "|", row.branch)
    print("  git worktrees:", worktrees(task.repo_path)[1:])
    store.update_task_status(key, "blocked", source="probe", blocked_reason="probe end of attempt")
    store.shutdown_runtime_supervisors()
    return resource


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="v1-step5-probe-"))
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "probe@example.invalid")
    git(repo, "config", "user.name", "Probe")
    (repo / "README.md").write_text("probe\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")

    db = root / "state.db"
    TaskMirrorStore(db).init_db()
    migrate_ticket_fields(db)
    repository = TicketRepository(
        repository="probe",
        repo_path=repo,
        worktrees_dir=repo / ".worktrees",
        artifacts_root=root / "artifacts",
        base_branch="main",
        branch_prefix="task/",
    )
    ticket = create_ticket(
        TicketCreationRequest(repository="probe", prompt="Add a probe file"),
        store=TicketStore(db),
        repository=repository,
    ).ticket
    key = ticket.task_key
    print("scratch root:", root)
    print("Step 1 Ticket:", key, "status", ticket.status)
    print("  derived worktree:", ticket.worktree_path)
    print("  derived branch:  ", ticket.branch)
    print("  task_worktrees row:", TaskMirrorStore(db).get_task_worktree(key))

    # What step5.md layer 1 asks for: the Ticket's real worktree + its row.
    git(repo, "worktree", "add", str(ticket.worktree_path), "-b", ticket.branch, "main")
    TaskMirrorStore(db).upsert_task_worktree(
        TaskWorktreeRecord(
            task_key=key,
            repo_path=repo,
            worktree_path=ticket.worktree_path,
            branch=ticket.branch,
            base_branch="main",
            status="active",
        )
    )
    print("after creating the per-Ticket worktree: git worktrees:", worktrees(repo)[1:])

    print("claim #1 (installed runtime path):")
    first = claim(db, key)

    reset = reset_task_status(
        TaskStatusResetRequest(
            task_key=key,
            db_path=db,
            from_status="blocked",
            reason="probe retry",
            actor="probe",
            request_id="probe-retry-1",
            expected_reset_generation=0,
            expected_old_attempt_id=first.attempt_id,
            confirm_reset=True,
        )
    )
    print("reset: blocked -> queued, new attempt", reset.new_attempt_id)
    print("claim #2 (retry):")
    second = claim(db, key)

    with closing(connect(db)) as conn:
        rows = conn.execute(
            "SELECT attempt_number, worktree_path, branch_name FROM attempt_resources"
            " WHERE task_key = ? ORDER BY attempt_number",
            (key,),
        ).fetchall()
        unique_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'attempt_resources'"
        ).fetchone()[0]
    print("attempt_resources rows:")
    for row in rows:
        print("  ", tuple(row))
    print("git worktrees for one Ticket:", len(worktrees(repo)) - 1)
    print("retry reused the first Attempt's worktree:", first.worktree_path == second.worktree_path)
    print("retry reused the Ticket's derived worktree:", second.worktree_path == ticket.worktree_path)
    for line in unique_sql.splitlines():
        if "UNIQUE" in line:
            print("  schema:", line.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
```
