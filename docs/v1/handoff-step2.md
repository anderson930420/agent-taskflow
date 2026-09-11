> This is the V1 Step 2 handoff. Until Ruling 9 it lived at the repository
> root as `HANDOFF.md`; main keeps no root handoff, so it is now
> `docs/v1/handoff-step2.md`. Step 1's handoff is
> `docs/v1/handoff-step1.md`. Mentions of `HANDOFF.md` below describe
> earlier events, when that was still the file's name.

---

# HANDOFF — V1 Step 2: Integration Controller

Branch: `task/v1-step2`
Draft PR: https://github.com/anderson930420/agent-taskflow/pull/196
Spec: `~/agent-taskflow-ops/v1/SPEC.md` §42 Step 2 (Integration Controller,
Re-integration, PR outcomes, Merge)
Instruction set: `~/agent-taskflow-ops/v1/step2.md`

Status: **implementation-complete, awaiting independent review.** Round 5
passed with no blocking item; its one note outside the checklist became
**Ruling 35**, which is now fixed: a PR is updated through the REST endpoint,
because `gh pr edit` cannot work on this box. The round-4 rulings are fixed
too: Ruling 29 (the GitHub adapter works against real `gh`), Ruling 30 (never
publish a branch that was not validated), and Ruling 31 (no exception of any
kind leaves a Ticket in `integrating`). The branch is up to date with `main`
as of `75bb6ba` (merge `b7b6314`), which carries Step 1 (#195), Step 3
(#197), F1 (#199), Step 4 (#200) and Step 5 (#201). No stop condition is
open. Nothing is approved, merged to `main`, or finally complete.

Step 1 was merged into this branch twice while it was still in review
(`96a6cd3`, `9e09ad7`). `main` was then merged in twice: `5b362bc` brought
the final Step 1 (#195), and `ff86d9a` brought Step 3 (#197) and F1 (#199).
`8bb699b` then brought Step 4 (#200). Every merge was a merge commit, never
a rebase. PR #196 reviews Step 2.

---

## Review round 5 — Ruling 35: update a PR through REST

Round 5 passed its checklist. Outside it, the reviewer found that
re-integration calls `gh pr edit`, which **fails on this box's gh 2.45.0**
with the Projects (classic) GraphQL deprecation error. Re-integration could
push a branch and then stop in `needs_decision` every time, so SPEC §43 items
20-22 could not complete in production. The owner chose one narrow fix.

**Field evidence.** The Step 4 builder already updated a PR body in this
repository with `gh api -X PATCH repos/<owner>/<repo>/pulls/<n>` and it
worked; `gh pr edit` is what fails. The failure is a gh-CLI/GitHub API
deprecation, not auth.

### a. `update_pr` now issues the REST PATCH

    gh api -X PATCH repos/<repo>/pulls/<number> -f title=<title> -f body=<body>

Only the fields the caller passes are sent. The signature, the return
contract (a fresh `poll_pr` snapshot of the same PR) and the `_run_checked`
path are unchanged, and a call with no field still only polls.

Fields go as **`-f`, not `-F`**. `-F` reads `@file` and retypes values, so a
PR body beginning with `@` would be read from disk and a body of `true` would
become a boolean. `-f` sends the string literally. A test pins that for
`@/etc/passwd`, `a=b=c`, `-starts-with-dash`, `true` and `merge me`.

The PATCH leaves the draft state alone, so a draft PR stays a draft.

### b. Every `gh api` argv passes an allowlist

`assert_gh_api_allowed(argv, repo=...)` runs inside `GitHubPrAdapter.run`,
next to the merge guard. REST is a merge vector —
`-X PUT repos/<o>/<r>/pulls/<n>/merge` merges a PR, `repos/<o>/<r>/merges`
merges branches, and `graphql` can run a `mergePullRequest` mutation — and
the old merge guard refused only `gh ... pr merge`. So this is an allowlist,
in the spirit of the push allowlist.

**The only admitted form**, with nothing else on the command line:

    gh api -X PATCH repos/<the adapter's own repo>/pulls/<n> -f title=<t> -f body=<b>

- the method must be exactly `-X PATCH`; `--method PATCH` and `-XPATCH` are
  refused, as is a lower-case `patch`, a repeated `-X`, and the default GET;
- the endpoint must match `repos/<owner>/<name>/pulls/<positive number>`
  exactly, where `<owner>/<name>` is the adapter's own repository;
- at least one of `title`/`body`, each at most once, and no other field
  (`state`, `base`, … are refused, so the adapter cannot close a PR here);
- no other flag at all — `-F`, `--input`, `--hostname` are refused;
- no global flag may precede `api`.

**The adapter issues no `gh api` GET**: it reads through `gh pr view`, so no
GET form is admitted either. argv is parsed the way the merge guard parses
it, so `/usr/bin/gh`, `./gh` and `env gh` are recognized, and a global flag
is skipped with its value.

Refused forms, each pinned by a test: `-X PUT .../pulls/42/merge`,
`--method PUT .../merge`, `-X POST repos/owner/repo/merges`, `graphql`,
a `DELETE`, a PATCH to another repository (`repos/other/repo/...`,
`repos/owner/repo2/...`), a PATCH to a non-pulls endpoint
(`repos/owner/repo/issues/42`), a full `https://api.github.com/...` URL, a
leading `/`, a trailing `/`, a query string (`...?merge=1`), a traversal
(`.../pulls/42/../../merges`), `pulls/0`, `pulls/x`, two endpoints, a missing
value, and global flags before `api`. A separate test runs every one of them
through `adapter.run` and asserts the runner was never called.

The **merge guard itself** now also refuses the branch-merge endpoint and
`graphql`, not just `pulls/<n>/merge`.

### c. The fake `gh` now fails `pr edit` like the real one

`FakeGhRunner` returns rc=1 with real gh 2.45.0's exact text for any
`gh pr edit`, so **a regression back to `pr edit` cannot pass the tests**, and
it serves the REST PATCH instead. Proven: with the pre-ruling `update_pr`
restored in memory for one throwaway run, all eight `RestUpdateTests`, the
adapter's update test and the controller's re-integration test go red.

The error text was captured read-only with
`gh pr view 196 --json projectCards`, which fails with the same message and
the same `(repository.pullRequest.projectCards)` field path:

    GraphQL: Projects (classic) is being deprecated in favor of the new
    Projects experience, see:
    https://github.blog/changelog/2024-05-23-sunset-notice-projects-classic/.
    (repository.pullRequest.projectCards)                              (rc=1)

**+13 tests**: 8 `RestUpdateTests`, 5 `GhApiAllowlistTests`. One existing test
outside the adapter changed —
`ReIntegrationTests::test_reintegration_updates_the_same_pr_number` asserted
that re-integration calls `gh pr edit`. It now asserts the opposite: no
`pr edit` call, exactly one PATCH to `repos/owner/repo/pulls/<same number>`,
and the new body landed on the PR. That is Ruling 35c's end-to-end
re-integration test. **It is the only change outside the ruling's file
scope**, and it is a test asserting the wire format the ruling replaced.

### d. Verified read-only against the real binary

    $ gh api repos/anderson930420/agent-taskflow/pulls/196
    {"number":196, "state":"open", "draft":true,
     "html_url":"https://github.com/anderson930420/agent-taskflow/pull/196",
     "head_ref":"task/v1-step2",
     "head_sha":"01e4c953f7ae96cf3bc88da9c93539b6c4f341e2",
     "base_ref":"main", "merged":false,
     "title":"V1 Step 2: Integration Controller ..."}                  (rc=0)

**No live PATCH, PUT, POST or DELETE was issued** against the real GitHub
API, and no PR was created, edited, closed or merged. The write path is
covered by the fake `gh` and by the Step 4 builder's field evidence above.

### Stop conditions this round — none fired

- **A fix needing a change outside the ruling's scope:** not hit, with the
  one test-assertion exception named in (c), which Ruling 35c itself calls
  for.
- **Pre-existing tests going red:** none.

---

## Merge of origin/main (`75bb6ba`) — Step 5 parallel scheduler (#201)

`origin/main` was merged into `task/v1-step2` as merge commit `b7b6314`
(parents `0ee2fac` — Step 2 with Ruling 35 — and `75bb6ba` — main). A normal
merge, never a rebase.

**Conflicts: none, and no silent merge.** Step 2 and Step 5 touch disjoint
files: Step 5 changed 34, Step 2 changed 33 since the merge base `a5fa8e2`,
and **no file was changed on both sides**. Verified rather than assumed:

- all 34 of Step 5's files in the result are byte-identical to `origin/main`;
- all 33 of Step 2's files are byte-identical to the pre-merge commit
  `0ee2fac`, including `WORKFLOW.md`;
- the result differs from the Step 2 parent in exactly Step 5's 34 files, and
  from the `main` parent in exactly Step 2's 33 files.

**Step 5's behaviour is unchanged**, by construction (byte-identical files)
and confirmed by its own tests: all **105** Step 5 tests pass on the merged
tree. The three named invariants pass by name:

- **The dependency gate stays inside the claim transaction:**
  `ClaimRefusesTests::test_claim_refuses_an_unreleased_blocker_and_writes_nothing`,
  `test_capacity_is_still_checked_first`, `test_claim_refuses_an_unknown_blocker`.
- **A Ticket whose blocker is not `completed` stays unclaimable:**
  `ReleaseOnlyAfterCompletedTests::test_no_status_short_of_completed_releases`,
  `test_a_running_blocker_does_not_release`, and the four `StartPathsRefuseTests`
  (direct dispatch, API start, scheduler tick, adoption claim).
- **The reset refusal stays:** `ResetRefusesTests::test_reset_of_the_reviewers_ticket_is_refused`,
  `test_reset_is_refused_for_an_unknown_blocker_too`,
  `test_reset_proceeds_once_the_blocker_completed`.

**The two steps do not touch each other.** No Step 5 module or script
mentions `task_pr_state` or any Step 2 module, and no Step 2 module mentions
`parallel_scheduler`, `ready_queue`, `ticket_dependencies`,
`scheduler_worker` or `runtime_admission`. That matches Ruling 21, which kept
Step 5 off Step 2's modules and deferred the integration handoff to
FOLLOWUPS F4. Step 2's two migrations in `store.py` are intact and still the
only `v1_step2_*` entries.

### Test counts for round 5 and the merge

| | unittest (`Ran`) | pytest |
| --- | --- | --- |
| Before (`01e4c95`) | 5237 | 5239 collected |
| Ruling 35 only (`0ee2fac`) | 5250 | 5252 collected |
| After the merge (`b7b6314`) | 5355 | **5349 passed, 8 skipped** (5357 collected) |

**0 failed. No test went red at any point.** The arithmetic:

- **+13** from Ruling 35's new tests (8 REST update, 5 allowlist).
- **+105** from Step 5's own tests, which is exactly the number that
  `tests/test_step5_*.py` runs.

The full suite now runs as **one** parallel command,
`PYTHONPATH=. python -m pytest tests -q -n 4`, in 298 s — under the 600 s
foreground limit, so it no longer needs splitting into parts.

Other checks, from the repository root:

- `python -m compileall -q agent_taskflow scripts tests`: exit 0.
- `scripts/validate_workflow_contract.py` (`WORKFLOW.md`): passed, exit 0.
- `scripts/validate_workflow_policy.py`: passed, exit 0.

### Stop conditions for this merge — none fired

- **A conflict that cannot be resolved without changing Step 5's or Step 4's
  behaviour:** not hit. There was no conflict at all, and no file needed a
  resolution.
- **Step 5's behaviour changing:** not hit — see the invariants above.

---

## Review round 4 — Rulings 29, 30 and 31

The round-4 review failed on three defects, each reproduced by the reviewer.
All three are fixed, each with the reviewer's own scenario as a test.

### Ruling 29 — the GitHub adapter must work against real `gh`

**The defect.** The adapter asked `gh pr view --json` for a `merged` field.
Real gh has no such field, so every poll failed. Two things followed:

- `create_pr` polled right after creating the PR, so a real PR was created
  and then orphaned.
- The watcher swallowed the failure (`except GitHubPrError: continue`), so
  it went blind without saying so.

The fake `gh` in the tests answered any field it was asked for, which is why
no test caught it.

**a. The field list.** `merged` is gone from `PR_VIEW_FIELDS`. Merged is now
*derived* by `_is_merged`: `state == "MERGED"`, or a `mergedAt`, or a
`mergeCommit`. This matters. Merely dropping the field would have made a
merged PR read as closed-unmerged, and the watcher would then cancel a merged
Ticket. §35/§36 verification is unchanged and still driven by the merge
commit.

**b. Verified against the real binary on this box.** gh 2.45.0, authenticated
as `anderson930420`. Only `gh pr view` was run (read-only), against this
repository's #196 (open draft) and #200 (merged):

    $ gh pr view 196 --repo anderson930420/agent-taskflow --json number,merged,state
    Unknown JSON field: "merged"
    Available fields:
      additions ...                                                    (rc=1)

    $ gh pr view 196 --json number,url,state,isDraft,mergedAt,mergeCommit,headRefName,baseRefName,headRefOid,reviewDecision,statusCheckRollup,reviews,title,body
    {"baseRefName":"main","headRefName":"task/v1-step2",
     "headRefOid":"25143529eea66560138feef2e4326b436b28c6ab","isDraft":true,
     "mergeCommit":null,"mergedAt":null,"number":196,"reviewDecision":"",
     "state":"OPEN", ...}                                              (rc=0)

    $ gh pr view 200 --json <the same list>
    {"baseRefName":"main","headRefName":"task/v1-step4",
     "headRefOid":"4f3bdb87350d12c536ec92f900de6ab856e66469","isDraft":false,
     "mergeCommit":{"oid":"a5fa8e2daee56d026ad11068122e99eb5fc78281"},
     "mergedAt":"2026-09-11T09:54:01Z","number":200,"reviewDecision":"",
     "state":"MERGED"}                                                 (rc=0)

The production adapter was also run end to end against real gh. A recording
runner asserted that every call was `gh pr view`, and the adapter read:

    196  state=open    merged=False  merge_commit_sha=None      ci_status=success  is_draft=True
    200  state=closed  merged=True   merge_commit_sha=a5fa8e2…  merged_at=2026-09-11T09:54:01Z

No PR was created, edited, closed or merged.

**c. The root cause in the tests.** `tests/v1_step2_fixtures.py` now holds
`GH_PR_VIEW_JSON_FIELDS`, copied verbatim from gh 2.45.0's own list. The fake
`gh`:

- rejects any other `--json` field with gh's own error (`Unknown JSON field:
  "<name>"`, rc=1);
- refuses to let a test script a field real gh lacks (`set_pr(merged=True)`
  raises).

Seventeen `merged=` arguments that tests fed the fake were removed. None of
their assertions changed, and every such test still passes, now through the
derivation. New tests:

- `RealGhFieldContractTests` (4) pins the field list. It fails if any
  requested field is unknown, and it proves a list containing `merged` fails
  the poll.
- `RealGhPayloadTests` (2) parses the real #196/#200 shapes.
- `MergeDerivationTests` (5).

**d. The PR's identity is recorded before any poll.** `create_pr` no longer
polls. It returns the number and URL that `gh pr create` printed. The
controller records `pr_number`, `pr_url`, `pr_state='open'` and `pr_head_sha`
(the validated, pushed commit) immediately. `integrated_base_sha` is still
recorded only at the end.

`PrIdentityTests` (2) covers this: initial integration makes exactly one
`gh` call, `pr create`; and a failure injected right after create leaves
PR #42's identity recorded, the Ticket in `needs_decision`, and no
`integrated_base_sha`. `CreateIdentityTests` (2) covers the adapter side.

**e. The watcher no longer swallows failures.** Any poll exception is caught
and recorded as a new `pr_poll_failed` event (added to `TASK_EVENT_TYPES`),
carrying the exception type and message. No §32.1 field is written from a
failed poll.

- **`needs_review` → `needs_decision`** via §27.2.1.
- **A Ticket the transition table does not let integration move there keeps
  its status and gets the event.** These are `ready_for_integration`,
  `paused`, and a Ticket already in `needs_decision`. For a queued Ticket,
  its own integration run reaches `needs_decision` when its `gh` call fails.
  I did not add a `ready_for_integration → needs_decision` edge: it would
  also have changed the changes-requested path. Say if you want it.
- With `confirm_poll=False`, the failure is reported in the outcome
  (`poll_error`) and nothing is written.

`PollFailureTests` (9) covers:

- gh's error, the old `Unknown JSON field` failure, a missing `gh` binary,
  and a `UnicodeDecodeError`;
- no PR fields written from a failed poll;
- `paused` and queued Tickets;
- the dry run;
- one failing poll not stopping the others.

### Ruling 30 — never publish a branch that was not validated

**The defect.** The push publishes the branch *by name*, but validators ran
against HEAD. Take a detached HEAD and no conflict at all: the rebase
succeeds on the detached HEAD, validators pass on it, and the push publishes
the untouched old branch. The Ticket then reached `waiting_for_review`, with
a recorded `integrated_base_sha` the published branch did not contain.

**The fix.** `integration_git.assert_head_on_task_branch` checks three
things:

- HEAD resolves to `refs/heads/<task-branch>`, so it is neither detached nor
  on another branch;
- the branch ref equals HEAD;
- when given `expected_sha`, the branch ref is exactly that commit.

It uses only `rev-parse`, which is read-only; no new git subcommand was
allowlisted. The controller runs it twice:

- **Before validators.** Its result becomes the `branch_sha` that validators
  see and that is recorded as `pr_head_sha`.
- **Before the push**, with `expected_sha=branch_sha`, so a validator that
  moved the branch or HEAD is caught. This also runs when a no-op
  re-integration skips the push, because `integrated_base_sha` is recorded
  either way.

A mismatch refuses: nothing is pushed, there is no `gh` call, no
`integrated_base_sha`, and `needs_decision` via §27.2.1. The
`integration_blocked` event carries `check=head_on_task_branch`, the
`stage`, the `task_branch` and the `mismatch` text. The push itself is
unchanged: still `git push origin <task-branch>` with optional `-u`, and no
SHA-to-ref form.

Tests: `UnvalidatedBranchRefusalTests` (6) in the controller and
`HeadOnTaskBranchTests` (6) in `integration_git`. The controller tests cover:

- the reviewer's detached-HEAD, no-conflict scenario. It asserts the Ticket
  never reaches review, validators never run, nothing is pushed, and the
  branch that would have been published lacks the target;
- HEAD on another branch;
- a validator that commits;
- a validator that detaches HEAD;
- a detached HEAD on re-integration, where the published branch and the
  recorded base both stay as they were;
- the positive case: the published branch equals `pr_head_sha` and contains
  `integrated_base_sha`.

To confirm the test is real, the guard was stubbed out with a mock for one
throwaway run: the detached scenario again reached `waiting_for_review`, and
the published branch lacked the recorded target.

### Ruling 31 — finish Ruling 19

**The defect.** Ruling 19's guard caught `IntegrationGitError` only. The
reviewer left a Ticket stuck in `integrating` two ways. That is permanent,
because the watcher defers `integrating` Tickets indefinitely:

- a rebase conflict on a Latin-1 filename, where git's raw bytes raised
  `UnicodeDecodeError`;
- a missing `gh` binary after the push.

**a. The boundary catches `Exception`.** Everything from the fetch to the
review hand-off is inside it. On any exception it aborts any in-progress
rebase or merge; a failed abort does not prevent the stop. It then ends in
`needs_decision` via §27.2.1. The `integration_blocked` event records
`exception_type` and `exception_message`, and the message names the type.

If the Ticket has already left `integrating` — the failure came after the
hand-off to review, or from inside a stop — it is not stuck. The guard
re-raises rather than guess a new status.

**b. Decoding.** All three subprocess chokepoints decode as UTF-8 with
`errors="replace"`: `integration_git._default_runner`, the validator runner,
and the `gh` adapter. A Latin-1 path therefore reaches evidence with
U+FFFD in place of the byte. If a resolver then acts on the mangled path,
its failure is caught by (a) and by the B2 verification.

**c. A missing `gh`** now lands in the same guard, including after the push.
The branch is published; no PR exists, and nothing claims one exists.

**d. Tests.** `IntegrationBoundaryTests` (7):

- an injected `UnicodeDecodeError`;
- `FileNotFoundError` for `git`, and for `gh` after the push;
- a `RuntimeError`;
- a non-git failure mid-rebase, which is aborted;
- a failure after the hand-off, which is raised while the Ticket stays in
  `needs_review`;
- **the reviewer's Latin-1 filename conflict, unmocked**, which ends in
  `needs_decision`. With the old strict-decoding runner patched back in, the
  same scenario still ends in `needs_decision`, this time through the guard,
  with `exception_type=UnicodeDecodeError`.

Real non-UTF-8 output is covered by one git test (`OutputDecodingTests`) and
one validator test (`ValidatorOutputDecodingTests`). An adapter test checks
the `gh` runner arguments.

**Not covered.** If `stop_for_decision` itself fails — the database is
unwritable while the stop is being recorded — the exception propagates and
the Ticket can stay `integrating`. No code path can record a status the
database refuses to take. I have not checked whether anything outside Step 2
recovers such a Ticket.

### Rulings 16 and 21, recorded here

These were made between rounds and missing from this file.

- **Ruling 16.** #196 went to independent review against the branch as it
  stood, with main at `b870b84` merged in. Merging main again, for Step 3 and
  F1, was a separate turn (it became `ff86d9a`). **Step 2 keeps creating its
  own tables at startup (`store.init_db()`) for V1.** Aligning that with Step
  1's explicit-migration-script rule is repo-wide cleanup owned by FOLLOWUPS
  **F2** ("Repo-wide vocabulary and migration cleanup"). This settles the
  question the `b870b84` merge section below used to leave open.
- **Ruling 21.** Step 5 started from `main` at `a5fa8e2`, in parallel with
  this fix round. The "integration handoff" item of §42 Step 5 was cut from
  its scope and deferred to FOLLOWUPS **F4**, "Wire the implementation →
  integration handoff". That is to be wired in one turn after #196 merges.
  Step 5 must not import, stub or guess at Step 2's modules. Nothing on this
  branch depends on Step 5.

### Stop conditions this round

- **Pre-existing tests going red:** none — see the counts below.
- **Rulings contradicting each other or the spec:** none. Ruling 30's check
  uses `rev-parse` only, so the push allowlist from Rulings 3, 18 and 22 is
  untouched.
- **The push being rejected because the branch diverged:** not expected to
  hit. Checked before pushing: `origin/task/v1-step2` was `2514352`, the
  commit this work sits on, so the push is a fast-forward. `origin/main` was
  still `a5fa8e2`, already merged in, so no merge was needed.

Two judgment calls are flagged, not stops:

- The watcher does not move `ready_for_integration` or `paused` Tickets on a
  poll failure; it audits them (29e above).
- The seventeen `merged=` arguments were removed from test inputs because
  real gh has no such field (29c). No assertion changed.

### Test counts for round 4

| | unittest (`Ran`) | `pytest tests` |
| --- | --- | --- |
| Before (`2514352`) | 5191 | 5185 passed, 8 skipped (5193 collected) |
| After (this commit) | 5237 | 5231 passed, 8 skipped (5239 collected) |

**+46 tests, 0 failed, and no test went red at any point.** The +46 is
exactly this round's new tests:

- controller: 15 (Ruling 30: 6, Ruling 29d: 2, Ruling 31: 7)
- adapter: 14
- watcher: 9
- `integration_git`: 7
- validators: 1

The unittest counts come from the unittest loader on both trees. The full
unittest run takes longer than the tool's 600 s limit, so every test was
*run* through pytest, in six foreground parts:

    Step 4's 8 files       102 passed
    test_[a-c]*.py         858 passed
    test_[d-l]*.py         926 passed
    test_[m-q]*.py         998 passed
    test_r*.py             903 passed, 8 skipped
    test_[s-z]*.py        1546 passed
    ------------------------------------------
    sum                   5333 passed, 8 skipped
    Step 4 counted twice  −102
    unique                5231 passed, 8 skipped = 5239 collected

**A correction to my own tooling.** Parts 2–6 passed `--ignore` for Step 4's
files, but pytest does not apply `--ignore` to paths named explicitly on the
command line. So Step 4's 102 tests ran twice: in part 1 and inside the glob
parts. `--collect-only` confirms it (41 + 9 + 12 + 32 + 8 = 102). The
arithmetic above removes the duplicates. Both runs of those tests passed.

Other checks, all from the repository root:

- `python -m compileall -q agent_taskflow scripts tests`: exit 0, no
  "Can't list" or error lines.
- `scripts/validate_workflow_contract.py` (`WORKFLOW.md`): status passed,
  exit 0.
- `scripts/validate_workflow_policy.py`
  (`examples/workflow-policy.example.json`): status passed, exit 0.

These are the two repo validators, run directly. `local_validation` was not
run as a whole this round, because it wraps a full unittest run that exceeds
the 600 s foreground limit. Its unit-test and compileall steps are covered
above.

---

## Merge of origin/main (`a5fa8e2`) — Step 4 concurrency readiness (#200)

**Ruling 23.** Step 4 was merged before the next review, because it changes
`store.connect()`, which every Step 2 read and write goes through. The review
has to see the code that will actually land.

`origin/main` was merged into `task/v1-step2` as merge commit `8bb699b`
(parents `25ba7ac` — Step 2 — and `a5fa8e2` — main). A normal merge, never a
rebase.

**Conflicts: none.** One file changed on both sides, `agent_taskflow/store.py`,
and git merged it automatically. Step 4 changed `connect()`; Step 2's changes
are its two migrations, in different hunks. The auto-merge was verified:

- No line of `main`'s `store.py` is missing from the result.
- The only pre-merge Step 2 lines that are gone are the two base lines of
  `connect()` that Step 4 replaced: its old docstring, and the plain
  `sqlite3.connect(...)` call.
- `connect()` in the result is byte-identical to Step 4's.
- `SCHEMA_MIGRATIONS` is the three legacy migrations plus Step 2's two, and
  `tasks_ticket_fields` is still absent (ruling 4a).

**Step 4's behaviour is unchanged.**

- By construction: all 26 of Step 4's files other than `store.py` are
  byte-identical to `origin/main`, and its `connect()` is carried exactly.
  So the capacity check stays the first statement inside the claim
  transaction, `max_concurrent_tasks` still defaults to 1 on every database,
  raising it still requires rehearsal evidence, and the reaper contract is
  unchanged.
- Confirmed by its own tests: Step 4's 102 tests, run as their own part of
  the suite, all pass. The files are `test_concurrency_atomic_claim`,
  `test_concurrency_crash_recovery`, `test_concurrency_rehearsal`,
  `test_concurrency_writes`, `test_lease_contention`,
  `test_observed_step_guard`, `test_runtime_capacity` and
  `test_sqlite_contention`.

### What Step 4 changed in `store.connect()`, and whether Step 2 depends on it

**The change.** `connect()` now opens every connection with
`factory=ContentionObservingConnection`, from the new
`agent_taskflow.sqlite_contention`, plus the import and a docstring.
Everything else in `connect()` is unchanged: the `timeout`, `PRAGMA
busy_timeout = 5000`, the WAL attempt, `foreign_keys = ON`, and
`row_factory`.

`ContentionObservingConnection` subclasses `sqlite3.Connection` and overrides
only `execute`, `executemany` and `executescript`. Each calls `super()`,
returns its cursor unchanged, and re-raises any error unchanged. It only adds
process-local counters and log lines:

- a *busy wait* — only for an explicit `BEGIN IMMEDIATE` or `BEGIN EXCLUSIVE`
  that waited at least 1 ms;
- a *busy timeout* — for `SQLITE_BUSY` or `SQLITE_LOCKED`.

It writes nothing to the database and adds no retries.

**Step 2 does not depend on any of the old behaviour.** Checked by reading
every Step 2 module and by running it on the new class:

- **Connection lifetime.** Step 2 opens one connection per operation with
  `with closing(connect(...)) as conn, conn:`. The context manager is
  inherited unchanged, so each block still commits on success and rolls
  back on error. No Step 2 code calls `sqlite3.connect` directly; every
  access goes through `store.connect()`.
- **WAL.** Unchanged. Step 2 neither sets nor reads the journal mode. It is
  still `wal` on a fresh database.
- **`busy_timeout`.** Unchanged at 5000 ms. Step 2 sets no timeout of its
  own; the one `timeout=` in Step 2 is the validator subprocess timeout.
- **Retries.** Step 4 adds none. Step 2 neither retries nor catches
  `OperationalError`, "database is locked" or "busy".
- **Contention counters.** Step 2 does not read them. Its writes are implicit
  deferred transactions with no explicit `BEGIN`, so they show up in the
  counters only if they time out. That is observability only; behaviour does
  not change.
- **The one thing Step 2 does rely on:** `cursor.rowcount` after
  `INSERT OR IGNORE` and `DELETE` in its per-repo integration lock. That
  cursor comes straight from `super().execute()`. Run on the new class:
  acquire `True`, second acquire `False`, wrong-owner release `False`,
  release `True` — unchanged.
- **Step 2's schema migration** uses `executescript`, and it builds all of
  Step 2's tables through the overridden `executescript`.

### Step 4 rehearsal with Step 2's code present — 16/16

`scripts/run_concurrency_rehearsal.py --output-dir /tmp/step2-rehearsal-MjWJLT`,
on a fresh, empty throwaway directory. The script creates its disposable
databases there and never opens the default state database. Result: exit 0,
`ok: true`, `all_checks_passed: true`, gate `passed`, **16/16 checks passed**,
evidence recorded against `repo_sha` `8bb699b` (this merge), and no stderr.

### Stop conditions this round — none fired

No conflict; no test went red; Step 4's behaviour is unchanged; the rehearsal
passes.

### Test counts for this merge

| | `pytest tests -q` |
| --- | --- |
| Before (`ff86d9a`; `28cc209` and `25ba7ac` changed only this file) | `5083 passed, 8 skipped, 0 failed` |
| After (`8bb699b`) | `5185 passed, 8 skipped, 0 failed` |

Run in six parts, each under the 600 s tool limit, with Step 4's tests as a
part of their own so that nothing ran twice. The parts add up to the whole:

    Step 4's 8 new test files       102 passed
    test_[a-c]*.py (excl. Step 4)   817 passed
    test_[d-l]*.py (excl. Step 4)   871 passed
    test_[m-q]*.py (excl. Step 4)   986 passed
    test_r*.py     (excl. Step 4)   871 passed, 8 skipped
    test_[s-z]*.py (excl. Step 4)  1538 passed
    -------------------------------------------
                                   5185 passed, 8 skipped = 5193 collected

**No test went red.** The +102 is exactly Step 4's tests. Step 4's one-line
fixture change to `test_project_class_controls` added no test.

`python -m compileall -q agent_taskflow scripts tests`, from the repository
root: exit 0, no "Can't list" or error lines.

Repo validators — `agent_taskflow.cli.local_validation`, exit 0. Both repo
validators passed: workflow contract validation and workflow policy
validation. So did the Python environment dependencies check, the Mission
Control and PiExecutor golden-path smokes, unit tests (`Ran 5191 tests`,
`OK (skipped=8)`, +102 over the last run, the same +102 as pytest) and
compileall. `openspec validate` was skipped: not on PATH, an optional check,
pre-existing.

---

## Independent review of PR #196 — FAIL, and what was done

The independent review (`review-step2.md`) returned FAIL on one blocking
item, plus five non-blocking points. All six are addressed in `75f91d8`.

### Blocking item — Ruling 18: normalize branch refs before comparing

**The defect.** `assert_push_allowed` compared bare branch names. A task
branch recorded as `refs/heads/main` or `heads/main` therefore passed the
protected-branch check, and `integrate_task` ran `git push origin
refs/heads/main`, which fast-forwarded `origin/main`. The reviewer reproduced
this end to end. Branch strings reach the store unchecked from `POST
/api/tasks` and `workspace_manager.py`.

**The fix.**

- `integration_git.normalize_branch_ref` strips surrounding whitespace and
  one `refs/heads/` or `heads/` prefix. Exactly one, because git reads
  `refs/heads/refs/heads/main` as a different branch literally named
  `refs/heads/main`. Case is kept: git refs are case-sensitive, and no
  case-insensitive matching was added.
- `assert_task_branch_pushable` normalizes the Ticket's branch and the
  protected names — main, master, trunk and the base branch — and refuses a
  match.
- `assert_push_allowed` uses the same check, and also requires the push
  target to normalize to the Ticket's own task branch (18b). So
  `refs/heads/<task-branch>` is accepted as the task branch itself, while
  `refs/heads/main` is refused even when paired with a legitimate task
  branch.
- **18c:** the controller calls the check as soon as integration reads the
  branch. It runs right after the Ticket enters `integrating` — a
  database-only step — and before the fetch, the first git command. A refusal
  runs no git at all, pushes nothing, records no `integrated_base_sha`, and
  ends in `needs_decision` via §27.2.1 with an `integration_blocked` event.
- `HEAD` is refused as a task branch too — accepted by Ruling 22b below.

**Tests.**

- `test_integration_git.py::BranchNormalizationTests` (4 tests): the
  normalization table, including the one-prefix and case-kept rows; every
  listed spelling of main or the base branch (`refs/heads/`, `heads/`,
  surrounding whitespace, master, trunk, HEAD) refused as the task branch;
  `refs/heads/main` / `heads/main` refused as the push target even for a
  legitimate task branch; and the target required to normalize to the
  Ticket's own branch.
- `test_integration_controller.py::ProtectedBranchNormalizationTests` (2
  tests): the reviewer's `branch="refs/heads/main"` scenario, end to end, with
  an unpushed commit on the clone's `main` so that any push would be visible;
  then `heads/main`, the base branch `develop` in both prefixed forms, and a
  whitespace-padded `refs/heads/main`. Each asserts no git command ran, no
  `gh` call, `origin/main` unchanged, no `integrated_base_sha`, the lock
  released, and exactly one `integration_blocked` event.
- **Every existing allowlist test passes unedited** — each one's source is
  byte-identical to `9f60b7f`. The exception is the one rename that fix 1
  required; that rename changed only its `def` line and added a docstring.

**The reviewer's reproduction, re-run** (`/tmp/step2_review_refs_heads_main.py`,
which puts the working directory on `sys.path`, so it tests this branch):

    result.status: needs_decision | final task status: needs_decision
    pushes: []
    origin/main before: 4a78f49ba29b after: 4a78f49ba29b
    origin/main advanced by the integration push: False

### Ruling 22 — the three choices beyond Ruling 18's text, accepted

All three were accepted exactly as implemented, and they stay:

- **a. Only one prefix is stripped.** `normalize_branch_ref` removes a single
  `refs/heads/` or `heads/`. To git, `refs/heads/refs/heads/main` is a
  genuinely different branch, one literally named `refs/heads/main`.
  Treating it as `main` would be wrong.
- **b. `HEAD` is refused as a task branch.** It is not a branch name, and
  pushing it would resolve to whatever the worktree happens to have checked
  out.
- **c. `refs/heads/<the Ticket's own task branch>` is accepted.** It
  normalizes to the Ticket's own branch, which is exactly what 18b requires.

`master` and `trunk` stay in the protected set, alongside main and the base
branch.

### Ruling 19 — no git failure may leave a Ticket in `integrating`

> Superseded in scope by **Ruling 31** (review round 4, above). The guard
> described here caught `IntegrationGitError` only. It now catches every
> exception and records its type and message.

The controller's whole git phase — from the fetch to the review hand-off —
now runs inside one `IntegrationGitError` guard. A git failure that no
narrower handler catches, such as the `behind_count` the report names, aborts
any in-progress rebase or merge. It then ends in `needs_decision` via §27.2.1,
with an audited `integration_blocked` event. The guard covers every git call
in that phase, including any added later.

Tests — `test_integration_controller.py::GitFailureGuardTests` (2 tests):

- a failing `behind_count` ends in `needs_decision`, not `integrating`
- a git failure in the middle of a rebase conflict aborts the rebase and
  ends in `needs_decision`

Both assert: the lock is released, nothing is pushed, there is no `gh` call,
no `integrated_base_sha` is recorded, and exactly one `integration_blocked`
event carries the failure.

**Handed to Step 5 — the §29.2 gap.** SPEC §29.2 reserves `failed` for
infrastructure failures, including git failures. step2.md's
integration-owned transitions have no `failed`, so every integration
failure — fetch, push, `gh`, and now any unhandled git error — ends in
`needs_decision`. Remapping infrastructure failures to §29.2 `failed` belongs
to Step 5, which owns the runtime lifecycle. As first implemented, the guard
did **not** ensure that no Ticket is ever stuck in `integrating`: a
non-git exception escaped it. The round-4 review reproduced exactly that, and
Ruling 31 closed it.

### The review's other points

1. **Overclaiming test name.** `test_the_git_allowlist_excludes_history_rewriting_subcommands`
   → `test_the_git_allowlist_excludes_each_listed_subcommand`. Its docstring
   now says `rebase` (§24) and `merge` (§26) are allowlisted on purpose.
2. **Unguarded git calls** — Ruling 19, above.
3. **Stale statements, corrected rather than left:**
   - handoff §1 table, `models.py` row: said Step 2 adds lifecycle
     statuses. It adds none since the §12.2 ruling.
   - handoff §1 table, `store.py` row: said one migration. There are two.
   - handoff "Git and gh execution" paragraph: described the old force-push
     denylist. It now describes the Ruling 3 allowlist and Ruling 18.
   - module map lines 16 and 18: the same two corrections.
   - module map line 46: "the eleven §32.1 fields". There are twelve.
4. **A test that cannot fail.** `test_item_34_dependents_are_not_released_before_completed`
   → `test_item_34_integration_records_neither_placeholder_release_event_name`.
   Its docstring states what it asserts: the Ticket is in `needs_review`, and
   no `dependency_released` / `blocked_by_cleared` event was recorded. Those
   names exist nowhere in the code, so that half cannot fail. **The real
   §43.34 invariant — dependents are released only after the blocker is
   completed — lands with Step 5's dependency-release mechanism.**

### Stop conditions this round — none fired

- **Branch normalization:** Ruling 18 was implementable as written. It
  contradicted nothing, and no existing test needed an edit beyond the
  required rename.
- **Pre-existing tests going red:** none, at any point this round.
- **The merge:** no conflict at all, so no conflict needed a behaviour
  change — see below.

---

## Merge of origin/main (`79e568d`) — Step 3 (#197) and F1 (#199)

`origin/main` was merged into `task/v1-step2` as merge commit `ff86d9a`
(parents `75f91d8` — Step 2 with the review fixes — and `79e568d` — main). A
normal merge, never a rebase.

**Conflicts: none.** The merge was textually clean, as the reviewer's trial
merge found. The last merge taught that clean is not the same as correct, so
these were checked as well:

- **No silent merges.** No file changed on both sides since the merge base
  `b870b84`.
- **Main's files are intact.** Every Step 3 and F1 file in the result is
  byte-identical to `origin/main`.

**F1's behaviour is unchanged.** Its files are byte-identical to main, and
its claim-rule tests pass by name:

- `CreatedTicketStartsTests`: persisted `created` stays claimable.
- `BlockedTicketCannotExecuteTests` and `PausedTicketCannotAcquireWorkTests`:
  `blocked` and `paused` stay unclaimable and unrunnable. Dispatch, claim and
  the `preparing` transition all refuse.
- `RefusalLeavesRowUntouchedTests`: a refused claim leaves the row untouched.

**Step 3 now runs with Step 2's data.** Step 3's `realtime_projection` reads
Step 2's §32.1 public PR fields only when `task_pr_state` exists (it checks
`sqlite_master` first). After this merge the table exists, so the two steps
run together for the first time. Every Step 3 test passes, including its
schema-diff gate.

Step 4 (#200) landed on `main` after this merge's instruction. It was
merged in the following round, in `8bb699b` — see "Merge of origin/main
(`a5fa8e2`)" above.

### Test counts for this merge

| | `pytest tests -q` |
| --- | --- |
| Before (`9f60b7f`, this branch) | `4844 passed, 8 skipped, 0 failed` |
| After (`ff86d9a`) | `5083 passed, 8 skipped, 0 failed` |

Run in five parts, each under the 600 s tool limit, adding up to the whole:

    test_[a-c]*.py    817 passed
    test_[d-l]*.py    871 passed
    test_[m-q]*.py    986 passed
    test_r*.py        871 passed, 8 skipped
    test_[s-z]*.py   1538 passed
    -------------------------------------
                     5083 passed, 8 skipped = 5091 collected

**No test went red.** The +239 is exactly:

- +231 from Step 3 and F1. The reviewer's trial merge of `9f60b7f` with main
  gave 5075 = 4844 + 231.
- +8 from this round's new tests: 4 normalization, 2 protected-branch
  controller, 2 git-failure guard.

`python -m compileall -q agent_taskflow scripts tests`, from the repository
root: exit 0, no "Can't list" or error lines.

Repo validators — `agent_taskflow.cli.local_validation`, exit 0. Both repo
validators passed: workflow contract validation and workflow policy
validation. So did the Python environment dependencies check, the Mission
Control and PiExecutor golden-path smokes, unit tests (`Ran 5089 tests`,
`OK (skipped=8)`, +239 over the last run) and compileall. `openspec validate`
was skipped: not on PATH, an optional check, pre-existing.

---

## Merge of origin/main (`b870b84`) — final Step 1 (#195) and CI pins (#198)

`origin/main` was merged into `task/v1-step2` as merge commit `5b362bc`
(parents `597604f` — Step 2 — and `b870b84` — main). A normal merge, never a
rebase: this branch is published as draft PR #196 and §26 forbids
force-pushing a published PR branch. Only `origin/main` was merged;
`task/v1-step1` was not merged separately, because `main` already contains it.

**Why this merge needed care.** Step 1 reached `main` as a *squash* merge
(#195). `main` therefore does not contain the intermediate Step 1 commits this
branch merged earlier (`764c9ff`, `dcad084`); the merge base is `4266c02`.
Two consequences:

- Every Step 1 file that both sides added or changed differently conflicts.
- Worse, any file where this branch still carried *intermediate* Step 1 code
  and `main` had nothing to say merges silently in this branch's favour. That
  happened in `store.py`, which `main` left byte-identical to base.

**Resolution principle.** For Step 1's files, take `main`'s final version: it
is the reviewed one, and this branch's copies were superseded intermediates.
Keep Step 2's own changes. Before taking `main`'s version of any file, I proved
Step 2 never modified it: in this branch's first-parent history the only
commits touching it are the two Step 1 merges.

| File | Conflict | Resolution |
| --- | --- | --- |
| `agent_taskflow/api/main.py` | content | `main`'s version. Only Step 1 merges ever touched it here. |
| `agent_taskflow/api/tickets.py` | add/add | `main`'s version. Only Step 1 merges ever touched it here. |
| `agent_taskflow/status_vocab.py` | add/add | `main`'s version. Only Step 1 merges ever touched it here. Its display↔persisted mapping is identical to what Step 2 resolves against. |
| `agent_taskflow/ticket_creation.py` | add/add | `main`'s version. Only Step 1 merges ever touched it here. |
| `agent_taskflow/ticket_store.py` | add/add | `main`'s version. Only Step 1 merges ever touched it here. |
| `tests/test_api_tickets.py` | add/add | `main`'s version. Step 1 test. |
| `tests/test_ticket_creation.py` | add/add | `main`'s version. Step 1 test. |
| `tests/test_ticket_store.py` | add/add | `main`'s version. Step 1 test. |
| `agent_taskflow/models.py` | content | Both steps. `main`'s file, whose `TASK_STATUSES` is kept exactly, plus Step 2's integration event types and artifact types. Checked: statuses equal `main`'s; event and artifact types equal `main` ∪ Step 2. |
| `agent_taskflow/store.py` | **none — silent** | Git kept this branch's copy, which still carried the intermediate Step 1 `tasks_ticket_fields` auto-migration. Final Step 1 (ruling 4a) moved that migration out of `init_db()` into `scripts/migrate_ticket_fields.py`. Resolved to `main`'s file (byte-identical to base) plus Step 2's two migrations, extracted verbatim from `597604f`. Checked: the result only *adds* lines to `main`'s file; `tasks_ticket_fields` is not in `SCHEMA_MIGRATIONS`. |
| `HANDOFF.md` | none | Moved by Ruling 9 — see below. |

All eight files resolved to `main` are byte-identical to `origin/main`
(checked by blob hash). The nine Step 1 files that both sides added
identically, and the thirty files only `main` changed, merged with no
action.

**Step 1's behaviour after the merge.** Checked on a fresh database:
`init_db()` creates Step 2's tables and **none** of Step 1's `tasks` columns.
`require_ticket_fields` fails closed until the explicit migration runs, and
passes after `migrate_ticket_fields`.

### Ruling 9 — the handoff lives in `docs/v1/`

`git mv HANDOFF.md docs/v1/handoff-step2.md`; `main` keeps no root handoff.
`docs/v1/handoff-step1.md` was not edited. Pointers updated: code comments in
`integration_metrics.py` and `integration_controller.py`. Three mentions of
`HANDOFF.md` inside this file describe earlier events, when that was the
file's name, and were left as history. This file also carried a copy of Step
1's handoff as an appendix, from the earlier merges; that intermediate copy is
replaced by a pointer to `docs/v1/handoff-step1.md`.

### Stop conditions for this merge — none fired

- **A conflict that cannot be resolved without changing either step's
  behaviour:** not hit. Every conflict resolves to one step's file exactly,
  or to a union of the two steps' disjoint additions (`models.py`).
- **Step 2 tests or fixtures that open a `TicketStore` or start the API going
  red (step 3):** not hit. No Step 2 test does either (checked by grep), no
  Step 2 test went red, and no fixture was changed.
- **Step 2's schema handling conflicting with Step 1's explicit-migration
  rule:** evaluated, not hit. Ruling 4a, as written in
  `docs/v1/handoff-step1.md`, moves *Step 1's `tasks` columns* out of
  `init_db()` and keeps the pre-existing migrations there. Step 2 adds no
  `tasks` column. Its two migrations create Step 2's own tables and add one
  column to its own private table. Step 1's schema tests pin only that
  `tasks_ticket_fields` is not in `SCHEMA_MIGRATIONS` and compare migration
  sets, so they pass with Step 2's entries present.

**Settled by Ruling 16.** Step 2 still creates its own tables inside
`store.init_db()`, the startup path, whereas ruling 4a moved Step 1's new V1
schema into an operator-run script. This merge left that as an open question.
Ruling 16 settled it: Step 2 keeps creating its tables at startup for V1, and
aligning the two steps is repo-wide cleanup owned by FOLLOWUPS F2
("Repo-wide vocabulary and migration cleanup").

Step 3 (#197) landed on `main` after this merge's fetch. It was merged in
the following round, together with F1 (#199), in `ff86d9a` — see "Merge of
origin/main (`79e568d`)" above.

### Test counts for this merge

| | `pytest tests -q` |
| --- | --- |
| Before the merge (`6f47160`) | `4800 passed, 8 skipped, 0 failed` |
| After the merge (`5b362bc`) | `4844 passed, 8 skipped, 0 failed` |

The full suite takes about ten minutes and the tool times out at 600 s, so it
was run in four parts. The parts add up to the whole:

    test_[a-c]*.py    779 passed
    test_[d-l]*.py    863 passed
    test_[m-r]*.py   1664 passed, 8 skipped
    test_[s-z]*.py   1538 passed
    -------------------------------------
                     4844 passed, 8 skipped = 4852 collected (one collection
                     run over the whole suite)

**No test went red.** The +44 is exactly Step 1 reaching its final form. Its
five test files carried 133 tests at `dcad084`; `main`'s final versions of
those five hold 144, and three new files add 33 (`test_git_ref_storage`,
`test_migrate_ticket_fields_script`, `test_ticket_fields_schema`). That is 177,
and 177 − 133 = 44. The merge deleted no test file.

`python -m compileall -q agent_taskflow scripts tests`, run from the repository
root: exit 0, with no "Can't list" or error lines.

`agent_taskflow.cli.local_validation` after the merge: exit 0, every required
check passed — Python environment dependencies, workflow contract validation,
workflow policy validation, Mission Control golden path smoke, PiExecutor
golden path smoke (fake Pi), unit tests (`Ran 4850 tests`, `OK (skipped=8)`),
compileall. `openspec validate` skipped: `openspec` is not on PATH (optional
check, pre-existing). The `unittest` count is 44 above the last one recorded
(`Ran 4806` at `6f47160`), the same +44 as pytest.

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
| `agent_taskflow/models.py` | Added the Step 2 integration event types and artifact types as new set members. Step 2 adds no status: under the §12.2 ruling it resolves its statuses through `status_vocab`, and `TASK_STATUSES` is Step 1's, identical to `main`. No existing value changed or removed. |
| `agent_taskflow/store.py` | Added two named idempotent migrations, registered in the existing `_MIGRATIONS` tuple: `v1_step2_integration_tables` (the §32.1 table plus Step 2's private tables) and `v1_step2_conflict_verification` (one column on Step 2's private conflict-evidence table). No existing method touched. |
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
`reset`, `checkout`, `clean`, `update-ref`, …) and, for pushes, the push allowlist of review Ruling 3 — only
`git push origin <task-branch>`, optional `-u` — with branch names normalized
before every comparison (Ruling 18), so `refs/heads/main` and `heads/main`
count as main. Every gh argv goes through `GitHubPrAdapter.run`, whose
parsed-argv guard rejects `gh ... pr merge`. A test
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

# Step 1 handoff

Step 1's handoff is `docs/v1/handoff-step1.md`, which reached main with
PR #195. While `task/v1-step1` was being merged into this branch, a copy of
it was carried here as an appendix. That copy was an intermediate version;
it was removed once main held the final one, so there is a single source.
