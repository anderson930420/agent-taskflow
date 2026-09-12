# HANDOFF — V1 FOLLOWUPS F8: move a finished Ticket to `ready_for_integration`

**Status: implemented, full suite green, ready for human review.**
Branch `task/v1-stepf8`, based on `main` at `655901e` (F4, PR #202).
**No stop condition fired.**

---

## 1. The two facts, established before any code was written

### Fact 1 — who sets `confirm_integration`?

**It is operator-only, and in fact stronger than the brief assumed: nothing in
production calls `integrate_task` at all.**

- `IntegrationRequest.confirm_integration` defaults to `False`
  (`agent_taskflow/integration_controller.py:99`). The gate is at `:285-298`,
  *before* the per-repo lock is acquired, and `:822` forces the result's
  `dry_run` to `request.dry_run or not request.confirm_integration`.
- `integrate_task` has **exactly two call sites in the whole repo**, both in
  tests (`tests/test_integration_controller.py:127`,
  `tests/test_v1_step2_acceptance.py:169`). `IntegrationRequest` is likewise
  never constructed outside those two files. Verified by grep over the whole
  worktree, not taken on report.
- `next_for_repo` — the queue-head helper — has **zero non-test callers**.
  Nothing reads the queue and acts on it today.

**Stated plainly for the owner, as the brief asked:** reaching
`ready_for_integration` does **not** integrate anything. A human step remains
before integration runs, and there are two of them stacked:

1. no automated caller of `integrate_task` exists — invoking it is a manual act
   (`docs/v1/handoff-step2-e2e.md:201` is a human operator transcript);
2. even once a runner is wired up, it must pass `confirm_integration=True` with
   `dry_run=False` explicitly.

Caveat the owner should know: `confirm_integration` is a plain dataclass
default, not an authorization mechanism. It cannot *prevent* a future scheduler
from setting it. What keeps a human in the loop today is the absence of a
caller, not the flag. **This flag's semantics were not changed in this turn**,
as instructed.

This does not go against the ruling — the ruling anticipates it and asks for it
to be reported, not fixed.

### Fact 2 — does anything else depend on Tickets resting at `waiting_approval`?

Full dependant list below. **The answer to the load-bearing question is no: none
of them is a human gate the SPEC requires.**

The only genuine human approval gate is `POST /api/tasks/{key}/approve`
(`agent_taskflow/api/main.py:442`), with its sibling `reject` (`:485`) and the
UI buttons in `mission-control/components/ActionPanel.tsx:21-22`. It is **not a
gate on the road to integration**: it writes `accepted`
(`api/main.py:456-461`), never `ready_for_integration`. SPEC §12's status model
has no approval state between `validating` and `ready_for_integration`, and
§18's manual controls list no "approve". It is the **legacy** path's gate, it
keeps working unchanged for legacy rows, and FOLLOWUPS F5 owns the rest of that
split.

| Dependant | Path | Effect of the change |
| --- | --- | --- |
| `approved_task_runner.py:55` and its wrappers, `scheduler_watcher_one_task.py:414`, `advisory_evidence_retry.py:73`, `reset_runtime_path.py:417+`, `scripts/run_approved_task.py`, `scripts/run_issue_to_waiting_approval_smoke.py` | (a) legacy | **None.** They write and read the status themselves; the dispatcher literal is not theirs. |
| `api/main.py:442` approve, `:485` reject, `ActionPanel.tsx` | (a) legacy human gate | Still work for legacy rows. A Ticket no longer reaches them — which is the ruling. |
| `canonical_runtime_path.py:38` `RUNTIME_RELEASE_TASK_STATUSES` | (b) Ticket | **Would have broken.** Fixed — see §3. |
| `lifecycle_runtime_path.py:402` `_release` | (b) Ticket | **Would have broken.** Fixed — see §3. |
| `dispatcher.py:94` `SKIPPED_STATUSES`, `api/main.py:322` start route, `scripts/run_dispatcher.py:84` exit code | (b) Ticket | **Would have broken.** Fixed — see §2. |
| `integration_handoff.py:55` `COMPLETION_STATUS` | (b) Ticket | Retargeted — see §2. |
| `integration_controller.py:254` status gate | (b) Ticket | Now satisfiable. This is the point of F8. |
| `ticket_dependencies.py:43-55`, `ticket_retry.py`, `task_status_reset.py` | (b) Ticket | **Safe.** Release is keyed on `completed`; retry only from `failed` / `needs_decision`. Neither ever sees either status. |
| `status_vocab.py:105` | (c) shared | **Safe and total.** `ready_for_integration` was already canonical in both directions; no mapping was added or renamed. Only the stale comment changed. |
| `api/tickets.py` filters, `realtime_projection.py:107` | (c) shared | Correct by construction. The board card moves from READY FOR REVIEW to RUNNING — a display change, no crash. |
| `pr_handoff`, `branch_push_confirm`, `draft_pr_confirm`, `task_closeout_confirm`, `post_merge_cleanup_recommendation`, `waiting_approval_summary`, `pr_preparation_pipeline`, `task_to_draft_pr_pipeline` | (c) legacy pre-Step-2 PR route | They refuse a Ticket now. That is correct: a Ticket's integration surface is Step 2's controller, not this parallel manual route. Two of them already carry `require_waiting_approval` / `allow_non_waiting` escape hatches. |
| `ops/taskflow_dc_notify.py:40` | (c) notifier | **Goes dark for finished Tickets.** Not fixed — see §6, follow-up 1. |
| `mission-control/lib/taskState.ts` | (c) frontend | Falls back gracefully (raw label, no actions). Not fixed — see §6, follow-up 2. |

---

## 2. What was implemented

The success terminal is now split exactly the way the §29 failure vocabulary
already was.

### `agent_taskflow/ticket_lifecycle.py`

Added `TICKET_SUCCESS_STATUS = "ready_for_integration"`,
`LEGACY_SUCCESS_STATUS = "waiting_approval"` and
`ticket_success_status(*, ticket: bool)`, mirroring the existing
`ticket_failure_status(kind)`.

### `agent_taskflow/dispatcher.py`

The success write takes `ticket_success_status(ticket=ticket)` instead of the
`waiting_approval` literal, and passes that status to the handoff instead of
re-hardcoding it. `TICKET_SUCCESS_STATUS` joins `SKIPPED_STATUSES`, so a
finished Ticket is skipped on re-dispatch exactly as a finished legacy task is.

### `agent_taskflow/integration_handoff.py` (F4's module, retargeted)

- `COMPLETION_STATUS` is now `TICKET_SUCCESS_STATUS`. The legacy terminal no
  longer hands off — and never could, since a legacy row fails the Ticket check
  anyway.
- **§22.1's FIFO key is now literally correct.** New `_entered_status_at()`
  reads the timestamp back from the `status_changed` audit event that
  `update_task_status` wrote, and passes it as the queue entry's `enqueued_at`.
  The key is the moment the Ticket *entered* `ready_for_integration`, not the
  moment the handoff got round to enqueueing it.

### `agent_taskflow/api/main.py`, `scripts/run_dispatcher.py`

`ready_for_integration` joins the `start` route's refusal set (otherwise an
operator could restart a finished Ticket) and the CLI's exit-0 set (otherwise
every successful Ticket run would report failure to cron).

