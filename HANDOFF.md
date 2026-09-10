> This file covers **two** V1 steps. `task/v1-step1` was merged into
> `task/v1-step2` to bring in `agent_taskflow/status_vocab.py` per the SPEC
> §12.2 ruling, so both handoffs travel together on this branch. Step 2 is
> the subject of draft PR #196; the Step 1 handoff is preserved verbatim
> below, unedited.

---

# HANDOFF — V1 Step 2: Integration Controller

Branch: `task/v1-step2`
Draft PR: https://github.com/anderson930420/agent-taskflow/pull/196
Spec: `~/agent-taskflow-ops/v1/SPEC.md` §42 Step 2 (Integration Controller,
Re-integration, PR outcomes, Merge)
Instruction set: `~/agent-taskflow-ops/v1/step2.md`

Status: **implementation-complete, awaiting human re-review of PR #196.**
All three re-review rulings are implemented, Ruling 2 as amended by the
human decision on its stop. No stop condition is open. Nothing is approved,
merged to `main`, or finally complete.

`task/v1-step1` has been merged into this branch twice, both times as a merge
commit and never a rebase: first (`96a6cd3`) to bring in
`agent_taskflow/status_vocab.py` for the SPEC §12.2 ruling — see §3(a) — and
again (`9e09ad7`) for the reworked Step 1 persistence layer — see "Merge of
the reworked Step 1" below. This branch therefore contains Step 1 *and*
Step 2; PR #196 reviews Step 2.

---

## Review re-review rulings (B1, B2, guards) — all three implemented

The independent re-review of PR #196 failed it on blockers B1 and B2 plus
guard gaps. Three rulings followed, to be implemented in order, each only
after the previous one's tests were green.

| Ruling | State | Commit |
| --- | --- | --- |
| 1 — B1, every watcher tick scoped to its own repo | **done, tests green** | `9c0e425` |
| 2 — B2, deterministic post-resolution verification | **done** — stopped on check (a), then amended by human decision (Option 2) | `742822e`, amendment `d2e9d4d` |
| 3 — push allowlist, parsed merge guard, guard-test renames | **done, tests green** | `6f47160` |

The Ruling 1 and Ruling 2 commits were held locally while Ruling 2 was
stopped. They are published to PR #196 together with the amendment, Ruling 3
and this HANDOFF update, in one normal push.

### Stop condition hit — Ruling 2 check (a) contradicts git's behaviour — RESOLVED (Option 2)

Ruling 2 check (a): *"no rebase or merge in progress: no REBASE_HEAD, no
MERGE_HEAD (and no rebase-merge/ or rebase-apply/ directory)"*.

