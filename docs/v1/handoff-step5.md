# Handoff — V1 Step 5: Parallel Scheduler

Branch: `task/v1-step5`
Draft PR: https://github.com/anderson930420/agent-taskflow/pull/201 (draft, base `main`)
Base: `a5fa8e2` (`main`, after Steps 1, 3, 4 and F1 merged)
Spec: `~/agent-taskflow-ops/v1/SPEC.md` §20, §21, §9, §29, §42 Step 5
Instructions: `~/agent-taskflow-ops/v1/step5.md`

**Status: STOPPED during the read-only inventory. Nothing is implemented.**
Two stop conditions fired before any test or code was written (§3):

- **S1:** "One worktree per Ticket, a retry reuses it" contradicts the
  installed Attempt-scoped resource layer. It gives every Attempt a new branch
  and a new worktree. The layer is documented, enforced by the schema, and
  pinned by a pre-existing test.
- **S2:** the §29 failure remap turns pre-existing tests red, because they
  assert `blocked` on failure. step5.md's watchlist names this exact case a
  stop condition. Remapping lease expiry would also change the Step 4 reaper,
  which step5.md forbids.

As the stop rule and the orchestration rules require, I stopped, wrote this
file, and repaired nothing. The only change on this branch is this file. Every
acceptance-gate row is unimplemented (§6). §5 lists the decisions needed.
The PR is a draft. Nothing is approved or merged.

---

## 1. Read-only inventory

### 1.1 What earlier steps handed to Step 5, checked against the code

| Handed-over fact (step5.md) | Result |
|---|---|
| A Step 1 Ticket has no `task_worktrees` row. `dispatch_task` refuses it with "Task worktree not found", and the governance branch writes `created → blocked`. | **Verified.** `create_ticket` writes `tasks` and `task_events` only (`ticket_store.py:236-287`); the probe (§3.1) reads the row as `None`. The refusal is `dispatcher.py:515-516`, and `_block_task` writes it (`dispatcher.py:200-203`). |
| Executor failure, validator failure, governance refusal and lease expiry all write `blocked`. | **Verified, and wider than listed.** In the dispatcher: `dispatcher.py:277, 324, 344, 386, 405` (failures) and `:203` (governance). The runtime layers write it too: `attempt_scoped_runtime_path.py:280` (resource allocation failure) and `:621-637` (workspace preparation failure). Lease expiry writes it in `runtime_admission.py:742-757`. The Level 2 runner and `lifecycle_runtime_path.py:165-203` use the same target. |
| `RuntimeAdmissionStore.claim()` checks capacity first, then `{"created", "queued"}`. | **Verified.** `runtime_admission.py:322-337`: `BEGIN IMMEDIATE` → `assert_runtime_capacity_available` → `_ensure_task_identity` → `assert_admission_allowed` → the status check. |
| `max_concurrent_tasks` is global, default 1. | Verified (`runtime_capacity.py`, ruling 15). |
| The reaper is one idempotent callable plus a CLI. | Verified: `runtime_reaper.reap_stale_runtime` (`runtime_reaper.py:93`) and `scripts/reap_stale_runtime.py`. It calls `RuntimeAdmissionStore.expire_stale_leases()`, and that call writes the `blocked` target. |
| F1 records Prepare / Implementer / Validator. | Verified (`runtime_progress_recorder.py`). |

### 1.2 Facts step5.md does not mention

1. **The installed runtime already creates a git worktree on every claim,
   one per Attempt.** `agent_taskflow/__init__.py` layers
   `AttemptScopedRuntimeTaskStore` under the dispatcher. On every
   `preparing` claim it allocates a new branch and a new worktree for that
   Attempt (`attempt_resources.py:280-284`), then runs `git worktree add -b`
   (`:424-434`) and **overwrites the Ticket's `task_worktrees` row** with the
   Attempt's path and branch (`:471-481`). Each Attempt gets:
   - branch `attempt/<task-slug>/<n>-<suffix>`
   - worktree `<worktrees_dir>/<task-slug>/<attempt-id>`

   See §3.1.
