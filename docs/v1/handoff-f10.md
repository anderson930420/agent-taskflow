# V1 F10: the rest of the integration side, and both cron invokers

F9 gave the integration queue a consumer. F10 gives every remaining
consumer-side component a real, non-test invoker (SPEC §47.2), puts a
non-overlap lock on both tick entry points (§47.3), and ships cron examples.
Nothing is installed: installing or changing a cron entry is a human operator
action (§47.4), and so is choosing which confirmations cron passes. F10's
real end-to-end gate (below) passed on 2026-09-25.

## One integration-tick pass

`scripts/run_integration_tick.py` runs one bounded pass for one repository,
then exits. In this fixed order, once each:

1. **PR outcomes** (`poll_pr_outcomes`, §32/§33/§35): a merge is recorded,
   never completed; `changes_requested` moves the Ticket to `needs_decision`
   once per reviewed head; closed-unmerged moves it to `cancelled`, leaves the
   queue, and keeps its worktree and branches (§33.5).
2. **Verified-merge cleanup** (`run_integration_cleanup`, §36/§37) for this
   repository's merged Tickets whose merge is not yet verified
   (`IntegrationStore.list_merged_unverified_pr_states`). The tick always passes
   `confirm_cancelled_cleanup=False` and `delete_remote_branch=False`: §37.1
   cancelled-work cleanup is never automated and remote branches are never
   deleted (ruling 5).
3. **Target freshness** (`poll_target_freshness`, §25.1): a stale `needs_review`
   Ticket is re-queued; `integrating` is deferred; `paused` and
   `needs_decision` are never re-queued.
4. **The F9 FIFO drain**, unchanged. Its snapshot is taken after step 3, so a
   Ticket re-queued in this pass is re-integrated in this pass.

The order puts outcomes first so closed PRs leave the queue and merges are
recorded before anything else; cleanup next so a verified merge completes;
freshness after that so a target the merge just advanced re-queues its stale
siblings; and the drain last. `PHASE_ORDER` in `agent_taskflow/integration_tick.py`
pins it and the tests pin `PHASE_ORDER`.

The composition lives in `run_integration_tick` behind a new request flag,
`consumer_phases`, which defaults to off. That keeps F9's drain-only API and its
16 tests unchanged, and it is the only home that keeps F9's CLI contract (the
script prints `run_integration_tick`'s result as-is). The CLI always turns the
phases on.

### Confirmations

Every phase is a read-only preview unless its own flag is passed:

| Flag | Lets this phase write |
| --- | --- |
| `--confirm-pr-poll` | §32.1 PR fields and the §33/§35 outcome transitions |
| `--confirm-cleanup` | worktree removal, local branch safe-delete, `completed` |
| `--confirm-freshness` | re-queueing a stale `needs_review` Ticket |
| `--confirm-integration` | F9's drain (push, draft PR create/update) |

`--dry-run` is still F9's explicit preview and cannot be combined with any
`--confirm-*` flag. With no flag the tick writes nothing to the database,
pushes nothing and changes no PR. The freshness poll and merge verification
still run `git fetch origin --prune` in preview, inside Step 2 functions F10
does not change. That fetch moves or deletes no existing local branch or tag,
and changes no worktree and nothing on the remote. It may add or update
remote-tracking refs and `FETCH_HEAD`, and it auto-follows origin's tags that
point into fetched history and are missing locally. With `fetch.pruneTags=true`
in the git configuration it would also prune local-only tags, in preview and in
confirmed runs alike.

### Result and exit codes

One JSON document (or one line with `--jsonl`). F9's keys keep their meaning:
`status`, `outcomes`, `stopped_reason`, `remaining_task_keys` and `dry_run`
describe the drain. F10 adds `phase_order`, `phases` (per phase: `ran`,
`confirmed`, `ok`, per-Ticket outcomes — `PrOutcome`, the cleanup
`to_summary_dict()`, `TargetFreshnessOutcome`), `confirmations`, `drain_ok`,
`tick_status`, `integration_lock_at_start`, `ready_for_integration_unqueued` and
`merge_verified_not_completed`.