### `agent_taskflow/status_vocab.py`

Comment only. No mapping was added, renamed, removed or repurposed —
`ready_for_integration` was already canonical in both directions. The
`waiting_approval` comment claimed it was "written by the dispatcher … the
repo's human review gate"; it now says it is the legacy path's terminal and
names F8 for the Ticket path.

---

## 3. The dependant that would have silently corrupted finished Tickets

This is the part of the change that is not obvious from the ruling, and the
reviewer should read it.

`Dispatcher.store` is not a plain `TaskMirrorStore`. `agent_taskflow/__init__.py`
installs a stack of nine store subclasses at import time. In that stack,
`canonical_runtime_path.RUNTIME_RELEASE_TASK_STATUSES` decides which task
statuses **end the claimed Attempt and release its runtime lease**.

`ready_for_integration` was not in that set. Writing it naively would have:

1. left `tasks.active_attempt_id` set and the lease active — the finished
   Ticket would hold its §19 capacity slot indefinitely;
2. then, on lease expiry, let the reaper flip the successfully-completed Ticket
   to `failed` (`runtime_admission.py:782-786`).

Fixed in three additive places:

| File | Change |
| --- | --- |
| `canonical_runtime_path.py` | `ready_for_integration` added to `RUNTIME_RELEASE_TASK_STATUSES`; `_terminal_attempt_status` treats it like `waiting_approval`; its own reason code in the release map. |
| `lifecycle_runtime_path.py` | `_release` gets an explicit branch. Without it the success terminal fell through to the final `else` and recorded a **governance-blocked** Attempt for a passing run. |
| `lifecycle_control.py`, `lifecycle_reason_compat.py` | Registered `runtime_ready_for_integration` and `canonical_runtime_ready_for_integration`, so the audit trail names which path released the claim. Step 5 added `runtime_failed` the same way. |