2. **Dependency storage already exists in part (Step 1).**
   - `tasks.blocked_by` is a single `TEXT` column (`ticket_fields_schema.py:41`).
   - Creation checks that the blocker exists (`ticket_store.py:206-216`).
     Its comment says "Cycle validation is Step 5".
   - A Ticket created with `blocked_by` is persisted as `blocked`
     (`ticket_models.py:105-113`).
   - The board projection already reads `blocked_by`
     (`realtime_projection.py:495`).
   - **Nothing can set or remove `blocked_by` after creation**, and nothing
     releases it.
3. **The failure statuses already exist.** `failed` and `needs_decision` are in
   `TASK_STATUSES` and map to themselves in `status_vocab.py`, so the remap
   needs no vocabulary change. §12 `completed` is persisted as `cleaned`, with
   aliases `completed` and `done` (`status_vocab.py:56, 99, 108, 115`).
4. **There is no preferred-order field.** No column, API field or projection
   field records a preferred order. §8 drag ordering is Step 6.
5. **The only retry path accepts `blocked` only.** It resets to `queued`
   (`task_status_reset.py:24`, `reset_lineage.py:330, 427-433`), a status
   §12.1 says V1 never writes.

### 1.3 Planned touch list (not executed; depends on §5)

| Module | Extend / Create | Planned purpose | Blocked by |
|---|---|---|---|
| `agent_taskflow/ticket_worktree.py` | create | Idempotent, audited creation of the Ticket's worktree and `task_worktrees` row, from the Step 1 derived path and branch | S1 / D1 |
| `agent_taskflow/attempt_resources.py`, `attempt_scoped_runtime_path.py` | extend | Bind a Ticket's Attempts to the Ticket's worktree instead of allocating a new one | S1 / D1 |
| `scripts/migrate_*.py` (new) | create | Any schema change D1 needs, for example relaxing the `attempt_resources` `UNIQUE` columns. No startup migration. | D1 |
| `agent_taskflow/ticket_dependencies.py` | create | Set/remove `blocked_by`: unknown and self rejected, cycle detection at set time, release on the §12 `completed` statuses only, failed/cancelled blocker → `needs_decision`, all audited | D2, D5, D6 |
| `agent_taskflow/ready_queue.py` | create | Eligibility and deterministic ordering | D3 |
| `agent_taskflow/parallel_scheduler.py` + `scripts/run_parallel_scheduler_tick.py` | create | One idempotent tick: reap → pick → claim → prepare worktree → start executor | S1, S2 |
| `agent_taskflow/dispatcher.py` | extend | §29 remap of the failure targets | S2 / D2 |
| `canonical_runtime_path.py`, `attempt_scoped_runtime_path.py`, `lifecycle_runtime_path.py` | extend | Release the lease and resources on `failed` / `needs_decision` too | S2 / D2 |
| `runtime_admission.py` (`expire_stale_leases`), `task_status_reset.py`, `reset_lineage.py` | extend | Lease expiry → `failed`, and a retry path out of `failed` | S2 / D2 (the reaper change is forbidden as written) |
| `tests/test_step5_*.py` | create | The acceptance gate | all of the above |

No existing module was modified.

---

## 2. What was implemented

Nothing, apart from this file. No test, module, script, schema or frontend
file changed. The read-only probe in §3.1 ran under `/tmp` and is reproduced
in Appendix A. It is not committed.

---

## 3. Stop conditions hit

### 3.1 S1 — "One Ticket = One Worktree, a retry reuses it" contradicts the Attempt-scoped resource contract

**The requirement.** step5.md, allowed layer 1: "One worktree per Ticket,
never one per Attempt. A retry reuses the Ticket's worktree." It is also in
the acceptance gate: "exactly one worktree and one `task_worktrees` row …
a retry reuses the same worktree". SPEC §9 and §33.3 agree: "同一 Ticket、同一
worktree".