| Exit | Meaning |
| --- | --- |
| 0 | every phase and the drain succeeded (including a clean preview) |
| 1 | not ok: a poll error, a cleanup refusal, failed verification or incomplete cleanup, a Ticket freshness could not examine, a drain refusal or `lock_unavailable`, a ready-but-unqueued Ticket, or a Ticket whose merge is verified but which is not completed. An exception while cleaning one Ticket is also exit 1, not 2: it becomes that Ticket's cleanup outcome with `status: error`, and the pass, including the drain, continues. |
| 2 | error: invalid input, a missing database, a non-overlap lock that cannot be taken (including a refused `--lock-path`), or a phase that raised (the pass then ends before the drain) |
| 75 | `skipped_overlap` (see below) |

### What the tick reports but never repairs

* **A held per-repository lock row.** The tick never takes or releases the
  §23.1 lock (ruling 62). `integration_lock_at_start` reports the holder and
  `acquired_at` read-only. A row left by a killed integration stays until an
  operator acts: the lock has no TTL or reaper. That is owner-reserved work
  (V1-INTEGRATION-LOCK-LIVENESS), and it must be decided before cron is
  installed.
* **Ready-but-unqueued Tickets.** The dispatcher audits an enqueue failure
  rather than failing the run, so a Ticket can sit in `ready_for_integration`
  outside every queue. The tick lists this repository's such Tickets in
  `ready_for_integration_unqueued` and exits 1. It never re-enqueues them:
  re-enqueue semantics are not ruled. A Ticket caught in the moment between the
  dispatcher's status write and its enqueue can be listed once.