Two deliberate non-changes here:

- **The Attempt vocabulary is not renamed.** `ready_for_integration` is not an
  `ATTEMPT_STATUSES` value, and the Attempt-level meaning of both terminals is
  identical — implementation done, validators passed. The Attempt keeps
  spelling that `waiting_approval`. Renaming it would have needed a migration
  and would have gone well beyond the allowed layers.
- **The SQL triggers were not touched, and no migration was needed.**
  `runtime_admission_schema.py:266` and `canonical_runtime_schema.py:153` key
  on a terminal-status list. They are *safety nets for unowned direct writes*:
  the real release path inserts a `runtime_claim_suppressions` row before its
  `UPDATE tasks`, which suppresses both. Adding `ready_for_integration` to the
  first would also have made it write an **invalid attempt status** at the DB
  level (`UPDATE attempts SET status = NEW.status`). Step 5's `failed` and
  `needs_decision` are in the Python release set and absent from both triggers
  for the same reason; F8 follows that precedent exactly.
  **Known, pre-existing gap shared with Step 5:** an *unowned* direct write to
  `ready_for_integration` is not ABORTed by
  `runtime_token_terminal_requires_owned_release`. No code path does that.

`ChainClosesTests::test_the_terminal_status_releases_the_runtime_claim` pins all
of it.

---

## 4. Acceptance gate

`tests/test_v1_f8_ready_for_integration.py` — 19 tests, written before the
implementation. Every one drives the real Dispatcher on a real Step 1 Ticket
and reads the queue through Step 2's public API.

| # | Requirement | Test |
| --- | --- | --- |
| §43.12 | The chain closes: reaches `ready_for_integration`, appears exactly once in its repo's queue | `ChainClosesTests::test_a_validated_ticket_reaches_ready_for_integration_and_its_queue` |
| §43.12 | No human step in between | `ChainClosesTests::test_no_human_step_sits_between_validating_and_ready_for_integration` (asserts `validating` is the immediately preceding status, that `waiting_approval` and `accepted` never appear, and that no approval row was written) |
| §19/§44 | The terminal releases the claim, lease and Attempt | `ChainClosesTests::test_the_terminal_status_releases_the_runtime_claim`, `::test_the_capacity_slot_is_free_for_the_next_ticket` |
| §22.1 | FIFO key is the moment of entry | `FifoKeyTests::test_the_queue_timestamp_is_when_the_ticket_entered_ready_for_integration` |
| §22.1 | Priority never affects integration order | `FifoKeyTests::test_queue_order_follows_entry_order_not_priority` |
| §44 | No self-approval, merge or push; no Step 2 PR field written | `NoSelfApprovalTests::test_nothing_approves_merges_or_pushes` |
| §44 | Integration still requires fact 1's gate | `NoSelfApprovalTests::test_integration_still_requires_its_confirmation_flag`, `::test_the_queue_entry_alone_does_not_advance_the_ticket` |
| §29 | Validator red → `needs_decision`; executor crash, worktree failure, lease expiry → `failed`; none enqueued | `FailuresUnchangedTests` (4 tests) |
| — | Blocked and paused: unclaimable, never enqueued, row and events untouched | `BlockedAndPausedTests::test_blocked_and_paused_are_refused_untouched` |
| — | Legacy untouched: the GitHub-issue path still writes `waiting_approval` | `LegacyUntouchedTests::test_a_legacy_mirror_row_still_ends_waiting_approval`, `::test_the_success_status_helper_splits_ticket_from_legacy` |
| — | Idempotent: reruns, retries, repeated handoffs → one entry, original timestamp | `IdempotenceTests` (3 tests) |