**The existing code that contradicts it:**

- **Documented contract.** `docs/attempt-scoped-runtime-resources.md`,
  "Fresh retry contract": "The next Attempt therefore receives a new: branch;
  worktree; … The prior Attempt cannot be reused as the retry workspace."
  Its status block lists `fresh_worktree_retry_identity = implemented`.
- **Schema.** In `attempt_resources_schema.py`, `branch_name` and
  `worktree_path` are `NOT NULL UNIQUE`, and the trigger
  `attempt_resources_immutable_paths` refuses any path update. Two Attempts
  cannot share a worktree path or a branch, so a Ticket's Attempts cannot
  either. Dropping a `UNIQUE` column constraint in SQLite means rebuilding the
  table.
- **Reuse is refused when dirty.** `provision_workspace` reopens an existing
  worktree only for the same Attempt, and only if it is clean
  (`attempt_resources.py:412-418`). A retry after a failed Attempt normally
  finds uncommitted work left in the worktree.
- **Pre-existing test.**
  `tests/test_attempt_resources.py::AttemptResourceTests::test_retry_gets_new_branch_worktree_and_artifact_root`
  (lines 147-171) asserts `assertNotEqual(first.worktree_path,
  second.worktree_path)` and the same for the branch. It is green today (§7).
- **The installed dispatcher always takes this path.**
  `AttemptScopedDispatcher.dispatch_task` stages resources for every
  `created`/`queued` task (`attempt_scoped_runtime_path.py:581-598`). The
  `preparing` claim always runs `preclaim_runtime` and
  `prepare_attempt_workspace` (`:350-366`).

**Probe evidence.** Appendix A ran against a scratch repository and database
under `/tmp`, with `HOME` redirected. It uses Step 1's real `create_ticket`,
then does what layer 1 asks for: a real `git worktree add` at the Ticket's
derived path and branch, plus a `task_worktrees` row. It then claims twice
through the installed runtime store, staged the same way
`AttemptScopedDispatcher` stages it, with the reset CLI's `blocked → queued`
retry in between. No executor, validator or scheduler ran. Output, trimmed:

```text
Step 1 Ticket: AT-0001 status created
  derived worktree: …/repo/.worktrees/AT-0001
  derived branch:   task/AT-0001-add-a-probe-file
  task_worktrees row: None
after creating the per-Ticket worktree: git worktrees: ['…/.worktrees/AT-0001']
claim #1 (installed runtime path):
  store class: ValidatorProcessRuntimeTaskStore | dispatcher class: Dispatcher
  attempt worktree: …/.worktrees/at-0001/attempt-28cf3b36…
  attempt branch:   attempt/at-0001/1-28cf3b3669db
  task_worktrees row now: …/.worktrees/at-0001/attempt-28cf3b36… | attempt/at-0001/1-28cf3b3669db
claim #2 (retry):
  attempt worktree: …/.worktrees/at-0001/attempt-9cf6f662…
  attempt branch:   attempt/at-0001/2-9cf6f662e0e9
git worktrees for one Ticket: 3
retry reused the first Attempt's worktree: False
retry reused the Ticket's derived worktree: False
```

With a per-Ticket worktree added in front, the installed path gives **one
Ticket three git worktrees after one retry**. The executor never runs in the
Ticket's worktree or on the Ticket's branch, and the `task_worktrees` row ends
up naming the latest Attempt's worktree. Without that front step (only a
`task_worktrees` row), dispatch would pass governance. Each Attempt would
still get its own worktree, and a retry would still get a new one. Neither
arrangement meets the acceptance row.

**Why this is a stop and not a design choice I can make.** Meeting the
requirement means changing a documented M0 / Level 2 contract: fresh worktree
identity on retry. That takes a schema rebuild and flips a pre-existing test,
or it takes a Ticket-only exception inside the Attempt resource layer. step5.md
does not cover either, and the spec never mentions Attempt-scoped worktrees.
That is "a spec requirement … contradicts existing code". See D1.