git 2.43.0 (this machine) leaves `REBASE_HEAD` behind after a rebase that
stopped on a conflict and was then **successfully** completed with
`git rebase --continue`. Observed directly:

    at the conflict:            REBASE_HEAD present, rebase-merge/ present
    after `rebase --continue`
      (exit 0, "Successfully
       rebased and updated
       refs/heads/task/...")    REBASE_HEAD present, rebase-merge/ gone

So check (a) as written flags every *correctly completed* rebase as still in
progress. Initial integration rebases (§24), so **every correct AI resolution
of an initial-integration conflict now stops at `needs_decision`.**
Re-integration merges (§26) and is unaffected: `MERGE_HEAD` is removed when
the merge commit is made.

**What I did:** implemented check (a) exactly as ruled — the fail-closed
reading, which never lets a half-finished rebase through and only also stops
finished ones — and did **not** edit any test around it. Six tests are red:

- `test_integration_controller.py::ConflictTests::test_a_resolved_conflict_adds_the_ai_hint` (pre-existing)
- `test_integration_controller.py::ConflictTests::test_a_resolved_conflict_continues_to_validators_and_pr` (pre-existing)
- `test_integration_controller.py::ConflictTests::test_a_resolved_conflict_with_red_validators_still_stops` (pre-existing)
- `test_integration_controller.py::ResolutionVerificationTests::test_check_b_a_stray_file_left_by_the_resolver_stops_integration` (new)
- `test_integration_controller.py::ResolutionVerificationTests::test_check_c_committed_conflict_markers_stop_integration` (new)
- `test_integration_git.py::NonInteractiveGitTests::test_a_conflicted_rebase_is_continued_without_an_editor` (new)

**All six fail for that single reason.** Proof: re-running exactly these six
with `in_progress_operation` patched *in memory only* — a `REBASE_HEAD` with no
`rebase-merge/` or `rebase-apply/` directory ignored — gives `6 passed`. No
file was changed by that diagnostic.

**Options — your decision:**

1. **Keep (a) as ruled.** Initial-integration conflicts then always go to a
   human; AI resolution never completes on that path, so it is effectively
   unreachable. The three pre-existing `ConflictTests` above would have to be
   rewritten to expect `needs_decision`.
2. **Amend (a):** a rebase is in progress when `rebase-merge/` or
   `rebase-apply/` exists; `REBASE_HEAD` counts only alongside one of them.
   A stopped rebase always has one of those directories — they are what git
   itself uses to track it — so no half-finished rebase can pass. All six go
   green with no other change.
3. **Have the control plane delete a stale `REBASE_HEAD`** before verifying.
   Not recommended: it edits the very state the check inspects, and it needs
   `update-ref`, which the git allowlist excludes on purpose.

My recommendation is **2**, but it changes the ruling's wording, so it is
yours to make.

**Resolution — human decision: Option 2** (`d2e9d4d`). Check (a) now reads: a
merge is in progress when `MERGE_HEAD` exists; a rebase is in progress when
`rebase-merge/` or `rebase-apply/` exists in the git dir; `REBASE_HEAD` counts
only alongside one of them. Checks (b)–(e) and the `GIT_EDITOR=true` fix are
unchanged.

- **The six tests above now pass with that change alone, and none of them
  was edited** — each one's source is byte-identical to `56a345b`
  (compared function by function).
- One unit test from the stop turn, `test_check_a_merge_head_or_rebase_head_fails_only_check_a`,
  asserted that `REBASE_HEAD` *alone* fails check (a) — exactly the
  semantics the ruling reverses. It is not one of the six. It was replaced
  by three: `test_check_a_merge_head_fails_only_check_a`,
  `test_check_a_a_leftover_rebase_head_alone_does_not_fail_check_a`, and
  `test_check_a_a_stopped_rebase_fails_check_a` (a real conflict, not a
  faked marker).
- **Required test added:** `test_a_stopped_rebase_still_fails_check_a_under_the_amended_rule`.
  A rebase stopped on a conflict and never continued — the resolver records
  that `rebase-merge/` existed when it returned — still fails check (a) and
  ends in `needs_decision`, with nothing pushed and no `integrated_base_sha`
  recorded.

### Found and fixed while implementing Ruling 2 — `rebase --continue` failed silently

Before this turn, `commit_conflict_resolution` ran `git rebase --continue`.
With no TTY that fails on the editor — *"There was a problem with the editor
'editor'"*, exit 1 — and leaves the rebase in progress; the return code was not
checked (both observed directly). The old post-resolution check looked only for
unmerged paths, so it passed, and the old flow went on to validate and push.
During a stopped rebase the task branch ref still points at the pre-rebase
commit, so by that flow even a *correct* AI resolution of an initial-integration
conflict would publish the unresolved branch and record an
`integrated_base_sha` the branch did not contain — the same outcome the
reviewer's stage-only reproduction showed for a stage-only resolver. B2 was
therefore real on the happy path too.

Ruling 2's check (a) caught it. Fix: the control-plane git runner now forces
`GIT_EDITOR=true` (accept the prepared message), the same way it already
forces `GIT_PAGER`. With the fix, `rebase --continue` exits 0. Regression test:
`test_a_conflicted_rebase_is_continued_without_an_editor` — itself one of the
six red tests, red only because of the stale `REBASE_HEAD` above.

### Ruling 1 — B1, repo-scoped watcher ticks: done

**Ruling.** Both ticks — PR-state and target-freshness — pick up exactly
`repo == tick repo AND pr_number IS NOT NULL AND pr_state = 'open'`, still not
scoped by display status (SPEC §32.0). The freshness tick queues only its own
repo's Tickets, into its own repo's queue.

**Implementation.**

- A PR's repository is read from its own §32.1 `pr_url`, which the controller
  records when it creates the PR. A PR number alone is not an identity, since
  the same number can be open in two repos. No new column was needed.
- The repository filter runs in SQL (`IntegrationStore.list_open_pr_states`),
  so another repository's rows are never loaded; each returned URL is then
  parsed and compared exactly, case-insensitively like GitHub.
- The freshness tick re-queues only `needs_review` (the §25.1 transition), into
  its own repository's queue. `paused`, `needs_decision` and
  `ready_for_integration` Tickets are picked up and reported but not re-queued.
  An `integrating` Ticket is deferred with no git work at all (§25.0).
- *My addition, same defect class, flagged for review:* the controller refuses
  to update a PR whose URL names a different repository than the request.

**Required test** — `tests/test_integration_watcher.py`, `CrossRepoScopeTests`:
two repositories with PR #42 open in both, checked in both directions for both
ticks (4 tests). Each asserts the other repository's Ticket row, PR state,
private state, events and review evidence are unchanged; that the tick issued
exactly one `gh` call (both Tickets are PR #42, so a call for the other Ticket
would have been a second one) and none through the other repository's
adapter; that nothing was queued into the other repository's queue; and that
the other repository was never even fetched.

**Tests added or renamed for Ruling 1:** `CrossRepoScopeTests` (4),
`OpenPrPickupScopeTests` in `test_integration_store.py` (4),
`CrossRepoGuardTests` in `test_integration_controller.py` (1). Freshness tests
updated because the pick-up is no longer status-scoped:
`test_only_needs_review_tickets_are_examined` → renamed
`test_only_needs_review_tickets_are_requeued`;
`test_a_paused_ticket_is_never_requeued_for_reintegration` → renamed
`test_a_stale_paused_ticket_is_picked_up_but_not_requeued`;
`test_freshness_polling_is_idempotent` and
`test_an_in_progress_integration_is_not_interrupted` now assert the Ticket is
picked up but not re-queued / deferred, instead of an empty result.

**Reviewer's cross-repo reproduction** (`/tmp/step2_review_crossrepo.py`),
re-run against this branch. It must be run with the worktree on `PYTHONPATH`:
the script only adds `tests/` to `sys.path`, so run plainly it imports the
*main* checkout, which has no Step 2 code at all.

    freshness tick(repo=owner/repoA) touched: [('AT-0901', 0, False)]
    queue owner/repoA: []
    queue owner/repoB: []
    gh argv: gh pr view 42 --repo owner/repoA
    AT-0901 status -> canceled | pr_state -> closed
    AT-0902 status -> waiting_for_review | pr_state -> open

Repo A's ticks touched only repo A's Ticket; one `gh` call; repo B's Ticket is
untouched. **B1 reproduced as fixed.**

### Ruling 2 — B2, post-resolution verification: implemented, stopped

After any AI conflict resolution and before validators, the control plane runs
five checks (`integration_git.verify_conflict_resolution`): (a) no operation in
progress, (b) clean worktree including untracked files, (c) no conflict
markers — `<<<<<<<`, `>>>>>>>`, `|||||||` in any tracked file, and a lone
`=======` only in files that conflicted, since it is also a valid setext
heading underline — (d) HEAD differs from HEAD captured right before the
resolver ran, (e) the target SHA resolved at the start of the run is an
ancestor of HEAD. Any failure → `needs_decision` via §27.2.1: the conflict
hunks and the AI's explanation are persisted as before, the failed checks are
persisted in a new Step-2-private column
(`integration_conflict_evidence.verification_json`, migration
`v1_step2_conflict_verification`) and in the `integration_blocked` event and
the result artifact; nothing is pushed and `integrated_base_sha` is not
recorded; no auto retry. A resolution that fails verification no longer counts
as a §39.3 AI success.

**Tests added for Ruling 2:** in `test_integration_git.py`,
`ResolutionVerificationTests` — all five checks passing, then one test per
check (a)–(e) in which only that check fails, plus the lone-separator scoping
test and the rebase-directory test; and `NonInteractiveGitTests` (the
regression test). In `test_integration_controller.py`,
`ResolutionVerificationTests` — the required *resolver that stages but does
not commit ends in needs_decision, not needs_review*, and one end-to-end test
per check. Plus one store test and one metrics test.

**Reviewer's stage-only reproduction** (`/tmp/step2_review_stageonly.py`),
re-run against this branch (worktree on `PYTHONPATH`, as above):

    result.status: needs_decision | ticket status: needs_decision
    recorded integrated_base_sha == latest target: False
    in-progress op left in worktree: None

Its last line then raises, because it runs `git rev-list` against
`origin/task/AT-0911`, which does not exist — nothing was pushed. Confirmed
with `git ls-remote`: the reproduction's origin has only `main`. **B2's
stage-only case reproduced as fixed.**

### Ruling 3 — guards: done (`6f47160`)

**Push allowlist.** `integration_git.assert_push_allowed` replaces the
force-push denylist. The only permitted push is `git push origin <task-branch>`
(optional `-u`), where `<task-branch>` is the Ticket's own branch and may not be
main, another protected branch, or the base branch. Everything else is refused:
`--force`, `-f`, `--force-with-lease`, `--force-with-lease=<ref>`,
`--force-if-includes`, `--mirror`, `--all`, `--tags`, `--delete`, any refspec
containing `:` (so `HEAD:<anything>`) or starting with `+`, combined short
flags such as `-vf` and `-uf`, `--set-upstream` (the ruling permits `-u`), any
other remote, any other branch, extra refspecs, and main or the base branch as
the target. `run_git` refuses any push that does not declare its task branch,
so nothing can push through the generic runner by accident. Non-push commands
keep the old force-flag rejection as defence in depth.

One literal consequence worth knowing: because the ruling names `origin`, an
integration request configured with any other remote name is now refused at
push time. Every current caller and fixture uses `origin`.

**Merge guard.** `github_pr_adapter.assert_not_a_merge_command` matches parsed
argv. It finds `gh` by basename whatever its path (`/usr/bin/gh`, `./gh`,
`env gh`), skips global flags before the subcommand together with their values
(`-R`, `--repo`, `--hostname`; the `--repo=o/r` form too), and rejects the
`pr merge` positional pair. Through the adapter it also still rejects the REST
merge endpoint via `gh api`, `git merge`, and a `git push` with a `:` refspec.

**Guard tests renamed to what they prove** (old → new):

- `test_integration_git.py`: `test_push_argv_never_contains_a_force_flag` →
  `test_built_push_argv_is_exactly_git_push_origin_task_branch`;
  `test_force_flags_are_rejected_at_the_guard` and
  `test_plus_refspec_force_form_is_rejected` →
  `test_the_push_allowlist_refuses_each_listed_form` (24 enumerated forms);
  `test_arbitrary_git_subcommands_are_not_reachable` →
  `test_run_git_refuses_reset_and_a_forced_push_to_main`. New:
  `test_the_allowlist_accepts_exactly_the_two_permitted_forms`,
  `test_the_task_branch_itself_may_not_be_main_protected_or_the_base_branch`,
  `test_run_git_refuses_a_push_that_declares_no_task_branch`.
- `test_github_pr_adapter.py`: `test_merge_subcommand_is_rejected_by_the_guard`
  → `test_merge_guard_rejects_each_listed_merge_argv`;
  `test_ordinary_pr_commands_pass_the_guard` →
  `test_merge_guard_allows_each_listed_non_merge_pr_command` (now includes
  `gh pr comment 42 --body merge`). New:
  `test_merge_guard_rejects_pr_merge_behind_each_listed_path_and_global_flag`
  (11 enumerated argv). `test_adapter_refuses_to_run_a_merge_argv` now also
  covers a pathed `gh` with `--repo`.
- `test_v1_step2_acceptance.py`:
  `test_git_and_gh_execution_is_confined_to_the_guarded_chokepoints` →
  `test_only_the_chokepoint_modules_import_subprocess`;
  `test_the_force_push_guard_rejects_every_force_form` →
  `test_push_allowlist_refuses_each_form_listed_in_ruling_3`;
  `test_every_step2_git_call_is_routed_through_run_git` →
  `test_run_git_refuses_each_listed_unallowlisted_subcommand`;
  `test_a_merge_argv_is_rejected_everywhere_it_could_be_built` →
  `test_gh_pr_merge_is_rejected_for_each_listed_merge_method_flag`, plus new
  `test_gh_pr_merge_is_rejected_behind_each_listed_path_and_global_flag`;
  `test_every_integration_uses_the_latest_available_target` →
  `test_initial_and_re_integration_both_use_the_latest_target`;
  `test_step2_never_touches_the_default_state_database` →
  `test_each_step2_request_type_accepts_an_explicit_db_path`.
- `test_integration_controller.py`: `test_reintegration_never_force_pushes` →
  `test_a_reintegration_pushes_only_the_allowlisted_form`;
  `test_no_git_command_is_ever_a_force_push` →
  `test_an_initial_integration_pushes_only_the_allowlisted_form` (both now
  assert the exact allowlisted argv); `test_no_git_command_pushes_the_target_branch`
  → `test_an_initial_integration_does_not_push_the_target_branch`;
  `test_no_gh_command_merges` → `test_an_initial_integration_issues_no_gh_merge`.
- `test_integration_cleanup.py`: class `NeverMergesTests` → `CleanupArgvTests`;
  `test_cleanup_never_invokes_a_merge_command` →
  `test_a_verified_merge_cleanup_runs_no_merge_argv`;
  `test_cleanup_never_force_pushes` →
  `test_a_verified_merge_cleanup_runs_no_push_at_all`;
  `test_remote_branch_cleanup_runs_only_when_requested` →
  `test_requesting_remote_branch_cleanup_is_refused_up_front_in_v1`. New:
  `test_the_remote_delete_form_is_refused_by_the_push_allowlist`.

Five remaining Step 2 test names contain "every":
`test_validator_evidence_records_every_spec_field`,
`test_evidence_persists_every_spec_29_field`,
`test_item_31_every_github_merge_method_is_supported`,
`test_reintegration_count_total_sums_every_ticket`,
`test_markdown_rendering_lists_every_hint`. Each enumerates what it names (the
§29 fields, the three GitHub merge methods, the fixture's tickets, the rendered
hints) and none is a guard test, so they were left as they are.

**Known consequence — decided, no longer an open stop condition.** Under the
allowlist, `git push <remote> --delete <branch>`, which Step 2's optional
remote-branch cleanup built, is refused. **Human decision: SPEC §37's
"optional remote branch cleanup" stays off in V1.** No exception was added to
the allowlist and `delete_remote_branch` stays `False`. Remote task branches
are left to GitHub's automatic head-branch deletion or to manual deletion.

In code: a cleanup request with `delete_remote_branch=True` is refused up
front, before anything is removed. Without that, the allowlist would have
raised mid-cleanup, after the worktree was already gone. The now-unreachable
remote-delete path was removed rather than left as dead code. Pinned by
`test_requesting_remote_branch_cleanup_is_refused_up_front_in_v1` and
`test_the_remote_delete_form_is_refused_by_the_push_allowlist`.

Out of Step 2's scope, noted for completeness: the legacy pre-V1 operator gate
`agent_taskflow/remote_branch_cleanup_confirm.py` still exists and can delete a
remote branch after explicit operator confirmation. It is one of the "manual
deletion" routes and was not touched.

`WORKFLOW.md` and `docs/v1-step2-integration-controller.md` were updated: both
still described the old denylist.

### Test counts

| | `pytest tests -q` |
| --- | --- |
| Before the re-review rulings (`a0f5728`) | `4767 passed, 8 skipped, 0 failed` |
| At the Ruling 2 stop (`742822e`) | `4786 passed, 6 failed, 8 skipped` |
| After the amendment and Ruling 3 (`6f47160`) | `4800 passed, 8 skipped, 0 failed` |

**No test is red.** The six tests that were red at the stop now pass, with
none of them edited. The count reconciles:

    4786  passed at the stop
      +6  the six previously red tests, now passing
      +8  tests added this turn
          (amendment: +3 — one check (a) unit test replaced by three, plus
           the required stopped-rebase test; Ruling 3: +5 — git push
           allowlist +2, adapter merge guard +1, acceptance +1, cleanup +1)
    ----
    4800  passed, 0 failed

At the Ruling 2 stop, the six failures were exactly the six listed under the
stop condition; nothing else in the repository was red. That count reconciled
as 4767 + 25 tests added (Ruling 1: 9, Ruling 2: 16) = 4792 = 4786 passed +
6 failed.

`agent_taskflow.cli.local_validation` after the amendment and Ruling 3: exit 0,
every required check passed — Python environment dependencies, workflow
contract validation, workflow policy validation, Mission Control golden path
smoke, PiExecutor golden path smoke (fake Pi), unit tests (`Ran 4806 tests`,
`OK (skipped=8)`), compileall. `openspec validate` skipped: `openspec` is not on
PATH (optional check, pre-existing). The `unittest` count is 33 above the last
one recorded (`Ran 4773` at `a0f5728`), matching the 25 tests added by Rulings
1–2 and the 8 added this turn.

`origin/main` advanced from `4266c02` to `975a3b0` while these rulings were
being implemented. Not by this branch — nothing in Step 2 pushes `main`. As
instructed, neither `main` nor `task/v1-step1` was merged in this turn.

---

## Merge of the reworked Step 1 (`dcad084`)

`task/v1-step1` was reworked after the first merge: the separate `tickets`
table is gone, Ticket rows now live in the legacy `tasks` table, and task keys
come from one global `AT-0001` counter. It was merged into this branch again
as merge commit `9e09ad7` (parents `76a0cfa` and `dcad084`) — a merge, not a
rebase, because this branch is published as draft PR #196 and §26 forbids
force-pushing a published PR branch. No other Step 2 rework was taken on.

**Conflicts: one file, resolved mechanically.**

- `agent_taskflow/store.py` — both branches added a named migration in the
  same three places (`SCHEMA_MIGRATIONS`, the migration functions, and
  `_MIGRATIONS`). Both were kept whole: Step 1's `tasks_ticket_fields` first,
  since it alters `tasks`, then Step 2's `v1_step2_integration_tables`, whose
  tables reference `tasks`. The middle hunk could not be resolved by pasting
  the two sides together: git folded the closing `"""` / `)` that both
  functions share into a single tail, so a naive union would have left Step
  2's migration unterminated with Step 1's code inside its SQL string. Each
  function got its own closing lines. Checked on a fresh DB: both migrations
  apply idempotently, Step 1's ticket columns are on `tasks`, Step 2's tables
  exist, and no separate `tickets` table is created. The only tests that
  inspect the migration list are set-based, so the order breaks nothing.
- `HANDOFF.md` auto-merged. Git applied Step 1's handoff edits to the appendix
  that carries Step 1's handoff, which is now byte-identical to
  `task/v1-step1`'s `HANDOFF.md` at `dcad084`.

**No semantic conflict with Step 2.** Step 1's `status_vocab.py` change only
adds a `persisted_statuses_for_display` helper; no display↔persisted mapping
moved, so Step 2's `integration_schema` status constants resolve exactly as
before. No Step 2 module or test imported the removed `ticket_schema` or the
Ticket store.

**Test counts, before and after the merge:**

| | `pytest tests -q` |
| --- | --- |
| Before the merge (`76a0cfa`) | `4748 passed, 8 skipped, 0 failed` |
| After the merge (`9e09ad7`) | `4767 passed, 8 skipped, 0 failed` |

The packaged `agent_taskflow.cli.local_validation` also passed after the
merge (exit 0, every required check): its `unittest` step went from
`Ran 4754` to `Ran 4773`, `OK (skipped=8)` — the same +19.

The +19 is exactly Step 1's rework: its five test files collect 133 tests now
against 114 at the previous merge (`764c9ff`), and it deleted no test file.
The before count is the full run on `76a0cfa` from the previous turn — the
identical commit this merge started from, with a clean tree.

---

## Human rulings applied on this branch

All three items previously left as "your call" have now been ruled on. Each is
implemented below; §3 marks the matching open item as resolved.

### Ruling 1 — §32 watcher pickup scope: code changed

**Ruling.** The PR-outcome watcher picks up Tickets by

    pr_number IS NOT NULL AND pr_state = 'open'

and is **not** scoped by status. A human can merge a PR on GitHub while its
Ticket sits in `needs_decision` or `paused`. A watcher scoped to `needs_review`
would never see that merge, so the Ticket would never reach `completed` and its
dependents would never be released. §32 says "all active PR Tickets", and this
is what that means.

**Required tests, all present** — `tests/test_integration_watcher.py`,
`PrPickupScopeTests`, whose docstring carries the rationale:

- a `needs_decision` Ticket whose open PR GitHub reports merged is picked up,
  verified, and reaches `completed`
- the same for a `paused` Ticket
- a Ticket with `pr_number` NULL is not picked up
- a Ticket whose `pr_state` is `closed` is not picked up

**What else had to change for that to work**, each with its own test:

- *Cleanup* completed a Ticket only from `needs_review`, so the two required
  completion tests could not pass without changing it. Which statuses a
  verified merge may complete now comes from the transition table via
  `integration_schema.can_transition`, which is now the runtime source of
  truth (before, only tests read it). `needs_decision`, `paused` and
  `ready_for_integration` can reach `completed` and `cancelled`;
  `integrating` still cannot.
- *In-flight integrations are picked up but deferred.* An `integrating` Ticket
  matches the condition, but nothing is polled, recorded or transitioned for
  it (§25.0), so the watcher never races the controller. Its `pr_state` stays
  `open`, so the next tick sees it again.
- *Changes requested* moves only `needs_review → needs_decision`, as §33.2
  describes. On a `paused` Ticket the review is still kept as retry context,
  but the pause stands (§13).
- *PR closed unmerged* cancels from any status with a `cancelled` edge (§33.5
  is unconditional) and removes the Ticket from the integration queue.
  Worktree, branch and evidence are retained, as before.
- *The controller* refuses to re-integrate a Ticket whose PR is already
  recorded as merged. The wider pickup makes that state reachable, and
  integrating it would only push to a dead branch.
- *Cleanup evidence* gets its own file per attempt (`cleanup-<id>.json`).
  Before, a refused re-run could overwrite the record of the cleanup that
  actually happened; the wider pickup makes re-runs more likely.

**Deliberately unchanged: target-freshness polling keeps its §25.1
`needs_review` scope.** The ruling governs PR-outcome pickup. Re-integration
is a lifecycle action: auto-re-integrating a `paused` Ticket would violate §13,
and a `needs_decision` one would bypass the human. `FreshnessScopeTests` pins
this.

**One acceptance assertion changed meaning.**
`test_item_29_polling_is_idempotent` expected a second poll to return the
merged PR again. Under the ruled condition a merged PR is recorded `closed`,
so it leaves the pickup set and a second poll returns nothing. The test now
asserts that, plus exactly one `merge_detected` event — a stronger idempotency
check than before.

**Flagged, not fixed — merged with no merge SHA.** If GitHub ever reports
`merged=true` with an empty `mergeCommit`, the first poll records
`pr_state=closed` with `merge_commit_sha` NULL. The Ticket then leaves the
pickup set and cleanup correctly refuses it (§36 needs the SHA), so it waits
for a human. I have not seen GitHub do this and the ruled condition is
explicit, so I have not widened the pickup. If it ever happens, the fix is to
keep such a Ticket in the pickup set until the SHA arrives.

### Ruling 2 — WORKFLOW.md Non-Goals: doc changed, no code

**Ruling.** There was no real conflict. The old Non-Goal said "automatic
merge", and Step 2 does automated integration up to a PR; the document failed
to distinguish the two. In one small, separate docs-only commit, the
Non-Goals section now states both:

- Automated integration up to a draft PR is **in scope**.
- Automated merge remains a **non-goal**: Taskflow never merges a pull request
  and never pushes the target branch.

The bare "automatic push" entry is narrowed to "automatic push of anything
other than a task branch". Left as it was, it would have contradicted the new
in-scope statement, since integrating means pushing the task branch.
"automatic cleanup/delete" is untouched: Step 2 cleanup always requires an
explicit operator confirmation.

### Ruling 3 — the two ambiguity flags: conservative defaults kept, no code

No default and no code changed. The reasoning is recorded here so it is not
lost; both are to be revisited after real usage.

- **`IntegrationCleanupRequest.delete_remote_branch = False`.** SPEC §37
  already calls remote branch cleanup optional, and GitHub's repository
  setting can auto-delete branches on merge. Taskflow does not need to do it.
- **`IntegrationRequest.push_no_op_reintegration = False`.** If a
  re-integration produced no new commit, there is nothing to push. Validators
  still re-run on every re-integration, no-op or not (§44).

---

## 1. What was implemented

### Extended (additive only — no existing module rewritten)

| Module | Change |
| --- | --- |
| `agent_taskflow/models.py` | Added the Step-2 lifecycle statuses, integration event types, and integration artifact types as new set members. No existing value changed or removed. |
| `agent_taskflow/store.py` | Added one named idempotent migration, `v1_step2_integration_tables`, registered in the existing `_MIGRATIONS` tuple. No existing method touched. |
| `WORKFLOW.md` | Added a descriptive Integration Controller boundary section. The Non-Goals list was **not** modified. |

### Created

| Module | Responsibility | Spec |
| --- | --- | --- |
| `integration_schema.py` | Authoritative §32.1 field list (names, types, enums, defaults), Step-2 statuses, allowed transitions | §32.1, §12 |
| `integration_store.py` | `task_pr_state` (public §32.1) + Step-2-private state, queue, lock, and evidence tables | §32.1 |
| `integration_git.py` | Allowlisted git ops: fetch, target resolution, `behind_count`, rebase, merge-target-into-branch, normal push, ancestry | §24, §25, §26, §36 |
| `github_pr_adapter.py` | `gh` adapter: create PR, update the same PR, poll state. Structurally cannot merge | §31, §32, §34 |
| `integration_validators.py` | Validator runner + §29 evidence (name, command, output, branch SHA, target SHA, diff context) | §29 |
| `integration_conflict_resolver.py` | Bounded AI resolver interface + conflict evidence; default resolver resolves nothing | §27, §27.2.1 |
| `integration_queue.py` | Per-repo FIFO queue and Loose Integration Lock | §22, §22.1, §23.1 |
| `reviewer_hints.py` | Hint generation and the §26.1 re-integration block | §26.1, §38 |
| `integration_controller.py` | The integration pipeline (initial + re-integration) under the loose lock | §23, §24, §26 |
| `merge_verification.py` | `merge_commit_sha` containment in the latest target history | §35, §36, §36.1 |
| `integration_watcher.py` | Target-freshness and PR-outcome polling, both idempotent | §25, §25.0, §32, §33, §35 |
| `integration_cleanup.py` | Cleanup gated on verified merge + the explicit closed-unmerged path | §37, §37.1 |
| `integration_metrics.py` | Optimistic-execution, re-integration, and AI-resolver metrics | §39 |

`docs/v1-step2-integration-controller.md` holds the read-only inventory and the
module map.

### Key design decisions worth reviewing

**`needs_review` is set after the lock is released.** §23.1 orders it that way,
so the controller releases the repo lock and only then transitions the Ticket.
This makes "the integration lock does not wait for human review" (§44)
structurally true rather than merely intended, and is what lets several
same-repo PRs sit in `needs_review` at once (§43.17).

**Field ownership is enforced by schema, not convention.** `task_pr_state` has
exactly the twelve §32.1 columns plus `task_key`; the column list is generated
from `integration_schema.TICKET_PR_FIELDS`, and a test pins the two together so
they cannot drift. `update_pr_state` rejects any field outside the §32.1 set and
any invalid enum value. Integration-internal state lives in a separate private
table.

**An empty validator set does not pass, and a missing validator binary fails.**
A gate that gates nothing, or that silently disappears when a tool is absent,
is not a gate. Both are tested.

**Git and gh execution is confined to two chokepoints.** Every git argv goes
through `integration_git.run_git`, which applies a subcommand allowlist (no
`reset`, `checkout`, `clean`, `update-ref`, …), a force-push denylist covering
the flag and `+refspec` forms, and a protected/target-branch push check. Every
gh argv goes through `GitHubPrAdapter.run`, which rejects merge argv. A test
asserts no other Step 2 module even imports `subprocess`, so a bypass would be
visible rather than possible.

**Cleanup uses `git branch -D`, not `-d`.** After a squash or rebase merge the
task branch is not an ancestor of the target, so `-d` would refuse even though
§36 verification has already proved the work landed. The safety gate is the
merge verification, not git's ancestry heuristic.

---

## 2. What was deliberately skipped, and why

1. **`needs_decision → ready` retry, and new Attempt creation.**
   §42's Step 2 checklist lists "Retry → new Attempt", but step2.md's *Allowed
   layers* list of integration-owned transitions does not include `→ ready`,
   and Attempt/lease/claim is explicitly a Step 4–5 forbidden layer. Step 2
   therefore **persists the retry context** (§43.24: reviewer identity,
   timestamp, comments, reviewed head SHA, PR URL, in
   `integration_review_evidence`) and stops there. The transition itself is
   left for whoever owns Attempts. `integration_schema.INTEGRATION_TRANSITIONS`
   deliberately has no `needs_decision → needs_review` edge, so nothing here can
   self-approve a Ticket out of `needs_decision`.

2. **§39.1 `late_dependency_rate` is reported as `0.0`.**
   It depends on the runtime-discovered dependency signal (§5.2), which Step 2
   does not own. It is exposed with its supporting counts rather than guessed
   at. `upstream_rework_rate` is currently derived from the re-integration
   signal, which is the only upstream-rework evidence Step 2 actually has.

3. **The "files overlap with recently merged Ticket" hint is built but not
   populated.** `reviewer_hints.build_reviewer_hints` accepts and renders
   `overlapping_files`, and it is tested, but the controller passes nothing:
   computing cross-Ticket file overlap needs merged-Ticket file history that
   Step 2 does not hold. The hook is ready for whoever adds it.

4. **No `scripts/` CLI wrappers.** step2.md's Allowed layers do not list a CLI,
   and the module entry points are directly callable. If operators want the
   usual `scripts/*.py` dry-run/confirm wrappers that the rest of this repo
   uses, that is a small, separate follow-up.

5. **No scheduler wiring, Mission Control rendering, or SSE.** Steps 3–5.
   Step 2 persists state; it does not render it and does not schedule itself.

---

## 3. Stop conditions

step2.md defines three. Here is what actually happened with each.

### "Any pre-existing test goes red" — NOT HIT

A full baseline was captured **before any edit**: `4390 passed, 8 skipped,
0 failed`. The post-change result is in §5 below. No pre-existing test was
modified, weakened, or skipped.

### "A spec requirement is ambiguous or contradicts existing code" — 3 flagged

These were reported rather than repaired, per the stop-condition rule. None of
them blocked delivery. **(a) and (b) have since been ruled on and
implemented**; (c) is still open.

**(a) `cancelled` vs `canceled` — spec/code spelling conflict. RESOLVED.**

*Original report:* SPEC §12 spells the cancelled state with two `l`s;
`models.py` already had a legacy `canceled`. Step 2 initially added `cancelled`
as a separate, non-aliased status alongside it, and flagged the two-spellings
wart for a decision.

*Human ruling (SPEC §12.2):* no repo-wide migration. Legacy `TASK_STATUSES`
stays the canonical **persisted** vocabulary; the §12 names are the Mission
Control **display** vocabulary; `agent_taskflow/status_vocab.py` is the single
bridge. Persisted spelling of cancelled is `canceled`.

*What was done:*

1. `task/v1-step1` was **merged** (not rebased) into this branch to bring in
   `status_vocab.py`. `task/v1-step2` is published as draft PR #196 and §26
   forbids force-pushing a published PR branch, so a merge commit is the
   accepted cost. Two conflicts, both resolved mechanically:
   `models.py` (took Step 1's additive §12.2 status block) and `HANDOFF.md`
   (add/add — both handoffs combined, Step 1's preserved verbatim in an
   appendix).
2. `cancelled` was **removed** from `TASK_STATUSES`. So was `needs_review`:
   it has the same problem, since `waiting_for_review` is its legacy persisted
   spelling. Neither now coexists with its legacy sibling.
3. `integration_schema` no longer hard-codes any §12 name. Its six constants
   keep their §12 display names but hold the **persisted** spelling, resolved
   at import through `status_vocab.to_persisted_status`:

   | §12 display name | persisted value |
   | --- | --- |
   | `ready_for_integration` | `ready_for_integration` |
   | `integrating` | `integrating` |
   | `needs_decision` | `needs_decision` |
   | `needs_review` | **`waiting_for_review`** |
   | `cancelled` | **`canceled`** |
   | `completed` | **`cleaned`** |

   Four of six are identity, two are not, plus `completed`. Every task-status
   read and write in Step 2 already went through these constants, so the
   modules needed no other change; the tests were converted from status
   literals to the constants so they do not re-duplicate the mapping either.
   `tests/test_integration_schema.py` now pins the bridge in both directions
   and asserts neither spelling pair coexists in the enum.

**Watcher scope — superseded by Ruling 1.** This paragraph originally flagged
that the watcher selected Tickets by status. The §32 pickup ruling replaced
status-based selection with `pr_number IS NOT NULL AND pr_state = 'open'`
for PR-outcome polling, so the question no longer arises there. Legacy
`waiting_approval` tasks are still not adopted: they have no Step 2 PR row,
so they can never match the pickup condition.

**(b) `WORKFLOW.md` Non-Goals vs Step 2's mandate. RESOLVED — see Ruling 2
above.**
The repo-owned contract lists "automatic push" and "automatic cleanup/delete"
as things agent-taskflow should not provide. V1 Step 2 requires exactly those.
I did not edit the Non-Goals list. Instead every Step 2 entry point is
**dry-run by default and requires an explicit confirmation flag**, and nothing
runs itself — which keeps WORKFLOW.md's actual guarantee (no background
mutation) intact while implementing the spec's machinery. **Your call:** V1
supersedes that Non-Goals line, or Step 2 stays operator-triggered forever.

**(c) §42 checklist vs step2.md Allowed layers on "Retry → new Attempt".**
Described in §2.1 above. I followed step2.md, which overrides.

### "You need a decision the spec assigns to the human" — 2 parameterised, not guessed — RESOLVED, see Ruling 3 above

These are the two items on step2.md's own **ambiguity watchlist**. Both are
exposed as explicit flags with a conservative default, and neither is baked in.

**(a) Does a no-op re-integration still push and re-run validators?**
The validator half is settled by §44 ("every re-integration reruns
validators"), so validators re-run unconditionally — including on a no-op —
and that is tested. The **push** half is genuinely unspecified. Flag:
`IntegrationRequest.push_no_op_reintegration`, default `False` (do not push a
branch with nothing new on it). Both behaviours are tested.

**(b) Is "optional remote branch cleanup" (§37) on or off by default?**
Flag: `IntegrationCleanupRequest.delete_remote_branch`, default `False`.
Deleting a remote branch is the least reversible action in this step, so off is
the conservative reading of "optional". Both behaviours are tested.

Also untouched, as assigned to you by the spec: the **concurrency gate** (§19 —
`max_concurrent_tasks` is not changed), **merging** (§34 — Taskflow never
merges), and **cleanup of unmerged work** (§37.1 — requires your explicit flag).

---

## 4. Acceptance gate

`tests/test_v1_step2_acceptance.py` maps onto the spec directly: every §43 item
in scope, every §44 invariant Step 2 can affect, and the required negative-scope
tests. 225 Step 2 tests total across 14 files.

The §43 end-to-end journey (items 12–14, 16, 18–22, 28–33, 35) runs as one
continuous lifecycle in `test_full_lifecycle_from_queue_to_completed`: queue →
integrate against latest target → PR → target advances → stale detected →
re-integrate same PR without force push → human merge → poll → verify → cleanup
→ `completed`.

Negative-scope tests, as required:

- no code path can force-push (guard rejects every flag and `+refspec` form;
  the controller's recorded argv are asserted force-free; git/gh execution is
  confined to two guarded chokepoints)
- no code path can merge (gh merge argv rejected; target-branch push refused;
  the test double raises if `gh pr merge` is ever invoked)
- no worktree removal on the cancelled-unmerged route without the explicit
  confirmation flag

Tests use **real throwaway git repositories**, not a faked git runner: rebase,
merge, `behind_count` and ancestry containment are the behaviours under test,
and a fake would only prove the fake agrees with itself. GitHub is faked,
because it is a remote service. The fixture reproduces all three GitHub merge
methods (§36.1), including that squash and rebase merges produce SHAs absent
from the task branch.

---

## 5. Exact commands to verify

Run from `/home/ubuntu/agent-taskflow/.worktrees/v1-step2`.
Use the venv interpreter — the system `python3` has no `pydantic`/`fastapi`.

```bash
VENV=/home/ubuntu/agent-taskflow/.venv/bin/python

# Step 2 acceptance gate on its own (fast — ~30s)
$VENV -m pytest tests/test_v1_step2_acceptance.py -v

# All Step 2 tests
$VENV -m pytest \
  tests/test_integration_schema.py tests/test_integration_store.py \
  tests/test_integration_queue.py tests/test_integration_git.py \
  tests/test_github_pr_adapter.py tests/test_integration_validators.py \
  tests/test_integration_conflict_resolver.py tests/test_reviewer_hints.py \
  tests/test_integration_controller.py tests/test_merge_verification.py \
  tests/test_integration_watcher.py tests/test_integration_cleanup.py \
  tests/test_integration_metrics.py tests/test_v1_step2_acceptance.py -q

# Full suite (the repo's canonical validator)
$VENV -m unittest discover -s tests -v

# Byte-compile check
$VENV -m compileall agent_taskflow scripts tests

# The repo's packaged local validation sequence
$VENV -m agent_taskflow.cli.local_validation
```

### Results observed on this branch

| Command | Result |
| --- | --- |
| Baseline `pytest tests -q` (captured **before any edit**) | `4390 passed, 8 skipped, 0 failed` |
| `pytest tests -q` (Step 2, before the Step 1 merge) | `4615 passed, 8 skipped, 0 failed` in 509s |
| `pytest tests -q` (after the Step 1 merge + §12.2 rework) | `4731 passed, 8 skipped, 0 failed` in 605s |
| `pytest tests -q` (after Rulings 1–3) | `4748 passed, 8 skipped, 0 failed` in 522s |
| `pytest tests -q` (after the reworked Step 1 merge) | `4767 passed, 8 skipped, 0 failed` in 697s |
| `compileall agent_taskflow scripts tests` | exit 0 |
| Step 2 tests only | 244 passed across 14 files |
| Step 1 tests merged in | 133 passed across 5 files (114 before the Step 1 rework) |

The counts reconcile exactly, which is the point of listing them:

    4390  baseline
    +225  Step 2 tests
    ----
    4615  Step 2 branch before the merge
    +114  Step 1 tests arriving with the merge
      +2  net new schema tests from the §12.2 rework
          (3 added, 1 removed: the old two-`l` spelling assertion)
    ----
    4731  after the merge
     +17  Ruling 1 tests (9 pickup-scope, 1 freshness-scope,
          3 cleanup, 1 controller, 3 schema)
    ----
    4748  after the rulings
     +19  Step 1 rework (its 5 test files: 114 -> 133 tests)
    ----
    4767  after the reworked Step 1 merge

The skip count is 8 throughout — the same 8 pre-existing skips. No test was
lost, silently skipped, or newly red at any point.

A second full `pytest tests -q` run after the merge, in the foreground, gave
the identical result: `4731 passed, 8 skipped, 0 failed` in 523s.

`agent_taskflow.cli.local_validation`, run after the reworked Step 1 merge
(exit 0) — every required check passed:

| Check | Result |
| --- | --- |
| Python environment dependencies | passed |
| workflow contract validation | passed |
| workflow policy validation | passed |
| Mission Control golden path smoke | passed |
| PiExecutor golden path smoke (fake Pi) | passed |
| unit tests (`unittest discover -s tests -v`) | passed — `Ran 4773 tests`, `OK (skipped=8)`, 617s |
| compileall | passed |
| openspec validate | skipped — `openspec` is not on PATH (optional check, pre-existing) |

`unittest` reports 4773 and `pytest` reports 4767 + 8 skipped because the two
runners collect tests differently; both report **zero failures**, and both
report the same 8 skips as the pre-edit baseline.

**No test went red at any point** — not after Step 2, not after the Step 1
merge, not after the §12.2 rework, not after Rulings 1–3, and not after the
reworked Step 1 merge. There was no pre-existing failure to
report.

The merged Mission Control frontend is byte-identical to `task/v1-step1`'s
(`git diff origin/task/v1-step1 HEAD -- mission-control/` is empty; Step 2
changes no frontend code), so Step 1's own frontend build validation carries
over unchanged.

---

## 6. Safety statement

- No push to `main`, no merge, no force-push. The branch pushed is
  `task/v1-step2` only, and its PR is a **draft**.
- `~/.agent-taskflow/state*` was never read or written. Every test passes an
  explicit `db_path` into a temp directory; a test asserts each Step 2 request
  type accepts one. `state.db` mtime is unchanged.
- No scheduler tick or scheduler entry point was run.
- No worktree, branch, or artifact outside this worktree's temp test
  directories was removed.
- No existing test, validator, governance check, or safety policy was weakened.

Human review remains the final gate. This work is not approved and not complete.

---

# Appendix — V1 Step 1 handoff (merged in, preserved verbatim)

The section below was written by the Step 1 builder on `task/v1-step1` and is
reproduced unchanged. It is included here because that branch was merged into
`task/v1-step2`; it is not part of the Step 2 review.

# Handoff — V1 Step 1: Minimal Ticket UX

Branch: `task/v1-step1`
Draft PR: https://github.com/anderson930420/agent-taskflow/pull/195
Worktree: `/home/ubuntu/agent-taskflow/.worktrees/v1-step1`
Spec: `~/agent-taskflow-ops/v1/SPEC.md` §42 Step 1, §12.2
Instructions: `~/agent-taskflow-ops/v1/step1.md`

Status: **implementation-complete, awaiting human review.** Not approved, not
merged. The PR is a draft.

---

## 0. Human rulings applied on this branch

Newest first. Each was implemented as ruled; nothing here re-argues them.

1. **PR #195 review ruling.** The review verdict was accepted. Root cause:
   Step 1 had created a separate `tickets` table, but the legacy `tasks` table
   is the **only** canonical Ticket entity. Applied:
   - the `tickets` and `ticket_events` tables and their migration are gone;
   - Step 1's columns now live on `tasks`, and POST `/api/tickets` inserts
     into `tasks`;
   - task keys come from a **single global counter**, zero-padded to 4 digits
     (`AT-0001`). The per-prefix counter is gone;
   - the Create Ticket link in `TaskBoard.tsx` is reverted, since the Board is
     Step 3 / §16 territory;
   - §6 below is rewritten, and the handoff's earlier false claims about
     `models.py` are corrected (see §1).
2. **§12.2 status-vocabulary ruling.** `TASK_STATUSES` stays the persisted
   vocabulary, the §12 names are the display vocabulary, and
   `agent_taskflow/status_vocab.py` bridges them. See §4.

---

## 1. Inventory, corrected

**Corrections to earlier versions of this handoff.** Two statements were
false, and so was the stop-conditions section (rewritten in §6):

- The inventory table listed `agent_taskflow/models.py` as "reuse,
  unchanged". The branch modifies it — the §12.2 commit adds five values to
  `TASK_STATUSES`.
- A sentence claimed "`models.py` / `store.py` / `schemas.py` and the legacy
  `tasks` mirror are untouched". `models.py` is modified, and after the PR #195
  ruling so are `store.py` and the `tasks` table. `schemas.py` is untouched.

Existing modules, and what this branch does to each:

| Module | Action | What |
| --- | --- | --- |
| `agent_taskflow/models.py` | **modified, additive** | 5 values added to `TASK_STATUSES` (§12.2 ruling) |
| `agent_taskflow/store.py` | **modified, additive** | new `tasks_ticket_fields` migration: 10 nullable columns on `tasks` and 2 partial unique indexes |
| `agent_taskflow/api/main.py` | **extended** | import router, construct `TicketStore`, `include_router` |
| `mission-control/components/TaskBoard.tsx` | **reverted** | byte-identical to base `4266c02` |
| `projects.py`, `config.py`, `worktree.py`, `artifacts.py`, `tasks.py`, `_helpers.py`, `api/schemas.py`, `mission-control/lib/api.ts` | reused, unchanged | |

No existing column, table, index or row is altered or removed.

New modules: `ticket_models.py`, `ticket_repositories.py`,
`ticket_metadata.py`, `ticket_ai_metadata.py`, `ticket_store.py`,
`ticket_creation.py`, `api/tickets.py`, `status_vocab.py`, plus the Mission
Control form, the detail page and `lib/tickets.ts`.

Removed: `agent_taskflow/ticket_schema.py` — the `tickets` table and its
`v1_ticket_creation_v1` migration.

---

## 2. What is implemented

**A Ticket is a `tasks` row.** Prompt-first creation inserts a normal `tasks`
row (`project`, `board`, `title`, `status`, `repo_path`, `artifact_dir`,
timestamps) and fills the Step 1 columns the `tasks_ticket_fields` migration
adds: `prompt`, `priority`, `ai_title_status`, `branch_slug_source`,
`blocked_by`, `github_repo`, `base_branch`, `branch`, `worktree_path`,
`commit_message_suggestion`. Legacy rows leave them NULL. The creation audit
event is a `created` row in the existing `task_events` log, with payload
`kind: ticket_created`.

Key allocation, the `tasks` insert and the audit write share one
`BEGIN IMMEDIATE` transaction.

**Task keys — one global counter, `AT-0001`.** I searched for an existing
generator before writing one, and **there isn't one**:
`scripts/kanban_create.py` takes `--task-key` from the user and only validates
it, and `github_issue_intake` derives `AT-GH-<issue>` from the issue number.
The new counter follows the `AT-0001` convention those scripts' help text
already uses. It counts every `AT-<digits>` key already in `tasks`, legacy
ones included, so a Ticket can never reuse a key — or the
`.worktrees/<key>` path — that an existing task owns. Other shapes
(`AT-GH-188`, `AT-MC-SMOKE`) are ignored.

**Status.** §12.1's display status is persisted through `status_vocab`:
`ready` → `created`, `blocked` → `blocked`. `queued` is never written.

**`ai_title_status`** is one of `generated` (the AI title was used),
`fallback` (AI was attempted and failed, so the §10.1 rule applied), or
`not_attempted` (no adapter configured, so the §10.1 rule applied).

**Visibility.** Because a Ticket is a task, it appears in `/api/tasks` and on
the existing `/tasks/<key>` page. `/api/tickets/<key>` returns the richer
Ticket view for prompt-first rows only; legacy rows return 404 there and stay
on `/api/tasks`.

**Legacy upsert safety.** `TaskMirrorStore.upsert_task` names its columns
explicitly in `ON CONFLICT DO UPDATE SET`, so a later mirror re-sync of the
same key cannot clobber the ticket-only columns. Shared legacy columns
(`title`, `artifact_dir`, `status`, …) follow the existing upsert policy
unchanged. A test pins that boundary.

**Structural invariants.** `ux_tasks_worktree_path` (unique `worktree_path`)
and `ux_tasks_repo_branch` (unique `(repo_path, branch)`) are partial indexes
over non-NULL values. `One Ticket = One Worktree` is enforced by storage.

Step 1 checklist (§42): repo dropdown, prompt-first Ticket, priority, auto
task key, AI title with deterministic fallback, auto branch, auto worktree
path (string only), and a basic detail page (`/tickets/<task_key>`).

---

## 3. Acceptance gate

133 tests across five files.

| Gate item | Test |
| --- | --- |
| §43.1 create from repo/prompt/priority | `test_ticket_creation.MinimalCreationInputTests` |
| §43.2 metadata generated automatically | `DerivedMetadataTests` |
| §43.3 AI title failure cannot block creation | `AiTitleFallbackTests` — raise, hang past deadline, `TimeoutError`, empty, whitespace, `None` |
| §43.4 unique worktree/branch derivation | `OneTicketOneWorktreeTests`, `test_ticket_store.AllocationTests` |
| §44 One Ticket = One Worktree | storage-level uniqueness tests in `AllocationTests` |
| §44 all lifecycle mutations auditable | `AuditabilityTests`, `test_ticket_store.AuditTests` |
| §12.1 / §12.2 initial status | `InitialStatusTests` |
| negative scope: no Git command | `NegativeScopeTests.test_creation_runs_no_subprocess` |
| negative scope: no directory created | `NegativeScopeTests.test_creation_creates_no_directory` |
| ruling: `tasks` is the only entity | `test_ticket_store.CanonicalEntityTests`, `test_api_tickets.TicketsAreTasksTests` |
| ruling: global `AT-0001` counter | `test_ticket_metadata.TaskKeyTests`, `AllocationTests` |

Per file: `test_ticket_metadata` 22, `test_ticket_store` 25,
`test_ticket_creation` 38, `test_api_tickets` 19, `test_status_vocab` 29.

One guarantee was lost with the table. The old `ticket_events` table had
append-only triggers, and its test is gone along with it. `task_events` is a
legacy table with no such triggers. Adding them would change legacy behaviour
and is outside this change — see §10.

---

## 4. Status vocabulary bridge (SPEC §12.2 ruling)

Added after the human ruling on the §12-vs-`TASK_STATUSES` conflict that all
three Step builders reported.

**Ruling as implemented.** No repo-wide migration. `TASK_STATUSES` stays the
canonical *persisted* vocabulary. The §12 names are the Mission Control
*display* vocabulary. `agent_taskflow/status_vocab.py` is the single bridge.

Since the PR #195 ruling, Tickets are `tasks` rows, so they use the bridge
directly: creation persists §12.1's `ready` as `created` and `blocked` as
`blocked`, and the API returns both `status` (persisted) and `display_status`
(§12). `persisted_statuses_for_display()` was added so that a display-name
filter matches every alias — a `needs_review` filter must also match
`waiting_approval` and `accepted` rows.

### Shape

`DISPLAY_TO_PERSISTED` is **injective** — 14 §12 names, 14 distinct persisted
values — so every display name round-trips exactly.

`PERSISTED_TO_DISPLAY` is **total but not injective**. The legacy vocabulary is
larger (25 values after the additive change) and carries several spellings of
the same idea. Those extra spellings are declared in `PERSISTED_ALIASES` and
round-trip to their canonical sibling, not to themselves. `canonical_persisted_status()`
performs that collapse and is idempotent.

Fixed by the ruling: `ready→created`, `running→implementing`,
`needs_review→waiting_for_review`, `completed→cleaned`, `cancelled→canceled`.

### Additive change to `TASK_STATUSES`

Added: `paused`, `needs_decision`, `ready_for_integration`, `integrating`,
`failed`. `blocked` was on the ruling's list but **already existed**, so it is
untouched and mapped as identity.

Nothing was removed, renamed or repurposed; a test pins the pre-ruling set of
20 values as a subset and asserts the delta is exactly those five. `TASK_STATUSES`
is consumed only by `validate_task_status`; `lifecycle_control` keeps its own
independent transition graph, so widening the enum does not widen any
lifecycle gate.

### The two values the ruling asked me to derive from the code

**`waiting_approval` → `needs_review`.** Written by `dispatcher.py` once the
executor *and* the validators have passed
("waiting for human approval"). It is then *required* by `pr_handoff`,
`pr_preparation_pipeline`, `branch_push_confirm`, `draft_pr_confirm`,
`post_merge_cleanup_recommendation` and `task_closeout_confirm` before any of
them will act. So it is this repo's human review gate — validated work, parked
for a human — which is §12 `needs_review`.

Note what it is *not*: it is not `ready_for_integration`. The instruction
states `ready_for_integration` has no legacy equivalent, and the code agrees —
`waiting_approval` is a human gate, not a queue position.

**`accepted` → `needs_review`.** Written by the API approve route after
`record_approval_decision(..., "accepted")`, only from `waiting_approval`. Per
`WORKFLOW.md`, approval implies no merge, no push and no cleanup; the
scheduler watcher preview treats it as no-further-action. SPEC §33.1 is
explicit that an approved-but-unmerged Ticket **stays** `needs_review`. So
`accepted` displays as `needs_review` rather than as `completed`.

### Every other legacy value, and why

| Persisted | Display | Kind | Reasoning |
| --- | --- | --- | --- |
| `queued` | `queued` | canonical | Same idea in both vocabularies. |
| `created` | `ready` | canonical | Ruling. |
| `preparing` | `preparing` | canonical | Identity. |
| `implementing` | `running` | canonical | Ruling. |
| `validating` | `validating` | canonical | Identity. |
| `blocked` | `blocked` | canonical | Already existed; identity. |
| `waiting_for_review` | `needs_review` | canonical | Ruling. |
| `cleaned` | `completed` | canonical | Ruling. |
| `canceled` | `cancelled` | canonical | Ruling; persisted keeps the legacy single-l spelling. |
| `paused` / `needs_decision` / `ready_for_integration` / `integrating` / `failed` | same | canonical | Newly added; identity. |
| `waiting_approval` | `needs_review` | alias | See above. |
| `accepted` | `needs_review` | alias | See above. |
| `rejected` | `needs_decision` | alias | Human said no; someone must now choose retry / cancel / rework. §33.2 routes exactly that to `needs_decision`. |
| `unknown` | `needs_decision` | alias | **No clean §12 equivalent.** A mirror value meaning the local state is not trustworthy. Mapped to `needs_decision` so it routes to a human instead of implying progress it cannot justify. Mapping it to `queued` or `ready` would have been a quiet lie. |
| `completed` | `completed` | alias | **No clean §12 equivalent, because the ruling gave the `completed` display name to `cleaned`.** In this repo legacy `completed` is the task-closeout terminal (`task_closeout_confirm.DEFAULT_TARGET_STATUS`), while cleanup is a separate later phase. §12 has no "done but not yet cleaned up" state, so both collapse to `completed`, canonicalizing on `cleaned`. |
| `archived` | `cancelled` | alias | Operator-confirmed evidence-only / superseded terminal. Work abandoned, evidence retained — which is §12 `cancelled` (§33.5, §37.1). |
| `backlog` | `queued` | alias | External Kanban mirror; not yet admitted. |
| `todo` | `ready` | alias | External Kanban mirror; admitted, not started. |
| `in_progress` | `running` | alias | External Kanban mirror. |
| `review` | `needs_review` | alias | External Kanban mirror. |
| `done` | `completed` | alias | External Kanban mirror; terminal success. |

**One tension worth the reviewer's attention.** The instruction says
`needs_decision` has "no legacy equivalent", and I still map two legacy values
(`rejected`, `unknown`) onto it as display aliases. I read that instruction as
governing which names had to be *added to `TASK_STATUSES`* — it is already
loose in the same way for `blocked`, which existed. Nothing is repurposed:
`needs_decision`'s canonical persisted value is the newly added
`needs_decision`. If the intent was that no legacy value may display as one of
those six names, say so and I will change `rejected` and `unknown`. There is no
better §12 name for either.

### Tests

`tests/test_status_vocab.py`, 29 tests (25 original, plus 4 for the
display-filter helper). Every §12 name round-trips exactly;
every one of the 25 `TASK_STATUSES` values maps to a valid display name and
canonicalizes into a real persisted value; canonicalization is idempotent; the
alias table is asserted as an exact dict; aliases and canonicals partition the
persisted vocabulary with no overlap and no gap; `unmapped_persisted_statuses()`
is empty, so a future addition to `TASK_STATUSES` that forgets this module
fails the suite instead of raising a `KeyError` in Mission Control.

---

## 5. Deliberately skipped, and why

Each is a **forbidden layer** in `step1.md`:

- Scheduler, eligibility, atomic claim, lease, Attempt, capacity (§19, §20).
- Executor runtime and any AI implementation agent.
- **All Git mutation.** No worktree, no branch, no fetch/rebase/merge/push.
  Step 1 records strings.
- Integration controller, per-repo queue, per-repo lock (§22, §23).
- Validators (§29).
- GitHub adapter — the §32.1 PR fields are not added. §32.1 assigns them to
  the Step 2 watcher.
- Cleanup (§37), SSE / live progress (§15), metrics (§39).
- `blocked_by` **mutation** endpoints and cycle validation (Step 5).
- The Mission Control Board (§16, Step 3). The nav link I had added is
  reverted.

The detail page shows derived metadata and the audit trail, and states plainly
that no execution has run. It renders no step list and no progress figure,
because §14.2 forbids inventing one.

---

## 6. Stop conditions

**Earlier versions of this section said "None fired". That was false.** Two
stop conditions fired, and I did not stop on either one when it did.

1. **The §12 vs `TASK_STATUSES` vocabulary conflict.** §12's status model
   contradicts the persisted vocabulary already in the code. That is the
   step1.md stop condition "a spec requirement is ambiguous or contradicts
   existing code". Instead of stopping, I sidestepped it by building a
   separate `tickets` table with its own §12 enum. That table is the root
   cause the PR #195 review identified. All three Step builders reported the
   conflict, and it took a human ruling (SPEC §12.2) to resolve.
2. **The Task ID format ambiguity.** step1.md listed it explicitly as "flag,
   do not guess". I guessed — `<prefix>-<NNN>`, a per-prefix counter starting
   at 1 — and flagged it only after the fact. It took a human ruling (a single
   global counter, `AT-0001`) to settle. That ruling is now what is
   implemented.

**One further contradiction, found while applying the PR #195 ruling. Not
repaired.** Tickets are `tasks` rows now, so the legacy task action routes can
reach them. §44 says "Blocked Ticket cannot execute", but the existing
dispatcher disagrees:

- verified: `dispatcher.RUNNABLE_STATUSES` is `{queued, blocked, preparing}`.
  A Ticket created with `blocked_by` is persisted as `blocked`, so it passes
  the dispatcher's status gate.
- verified: a fresh `created` Ticket is not runnable. But a non-dry-run
  `POST /api/tasks/<key>/start` calls `_block_task`, which flips the row to
  `blocked` ("Task status is not runnable: created"), and after that it *is*
  runnable. Legacy `blocked` means "retry after failure", while §12 `blocked`
  means "waiting on a dependency". The §12.2 ruling maps the two to the same
  persisted value.
- **not verified:** the `/start` route calls `level2_direct_execution_error`
  before dispatching. I did not trace whether that check stops Ticket rows,
  which are inserted without Level 2 identity, exactly like the existing
  `POST /api/tasks`. It may already close this path.

Dispatcher eligibility is a forbidden layer for Step 1 (§20, Step 5), so I
recorded this and did not repair it. I still pushed, because the push goes to
a draft branch and the path only becomes live if the branch merges. The
reviewer should decide before merge. Tell me if this should have held the push
instead.

Hard rules, all held: no pre-existing test went red (counts in §8). Nothing was
approved, merged, rebased or force-pushed, and nothing was pushed to `main`. No
scheduler tick or entry point was run. `~/.agent-taskflow/state.db` was never
read or written.

---

## 7. Known ambiguity

**(a) Task key format — resolved by the PR #195 ruling.** A single global
counter, `AT-0001`. One reading needed confirming: I took "`AT-0001`, no
per-prefix counters" literally, so the prefix is fixed at `AT` for every
repository. A `bullet_journal` Ticket is `AT-0002`, not `BJ-0002`, and the
registry's `task_key_prefix` is no longer read. If per-repo prefixes should
share the one counter instead, that is a one-line change in
`ticket_metadata.py` plus test updates.

**(b) Branch-name collision with an existing Git branch — still open.**
Collisions *between Tickets* are impossible, because the unique task key is
embedded in the branch name and storage enforces uniqueness. A collision with
a branch that already exists in the repository cannot be checked here:
reading refs means running Git, and the negative-scope test forbids that.
Step 2 needs a policy for it.

**(c) `blocked_by` at creation.** Accepted, stored, and it drives `ready` vs
`blocked`. The blocker must exist in `tasks`: since there is one entity, a
legacy task is a valid blocker. Cycle validation stays in Step 5; a freshly
created Ticket cannot be inside a cycle. Not in the Mission Control form.

**(d) `cancelled` vs `canceled` — resolved by §12.2.** The persisted spelling
is the legacy `canceled`.

---

## 8. Verification

Run from `/home/ubuntu/agent-taskflow/.worktrees/v1-step1`.

The repo's canonical validator sequence, the one that gates this handoff:

```bash
/home/ubuntu/agent-taskflow/.venv/bin/python scripts/run_local_validation.py
```

Observed result after the PR #195 ruling, run in the foreground (exit 0):

```text
- check: Python environment dependencies    passed
- check: workflow contract validation       passed
- check: workflow policy validation         passed
- check: Mission Control golden path smoke  passed
- check: PiExecutor golden path smoke       passed
- check: unit tests                         passed   (Ran 4529 tests, OK, skipped=8)
- check: compileall                         passed
- check: openspec validate                  skipped  (openspec not on PATH)
```

`cd mission-control && npm run build` also passed, and both `/tickets/new`
and `/tickets/[taskKey]` built. No test went red at any point on this branch.

Test count history on this branch:

| Point | `Ran N tests` | Result |
| --- | --- | --- |
| base `4266c02`, before any Step 1 edit | 4396 | OK (skipped=8) |
| after Step 1 (`ea41f3a`) | 4485 | OK (skipped=8) |
| after the §12.2 ruling (`764c9ff`) — **before** this change | 4510 | OK (skipped=8) |
| after the PR #195 ruling — **after** this change | 4529 | OK (skipped=8) |

Step 1 tests only:

```bash
/home/ubuntu/agent-taskflow/.venv/bin/python -m unittest \
  tests.test_ticket_metadata tests.test_ticket_store \
  tests.test_ticket_creation tests.test_api_tickets \
  tests.test_status_vocab -v
```

Mission Control:

```bash
cd mission-control && npm ci --prefer-offline --no-audit --no-fund && npm run build
```

`.venv/bin/python` is required — the system `python3` has no `pydantic`.

Manual API check against a throwaway database, never the default path:

```bash
/home/ubuntu/agent-taskflow/.venv/bin/python scripts/run_api.py --db-path /tmp/step1-demo.db &
curl -s localhost:8100/api/repositories | python3 -m json.tool
curl -s -X POST localhost:8100/api/tickets -H 'content-type: application/json' \
  -d '{"repository":"agent-taskflow","prompt":"Separate the ending page image","priority":"high"}'
curl -s localhost:8100/api/tasks | python3 -m json.tool   # the Ticket is a task
```

---

## 9. Deployment note

`TicketStore.init_db()` delegates to the task store's `init_db()`, which runs
from the FastAPI lifespan. On the next API start against an existing database,
the `tasks_ticket_fields` migration adds 10 nullable columns and 2 partial
unique indexes to `tasks`, and records itself in `schema_migrations`. It
creates no table. Existing rows keep NULL in the new columns, so neither index
can conflict with them. This has not been run against the production database.

The earlier `v1_ticket_creation_v1` migration never merged. If any environment
did run an earlier revision of this branch, it will have orphan `tickets` /
`ticket_events` tables and a `v1_ticket_creation_v1` row in
`schema_migrations`. This code neither creates nor reads them, and dropping
them is a human-controlled cleanup.

---

## 10. Follow-ups for the human

1. Confirm the fixed `AT` prefix for every repository — §7(a).
2. Decide the Step 2 policy for a derived branch name that already exists
   in the repository — §7(b).
3. Decide the dispatcher / `blocked` contradiction in §6 before merge, and
   whether the Level 2 check already closes it.
4. Decide whether `task_events` should become append-only, which would
   restore what `ticket_events` had.
5. Confirm or overrule the status-vocabulary judgement calls in §4.
6. Step 2 owns the §32.1 PR fields; they are absent here on purpose.
7. Decide whether `WORKFLOW.md` should describe the V1 Ticket lifecycle.
8. The deferred repo-wide `TASK_STATUSES` migration (§12.2, after Steps 1-3
   merge) should decide whether the legacy aliases collapse for real.