### Pre-existing tests updated, and why

11 tests went red, **all for the reason the ruling authorises** (the Ticket
success terminal moved). None was weakened; each assertion was retargeted, not
deleted.

| File | Change |
| --- | --- |
| `test_step5_ticket_worktree.py` | 3 Ticket dispatch assertions retargeted. `AT-LEGACY-1` / `AT-LEGACY-2` keep `waiting_approval`, now with a comment saying why. |
| `test_step5_failure_vocabulary.py` | 3 retargeted. `ReviewFixRefusalTests` split: `ready_for_integration` is now *skipped* rather than "not runnable", so it got its own test — `test_a_finished_ticket_is_skipped_untouched` — which keeps the same untouched-row property. |
| `test_step5_dependency_admission.py`, `test_step5_parallel_scheduler.py` | Status literals retargeted. |
| `test_v1_f4_integration_handoff.py` | `test_the_handoff_changes_no_lifecycle_status` had its premise reversed by the ruling. Rewritten as `test_the_handoff_itself_changes_no_lifecycle_status`: the *handoff* is still a pure queue producer — calling it again writes no further status — while the dispatcher owns the status write. The direct-call guard now also asserts the legacy terminal is refused. |
| `test_v1_step2_acceptance.py` | The §43.12 step no longer hand-drives `waiting_approval → ready_for_integration`; F8 removed that operator edge, so the test now asserts the dispatcher already left the Ticket there. |

`tests/test_dispatcher.py` passed unchanged — its tasks are legacy mirror rows.

---

## 5. Stop conditions

**None fired.**

- Neither fact went against the ruling. Fact 1 confirms a human step remains
  before integration (reported, not changed). Fact 2 found one human gate, and
  it is the legacy path's, not one the SPEC requires.
- No pre-existing test went red for a reason the ruling does not authorise. The
  11 that went red are enumerated in §4.
- No forbidden layer was needed. Step 2's queue, lock, controller, watcher,
  cleanup and GitHub adapter are untouched; so are the capacity gate, the claim
  transaction, the dependency gate and F1's claimable set beyond adding the new
  terminal to the release and skip sets. No migration was added and no schema
  changed. The legacy `approved_task_runner.py` path is untouched.

---

## 6. Follow-ups for the human reviewer

1. **The Discord notifier goes dark on finished Tickets — do this first.**
   `ops/taskflow_dc_notify.py:40` has `NOTIFY_STATUSES = {"waiting_approval",
   "waiting_for_review", "blocked"}` and `:137` returns `None` for anything
   else, silently. After F8 the operator stops being notified when a Ticket
   finishes. **Not fixed here**: `ops/` is outside F8's allowed layers, and
   `CLAUDE.md` forbids changing cron configuration without an explicit ask. The
   one-line fix is adding `"ready_for_integration": "🟢 可整合"` (or similar) to
   that dict. This is the highest-value follow-up.