### 3.2 S2 — The §29 failure remap turns pre-existing tests red, and lease expiry would change the reaper's contract

**The requirement.** Allowed layer 6 and the acceptance gate: "A validator
failure ends `needs_decision`; an executor crash, a worktree preparation
failure and a lease expiry end `failed`". step5.md's own watchlist: "a
pre-existing test that asserts `blocked` on failure is a stop condition."

**Pre-existing tests that assert `blocked` on a failure the remap moves.**
All are green today (§7):

| Test | Failure it pins to `blocked` |
|---|---|
| `tests/test_dispatcher.py::DispatcherTests::test_executor_failed_blocks_task` (147) | executor `failed` |
| `…::test_executor_blocked_blocks_task` (161) | executor `blocked` |
| `…::test_validator_failed_blocks_task` (175) | validator `failed` (→ `needs_decision` under §29.1) |
| `…::test_validator_blocked_blocks_task` (192) | validator `blocked` |
| `…::test_unknown_executor_blocks_task` (432), `…::test_unknown_executor_still_blocks_task` (754) | executor unavailable |
| `…::test_unknown_validator_blocks_task` (445) | validator unavailable |
| `tests/test_runtime_admission.py::…::test_stale_lease_reaper_aborts_attempt_and_blocks_task` (296) | lease expiry: status `blocked`, reason `runtime_lease_expired` |
| `tests/test_runtime_progress_wiring.py` (F1): `test_failing_executor_leaves_implementer_failed_and_validator_pending` (507), `test_raising_executor_leaves_implementer_failed` (529), `test_failing_validator_leaves_validator_failed` (547), `test_unavailable_executor_leaves_prepare_failed` (565), `test_failure_outcome_is_unchanged_by_a_raising_progress_store` (791) | `result.status == "blocked"` on failure |
| `tests/test_concurrency_crash_recovery.py` (class fixture, line 95), and the rehearsal it drives, `concurrency_rehearsal.py:940-955` | after lease expiry, recovery runs `reset_task_status.py --from-status blocked` |

**Why lease expiry cannot be remapped within step5.md's rules.** The
`blocked` target is written inside `RuntimeAdmissionStore.expire_stale_leases()`
(`runtime_admission.py:742-757`), which is what `reap_stale_runtime()` runs.
step5.md forbids "Changing … the reaper's contract (Step 4)", but its
acceptance gate requires "a lease expiry end `failed`". The two cannot both
hold.

**What breaks beyond those tests if only the dispatcher is remapped:**

1. **Leases would not be released.** `CanonicalRuntimeTaskStore` and
   `AttemptScopedRuntimeTaskStore` release the lease and Attempt resources
   only on `{blocked, waiting_approval, canceled, completed}`
   (`canonical_runtime_path.py:293`, `attempt_scoped_runtime_path.py:368`).
   `_terminal_attempt_status` maps any other status to Attempt `blocked`
   (`canonical_runtime_path.py:204-211`). A dispatcher writing `failed`
   through these layers would keep the lease and its capacity slot until the
   lease expires.
2. **Nothing could retry `failed` or `needs_decision`.** The reset CLI accepts
   `blocked` only (§1.2.5).
3. **The Step 4 gate would stop passing.** Its §19.3 rehearsal checks recovery
   "through the existing retry" with `--from-status blocked`. With lease
   expiry writing `failed`, that check fails and the evidence gate stops
   passing, so `max_concurrent_tasks` could no longer go above 1. That would
   also block acceptance row §43.10 ("above 1 with evidence").
4. **Failure alerts would stop.** Outside the repo:
   `~/agent-taskflow-ops/taskflow_dc_notify.py` (5-minute cron) alerts
   Discord on `blocked` (`NOTIFY_STATUSES`, lines 41-45) but not on `failed`
   or `needs_decision`. After the remap, failures would stop alerting. This is
   an ops-side change, and I did not touch it.

### Not hit

