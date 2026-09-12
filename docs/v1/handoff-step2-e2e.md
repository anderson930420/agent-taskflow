# HANDOFF — V1 Step 2 final gate: one real end-to-end run (Ruling 43)

**Status: BOTH PHASES COMPLETE — THE END-TO-END GATE PASSED.**
Every step ran against the real `git` and `gh`; every command exited 0.
Phase 1 stopped at the owner's merge; phase 2 ran the merge detection,
§36 verification and gated cleanup after it. See "Phase 2" at the end.

Branch: `task/v1-step2` · PR under review: #196 (stays a DRAFT)
Ruling: 43 — one real end-to-end run against a throwaway GitHub repository,
with the real `git` and `gh`, replacing a seventh review round.

---

## Tools and safety

| | |
| --- | --- |
| `git` | 2.43.0 |
| `gh` | 2.45.0 (2025-07-18 Ubuntu 2.45.0-1ubuntu0.3) |
| GitHub account | `anderson930420` |
| Python | 3.12.3 (`/home/ubuntu/agent-taskflow/.venv/bin/python`) |

**What this run was allowed to write to**, and nothing else:

- the throwaway GitHub repository created in step 1 below;
- scratch files under `/tmp/e2e-20260912-1055/` (a clone, task worktrees,
  artifacts and a scratch SQLite database).

`anderson930420/agent-taskflow` was **read-only** for the whole run. No push,
no PR create or edit, no merge there. `~/.agent-taskflow` and every
production database were never opened; the scratch database is
`/tmp/e2e-20260912-1055/state.db`. No environment variable pointed at a
production path (checked: no `AGENT_TASKFLOW*` variable is set).

---

## Step 1 — create the throwaway repository

    $ gh repo create agent-taskflow-e2e-20260912-1055 --private --add-readme \
        --description "Throwaway E2E target for agent-taskflow V1 Step 2 final gate (ruling 43). Safe to delete."
    https://github.com/anderson930420/agent-taskflow-e2e-20260912-1055
    exit=0

    $ gh repo view anderson930420/agent-taskflow-e2e-20260912-1055 --json nameWithOwner,url,isPrivate,defaultBranchRef
    {"defaultBranch":"main","isPrivate":true,
     "nameWithOwner":"anderson930420/agent-taskflow-e2e-20260912-1055",
     "url":"https://github.com/anderson930420/agent-taskflow-e2e-20260912-1055"}
    exit=0

| | |
| --- | --- |
| Full name | `anderson930420/agent-taskflow-e2e-20260912-1055` |
| URL | https://github.com/anderson930420/agent-taskflow-e2e-20260912-1055 |
| Private | yes |
| Default branch | `main` |

Cloned into the scratch area:

    $ git clone git@github.com:anderson930420/agent-taskflow-e2e-20260912-1055.git repo
    exit=0

Initial `main` SHA: `9a5f2583c5de904e69df0210d9718f5ac556e27d`.

---

## READ THIS FIRST — the outcome

**Both phases are complete and the gate PASSED.** Phase 1 (below) stopped
exactly where ruling 43 says it must, at the owner's merge. The owner then
merged PR #1 on GitHub, and **phase 2 — at the end of this file — ran the
merge detection, the §36 verification and the gated cleanup, all green.**

The phase-1 section below is kept as it was written at the time, so the
"waiting for you" wording there describes that moment, not now.

| | |
| --- | --- |
| **PR to merge** | **https://github.com/anderson930420/agent-taskflow-e2e-20260912-1055/pull/1** |
| State | OPEN, **still a DRAFT**, base `main`, head `126be95fc3eacdb17c7f1bbbc66230bf858795a8` |
| Ticket | `AT-0001`, status `waiting_for_review` |

That PR is a **draft**, so GitHub will not let it merge until you mark it
ready. The flow never marked it ready and never approved it, because nothing
in Step 2 does either. To merge it you have to mark it ready for review
first, then merge it yourself.

**What I will run after you merge, and nothing else:** the PR-outcome watcher
tick, then the §36 merge verification and gated cleanup —

    poll_pr_outcomes(WatcherRequest(repo=..., repo_path=..., db_path=..., confirm_poll=True))
    run_integration_cleanup(IntegrationCleanupRequest(..., confirm_cleanup=True))