* **Verified merges that are not completed.** Cleanup records
  `merge_verified_at` before it removes anything (Step 2's existing order). If
  git then declines part of the removal, the result is `cleanup_incomplete` and
  the Ticket leaves every pick-up: its PR is closed, its merge is no longer
  unverified, and it is not queued. So every tick lists this repository's
  Tickets whose merge is verified but which are not completed in
  `merge_verified_not_completed` (with `cleanup_confirmed_at`, the worktree and
  the branch), read-only, and exits 1. A human-confirmed retry of
  `run_integration_cleanup`, once the cause is fixed, finishes the Ticket.
  Making this self-healing by writing `merge_verified_at` only after removal
  reorders Step 2's writes; that is follow-up F10-FU3, an owner decision.

## Cleanup fails closed on its target

Because cron may now run cleanup unattended, `integration_cleanup` fails closed
(orchestrator ruling OR-3):

* It never deletes a directory itself. The `shutil.rmtree` fallback is gone; a
  worktree git declines to remove is retained.
* Before anything is recorded or removed, the target must be the registered
  task worktree at exactly the recorded path, on exactly the recorded branch,
  under `<repo>/.worktrees/`, for the requested repository, and clean. The local
  branch's tip must already be in the recorded PR head or the remote task
  branch. Otherwise the result is `cleanup_refused`, nothing changes, and the
  Ticket stays a candidate that the next tick reports again.
* The branch tip is read again after the worktree is removed. If it is no
  longer exactly the tip the check proved published (for example, a commit
  landed while cleanup ran), the branch is kept. Only a gap of milliseconds
  between that read and `git branch -D` remains. Closing it would take
  `git update-ref -d <ref> <old>`, which the git allowlist does not include.
* If git still leaves the worktree or branch behind, the result is
  `cleanup_incomplete`, its summary says why, and the Ticket is not completed.
  A retry treats an already-removed worktree or branch as progress.
* A preview (`confirm_cleanup=False`) persists nothing, not even a refusal's
  evidence file.

## Non-overlap locks (§47.3)

Each tick holds a non-blocking `flock` for its whole run, beside the resolved
database and derived only from `--db-path` (or an explicit absolute
`--lock-path`):

* execution tick: `<db>.execution-tick.lock`
* integration tick: `<db>.integration-tick.<owner>@<name>.lock`, keyed by
  database and repository so different repositories integrate concurrently.

A second invocation prints one JSON result with `"status": "skipped_overlap"`,
the lock path and the recorded holder, and exits 75 without doing any work.
Hand-run and cron-run invocations contend on the same lock, and so does a
`flock(1)` holding that file. The kernel releases the lock when its holder
dies, including by SIGKILL; git, gh, validator and worker children do not
inherit it. The execution tick's `run_scheduler_tick` is unchanged; only its
entry point gained the lock, `--lock-path` and `--jsonl`.

A lock file only ever holds nothing or one holder record, and a lock never
overwrites anything else. Before it writes, it refuses these paths:

* the database, or its `-wal`, `-shm` or `-journal` file, whether named
  directly, through a symlink or as a hard link;
* a symlink;
* an existing file with any other content.

A record left by a killed holder is replaced. A lock that cannot be taken for
any reason other than overlap is an error: either tick prints one error JSON
line and exits 2.

## Cron examples

`deploy/cron/v1-execution-tick.cron.example` (every 5 minutes) and
`deploy/cron/v1-integration-tick.cron.example` (every 5 minutes, offset by 2,
one line per repository) follow the existing "Example only" convention. Each
line names an explicit `--db-path`, appends one JSONL line per run (a skip
included) and keeps stderr in its own log. The integration line carries all four
confirmation flags, each commented as a human choice (human decision H4).
`--confirm-cleanup` acts only on merges that a confirmed PR poll has recorded:
without `--confirm-pr-poll` it cleans up no new merge. Do not wrap either line
in `flock(1)` on the tick's own lock file, and do not add `timeout(1)`. The
cadence is provisional (§47.2).

## Known limits

* A confirmed PR poll records one `pr_state_polled` event per open PR on every
  run, and refreshes `pr_last_polled_at` (existing `integration_watcher`
  behaviour). Everything else is idempotent: a repeat tick with nothing new
  changes no status and writes no other event. Acceptance criterion 7
  ("re-invoking with nothing new changes nothing") was accepted under SPEC
  §32.1, which makes `pr_last_polled_at` a watcher-owned field written on each
  poll: no lifecycle or domain change, and no event but that per-poll audit.
  Recording the event only when a polled field changes is follow-up F10-FU1,
  to settle before cron is installed. Reading criterion 7 literally would be an
  owner decision (AGENT-EXECUTION §14).
* The execution tick's worker uses the Dispatcher defaults, whose default
  executor is `manual`. Executor selection is owner-reserved
  (V1-EXECUTOR-CONTRACT); F10 does not touch it.
* Cleanup's `git worktree remove` and `git branch -D` can run while an execution
  tick creates another worktree in the same repository. Git's ref and admin
  locks serialize them; a lost race fails the git command, which surfaces as
  `cleanup_incomplete`. This is not reproduced by a test.
* A cleanup that fails after the merge was verified (for example a locked
  worktree, or a branch that moved during cleanup) is no longer selected by the
  tick. It is listed in `merge_verified_not_completed` on every tick until a
  human-confirmed retry of `run_integration_cleanup` finishes it (F10-FU3).
* After a verified merge, `git worktree remove` also deletes gitignored files
  in the task worktree, such as a `.env`. `git status` does not show them, so
  the clean-worktree check cannot see them. This is git's own behaviour, and
  cleanup did the same before F10.

## Real end-to-end gate (ruling 56)

**PASSED on 2026-09-25** (`real_e2e_passed`). The orchestrator ran it after the
round-2 review, on the private throwaway repository
`anderson930420/agent-taskflow-f10-e2e-20260925-opus55`, against the reviewed
frozen source. After setup the two cron lines were the only drivers
(orchestrator ruling OR-2), and a human performed every GitHub PR action.

* Phase 1: four Tickets drained in FIFO order (priority ignored) to
  `needs_review` with four draft PRs. The overlapping cron run was refused
  with `skipped_overlap` (exit 75). Both tick locks recovered after SIGKILL.
* Human action 1: PR(A) squash-merged and PR(C) closed unmerged. PR(B) could
  not get "Request changes" from its own author, so that sub-case is not
  covered.
* Phase 2: A's merge was detected, verified and cleaned up. C was cancelled
  with its worktree kept. The stale B and D were re-integrated on the same PRs.
* Human action 2: PR(D) merged with a merge commit.
* Phase 3: D was verified and cleaned up. The orchestrator checked the final
  fixture DB independently: A and D `cleaned`, B `waiting_for_review`,
  C `canceled`, and no `integration_locks` rows.

Evidence: `.agent-taskflow/evidence/V1-F10/opus55-20260925-01/builder-r2/real-e2e/` (on the VPS, gitignored), in the run directories
`*-setup-phase1-*`, `*-after-human-1-*` and `*-after-human-2-*`, and
`fixture.json`.

Rollback is a human-reviewed revert. The drain keeps working without the
consumer phases, and no schema or migration was added.