- **No pre-existing test went red.** Nothing was changed. The full suite on
  this tree is in §7.
- No forbidden layer was entered, no migration was added, and no startup
  behaviour changed.

---

## 4. Ambiguities flagged (step5.md watchlist and others)

1. **Preferred ordering beyond priority.** §7 names "priority → preferred
   queue order → deterministic tie-breaker". §20 says only "highest-priority
   eligible". §8 makes drag ordering optional and Step 6 owns it. No field
   stores a preferred order (§1.2.4).
2. **A running dependent whose blocker fails.** A dependent can only be
   running while its blocker is incomplete if the dependency arrived after it
   started, as a §5.2 runtime-discovered dependency. §13's
   stop-at-safe-boundary is the nearest rule. The spec does not say whether
   to stop the dependent.
3. **Retrying a `failed` Ticket.** The reset CLI takes `blocked` only and
   writes `queued`. §33.3 retries `needs_decision → ready` (persisted
   `created`) with a new Attempt. There is no path out of `failed` or
   `needs_decision` today.
4. **Targets step5.md does not name:** governance refusal (for example a
   worktree outside `.worktrees`), an executor that returns `blocked` (for
   example a cooperative operator kill), and a validator that raises or is
   unavailable. §29.2 "invalid repository state" suggests `failed` for the
   first. For the validator cases, "validator red" (`needs_decision`) and
   "infrastructure" (`failed`) are both plausible.
5. **A dependency wait and a failure are both persisted `blocked`.** Step 1
   persists "created with `blocked_by`" as `blocked`, the same value failures
   write today. Releasing a dependency needs a new `blocked → created`
   transition, which no path has. That transition must never release a
   failure-`blocked` row. D2 mostly settles this: once failures stop writing
   `blocked`, only legacy rows stay ambiguous.
6. **`needs_decision` has no exit.** No route or CLI moves a Ticket out of it.
   §5.4 lists the choices (remove or replace the dependency, retry the
   blocker, cancel the dependent), and §18 lists them as manual controls.

---

## 5. Decisions needed

**D1 (S1): how "One Ticket = One Worktree" coexists with Attempt-scoped resources.**

- *Option A (recommended):* a Ticket's Attempts run in the Ticket's worktree.
  - For a row with Step 1 columns, the Attempt resource record points at the
    Ticket's derived worktree and branch instead of a new one. The Attempt's
    own artifact root, lock and PID stay.
  - Step 5 creates that worktree and its `task_worktrees` row before the
    claim, idempotently and with an audit event.
  - Legacy tasks keep the fresh-worktree contract, so
    `test_retry_gets_new_branch_worktree_and_artifact_root` stays green.
  - Sub-decisions:
    - (i) Authorize an explicit `scripts/` migration that rebuilds
      `attempt_resources` without `UNIQUE` on `worktree_path` and
      `branch_name`. `UNIQUE(task_id, attempt_number)` and the other
      constraints stay.
    - (ii) A retry reuses the worktree as the last Attempt left it. §33.3
      continuity implies this; today reuse requires a clean tree.
    - (iii) Amend `docs/attempt-scoped-runtime-resources.md` for Tickets.
- *Option B:* keep per-Attempt worktrees for everything, and amend SPEC §9,
  §33.3 and step5.md's acceptance row. Not recommended: it gives up §33.3's
  "same worktree".

**D2 (S2): the failure remap.**

- (a) Authorize updating the pre-existing tests in §3.2 to the new targets.
  Nothing else they assert would be weakened.
- (b) Lease expiry → `failed` requires changing the target inside
  `expire_stale_leases`. Either authorize that change, keeping the rest of the
  reaper contract (idempotent, no schema, same return value, one audited event
  per lease), or drop lease expiry from the acceptance row.
- (c) Which statuses the retry path accepts. Recommended: `reset_task_status`
  also accepts `failed`, and a Ticket's retry target follows §33.3. Either way,
  the Step 4 rehearsal and `test_concurrency_crash_recovery` need their
  `--from-status` updated.
