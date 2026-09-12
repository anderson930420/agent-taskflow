# HANDOFF — V1 FOLLOWUPS F4: integration handoff (SPEC §43.12)

**Status: implemented, full suite green, ready for human review.**
Branch `task/v1-stepf4`, based on `main` at `92625e9` (Step 2, PR #196).
No stop condition fired.

---

## 1. The open question, settled by the SPEC

> Does §43.12's handoff fire when implementation completes
> (`waiting_approval`), or only after a human approval moves the Ticket on
> (`ready_for_integration`)?

**It fires when implementation completes.** The SPEC settles this, and it does
so one-sidedly: there is no text anywhere in it supporting a human approval
before the Integration Queue.

Evidence for "on completion":

| Where | What it says |
| --- | --- |
| §43.12 | "Send **completed implementation** to per-repo Integration Queue." The named trigger is completed implementation, not approved implementation. |
| §43 ordering | 11 (show runtime state) → **12 (queue)** → 13 (serialize per repo) → 14 (initial integration). The only human steps in §43 are 23/24 (PR review outcomes), 26 (cleanup confirmation) and 28 (human performs the final GitHub merge) — all *after* 12. |
| §1 | 「9. 完成後進入該 Repository 的 Integration Queue」, then 10-11 Taskflow integrates and opens the PR, and only at 12 「Human 在 GitHub review」. |
| §12 | The status model has no approval state between `validating` and `ready_for_integration`. §12.1 is careful to call out `queued` as reserved-and-never-written, so an unused approval state would have been named if one existed. |
| §18 | Manual controls are priority, pause, resume, cancel, retry, set/remove `blocked_by`. There is no "approve". |
| §31 | The PR *is* the review surface, and the Integration Controller creates it. A human approval before integration would have nothing to review — the PR does not exist yet. |
| §44 | Every human invariant is about merge: "Every merge requires human GitHub review", "AI cannot merge", "AI cannot self-approve". §23.1 adds "Integration lock does not wait for human review". |

Evidence for "only after approval": none found in the SPEC. §22 and §22.1 show
that a *queued* Ticket is at `ready_for_integration` and that the FIFO key is
that entry timestamp, but neither says who puts it there, so neither
discriminates between the two options.

**Verified against the code, not assumed:** the repo has no approval path that
produces `ready_for_integration` either. `api/main.py` `approve_task` requires
`waiting_approval` and writes `accepted`; `reject_task` writes `rejected`.
Neither reaches `ready_for_integration`. The single production writer of
`ready_for_integration` is `integration_watcher.py:202`, the stale-target
re-integration edge, which is reachable only from `waiting_for_review` — i.e. a
cycle inside integration, not an entry into it. That confirms the ruling-43
finding in `docs/v1/handoff-step2-e2e.md`.

## 2. What was implemented

One new module and one call site.

### `agent_taskflow/integration_handoff.py` (new)

`handoff_completed_implementation(store, task_key, *, task_status, ...)`:

1. refuses any `task_status` other than `waiting_approval`;
2. resolves the Ticket through `TicketStore.get_ticket`; a legacy mirror row is
   skipped;
3. takes the repo from **the Ticket's own `github_repo`**, never a default and
   never a constant;
4. returns early if the Ticket is already queued (idempotence guard), reporting
   the existing entry;
5. otherwise calls Step 2's public `enqueue_for_integration(...)` and writes one
   `integration_queued` audit event naming the Ticket, the repo and the queue.

It returns an `IntegrationHandoffResult` with `status` in
`enqueued` / `already_queued` / `skipped`.

### `agent_taskflow/dispatcher.py` (extended, not rewritten)

`Dispatcher._handoff_to_integration` is called immediately after the existing
`waiting_approval` write in `dispatch_task`. It is best-effort in the same sense
as the runtime progress writes: the implementation is already complete and
already recorded, so a queue failure is audited as a `integration_handoff_failed`
note and never turns a finished run into a failure. This is the only chokepoint
needed — the parallel scheduler reaches the dispatcher through
`scheduler_worker.py`, so a scheduler tick uses the same path.

### Modules touched

| Module | Extended or created |
| --- | --- |
| `agent_taskflow/integration_handoff.py` | created |
| `agent_taskflow/dispatcher.py` | extended (one call, one private method, one import) |
| `tests/test_v1_f4_integration_handoff.py` | created |
| `tests/test_v1_step2_acceptance.py` | extended (the §43.12 step now drives the real path) |

Nothing else was touched. Step 2's queue, lock, controller, watcher, cleanup and
GitHub adapter are unchanged; so are the claim transaction, the capacity gate,
the dependency gate and the reaper.

## 3. What was deliberately NOT done, and why

### No lifecycle status change

The handoff writes **no** status. The Ticket stays exactly where the dispatcher
left it, at `waiting_approval`.

This is deliberate and it is the one place where the SPEC's model and this
repository's do not line up. `status_vocab.py` (merged, human-ruled in Step 1)
binds persisted `waiting_approval` to display `needs_review` and calls it "the
repo's human review gate", and `WORKFLOW.md` — the repo-owned workflow contract
— spells the lifecycle `waiting_approval -> approved / rejected / blocked`.
Auto-advancing a Ticket off `waiting_approval` would be approving it, which
`CLAUDE.md` forbids outright, and would break every consumer that requires that
status (`pr_handoff`, `branch_push_confirm`, `draft_pr_confirm`,
`task_closeout_confirm`, `post_merge_cleanup_recommendation`, both API routes).
It is also outside F4's allowed layers, whose first item is "a call into Step
2's queue API", not a lifecycle edge.

**Consequence, stated plainly:** a queue entry is a handoff record, not
permission to integrate. `integration_controller` still requires
`expected_current_status=ready_for_integration`, so a Ticket sitting in the
queue at `waiting_approval` cannot be integrated until something moves it on.
**Nothing moves it on yet.** The `waiting_approval → ready_for_integration` edge
remains unbuilt, exactly as ruling 43 found it; F4 closes the queue-producer
half of that gap and not the lifecycle half. Whoever owns that edge should read
this section first, because it is a SPEC-vs-WORKFLOW.md reconciliation and not a
coding task.

### Tickets with no `github_repo` are skipped

Step 2's integration always fetches, pushes and updates a GitHub PR, so a Ticket
whose registry entry has no `github_repo` could never leave the queue.
Enqueueing it would create a permanently stuck entry, so the handoff skips it
and writes nothing. This is a limitation of the registry entry, not of the
handoff. It is covered by a test.

### Step 2's §43.12 test

`test_full_lifecycle_from_queue_to_completed` no longer hand-enqueues. It now
brings AT-101 to the state the dispatcher leaves (a Ticket row with
`github_repo`, at `waiting_approval`) and calls the production handoff, then
asserts the queue entry and the audit event. Because F4 owns no lifecycle edge,
that test then drives `waiting_approval → ready_for_integration` itself, with a
compare-and-set and a comment saying why. The dispatcher is exercised end to end
in the new F4 test file instead, which is the stronger of the two proofs.

## 4. Acceptance gate

| # | Requirement | Test |
| --- | --- | --- |
| §43.12 | Handoff happens, once, in the Ticket's own repo queue, repo from the Ticket | `HandoffHappensTests::test_a_completed_implementation_is_enqueued_once_in_its_own_repo_queue` |
| — | Idempotent across two dispatches, a retry and two ticks | `IdempotenceTests::test_reruns_reuse_the_original_queue_entry` |
| — | Only on success (`failed`, `blocked`, `needs_decision`, `paused`) | `OnlyOnSuccessTests` (6 tests) |
| — | Per-repo isolation | `PerRepoIsolationTests::test_queues_stay_isolated_per_repo` |
| §44 | Auditable | `AuditTests::test_the_handoff_writes_one_audit_event_naming_ticket_repo_and_queue` |
| — | Step 2 still green, former hand-enqueued test drives the real path | `tests/test_v1_step2_acceptance.py`, all 27 pass unchanged apart from the §43.12 step |

Plus `test_the_handoff_changes_no_lifecycle_status` and
`test_a_queue_failure_is_audited_and_never_fails_a_finished_run`.

## 5. Stop conditions

None fired.

- The SPEC settled the question (§1 above), so the "not settled" condition did
  not apply.
- No pre-existing test went red. The full suite is green.
- No forbidden layer was needed. The one place that would have needed one — the
  lifecycle edge — was left unbuilt and is documented in §3 above rather than
  built around.

## 6. Exact commands to verify

    cd /home/ubuntu/agent-taskflow/.worktrees/v1-stepf4

    # the F4 acceptance gate on its own
    PYTHONPATH=. /home/ubuntu/agent-taskflow/.venv/bin/python -m pytest \
      tests/test_v1_f4_integration_handoff.py -q -p no:cacheprovider

    # Step 2 still green
    PYTHONPATH=. /home/ubuntu/agent-taskflow/.venv/bin/python -m pytest \
      tests/test_v1_step2_acceptance.py tests/test_integration_queue.py \
      tests/test_integration_controller.py tests/test_integration_watcher.py \
      tests/test_integration_cleanup.py -q -p no:cacheprovider

    # the full suite, one command
    PYTHONPATH=. /home/ubuntu/agent-taskflow/.venv/bin/python -m pytest \
      tests -q -p no:cacheprovider -n 4

    /home/ubuntu/agent-taskflow/.venv/bin/python -m compileall -q agent_taskflow scripts tests
    PYTHONPATH=. /home/ubuntu/agent-taskflow/.venv/bin/python scripts/validate_workflow_contract.py
    PYTHONPATH=. /home/ubuntu/agent-taskflow/.venv/bin/python scripts/validate_workflow_policy.py

### Results observed

| Command | Result |
| --- | --- |
| F4 acceptance gate | `13 passed, 7 subtests passed in 10.38s` |
| Step 2 + dispatcher suites | `226 passed, 36 subtests passed` (before the §43.12 rewrite landed), then `27 passed, 27 subtests passed` for Step 2 acceptance after it |
| Full suite | **`5369 passed, 8 skipped, 1990 subtests passed in 296.22s`** |
| `compileall` | exit 0 |
| `validate_workflow_contract.py` | `status: passed`, exit 0 |
| `validate_workflow_policy.py` | `status: passed`, exit 0 |

## 7. Safety

No production database was opened: every test builds its own SQLite file in a
`TemporaryDirectory`. `~/.agent-taskflow/state` was never read or written. No
scheduler tick and no scheduler entry point was run. Nothing was merged,
rebased or force-pushed; only `task/v1-stepf4` was pushed, and the PR is a
**draft**.

## 8. Follow-ups for the human reviewer

1. **The lifecycle edge.** `waiting_approval → ready_for_integration` still has
   no producer. Deciding it means reconciling the SPEC (no pre-integration human
   gate) with `WORKFLOW.md` (an explicit one). That is a ruling, not a patch.
2. **`github_repo` on the registry.** Repositories in `config/projects.yaml`
   without `github_repo` silently never hand off. Worth an audit of the registry.