2. **Mission Control's legacy task page shows the raw status.**
   `mission-control/lib/taskState.ts` has no `ready_for_integration` entry, so
   `getStateInfo` falls back to a raw label with no actions and `terminal:
   true`. It degrades gracefully and — correctly — shows **no approve button**,
   but it is cosmetically wrong, and `SummaryCards.tsx` / `TaskBoard.tsx`
   waiting-approval counts now read 0 for Tickets. The V1 board
   (`realtime_projection.py`) is already correct; it moves the card from READY
   FOR REVIEW to RUNNING. The frontend is outside F8's allowed layers.
3. **Nothing consumes the queue yet.** Per fact 1, `integrate_task` has no
   production caller. F8 closes §43.12's producer *and* lifecycle halves; the
   consumer half — whatever calls `integrate_task` with `confirm_integration` —
   is still unbuilt and is the natural next follow-up.
4. **The legacy pre-Step-2 PR route now refuses Tickets** (`pr_handoff`,
   `branch_push_confirm`, `draft_pr_confirm`, `task_closeout_confirm`, …).
   Intended — Step 2's controller is a Ticket's integration surface — but worth
   confirming no operator runbook still points a Ticket at those scripts.
5. **`github_repo` on the registry** (carried over from F4): repositories in
   `config/projects.yaml` without `github_repo` silently never hand off.

---

## 7. Exact commands to verify

```bash
cd /home/ubuntu/agent-taskflow/.worktrees/v1-stepf8

# the F8 acceptance gate on its own
PYTHONPATH=. /home/ubuntu/agent-taskflow/.venv/bin/python -m pytest \
  tests/test_v1_f8_ready_for_integration.py -q -p no:cacheprovider

# the suites whose expectations the ruling changed
PYTHONPATH=. /home/ubuntu/agent-taskflow/.venv/bin/python -m pytest \
  tests/test_step5_ticket_worktree.py tests/test_step5_failure_vocabulary.py \
  tests/test_step5_dependency_admission.py tests/test_step5_parallel_scheduler.py \
  tests/test_step5_dependencies.py tests/test_v1_f4_integration_handoff.py \
  tests/test_v1_step2_acceptance.py tests/test_dispatcher.py \
  -q -p no:cacheprovider

# the full suite, ONE command
PYTHONPATH=. /home/ubuntu/agent-taskflow/.venv/bin/python -m pytest \
  tests -q -p no:cacheprovider -n 4

/home/ubuntu/agent-taskflow/.venv/bin/python -m compileall -q agent_taskflow scripts tests
PYTHONPATH=. /home/ubuntu/agent-taskflow/.venv/bin/python scripts/validate_workflow_contract.py
PYTHONPATH=. /home/ubuntu/agent-taskflow/.venv/bin/python scripts/validate_workflow_policy.py

# Step 4's rehearsal, into a fresh directory
PYTHONPATH=. /home/ubuntu/agent-taskflow/.venv/bin/python \
  scripts/run_concurrency_rehearsal.py --output-dir "$(mktemp -d)"
```

### Results observed

| Command | Result |
| --- | --- |
| F8 acceptance gate | `19 passed, 2 subtests passed in 14.82s` |
| The 8 affected suites | `200 passed, 72 subtests passed in 115.88s` |
| **Full suite** | **`5389 passed, 8 skipped, 1992 subtests passed in 310.86s`** (F4's baseline was 5369 / 8 / 1990) |
| `compileall` | exit 0 |
| `validate_workflow_contract.py` | `status: passed`, exit 0 |
| `validate_workflow_policy.py` | `status: passed`, exit 0 |
| Step 4 rehearsal | exit 0, **16/16 checks**, `all_checks_passed: true`, `production_database_touched: false` |

---

## 8. Safety

No production database was opened: every test builds its own SQLite file in a
`TemporaryDirectory`, and the rehearsal ran against fresh disposable databases
in a fresh `mktemp -d`. `~/.agent-taskflow/state` was never read or written. No
scheduler tick and no scheduler entry point was run. No GitHub repository was
created. Nothing was merged, rebased or force-pushed; only `task/v1-stepf8` was
pushed, and the PR is a **draft**.