- (d) Authorize adding `failed` and `needs_decision` to the release sets in
  `canonical_runtime_path.py`, `attempt_scoped_runtime_path.py` and
  `lifecycle_runtime_path.py`, and to `_terminal_attempt_status`.
- (e) Targets for §4.4. Recommended:
  - governance refusal → `failed`
  - executor failed, blocked, raised or unavailable → `failed`
  - validator `failed` → `needs_decision`
  - validator raised or unavailable → `failed`
- (f) Scope: Tickets only, or the legacy GitHub-issue path as well
  (`approved_task_runner.py`)?
- (g) Ops: update `taskflow_dc_notify.py` to alert on `failed` and
  `needs_decision` in the same change.

**D3: ordering.** Recommended: priority (`critical > high > normal > low`),
then `created_at` (FIFO), then `task_key`. No preferred-order column until
Step 6.

**D4: a running dependent whose blocker fails.** Recommended: never interrupt
a running Attempt. Refuse to start it again, and move it to `needs_decision`
when its Attempt ends.

**D5: dependency release.** Recommended: release only a `blocked` row that
has `blocked_by` set and a dependency-owned `blocked_reason` (or none), moving
`blocked → created`, audited. Setting `blocked_by` on a `created` Ticket moves
it to `blocked`, as in §6's example history.

**D6: the `needs_decision` exit.** Should Step 5 ship a CLI to remove or
replace `blocked_by` and to retry, or is that Step 6's §18 UX?

---

## 6. Deliberately skipped, and why

Every acceptance-gate row is unimplemented, and no test was written for it.
Writing tests or code for any row would pick a side of S1 or S2 before a
ruling, and would mean continuing after a stop condition.