Expected: the poll records `pr_merged=true` and the `merge_commit_sha`,
`merge_detected` is audited, then verification confirms the merge commit
contains the branch and only then does cleanup run. **I will not merge, will
not approve, and will not mark the PR ready** — those are yours.

---

## Step 2 — one Ticket through Step 1's real creation path

Scratch database: `/tmp/e2e-20260912-1055/state.db`, built with the real
migrations, in the order the repo requires:

    TaskMirrorStore(db).init_db()                exit=0
    migrate_ticket_fields(db)                    exit=0
    migrate_task_attempt_lifecycle(db)           exit=0
    migrate_runtime_progress(db)                 exit=0
    migrate_ticket_worktree_resources(db)        exit=0
    → 12 migrations recorded; task_pr_state, integration_queue,
      integration_locks, integration_validator_evidence,
      integration_review_evidence, integration_conflict_evidence all present.

The repository was **not** added to `config/projects.yaml`. A
`TicketRepository` was constructed for the clone and passed to `create_ticket`,
which bypasses the registry. That is why nothing could point at the real repo.

    create_ticket(
        TicketCreationRequest(repository="e2e-target", prompt="Add a trivial marker
            file so the end-to-end integration path can be proven", priority="normal"),
        store=TicketStore(/tmp/e2e-20260912-1055/state.db),
        repository=TicketRepository(repository="e2e-target",
            repo_path=/tmp/e2e-20260912-1055/repo,
            worktrees_dir=/tmp/e2e-20260912-1055/repo/.worktrees,
            artifacts_root=/tmp/e2e-20260912-1055/artifacts,
            base_branch="main", branch_prefix="task/",
            github_repo="anderson930420/agent-taskflow-e2e-20260912-1055"),
    )                                            exit=0

| | |
| --- | --- |
| Task ID | `AT-0001` |
| Initial status | `created` |
| Title (derived) | `Add a trivial marker file so the end-to-end integration path` |
| Branch (derived) | `task/AT-0001-add-a-trivial-marker-file-so-the-end-to` |
| Worktree | `/tmp/e2e-20260912-1055/repo/.worktrees/AT-0001` |
| Artifact dir | `/tmp/e2e-20260912-1055/artifacts/AT-0001` |

The worktree was then created by the **production** path, not by hand:

    ensure_ticket_worktree(db, "AT-0001", source="dispatcher")   exit=0
    → action=created
    → git worktree add .worktrees/AT-0001 -b task/AT-0001-... main
    → task_worktrees row: status=active, base_branch=main,
      base_sha=9a5f2583c5de904e69df0210d9718f5ac556e27d

### §32.1 after step 2 — nothing set yet

| field | value |
| --- | --- |
| pr_number | null |
| pr_url | null |
| pr_state | null |
| pr_merged | false |
| pr_head_sha | null |
| merge_commit_sha | null |
| review_decision | null |
| ci_status | null |
| integrated_base_sha | null |
| reintegration_count | 0 |
| reintegration_required | false |
| pr_last_polled_at | null |

---

## Step 3 — the implementation-phase commit

Made with the real `git`, inside the Ticket's own worktree, on the Ticket's
own branch:

    git add marker.txt                                          exit=0
    git commit -m "AT-0001: add a trivial marker file ..."       exit=0

Commit `177fcfdcf937cd44f41cbac9061a37bb352850b0`; worktree clean afterwards
(`git status --porcelain` empty). §32.1 is unchanged from step 2 — the
implementation phase writes none of those fields.

---

## Step 4 — INITIAL integration

The Ticket was parked at `ready_for_integration` first:

    store.update_task_status("AT-0001", "ready_for_integration",
        source="e2e-ruling-43", expected_current_status="created")   exit=0

**Finding, not a failure.** There is no automated producer of
`ready_for_integration` on this branch: the execution pipeline ends at
`waiting_approval`, and the only writers of `ready_for_integration` are Step
2's own watcher (the re-integration edge) and this kind of direct write, which
is what the repo's own Step 2 tests do. The compare-and-set
(`expected_current_status="created"`) was used so the write could not paper
over a wrong starting state. Whoever owns the hand-off from execution to
integration still has to build that edge; today it is manual.

    integrate_task(IntegrationRequest(
        task_key="AT-0001", repo="anderson930420/agent-taskflow-e2e-20260912-1055",
        db_path=/tmp/e2e-20260912-1055/state.db, target_branch="main", remote="origin",
        validator_specs=(IntegrationValidatorSpec(name="marker-present",
            command=("test", "-f", "marker.txt")),),
        owner="e2e-ruling-43", dry_run=False, confirm_integration=True, draft=True))
                                                                 exit=0

Result: `ok=True`, `status=integrated`, `mode=initial`,
`validators_passed=True`, `force_pushed=False`,
`final_task_status=waiting_for_review`.

The validator was a real one that can only pass inside the task worktree
(`test -f marker.txt`), not a `true` stub.

Every git command the run executed, in order:

    git fetch origin --prune
    git rev-parse origin/main
    git rev-list --count HEAD..origin/main
    git rebase origin/main                       ← §24 initial rebase
    git rev-parse --symbolic-full-name HEAD      ← Ruling 30 check, before validators
    git rev-parse --verify --quiet refs/heads/task/AT-0001-add-a-trivial-marker-file-so-the-end-to
    git rev-parse HEAD
    git diff --stat origin/main...HEAD
    git diff --name-only origin/main...HEAD
    git rev-parse --symbolic-full-name HEAD      ← Ruling 30 check, before the push
    git rev-parse --verify --quiet refs/heads/task/AT-0001-add-a-trivial-marker-file-so-the-end-to
    git rev-parse HEAD
    git push origin task/AT-0001-add-a-trivial-marker-file-so-the-end-to

Then, on GitHub:

    gh pr view 1 --repo anderson930420/agent-taskflow-e2e-20260912-1055 --json ...
    {"number":1, "state":"OPEN", "isDraft":true, "baseRefName":"main",
     "headRefName":"task/AT-0001-add-a-trivial-marker-file-so-the-end-to",
     "headRefOid":"177fcfdcf937cd44f41cbac9061a37bb352850b0"}       exit=0

**The PR was created as a DRAFT**, and the remote branch head equals the
recorded `pr_head_sha`.

### §32.1 after step 4 — ticket status `waiting_for_review`

| field | value |
| --- | --- |
| pr_number | 1 |
| pr_url | https://github.com/anderson930420/agent-taskflow-e2e-20260912-1055/pull/1 |
| pr_state | `open` |
| pr_merged | false |
| pr_head_sha | `177fcfdcf937cd44f41cbac9061a37bb352850b0` |
| merge_commit_sha | null |
| review_decision | null |
| ci_status | null |
| integrated_base_sha | `9a5f2583c5de904e69df0210d9718f5ac556e27d` |
| reintegration_count | 0 |
| reintegration_required | false |
| pr_last_polled_at | null |

`review_decision`, `ci_status` and `pr_last_polled_at` are null **by design**:
since Ruling 29d `create_pr` does not poll, so those three are written only by
a PR-outcome watcher tick, which ruling 43 does not ask for before the merge.

---

## Step 5 — make the branch stale

One unrelated commit, pushed to the **throwaway** `main` only:

    git remote get-url origin
    → git@github.com:anderson930420/agent-taskflow-e2e-20260912-1055.git
    git add unrelated.txt                        exit=0
    git commit -m "Unrelated change on main ..."  exit=0
    git push origin main                         exit=0
    → 9a5f258..27db287  main -> main

New target SHA: **`27db287775d01efd6b132f0ff4a3f03c64a9c9ef`**. §32.1 is
unchanged by this step; nothing in Taskflow ran.

---

## Step 6 — freshness detection, then RE-INTEGRATION

### 6a/6b — the freshness tick found it

    poll_target_freshness(WatcherRequest(..., confirm_poll=False))   exit=0
    → behind_count=1, stale=True, requeued=False, task_status=waiting_for_review

    poll_target_freshness(WatcherRequest(..., confirm_poll=True))    exit=0
    → behind_count=1, stale=True, requeued=True,
      previous_integrated_base_sha=9a5f258..., new_target_sha=27db287...

The dry tick reported and wrote nothing; the confirmed tick set
`reintegration_required`, moved the Ticket `waiting_for_review →
ready_for_integration` and enqueued it (`queue: [('AT-0001', '<throwaway>',
'integration_watcher')]`).

### §32.1 after the freshness tick — ticket status `ready_for_integration`

| field | value |
| --- | --- |
| pr_number | 1 |
| pr_url | https://github.com/anderson930420/agent-taskflow-e2e-20260912-1055/pull/1 |
| pr_state | `open` |
| pr_merged | false |
| pr_head_sha | `177fcfdcf937cd44f41cbac9061a37bb352850b0` |
| merge_commit_sha | null |
| review_decision | null |
| ci_status | null |
| integrated_base_sha | `9a5f2583c5de904e69df0210d9718f5ac556e27d` |
| reintegration_count | 0 |
| **reintegration_required** | **true** |
| pr_last_polled_at | null |

### 6c — the re-integration

    integrate_task(IntegrationRequest(..., trigger="e2e-target-advanced"))   exit=0

Result: `ok=True`, `status=integrated`, **`mode=reintegration`**,
`validators_passed=True`, `force_pushed=False`, `pr_number=1`,
`final_task_status=waiting_for_review`.

Every git command, in order — note `git merge`, **not** a rebase (§26):

    git fetch origin --prune
    git rev-parse origin/main
    git rev-list --count HEAD..origin/main
    git merge --no-edit origin/main              ← the latest target merged in
    git rev-parse --symbolic-full-name HEAD
    git rev-parse --verify --quiet refs/heads/task/AT-0001-add-a-trivial-marker-file-so-the-end-to
    git rev-parse HEAD
    git diff --stat origin/main...HEAD
    git diff --name-only origin/main...HEAD
    git rev-parse --symbolic-full-name HEAD
    git rev-parse --verify --quiet refs/heads/task/AT-0001-add-a-trivial-marker-file-so-the-end-to
    git rev-parse HEAD
    git push origin task/AT-0001-add-a-trivial-marker-file-so-the-end-to

**Proof the push was not forced.** The exact push argv, as recorded by the
run:

    ['git', 'push', 'origin', 'task/AT-0001-add-a-trivial-marker-file-so-the-end-to']

Four tokens: no `--force`, no `--force-with-lease`, no `+` refspec, no `:`
refspec, and the branch is the Ticket's own. `force_pushed=False` in the
result. Independently, the published branch only moved **forward**: the first
published head `177fcfd` is still an ancestor of the new head `126be95`, so no
history was rewritten.

**The same PR was updated through the REST PATCH** — Ruling 35's fix working
against real GitHub, which is what the round-5 note could not test. PR #1's
body on GitHub now reads:

    Ticket: AT-0001
    Add a trivial marker file so the end-to-end integration path

    Integrated against `origin/main` @ `27db287775d01efd6b132f0ff4a3f03c64a9c9ef`.

    ⚠ Re-integrated after target branch advanced.

    Previous base:
    9a5f2583c5de904e69df0210d9718f5ac556e27d

    Current base:
    27db287775d01efd6b132f0ff4a3f03c64a9c9ef

    Trigger:
    e2e-target-advanced

    Reviewer hints:
    ⚠ Re-integrated after target branch advanced.

No second PR was opened: `gh pr view 1` still reports `number=1`,
`isDraft=true`, head `126be95fc3eacdb17c7f1bbbc66230bf858795a8`, which equals
the remote branch head and the recorded `pr_head_sha`. The new `main` commit
`27db287` is an ancestor of the published head, so the branch really does
contain the latest target.

### §32.1 after the re-integration — ticket status `waiting_for_review`

| field | value |
| --- | --- |
| pr_number | 1 (unchanged — the same PR) |
| pr_url | https://github.com/anderson930420/agent-taskflow-e2e-20260912-1055/pull/1 |
| pr_state | `open` |
| pr_merged | false |
| **pr_head_sha** | **`126be95fc3eacdb17c7f1bbbc66230bf858795a8`** |
| merge_commit_sha | null |
| review_decision | null |
| ci_status | null |
| **integrated_base_sha** | **`27db287775d01efd6b132f0ff4a3f03c64a9c9ef`** |
| **reintegration_count** | **1** |
| reintegration_required | false (cleared) |
| pr_last_polled_at | null |

---

## Step 7 — stop

Stopped here, as instructed. Nothing was merged, approved or marked ready.

### Nothing failed

Every command above exited 0. No step had to be retried, no code was patched
during the run.

### Confirmation about the real repository

`anderson930420/agent-taskflow` was **read-only** throughout. The only
commands that named it were reads: `gh pr view 196 --json ...` and
`gh api user`. Its working tree is unchanged apart from this new handoff file,
`HEAD` is still `645ccbe`, `origin/task/v1-step2` is still `645ccbe`, and
`origin/main` is still `75bb6ba`. PR #196 is still OPEN and **still a DRAFT**
at `645ccbe`. Every write in this run went to
`anderson930420/agent-taskflow-e2e-20260912-1055` or to `/tmp`.

`~/.agent-taskflow` and every production database were never opened. Every
store was constructed with the explicit absolute scratch path, and
`IntegrationRequest(db_path=...)` was passed explicitly — that argument
defaults to the production database when omitted, which is the one real
foot-gun on this path.

### What this run proves, and what it does not

Proven end to end, against real `git` and real `gh`: Ticket creation, worktree
creation, initial rebase onto the target, the §29 validator gate, the
allowlisted non-forced push, draft PR creation, target-freshness detection,
the §26 merge-based re-integration, the REST PATCH update of the same PR, and
`reintegration_count`.

Not exercised by phase 1, because it stops before the merge: §35/§36 merge
detection and verification, and the gated cleanup. Those are phase 2, after
you merge. A conflict path was not exercised either — this run was designed
to be conflict-free.

---

# Phase 2 — after the owner merged PR #1

**THE END-TO-END GATE PASSED.** The owner merged PR #1 on the throwaway
repository through GitHub; the rest of the chain then ran here. Every command
exited 0, nothing failed, no code was patched.

The merge was a **merge commit**, so GitHub's merge result
`1d9a89d85dc959c65b3d6509c0040b3b697751b2` is a different commit from the
branch head `126be95fc3eacdb17c7f1bbbc66230bf858795a8`. That is what makes
this a real test of §36: verifying the task SHA would have been wrong.

State captured **before** anything in phase 2 ran, so the later removals are
provable:

    $ git worktree list
    /tmp/e2e-20260912-1055/repo                     27db287 [main]
    /tmp/e2e-20260912-1055/repo/.worktrees/AT-0001  126be95 [task/AT-0001-...]
    $ git branch --list
    * main
    + task/AT-0001-add-a-trivial-marker-file-so-the-end-to
    $ git ls-remote origin 'refs/heads/*'
    1d9a89d…  refs/heads/main
    126be95…  refs/heads/task/AT-0001-add-a-trivial-marker-file-so-the-end-to

The worktree, the local branch and the remote branch were all intact at this
point.

## Step 1 — poll the PR through the adapter

    $ (adapter) GitHubPrAdapter(<throwaway>).poll_pr(pr_number=1, cwd=<clone>)   exit=0

The only argv it issued, recorded by a wrapping runner:

    gh pr view 1 --repo anderson930420/agent-taskflow-e2e-20260912-1055 --json \
      number,url,state,isDraft,mergedAt,mergeCommit,headRefName,baseRefName,\
      headRefOid,reviewDecision,statusCheckRollup,reviews,title,body

What the adapter returned:

| | |
| --- | --- |
| `merged` | **True** |
| `state` | `closed` (gh said `MERGED`; §32.1 carries the merge fact in `pr_merged`) |
| `merged_at` | `2026-09-12T13:59:28Z` |
| `merge_commit_sha` | `1d9a89d85dc959c65b3d6509c0040b3b697751b2` |
| `head_sha` | `126be95fc3eacdb17c7f1bbbc66230bf858795a8` |
| `is_draft` | False (the owner marked it ready to merge it) |
| `review_decision` / `ci_status` | `none` / `none` |

### Ruling 29: `merged` was derived, not read

There is no `merged` field to read. Proven three ways:

- `"merged" in PR_VIEW_FIELDS` → **False**;
- the raw payload for that exact field list has keys
  `["baseRefName","body","headRefName","headRefOid","isDraft","mergeCommit","mergedAt","number","reviewDecision","reviews","state","statusCheckRollup","title","url"]`
  — **no `merged` key**;
- the derivation itself: `_is_merged("MERGED", None, None)` → True,
  `_is_merged("CLOSED", mergedAt, None)` → True,
  `_is_merged("CLOSED", None, mergeCommit)` → True,
  `_is_merged("CLOSED", None, None)` → **False**.

This PR satisfied all three signals at once. Had the adapter still asked for
`merged`, real gh would have failed the poll outright — which is the round-4
defect Ruling 29 fixed.

Then the watcher tick recorded it:

    poll_pr_outcomes(WatcherRequest(..., confirm_poll=True))      exit=0
    → merged=True, merge_commit_sha=1d9a89d…, pr_state='closed',
      applied_transition=None, cleanup_performed=False

**Detection alone changed nothing about the Ticket** — it stayed
`waiting_for_review`, with no transition applied and no cleanup. §35 detects;
§36 verification and §37 cleanup are separate, which is exactly the gate.

### §32.1 after step 1 — ticket status `waiting_for_review`

| field | value |
| --- | --- |
| pr_number | 1 |
| pr_url | https://github.com/anderson930420/agent-taskflow-e2e-20260912-1055/pull/1 |
| **pr_state** | **`closed`** |
| **pr_merged** | **true** |
| pr_head_sha | `126be95fc3eacdb17c7f1bbbc66230bf858795a8` |
| **merge_commit_sha** | **`1d9a89d85dc959c65b3d6509c0040b3b697751b2`** |
| review_decision | `none` |
| ci_status | `none` |
| integrated_base_sha | `27db287775d01efd6b132f0ff4a3f03c64a9c9ef` |
| reintegration_count | 1 |
| reintegration_required | false |
| **pr_last_polled_at** | **`2026-09-12T14:01:02Z`** |

## Step 2 — fetch the latest target and verify the merge (§35, §36, §36.1)

    verify_merge(MergeVerificationRequest(task_key="AT-0001",
        worktree_path=<clone>/.worktrees/AT-0001, remote="origin",
        target_branch="main", pr_merged=True,
        merge_commit_sha="1d9a89d85dc959c65b3d6509c0040b3b697751b2"))    exit=0

The three git commands it ran, in order:

    git fetch origin --prune
    git rev-parse origin/main
    git merge-base --is-ancestor 1d9a89d85dc959c65b3d6509c0040b3b697751b2 origin/main

Verdict: `verified=True`, `contained_in_target=True`, `reasons=()`,
`target_sha=1d9a89d85dc959c65b3d6509c0040b3b697751b2`,
`verified_at=2026-09-12T14:01:16Z`.

**The SHA verified was the PR's merge result, not the task SHA.** The
containment test names `1d9a89d…`, GitHub's merge commit. The result carries
`original_task_sha_used=False`, and neither the original task commit
`177fcfd…` nor the branch head `126be95…` appears in any verification
command. §32.1 is unchanged by this step — verification writes none of the
twelve fields.

## Step 3 — cleanup, gated on that verification (§37)

    run_integration_cleanup(IntegrationCleanupRequest(task_key="AT-0001",
        repo=<throwaway>, repo_path=<clone>, target_branch="main",
        remote="origin", db_path=<scratch>, confirm_cleanup=True))       exit=0

Result: `ok=True`, `status=cleaned`, **`route=verified_merge`**,
`merge_verified=True`, `verification_reasons=()`, `force_pushed=False`.

Cleanup re-ran the verification itself before touching anything — its own
first three git commands are the same fetch/rev-parse/`--is-ancestor` trio —
and only then removed things:

    git fetch origin --prune
    git rev-parse origin/main
    git merge-base --is-ancestor 1d9a89d85dc959c65b3d6509c0040b3b697751b2 origin/main
    git worktree remove /tmp/e2e-20260912-1055/repo/.worktrees/AT-0001
    git branch -D task/AT-0001-add-a-trivial-marker-file-so-the-end-to

**Removed:** the worktree (`worktree_removed=True`) and the local branch
(`local_branch_deleted=True`).

**Kept:** the remote branch (`remote_branch_deleted=False`) and all evidence
(`evidence_archived=True`).

Confirmed on disk afterwards:

    $ git worktree list        → only /tmp/e2e-20260912-1055/repo [main]
    $ git branch --list        → only main   (task branch gone)
    $ ls .worktrees/AT-0001    → removed
    $ git ls-remote origin 'refs/heads/*'
    1d9a89d…  refs/heads/main
    126be95…  refs/heads/task/AT-0001-add-a-trivial-marker-file-so-the-end-to   ← SURVIVES

**Ruling 5 held: §37 remote-branch cleanup stays off in V1.** The remote task
branch is still there, and no `git push --delete` was issued — the push
allowlist would have refused it. `delete_remote_branch` was left at its
default; the request refuses `True` up front.

Evidence kept in `artifacts/AT-0001/integration/`: `cleanup-f8427971586a.json`,
two `integration-*.json` and two `validators-*.json` — one pair per
integration run. `merged=False` in the cleanup result is the safety flag
meaning *Taskflow did not merge anything*; the human did.

### §32.1 after step 3 — unchanged by cleanup, ticket status `cleaned`

Cleanup writes no §32.1 field. All twelve are exactly as after step 1.

## Step 4 — the Ticket reaches `completed`

    persisted status: cleaned
    display status  : completed      (§12 vocabulary, via status_vocab)

The full audit trail for the whole run, in order — 19 events, no gaps:

    1. created                        [mission_control]
    2. worktree_recorded              [dispatcher]
    3. status_changed -> ready_for_integration   [e2e-ruling-43]
    4. status_changed -> integrating             [integration_controller]
    5. integration_started (initial)             [integration_controller]
    6. status_changed -> waiting_for_review      [integration_controller]
    7. integration_completed (initial)           [integration_controller]
    8. status_changed -> ready_for_integration   [integration_watcher]
    9. reintegration_required  behind_count=1    [integration_watcher]
   10. integration_queued                        [integration_watcher]
   11. status_changed -> integrating             [integration_controller]
   12. integration_started (reintegration)       [integration_controller]
   13. status_changed -> waiting_for_review      [integration_controller]
   14. integration_completed (reintegration)     [integration_controller]
   15. pr_state_polled  PR #1                    [integration_watcher]
   16. merge_detected   PR #1 reported merged    [integration_watcher]
   17. merge_verified   1d9a89d… in target       [integration_cleanup]
   18. status_changed -> cleaned                 [integration_cleanup]
   19. integration_cleanup_completed             [integration_cleanup]

Note the ordering that matters: **16 detect → 17 verify → 18 complete**. The
Ticket was never marked complete on detection alone.

## Step 5 — the total

### Final §32.1, all twelve fields

| field | final value |
| --- | --- |
| pr_number | 1 |
| pr_url | https://github.com/anderson930420/agent-taskflow-e2e-20260912-1055/pull/1 |
| pr_state | `closed` |
| pr_merged | true |
| pr_head_sha | `126be95fc3eacdb17c7f1bbbc66230bf858795a8` |
| merge_commit_sha | `1d9a89d85dc959c65b3d6509c0040b3b697751b2` |
| review_decision | `none` |
| ci_status | `none` |
| integrated_base_sha | `27db287775d01efd6b132f0ff4a3f03c64a9c9ef` |
| reintegration_count | 1 |
| reintegration_required | false |
| pr_last_polled_at | `2026-09-12T14:01:02Z` |

**Final Ticket status: `cleaned` (persisted) = `completed` (§12 display).**

### Did the §43 items 28-33 chain hold?

**Yes — end to end, against real `git` 2.43.0 and real `gh` 2.45.0, the chain
held: a human merged on GitHub, Taskflow detected the merge without ever
merging anything itself, verified GitHub's own merge commit was contained in
the target before touching a file, and only then removed the worktree and
local branch, left the remote branch alone, kept every piece of evidence and
marked the Ticket completed.**

## The throwaway repository — left in place for inspection

| | |
| --- | --- |
| Repository | `anderson930420/agent-taskflow-e2e-20260912-1055` (private) |
| URL | https://github.com/anderson930420/agent-taskflow-e2e-20260912-1055 |
| PR | #1, MERGED, merge commit `1d9a89d85dc959c65b3d6509c0040b3b697751b2` |
| Scratch tree | `/tmp/e2e-20260912-1055/` (clone, scratch DB, artifacts) |

**Not deleted**, as instructed. The remote task branch is still there too, so
the whole history is inspectable.

## Safety, phase 2

`anderson930420/agent-taskflow` stayed read-only: the only commands naming it
were `gh pr view 196` and `git status`/`rev-parse` on the local worktree. Its
`HEAD` is `fb2673f`, and **PR #196 is still OPEN and still a DRAFT** — the
owner merges that one. `~/.agent-taskflow` and every production database were
never opened; every store was constructed with the explicit scratch path.
Nothing was merged by me anywhere, nothing was force-pushed, nothing was
rebased.