| Row | Blocked by |
|---|---|
| §43.4 / §9 one worktree per Ticket | S1 (D1) |
| §43.5 blocked / paused never execute | Unchanged F1 behaviour, already covered by `test_runtime_progress_wiring`. A Step 5 test of the loop needs the loop (S1, S2). |
| §43.6 invalid dependencies | D5 and D6 decide what a set/remove API looks like. Cycle detection itself is independent of S1 and S2 and small once they are settled. |
| §43.7 / §43.34 release after `completed` | D5 |
| §43.8 failed / cancelled blocker | S2 (D2), because what `failed` means depends on the remap. Also D4. |
| §43.10 capacity | S1 (the loop's "prepare worktree" step). S2(b)/(c) decide whether the Step 4 gate can still produce evidence above 1. |
| §21 parallel implementation | S1 |
| §29.1 / §29.2 failure vocabulary | S2 |
| Loop idempotency | S1, S2 |

---

## 7. Validation (run for this handoff)

All Python commands used `/home/ubuntu/agent-taskflow/.venv/bin/python`
with `PYTHONPATH` set to the worktree root and `HOME` set to a fresh
`mktemp -d` directory, so nothing could reach `~/.agent-taskflow`. Each
temporary `HOME` was empty afterwards.

| Command | Result |
|---|---|
| Pinned tests (S1, S2): the 7 `test_dispatcher` tests in §3.2, `test_attempt_resources…test_retry_gets_new_branch_worktree_and_artifact_root`, all of `test_runtime_admission`, all of `test_concurrency_crash_recovery` | `Ran 36 tests in 13.269s` — `OK` |
| Full suite, part `-p 'test_[a-f]*.py'` (78 files) | `Ran 1220 tests in 211.610s` — `OK` |
| Full suite, part `-p 'test_[g-q]*.py'` (66 files) | `Ran 1267 tests in 78.683s` — `OK` |
| Full suite, part `-p 'test_r*.py'` (63 files) | `Ran 900 tests in 246.848s` — `OK (skipped=8)` |
| Full suite, part `-p 'test_[s-z]*.py'` (80 files) | `Ran 1519 tests in 67.371s` — `OK` |
| **Total** | **Ran 4906 tests — OK (skipped=8)**. Same as Step 4 after its merge (4906, skipped=8), so the baseline is unchanged. |
| `python -m compileall -q agent_taskflow scripts tests` (from the worktree root) | exit 0 |
| `python scripts/validate_workflow_contract.py` | exit 0, `status: passed` |
| `python scripts/validate_workflow_policy.py` | exit 0, `status: passed` |
| Appendix A probe | exit 0, output in §3.1 |
| `cd mission-control && npm run build` | **not run**: no `mission-control/` file changed |

The four parts cover all 287 `tests/test_*.py` files exactly once.

---

## 8. Exact commands to verify

From the worktree root:

```bash
PY=/home/ubuntu/agent-taskflow/.venv/bin/python
export HOME=$(mktemp -d) PYTHONPATH=$PWD
export GIT_AUTHOR_NAME=T GIT_AUTHOR_EMAIL=t@example.invalid
export GIT_COMMITTER_NAME=T GIT_COMMITTER_EMAIL=t@example.invalid

# The branch changes only this file
git diff --stat a5fa8e2..HEAD

# S1 and S2: the pre-existing tests that pin today's behaviour (all green)
$PY -m unittest \
  tests.test_attempt_resources.AttemptResourceTests.test_retry_gets_new_branch_worktree_and_artifact_root \
  tests.test_dispatcher.DispatcherTests.test_executor_failed_blocks_task \
  tests.test_dispatcher.DispatcherTests.test_executor_blocked_blocks_task \
  tests.test_dispatcher.DispatcherTests.test_validator_failed_blocks_task \
  tests.test_dispatcher.DispatcherTests.test_validator_blocked_blocks_task \
  tests.test_dispatcher.DispatcherTests.test_unknown_executor_blocks_task \
  tests.test_dispatcher.DispatcherTests.test_unknown_validator_blocks_task \
  tests.test_runtime_admission tests.test_concurrency_crash_recovery

# S1 probe: save Appendix A as /tmp/probe_s1.py, then
$PY /tmp/probe_s1.py

# Full suite in four parts, each under 600 s
for p in 'test_[a-f]*.py' 'test_[g-q]*.py' 'test_r*.py' 'test_[s-z]*.py'; do
  $PY -m unittest discover -s tests -p "$p"
done

# Byte-compile and repo validators
$PY -m compileall -q agent_taskflow scripts tests
$PY scripts/validate_workflow_contract.py
$PY scripts/validate_workflow_policy.py

# Code behind S1 and S2
grep -n "UNIQUE" agent_taskflow/attempt_resources_schema.py
sed -n 280,284p agent_taskflow/attempt_resources.py
sed -n 471,481p agent_taskflow/attempt_resources.py
sed -n 204,211p agent_taskflow/canonical_runtime_path.py
sed -n 293p agent_taskflow/canonical_runtime_path.py
sed -n 368p agent_taskflow/attempt_scoped_runtime_path.py
sed -n 742,757p agent_taskflow/runtime_admission.py
sed -n 24p agent_taskflow/task_status_reset.py
```

---

## 9. Governance

- **Git.** Three commits on `task/v1-step5`: this file, its PR link, and the
  §10 re-verification. All three touch only this file. Only that
  branch was pushed, with a normal push, and the PR is a draft against `main`.
  Nothing was pushed to or merged into `main`, nothing was force-pushed, and
  nothing was rebased.
- **State.** `~/.agent-taskflow/state` and the production database were never
  read or written. Every database was a scratch one under `/tmp`.
  `taskflow_dc_notify.py` was read (lines 38-53) and not changed.
- **Scheduler.** No scheduler tick or scheduler entry point ran. The probe
  called the runtime store's claim API directly, as the existing unit tests
  do, and ran no executor or validator.
- **Approvals.** Nothing was approved, closed, cleaned up or deleted, and no
  config, cron or deployment file was touched.

---

## 10. Re-verification by a second builder run (2026-09-11, HEAD `f339d54`)

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
