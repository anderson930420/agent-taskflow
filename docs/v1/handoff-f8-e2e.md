# HANDOFF — V1 FOLLOWUPS F8 final gate: one real end-to-end run (rulings 53/56)

## READ THIS FIRST — the outcome

**BOTH PHASES ARE COMPLETE AND THE F8 END-TO-END GATE PASSED.** Every step ran
against the real `git` and `gh`; every command exited 0. Nothing failed, so
nothing was patched and nothing was rerun.

Phase 1 (below) stopped exactly where ruling 56 says it must, at the owner's
merge. The owner then merged PR #1, and **phase 2 — at the end of this file —
ran the merge detection, the §36 verification and the gated cleanup, all green.**
The Ticket reached persisted `cleaned` = §12 `completed`.

**The phase-1 section below is kept as it was written at the time**, so its
"waiting for you" wording describes that moment, not now.

| | |
| --- | --- |
| Throwaway repo | **https://github.com/anderson930420/agent-taskflow-e2e-f8-20260912-1644** (private, left in place) |
| PR | https://github.com/anderson930420/agent-taskflow-e2e-f8-20260912-1644/pull/1 — **MERGED** by the owner |
| Final Ticket state | `AT-0001` → persisted `cleaned` = **`completed`** |
| Hand-written statuses | **NONE, in either phase** |
| One manual step remaining | the operator's `confirm_integration` trigger on `integrate_task` |

---

## Phase 1, as written at the time

| | |
| --- | --- |
| **PR for you to merge** | **https://github.com/anderson930420/agent-taskflow-e2e-f8-20260912-1644/pull/1** |
| State | OPEN, **still a DRAFT**, base `main`, head `53564f650054da7e513620db958c01a8e14f3a49` |
| Ticket | `AT-0001`, status `waiting_for_review`, `reintegration_count` 1 |
| Throwaway repo | `anderson930420/agent-taskflow-e2e-f8-20260912-1644` (private) |

That PR is a **draft**, so GitHub will not let it merge until you mark it ready.
The flow never marked it ready and never approved it, because nothing in
Taskflow does either. To merge it you must mark it ready for review yourself,
then merge it yourself. **I will not merge, will not approve, and will not mark
it ready.**

**What I will run after you merge, and nothing else** — phase 2:

```python
poll_pr_outcomes(WatcherRequest(repo=..., repo_path=..., db_path=..., confirm_poll=True))
run_integration_cleanup(IntegrationCleanupRequest(..., confirm_cleanup=True))
```

Expected: the poll records `pr_merged=true` and the `merge_commit_sha`, §36
verification confirms that commit is contained in `main`'s history, and only
then does the gated cleanup run and the Ticket reach `completed`.

**PR #203 on `anderson930420/agent-taskflow` stays a DRAFT and was not touched.**

---

## The headline result — what this run proves that ruling 43's did not

Ruling 43's run had to park the Ticket by hand to get it into the queue:

```python
# ruling 43, docs/v1/handoff-step2-e2e.md step 4
store.update_task_status("AT-0001", "ready_for_integration",
    source="e2e-ruling-43", expected_current_status="created")   # ← GONE
```

**That step is gone.** In this run the Ticket reached `ready_for_integration` by
itself, out of the real Dispatcher, and enqueued itself.

> **Was any compare-and-set or hand-written status needed? No. None. Not one.**
> The only status writes in the entire database came from `dispatcher:<attempt>`
> and, later, from Step 2's own watcher and controller. No script in this run
> called `update_task_status`, at all.

---

## Tools and safety

| | |
| --- | --- |
| `git` | `2.43.0` |
| `gh` | `2.45.0 (2025-07-18 Ubuntu 2.45.0-1ubuntu0.3)` |
| GitHub account | `anderson930420` |
| Python | `/home/ubuntu/agent-taskflow/.venv/bin/python` |
| Run started (UTC) | `2026-09-12 16:44` |

**What this run wrote to, and nothing else:**

- the throwaway repository created in step 1;
- scratch files under `/tmp/e2e-f8-20260912-1644/` (a clone, the task worktree,
  artifacts and a scratch SQLite database) and a few `/tmp/e2e-f8-*.py` scripts.

**`anderson930420/agent-taskflow` was read-only for the whole run.** No push, no
PR create or edit, no merge there. The only `gh` calls against it were the
`gh pr view 203` checks in the previous turn; this run made none. The only
pushes anywhere were to the throwaway repo (step 6, step 7, step 8).

**`~/.agent-taskflow` and every production database were never opened.** Proven
by construction, not by assertion: every entry point in this run took an
explicit `db_path=/tmp/e2e-f8-20260912-1644/state.db`, and no `AGENT_TASKFLOW*`
environment variable is set —

```text
$ env | grep -i "AGENT_TASKFLOW" || echo "no AGENT_TASKFLOW* env var set"
no AGENT_TASKFLOW* env var set
exit=0
```

**`agent-taskflow-e2e-20260912-1055` (ruling 43's repo) was never touched.** A
new repository was created for this run.

No scheduler tick and no scheduler entry point was run.

---

## Step 1 — create the throwaway repository

```text
$ gh repo create agent-taskflow-e2e-f8-20260912-1644 --private --add-readme \
    --description "Throwaway E2E target for agent-taskflow V1 FOLLOWUPS F8 final gate (rulings 53/56). Safe to delete."
https://github.com/anderson930420/agent-taskflow-e2e-f8-20260912-1644
exit=0

$ gh repo view anderson930420/agent-taskflow-e2e-f8-20260912-1644 \
    --json nameWithOwner,url,isPrivate,defaultBranchRef
{"defaultBranchRef":{"name":"main"},"isPrivate":true,
 "nameWithOwner":"anderson930420/agent-taskflow-e2e-f8-20260912-1644",
 "url":"https://github.com/anderson930420/agent-taskflow-e2e-f8-20260912-1644"}
exit=0

$ git clone git@github.com:anderson930420/agent-taskflow-e2e-f8-20260912-1644.git repo
exit=0
$ git rev-parse HEAD
76f142bad636177aea666f426be1949cb2ca1e5d
$ git remote get-url origin
git@github.com:anderson930420/agent-taskflow-e2e-f8-20260912-1644.git
```

| | |
| --- | --- |
| Full name | `anderson930420/agent-taskflow-e2e-f8-20260912-1644` |
| URL | https://github.com/anderson930420/agent-taskflow-e2e-f8-20260912-1644 |
| Private | yes |
| Default branch | `main` |
| Initial `main` SHA | `76f142bad636177aea666f426be1949cb2ca1e5d` |

---

## Step 2 — one Ticket through Step 1's real creation path

Scratch database `/tmp/e2e-f8-20260912-1644/state.db`, built with the real
migrations:

```text
TaskMirrorStore.init_db                ok
migrate_ticket_fields                  ok
migrate_task_attempt_lifecycle         ok
migrate_runtime_progress               ok
migrate_ticket_worktree_resources      ok
IntegrationStore.init_db               ok
exit=0

integration tables present: ['integration_conflict_evidence', 'integration_locks',
  'integration_queue', 'integration_review_evidence',
  'integration_validator_evidence', 'task_pr_state']
```

The repository was **not** added to `config/projects.yaml`. A `TicketRepository`
was constructed for the clone and passed straight to `create_ticket`, which
bypasses the registry — that is why nothing in this run could point at the real
repo.

```python
create_ticket(
    TicketCreationRequest(
        repository="e2e-f8-target",
        prompt="Add a trivial marker file so the F8 end-to-end path can be proven",
        priority="normal"),
    store=TicketStore("/tmp/e2e-f8-20260912-1644/state.db"),
    repository=TicketRepository(
        repository="e2e-f8-target",
        repo_path="/tmp/e2e-f8-20260912-1644/repo",
        worktrees_dir="/tmp/e2e-f8-20260912-1644/repo/.worktrees",
        artifacts_root="/tmp/e2e-f8-20260912-1644/artifacts",
        base_branch="main", branch_prefix="task/",
        github_repo="anderson930420/agent-taskflow-e2e-f8-20260912-1644"),
)                                                                     exit=0
```

| | |
| --- | --- |
| Task ID | `AT-0001` |
| Initial status | `created` |
| Title (derived) | `Add a trivial marker file so the F8 end-to-end path can be p` |
| Branch (derived) | `task/AT-0001-add-a-trivial-marker-file-so-the-f8-end` |
| Worktree | `/tmp/e2e-f8-20260912-1644/repo/.worktrees/AT-0001` |
| Artifact dir | `/tmp/e2e-f8-20260912-1644/artifacts/AT-0001` |
| `github_repo` | `anderson930420/agent-taskflow-e2e-f8-20260912-1644` |

### §32.1 after step 2 — ticket status `created`

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

## Step 3 — the implementation phase, made by the system

Unlike ruling 43's run — which made the commit by hand with `git` — this run put
the commit inside the **real Dispatcher**, driven by the **real `ShellExecutor`**
from the production registry, with **two real production validators**. Nothing
here is a test double.

```python
executor  = build_shell_executor(["bash", "/tmp/e2e-f8-implement.sh"], name="shell")
validators = {"policy":        get_validator("policy"),          # PolicyCheckValidator
              "changed-files": ChangedFilesValidator(allow_no_changes=True)}
Dispatcher(db_path=DB, executor_registry={"shell": executor},
           validator_registry=validators,
           validators=("policy", "changed-files"),
           default_executor="shell").dispatch_task("AT-0001")
```

The executor's argv, run by the dispatcher inside the Ticket's own worktree —
it writes one file and commits it, and never pushes, merges, approves or cleans
up:

```bash
#!/usr/bin/env bash
set -euo pipefail
printf 'F8 end-to-end marker\n' > marker.txt
git add marker.txt
git commit -m "AT-0001: add a trivial marker file for the F8 end-to-end gate"
```

The dispatcher prepared the worktree itself (`_prepare_ticket_worktree` →
`ensure_ticket_worktree`); it was not created by hand.

```text
$ git rev-parse --abbrev-ref HEAD      (exit=0)
task/AT-0001-add-a-trivial-marker-file-so-the-f8-end
$ git log --oneline -2                 (exit=0)
d17b2b8 AT-0001: add a trivial marker file for the F8 end-to-end gate
76f142b Initial commit
$ git rev-parse HEAD                   (exit=0)
d17b2b8165aaf7089247d40ae27838c633b1248a
$ git status --porcelain               (exit=0)
(empty)
```

Dispatcher result:

```json
{
  "task_key": "AT-0001",
  "status": "ready_for_integration",
  "summary": "Task dispatched successfully and is ready for integration.",
  "executor_status": "completed",
  "validator_statuses": {"policy": "passed", "changed-files": "passed"},
  "blocked_reason": null
}
exit=0
```

`policy` is `PolicyCheckValidator`: it checks the mission contract, that
`human_approval_required` is true, that `forbidden_actions` contains
`approve` / `push` / `merge` / `cleanup` / `self_approve` / `force_push`, and it
scans the executor's artifacts for evidence of forbidden actions. It passed. It
is not a `true` stub.

---

## Step 4 — THE F8 ASSERTION

### 4a — the audit events, in order

Every `status_changed` event for `AT-0001`, verbatim, in id order:

```text
 id=3    2026-09-12T16:45:53Z  preparing              source=dispatcher:c8fc68c9a33448a0a0458a618eab8eb0  Runtime admission claimed task
 id=5    2026-09-12T16:45:53Z  implementing           source=dispatcher:c8fc68c9a33448a0a0458a618eab8eb0  Dispatcher running executor shell
 id=8    2026-09-12T16:45:54Z  validating             source=dispatcher:c8fc68c9a33448a0a0458a618eab8eb0  Dispatcher running validators
 id=11   2026-09-12T16:45:54Z  ready_for_integration  source=dispatcher:c8fc68c9a33448a0a0458a618eab8eb0  Runtime admission released attempt: runtime_ready_for_integration

final persisted status: ready_for_integration
```

Read that list carefully. It is the whole proof:

- `created → preparing → implementing → validating → ready_for_integration`,
  with **`validating` immediately before `ready_for_integration`**;
- **`waiting_approval` never appears.** Neither does `accepted`;
- **every one of the four writes has `source=dispatcher:<attempt-id>`.** There
  is no `source=e2e-*` write anywhere, because this run never wrote a status;
- the last one carries `runtime_ready_for_integration` — F8's new reason code —
  which is the runtime *releasing the claim and lease* on the success terminal.
  That is the landmine described in `docs/v1/handoff-f8.md` §3, working.

The `approvals` table does not exist in this database and no approval decision
was recorded.

### 4b — exactly once in its repo's queue

```text
task_key=AT-0001  repo=anderson930420/agent-taskflow-e2e-f8-20260912-1644
enqueued_at=2026-09-12T16:45:54Z  seq=1  source=integration_handoff  priority=normal
queue length: 1
```

The handoff's own §44 audit event:

```text
id=12  source=integration_handoff  created_at=2026-09-12T16:45:54Z
message: Ticket AT-0001 handed off to the anderson930420/agent-taskflow-e2e-f8-20260912-1644 integration_queue
payload: {"enqueued_at": "2026-09-12T16:45:54Z", "priority": "normal",
          "queue": "integration_queue",
          "repo": "anderson930420/agent-taskflow-e2e-f8-20260912-1644",
          "task_key": "AT-0001", "task_status": "ready_for_integration"}
```

### 4c — §22.1: the FIFO key is the moment of entry, not the enqueue call's clock

Equality alone would be weak here, because both timestamps are second-
granularity and the handoff runs milliseconds after the status write. So the
mechanism was tested directly: 3 seconds were allowed to pass, then the value
the handoff uses was read back and compared with the wall clock.

```text
wall clock now                          : 2026-09-12T16:46:19Z
_entered_status_at(ready_for_integration): 2026-09-12T16:45:54Z
queue entry enqueued_at                  : 2026-09-12T16:45:54Z
read_back == enqueued_at : True
read_back <  now         : True   <- the key is NOT the clock
```

The queue's ordering timestamp is the value carried by the
`status_changed → ready_for_integration` audit event, 25 seconds older than
"now" at the time of the check. It is read back from the audit trail, not
sampled from the clock.

### 4d — idempotent

A second `handoff_completed_implementation(...)` call, which is what a second
scheduler tick would do:

```text
result.status      = already_queued
result.enqueued_at = 2026-09-12T16:45:54Z
queue length       = 1
enqueued_at        = 2026-09-12T16:45:54Z (unchanged: True)
ticket status      = ready_for_integration (unchanged)
status events      = 4 (unchanged)
```

### §32.1 after step 4 — ticket status `ready_for_integration`

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

All twelve still unset — the implementation phase writes none of them, exactly
as §32.1 requires (only Step 2's watcher and controller own those fields).

---

## Step 5 — the remaining human step, recorded honestly

**This is a manual step and it is the one thing F8 does *not* close.**

`integrate_task` has no non-test caller anywhere in the repository, and
`IntegrationRequest.confirm_integration` defaults to `False`. Nothing in
Taskflow reads the integration queue and acts on it. So after step 4 the Ticket
sat in its queue and **stopped there**, and an operator — me, explicitly — had
to trigger integration.

This is exactly what `docs/v1/handoff-f8.md` §1 (fact 1) reports, now confirmed
against a real repository rather than by grep.

The exact call I ran, in full:

```python
integrate_task(IntegrationRequest(
    task_key='AT-0001',
    repo='anderson930420/agent-taskflow-e2e-f8-20260912-1644',
    db_path=PosixPath('/tmp/e2e-f8-20260912-1644/state.db'),
    target_branch='main',
    remote='origin',
    owner='e2e-f8-operator',
    dry_run=False,
    confirm_integration=True,        # ← the operator gate. Nothing sets this for me.
    draft=True,
    validator_specs=(IntegrationValidatorSpec(name='marker-present',
                      command=('test','-f','marker.txt')),),
))
```

The integration validator is a real one that can only pass inside the task
worktree (`test -f marker.txt`), not a `true` stub.

---

## Step 6 — initial integration

```json
{
  "ok": true, "status": "integrated", "mode": "initial",
  "final_task_status": "waiting_for_review",
  "validators_passed": true,
  "force_pushed": false, "merged": false, "cleanup_performed": false,
  "lock_held_after_return": false,
  "pr_number": 1,
  "pr_url": "https://github.com/anderson930420/agent-taskflow-e2e-f8-20260912-1644/pull/1",
  "integrated_base_sha": "76f142bad636177aea666f426be1949cb2ca1e5d",
  "reintegration_count": 0, "behind_count": 0,
  "summary": "Integrated against origin/main@76f142bad636; PR #1 is awaiting human review"
}
exit=0
```

Every git command the controller ran, in order — note `git rebase`, which §24
allows because the branch was not yet published:

```text
git fetch origin --prune
git rev-parse origin/main
git rev-list --count HEAD..origin/main
git rebase origin/main                       ← §24 initial rebase
git rev-parse --symbolic-full-name HEAD
git rev-parse --verify --quiet refs/heads/task/AT-0001-add-a-trivial-marker-file-so-the-f8-end
git rev-parse HEAD
git diff --stat origin/main...HEAD
git diff --name-only origin/main...HEAD
git rev-parse --symbolic-full-name HEAD
git rev-parse --verify --quiet refs/heads/task/AT-0001-add-a-trivial-marker-file-so-the-f8-end
git rev-parse HEAD
git push origin task/AT-0001-add-a-trivial-marker-file-so-the-f8-end

commands containing a force flag: NONE
```

The queue is empty afterwards — the controller dequeued the entry it consumed.

On GitHub:

```text
$ gh pr view 1 --repo anderson930420/agent-taskflow-e2e-f8-20260912-1644 --json ...
{"baseRefName":"main","headRefName":"task/AT-0001-add-a-trivial-marker-file-so-the-f8-end",
 "headRefOid":"d17b2b8165aaf7089247d40ae27838c633b1248a","isDraft":true,
 "mergeable":"MERGEABLE","number":1,"state":"OPEN",
 "url":"https://github.com/anderson930420/agent-taskflow-e2e-f8-20260912-1644/pull/1"}
exit=0
```

**The PR was created as a DRAFT**, and the remote branch head equals the
recorded `pr_head_sha`.

### §32.1 after step 6 — ticket status `waiting_for_review`

| field | value |
| --- | --- |
| pr_number | 1 |
| pr_url | `https://github.com/anderson930420/agent-taskflow-e2e-f8-20260912-1644/pull/1` |
| pr_state | `open` |
| pr_merged | false |
| pr_head_sha | `d17b2b8165aaf7089247d40ae27838c633b1248a` |
| merge_commit_sha | null |
| review_decision | null |
| ci_status | null |
| integrated_base_sha | `76f142bad636177aea666f426be1949cb2ca1e5d` |
| reintegration_count | 0 |
| reintegration_required | false |
| pr_last_polled_at | null |

`review_decision`, `ci_status` and `pr_last_polled_at` are null **by design**:
`create_pr` does not poll, so those three are written only by a PR-outcome
watcher tick, which this phase does not run.

---

## Step 7 — make the branch stale

One unrelated commit, pushed to the **throwaway** `main` only. The remote was
checked before the push:

```text
$ git remote get-url origin
git@github.com:anderson930420/agent-taskflow-e2e-f8-20260912-1644.git
$ git checkout main                    → Already on 'main'
$ git pull --ff-only                   → Already up to date.
$ git add unrelated.txt                exit=0
$ git commit -m "Unrelated change on main to make the F8 task branch stale"
[main 7b4a57e] 1 file changed, 1 insertion(+)
$ git push origin main                 exit=0
   76f142b..7b4a57e  main -> main
$ git rev-parse HEAD
7b4a57ea8b40a42489c7ebc805f9f4a096d6d545
```

**New target SHA: `7b4a57ea8b40a42489c7ebc805f9f4a096d6d545`.**

§32.1 is unchanged by this step; nothing in Taskflow ran.

---

## Step 8 — freshness detection, then RE-INTEGRATION

### 8a — the dry tick reports and writes nothing

```json
{"task_key": "AT-0001", "behind_count": 1, "stale": true,
 "previous_integrated_base_sha": "76f142bad636177aea666f426be1949cb2ca1e5d",
 "new_target_sha": "7b4a57ea8b40a42489c7ebc805f9f4a096d6d545",
 "requeued": false, "task_status": "waiting_for_review", "deferred": false}
exit=0
ticket status after dry tick: waiting_for_review
```

### 8b — the confirmed tick requeues it (§25.1)

```json
{"task_key": "AT-0001", "behind_count": 1, "stale": true,
 "previous_integrated_base_sha": "76f142bad636177aea666f426be1949cb2ca1e5d",
 "new_target_sha": "7b4a57ea8b40a42489c7ebc805f9f4a096d6d545",
 "requeued": true, "task_status": "waiting_for_review", "deferred": false}
exit=0
ticket status after confirmed tick: ready_for_integration
queue: [('AT-0001', 'anderson930420/agent-taskflow-e2e-f8-20260912-1644',
         'integration_watcher', '2026-09-12T16:47:33Z')]
```

`behind_count > 0` was found, `needs_review → ready_for_integration` happened,
and the entry was re-queued with `source=integration_watcher` — Step 2's own
producer, distinct from F8's `integration_handoff` in step 4.

### §32.1 after the freshness tick — ticket status `ready_for_integration`

| field | value |
| --- | --- |
| pr_number | 1 |
| pr_url | `https://github.com/anderson930420/agent-taskflow-e2e-f8-20260912-1644/pull/1` |
| pr_state | `open` |
| pr_merged | false |
| pr_head_sha | `d17b2b8165aaf7089247d40ae27838c633b1248a` |
| merge_commit_sha | null |
| review_decision | null |
| ci_status | null |
| integrated_base_sha | `76f142bad636177aea666f426be1949cb2ca1e5d` |
| reintegration_count | 0 |
| **reintegration_required** | **true** |
| pr_last_polled_at | null |

### 8c — the re-integration (operator-triggered again)

```json
{
  "ok": true, "status": "integrated", "mode": "reintegration",
  "final_task_status": "waiting_for_review",
  "validators_passed": true,
  "force_pushed": false, "merged": false, "cleanup_performed": false,
  "pr_number": 1,
  "pr_url": "https://github.com/anderson930420/agent-taskflow-e2e-f8-20260912-1644/pull/1",
  "integrated_base_sha": "7b4a57ea8b40a42489c7ebc805f9f4a096d6d545",
  "previous_integrated_base_sha": "76f142bad636177aea666f426be1949cb2ca1e5d",
  "reintegration_count": 1, "behind_count": 1,
  "conflict_detected": false,
  "summary": "Integrated against origin/main@7b4a57ea8b40; PR #1 is awaiting human review"
}
exit=0
```

Every git command, in order — note `git merge`, **not** a rebase (§26):

```text
git fetch origin --prune
git rev-parse origin/main
git rev-list --count HEAD..origin/main
git merge --no-edit origin/main              ← the latest target merged in
git rev-parse --symbolic-full-name HEAD
git rev-parse --verify --quiet refs/heads/task/AT-0001-add-a-trivial-marker-file-so-the-f8-end
git rev-parse HEAD
git diff --stat origin/main...HEAD
git diff --name-only origin/main...HEAD
git rev-parse --symbolic-full-name HEAD
git rev-parse --verify --quiet refs/heads/task/AT-0001-add-a-trivial-marker-file-so-the-f8-end
git rev-parse HEAD
git push origin task/AT-0001-add-a-trivial-marker-file-so-the-f8-end
```

**The exact push argv, as proof there is no force flag:**

```python
('git', 'push', 'origin', 'task/AT-0001-add-a-trivial-marker-file-so-the-f8-end')
```

```text
commands with a force flag (-f / --force / --force-with-lease / --force-if-includes): NONE
```

Reviewer hint published: `Re-integrated after target branch advanced.`

The resulting branch, showing the merge rather than a rewrite:

```text
*   53564f6 Merge remote-tracking branch 'origin/main' into task/AT-0001-add-a-trivial-...
|\
| * 7b4a57e Unrelated change on main to make the F8 task branch stale
* | d17b2b8 AT-0001: add a trivial marker file for the F8 end-to-end gate
|/
* 76f142b Initial commit

$ git diff --name-only origin/main...HEAD
marker.txt
$ git ls-remote origin refs/heads/task/AT-0001-add-a-trivial-marker-file-so-the-f8-end
53564f650054da7e513620db958c01a8e14f3a49
```

`d17b2b8` is still there. Nothing was rewritten, so review history on the PR
survives.

**The SAME PR was updated — there is exactly one PR in the repository:**

```text
$ gh pr list --repo anderson930420/agent-taskflow-e2e-f8-20260912-1644 --state all
[{"isDraft":true,"number":1,"title":"Add a trivial marker file so the F8 end-to-end path can be p"}]

$ gh pr view 1 --repo ... --json number,state,isDraft,baseRefName,headRefOid,commits
{"baseRefName":"main","commitCount":2,
 "headRefOid":"53564f650054da7e513620db958c01a8e14f3a49",
 "isDraft":true,"number":1,"state":"OPEN"}
exit=0
```

### §32.1 after step 8 — ticket status `waiting_for_review`

| field | value |
| --- | --- |
| pr_number | 1 |
| pr_url | `https://github.com/anderson930420/agent-taskflow-e2e-f8-20260912-1644/pull/1` |
| pr_state | `open` |
| pr_merged | false |
| pr_head_sha | `53564f650054da7e513620db958c01a8e14f3a49` |
| merge_commit_sha | null |
| review_decision | null |
| ci_status | null |
| **integrated_base_sha** | **`7b4a57ea8b40a42489c7ebc805f9f4a096d6d545`** |
| **reintegration_count** | **1** |
| reintegration_required | false |
| pr_last_polled_at | null |

`pr_head_sha` on the record equals the remote branch head and the PR's
`headRefOid`. `integrated_base_sha` advanced to the new target.
`reintegration_required` was cleared by the successful re-integration.

---

## Step 9 — STOP

This is where phase 1 ends, by ruling. **Waiting for you to merge PR #1** on the
throwaway repository (mark it ready for review first — it is a draft).

Nothing further was run. The Ticket is at `waiting_for_review`, which is
correct: §33.1 keeps an approved-but-unmerged Ticket there, and Taskflow never
merges.

---

## Ruling-43 comparison, side by side

| | Ruling 43 run | This F8 run |
| --- | --- | --- |
| Implementation commit | made by hand with `git` | made by the **real Dispatcher** + real `ShellExecutor` |
| Taskflow validators | not run | `policy` + `changed-files`, both **passed** |
| Reaching `ready_for_integration` | **hand-written** `update_task_status(..., expected_current_status="created")` | **automatic**, `source=dispatcher:<attempt>` |
| Entering the queue | hand-enqueued in Step 2's test | **automatic**, `source=integration_handoff` |
| §22.1 FIFO key | the enqueue call's clock | **the moment of entry**, read back from the audit event |
| Compare-and-set needed | **yes** | **no** |
| Human step before integration | 2 (park + confirm) | **1 (confirm only)** |
| Initial integration | `mode=initial`, rebase, draft PR | same |
| Re-integration | `mode=reintegration`, merge, no force | same |

---

## Verdict

**The F8 gate passed.** The step ruling 43 had to perform by hand is gone. A
Ticket whose validators pass now reaches `ready_for_integration` and its repo's
integration queue by itself, with no compare-and-set and no hand-written status
of any kind, and §22.1's FIFO key is the moment it entered that status.

The one human step that remains — `confirm_integration=True` on `integrate_task`,
which has no automated caller — is reported here honestly and is unchanged by
F8. It is the natural next follow-up, and it is the owner's call.

## Every command that ran, and its exit code

| # | Command | exit |
| --- | --- | --- |
| — | `git --version` / `gh --version` / `gh auth status` | 0 |
| — | `env \| grep -i AGENT_TASKFLOW` (none set) | 1 (no match, expected) |
| 1 | `gh repo create agent-taskflow-e2e-f8-20260912-1644 --private --add-readme …` | 0 |
| 1 | `gh repo view anderson930420/agent-taskflow-e2e-f8-20260912-1644 --json …` | 0 |
| 1 | `git clone git@github.com:anderson930420/agent-taskflow-e2e-f8-20260912-1644.git repo` | 0 |
| 1 | `git log --oneline -1` / `git rev-parse HEAD` / `git remote get-url origin` | 0 |
| 2 | `git config user.email` / `user.name` (on the scratch clone) | 0 |
| 2 | `python /tmp/e2e-f8-step2.py` (migrations + `create_ticket`) | 0 |
| 3+4 | `python /tmp/e2e-f8-step34.py` (real Dispatcher run) | 0 |
| 4 | `python /tmp/e2e-f8-step4b.py` (§22.1 mechanism + idempotence) | 0 |
| 5+6 | `python /tmp/e2e-f8-step56.py` (operator-triggered initial integration) | 0 |
| 6 | `gh pr view 1 --repo <throwaway> --json …` | 0 |
| 7 | `git remote get-url origin` / `checkout main` / `pull --ff-only` | 0 |
| 7 | `git add unrelated.txt` / `git commit` / `git push origin main` | 0 |
| 8 | `python /tmp/e2e-f8-step8.py` (freshness ticks + re-integration) | 0 |
| 8 | `gh pr view 1` / `gh pr list --state all` (throwaway) | 0 |
| 8 | `git log --graph` / `git diff --name-only` / `git ls-remote` (scratch worktree) | 0 |

Every command exited 0 apart from the deliberate `grep` non-match. No step
failed, so no code was patched and nothing was rerun.

---

# Phase 2 — after the owner's merge

**BOTH PHASES ARE COMPLETE. THE F8 END-TO-END GATE PASSED.**

The owner merged PR #1 on the throwaway repository at `2026-09-12T17:25:00Z`
(`mergedBy: anderson930420`). Phase 2 ran the PR-outcome poll, the §36 merge
verification and the gated cleanup. Every command exited 0. Nothing failed, so
nothing was patched and nothing was rerun.

| | |
| --- | --- |
| Throwaway repository | **https://github.com/anderson930420/agent-taskflow-e2e-f8-20260912-1644** (private, **left in place**) |
| PR | https://github.com/anderson930420/agent-taskflow-e2e-f8-20260912-1644/pull/1 — `MERGED` |
| GitHub merge commit | `be56ff48f2b60911081d448afeb9dd56e74bed56` |
| Original task SHA | `53564f650054da7e513620db958c01a8e14f3a49` (**different**) |
| Ticket | `AT-0001`, persisted `cleaned` = §12 display **`completed`** |

Write boundaries were unchanged and were kept: only the throwaway repository and
`/tmp`. `anderson930420/agent-taskflow` stayed read-only (PR #203 is still a
draft and was not edited), `~/.agent-taskflow` was never opened, nothing was
merged by me, nothing was force-pushed and nothing was rebased.

---

## Step 1 — poll the PR through the adapter (ruling 29)

### The ruling-29 problem is real, and this run hit it

Before running anything I asked `gh` for a `merged` field directly. It does not
exist:

```text
$ gh pr view 1 --repo anderson930420/agent-taskflow-e2e-f8-20260912-1644 \
    --json number,state,isDraft,merged,mergedAt,mergeCommit,...
Unknown JSON field: "merged"
exit=1
```

That is exactly the failure ruling 29 was written for. The adapter never asks
for it:

```text
PR_VIEW_FIELDS = ('number', 'url', 'state', 'isDraft', 'mergedAt', 'mergeCommit',
                  'headRefName', 'baseRefName', 'headRefOid', 'reviewDecision',
                  'statusCheckRollup', 'reviews', 'title', 'body')
'merged' present: False    <- the merge outcome is DERIVED
```

`GitHubPrAdapter._is_merged(state, merged_at, merge_commit_sha)` returns True
when `state == "MERGED"`, or `mergedAt` is set, or `mergeCommit` is set. All
three were present here, so the derivation is unambiguous.

### The adapter's own snapshot

```python
GitHubPrAdapter(repo=GH).poll_pr(pr_number=1, cwd="/tmp/e2e-f8-20260912-1644/repo")
```

```json
{
  "number": 1,
  "state": "closed",
  "merged": true,
  "merged_at": "2026-09-12T17:25:00Z",
  "merge_commit_sha": "be56ff48f2b60911081d448afeb9dd56e74bed56",
  "head_sha": "53564f650054da7e513620db958c01a8e14f3a49",
  "is_draft": false,
  "review_decision": "none",
  "ci_status": "none"
}
exit=0
```

`state` is normalized to `closed`: §32.1 carries the merge fact in `pr_merged`,
not in `pr_state`.

### The dry poll writes nothing, the confirmed poll writes

```python
poll_pr_outcomes(WatcherRequest(repo=GH, repo_path=..., db_path=...,
                                target_branch="main", remote="origin",
                                confirm_poll=False))    # then True
```

Both returned the same outcome; only the confirmed one persisted it:

```json
{"task_key": "AT-0001", "pr_number": 1, "pr_state": "closed", "merged": true,
 "review_decision": "none", "ci_status": "none",
 "merge_commit_sha": "be56ff48f2b60911081d448afeb9dd56e74bed56",
 "proposed_transition": null, "applied_transition": null,
 "cleanup_performed": false, "task_status": "waiting_for_review",
 "deferred": false, "poll_error": null}
exit=0
```

```text
ticket status after dry poll:       waiting_for_review
ticket status after confirmed poll: waiting_for_review
```

The poll deliberately does **not** transition the Ticket on merge. Detecting a
merge is not the same as verifying it (§36), so the status move waits for
verification in step 3. `merge_detected` was audited (event id 25).

### §32.1 after step 1 — ticket status `waiting_for_review`

| field | value |
| --- | --- |
| pr_number | 1 |
| pr_url | `https://github.com/anderson930420/agent-taskflow-e2e-f8-20260912-1644/pull/1` |
| **pr_state** | **`closed`** |
| **pr_merged** | **true** |
| pr_head_sha | `53564f650054da7e513620db958c01a8e14f3a49` |
| **merge_commit_sha** | **`be56ff48f2b60911081d448afeb9dd56e74bed56`** |
| **review_decision** | **`none`** |
| **ci_status** | **`none`** |
| integrated_base_sha | `7b4a57ea8b40a42489c7ebc805f9f4a096d6d545` |
| reintegration_count | 1 |
| reintegration_required | false |
| **pr_last_polled_at** | **`2026-09-12T17:27:43Z`** |

All twelve §32.1 fields are now populated. The three that were null "by design"
in phase 1 — `review_decision`, `ci_status`, `pr_last_polled_at` — were filled
by this watcher tick, exactly as phase 1 predicted.

---

## Step 2 — merge verification (§35, §36, §36.1)

### The two identities differ in this run

```text
PR merge_commit_sha (GitHub's merge result): be56ff48f2b60911081d448afeb9dd56e74bed56
pr_head_sha         (the ORIGINAL task SHA): 53564f650054da7e513620db958c01a8e14f3a49
THEY DIFFER: True
```

**Yes, they differ.** The owner used a merge commit, so GitHub created a new
commit `be56ff4` whose parents are the previous `main` and the task head. §35
exists precisely because these are not interchangeable, and this run exercises
that difference rather than getting a lucky fast-forward where both SHAs match.

### The worktree and branch were intact before verification

```text
$ git rev-parse --abbrev-ref HEAD  (exit=0) -> task/AT-0001-add-a-trivial-marker-file-so-the-f8-end
$ git rev-parse HEAD               (exit=0) -> 53564f650054da7e513620db958c01a8e14f3a49
worktree dir exists: True
```

### The verdict

```python
verify_merge(MergeVerificationRequest(
    task_key="AT-0001", worktree_path=..., remote="origin", target_branch="main",
    pr_merged=True, merge_commit_sha="be56ff48f2b60911081d448afeb9dd56e74bed56"))
```

```json
{
  "kind": "merge_verification",
  "task_key": "AT-0001",
  "verified": true,
  "pr_merged": true,
  "merge_commit_sha": "be56ff48f2b60911081d448afeb9dd56e74bed56",
  "target_sha": "be56ff48f2b60911081d448afeb9dd56e74bed56",
  "contained_in_target": true,
  "original_task_sha_used": false,
  "reasons": [],
  "verified_at": "2026-09-12T17:28:07Z"
}
exit=0
```

Its git commands, in order:

```text
git fetch origin --prune
git rev-parse origin/main
git merge-base --is-ancestor be56ff48f2b60911081d448afeb9dd56e74bed56 origin/main
```

**Confirmed: the ancestry check ran on `be56ff4…`, GitHub's merge result — the
original task SHA `53564f6…` appears nowhere in it.** The result records that
fact explicitly rather than leaving it to be inferred:
`"original_task_sha_used": false`.

### §36.1 — the merge method actually used

```text
$ git merge-base --is-ancestor 53564f65... be56ff48...   (exit=0)

*   be56ff4 Merge pull request #1 from anderson930420/task/AT-0001-add-a-trivial-...
|\
| *   53564f6 Merge remote-tracking branch 'origin/main' into task/AT-0001-...
| |\
| |/
|/|
* | 7b4a57e Unrelated change on main to make the F8 task branch stale
| * d17b2b8 AT-0001: add a trivial marker file for the F8 end-to-end gate
|/
* 76f142b Initial commit
```

A **merge commit**. The task SHA happens to also be an ancestor here, but that
is incidental to the method chosen — under squash or rebase merge it would not
be, and the verification would still have passed, because it never looks at it.

§32.1 is unchanged by this step: verification is a read, and only Step 2's
watcher writes those fields.

---

## Step 3 — gated cleanup (§37), only after verification

### Before cleanup — everything intact

```text
worktree dir exists : True
git worktree list   : /tmp/e2e-f8-20260912-1644/repo                     7b4a57e [main]
                      /tmp/e2e-f8-20260912-1644/repo/.worktrees/AT-0001  53564f6 [task/AT-0001-...]
local branch        : + task/AT-0001-add-a-trivial-marker-file-so-the-f8-end
REMOTE branch       : 53564f65... refs/heads/task/AT-0001-add-a-trivial-marker-file-so-the-f8-end
```

### 3a — the dry run verifies but removes nothing

```python
run_integration_cleanup(IntegrationCleanupRequest(..., confirm_cleanup=False))
```

```json
{"ok": true, "status": "dry_run", "route": "verified_merge",
 "final_task_status": "waiting_for_review",
 "merge_verified": true,
 "merge_commit_sha": "be56ff48f2b60911081d448afeb9dd56e74bed56",
 "target_sha": "be56ff48f2b60911081d448afeb9dd56e74bed56",
 "verification_reasons": [],
 "worktree_removed": false, "local_branch_deleted": false,
 "remote_branch_deleted": false, "evidence_archived": false,
 "summary": "Merge is verified, but cleanup is dry-run by default; pass confirm_cleanup=True to remove the worktree and branch."}
exit=0
```

Re-checked afterwards: the worktree, the local branch and the remote branch were
all still there, and the Ticket was still `waiting_for_review`. **The worktree
and branch survived right up to the confirmed call — they were never at risk
before verification passed.**

### 3b — the confirmed cleanup

```python
run_integration_cleanup(IntegrationCleanupRequest(
    task_key="AT-0001", repo=GH, repo_path=..., target_branch="main",
    remote="origin", db_path=..., confirm_cleanup=True))
```

```json
{"ok": true, "status": "cleaned", "route": "verified_merge",
 "final_task_status": "cleaned",
 "merge_verified": true,
 "merge_commit_sha": "be56ff48f2b60911081d448afeb9dd56e74bed56",
 "target_sha": "be56ff48f2b60911081d448afeb9dd56e74bed56",
 "verification_reasons": [],
 "worktree_removed": true,
 "local_branch_deleted": true,
 "remote_branch_deleted": false,
 "evidence_archived": true,
 "merged": false, "force_pushed": false,
 "safety": {"merged_by_taskflow": false, "force_pushed": false,
            "human_review_required": true,
            "cleanup_requires_verified_merge_or_explicit_confirmation": true},
 "summary": "Merge be56ff48f2b60911081d448afeb9dd56e74bed56 verified; cleanup completed and Ticket marked completed"}
exit=0
```

Every git command, in order — note that verification runs **again inside**
cleanup, before anything is removed:

```text
git fetch origin --prune
git rev-parse origin/main
git merge-base --is-ancestor be56ff48f2b60911081d448afeb9dd56e74bed56 origin/main   ← re-verified
git worktree remove /tmp/e2e-f8-20260912-1644/repo/.worktrees/AT-0001
git branch -D task/AT-0001-add-a-trivial-marker-file-so-the-f8-end

commands with a force flag or a remote delete: NONE
```

### What was removed, and what was kept

| | |
| --- | --- |
| Worktree `/tmp/.../.worktrees/AT-0001` | **removed** |
| Local branch `task/AT-0001-…` | **deleted** (`git branch -D`) |
| **Remote branch `task/AT-0001-…`** | **KEPT — ruling 5** |
| Evidence under `artifacts/AT-0001/attempt-b23e82b6…/` | **archived, kept** |
| The merged commits on `main` | untouched |

After:

```text
worktree dir exists : False
git worktree list   : /tmp/e2e-f8-20260912-1644/repo  7b4a57e [main]
local branch        : (gone)
REMOTE branch       : 53564f65... refs/heads/task/AT-0001-add-a-trivial-marker-file-so-the-f8-end
```

Confirmed on GitHub — the remote branch is still there:

```text
$ gh api repos/anderson930420/agent-taskflow-e2e-f8-20260912-1644/branches --jq '.[].name'
main
task/AT-0001-add-a-trivial-marker-file-so-the-f8-end
exit=0
```

**Ruling 5 holds.** `delete_remote_branch` is refused up front by the request
object — `git push --delete` is outside the push allowlist and no exception is
made for it — so remote branch deletion is left to GitHub's automatic
head-branch deletion or to a human.

The archived evidence, 18 files, kept in full:

```text
artifacts/AT-0001/attempt-b23e82b6572d4bc29e618e4f075fc833/
    mission_contract.json
    shell-shell.log                          ← the executor's own log
    policy-validate.log
    changed-files-audit.json  changed-files-validate.log
    changed-files-git-status.out / .err
    executor-launch-spec-shell.json  executor-process-shell.pid.json
    validator-launch-spec-changed-files-git-status.json
    validator-process-changed-files-git-status.pid.json
    attempt-resources.json  runtime.lock
    integration/integration-f4d1f9370eba.json   ← initial integration
    integration/validators-f4d1f9370eba.json
    integration/integration-357ca20f79fc.json   ← re-integration
    integration/validators-357ca20f79fc.json
    integration/cleanup-55c5cd950f6a.json       ← this cleanup
```

### §32.1 after step 3 — ticket status `cleaned`

| field | value |
| --- | --- |
| pr_number | 1 |
| pr_url | `https://github.com/anderson930420/agent-taskflow-e2e-f8-20260912-1644/pull/1` |
| pr_state | `closed` |
| pr_merged | true |
| pr_head_sha | `53564f650054da7e513620db958c01a8e14f3a49` |
| merge_commit_sha | `be56ff48f2b60911081d448afeb9dd56e74bed56` |
| review_decision | `none` |
| ci_status | `none` |
| integrated_base_sha | `7b4a57ea8b40a42489c7ebc805f9f4a096d6d545` |
| reintegration_count | 1 |
| reintegration_required | false |
| pr_last_polled_at | `2026-09-12T17:27:43Z` |

---

## Step 4 — `completed`, and the whole chain in one place

```text
persisted status            : cleaned
§12 display (status_vocab)  : completed
```

### Every `status_changed` event, in order — §43 items 12 → 33

| id | timestamp (UTC) | status | written by | §43 |
| --- | --- | --- | --- | --- |
| 3 | `16:45:53` | `preparing` | `dispatcher:c8fc68c9…` | 4 (one worktree) |
| 5 | `16:45:53` | `implementing` | `dispatcher:c8fc68c9…` | 11 |
| 8 | `16:45:54` | `validating` | `dispatcher:c8fc68c9…` | 15 |
| **11** | **`16:45:54`** | **`ready_for_integration`** | **`dispatcher:c8fc68c9…`** | **12 — F8** |
| 13 | `16:46:38` | `integrating` | `integration_controller` | 13 |
| 15 | `16:46:44` | `waiting_for_review` | `integration_controller` | 14, 16 |
| 17 | `16:47:33` | `ready_for_integration` | `integration_watcher` | 18, 19 |
| 20 | `16:47:33` | `integrating` | `integration_controller` | 20 |
| 22 | `16:47:40` | `waiting_for_review` | `integration_controller` | 21, 22 |
| **27** | **`17:28:33`** | **`cleaned`** | **`integration_cleanup`** | **32, 33** |

The full 28-event audit trail, including the non-status events:

```text
id=1    16:45:03  created                        mission_control
id=2    16:45:53  worktree_recorded              dispatcher
id=3    16:45:53  status_changed                 dispatcher:c8fc68c9…   → preparing
id=4    16:45:53  note                           ticket_worktree
id=5    16:45:53  status_changed                 dispatcher:c8fc68c9…   → implementing
id=6    16:45:53  note                           canonical_runtime_admission
id=7    16:45:54  note                           dispatcher
id=8    16:45:54  status_changed                 dispatcher:c8fc68c9…   → validating
id=9    16:45:54  note                           dispatcher
id=10   16:45:54  note                           dispatcher
id=11   16:45:54  status_changed                 dispatcher:c8fc68c9…   → ready_for_integration   ← F8
id=12   16:45:54  integration_queued             integration_handoff                              ← F8
id=13   16:46:38  status_changed                 integration_controller → integrating
id=14   16:46:38  integration_started            integration_controller
id=15   16:46:44  status_changed                 integration_controller → waiting_for_review
id=16   16:46:44  integration_completed          integration_controller
id=17   16:47:33  status_changed                 integration_watcher    → ready_for_integration
id=18   16:47:33  reintegration_required         integration_watcher
id=19   16:47:33  integration_queued             integration_watcher
id=20   16:47:33  status_changed                 integration_controller → integrating
id=21   16:47:33  integration_started            integration_controller
id=22   16:47:40  status_changed                 integration_controller → waiting_for_review
id=23   16:47:40  integration_completed          integration_controller
id=24   17:27:43  pr_state_polled                integration_watcher
id=25   17:27:43  merge_detected                 integration_watcher
id=26   17:28:33  merge_verified                 integration_cleanup
id=27   17:28:33  status_changed                 integration_cleanup    → cleaned
id=28   17:28:33  integration_cleanup_completed  integration_cleanup
```

The per-repo integration queue is empty; both entries (F8's handoff at id 12 and
the watcher's re-queue at id 19) were consumed by the controller.

### Who wrote each status, across BOTH phases

```text
 - dispatcher:c8fc68c9a33448a0a0458a618eab8eb0
 - integration_controller
 - integration_watcher
 - integration_cleanup

hand-written by this run: NONE
```

Four sources, all of them production components. **There is no `source=e2e-*`
status write anywhere in this database.**

---

## Step 5 — does the chain hold end to end without a hand-written status?

**Yes. It holds, and no status in either phase was written by hand.**

Compare with ruling 43's run, which needed this to get past the same point:

```python
store.update_task_status("AT-0001", "ready_for_integration",
    source="e2e-ruling-43", expected_current_status="created")
```

That call has no counterpart here. Event id 11 shows
`ready_for_integration` written by `dispatcher:<attempt-id>`, one step after
`validating`, with `waiting_approval` never appearing and no approval row
recorded. From there every remaining transition was written by
`integration_controller`, `integration_watcher` or `integration_cleanup`.

### The one remaining manual step

**The operator's `confirm_integration` trigger.**

`integrate_task` has no non-test caller anywhere in the repository, and
`IntegrationRequest.confirm_integration` defaults to `False`. Nothing reads the
per-repo integration queue and acts on it. So twice in this run — for the
initial integration and again for the re-integration — a human had to call:

```python
integrate_task(IntegrationRequest(..., dry_run=False, confirm_integration=True))
```

That is the *only* step in the whole §43 12→33 chain that a person had to
perform, other than the merge itself (§34, which is human by design and must
stay that way). F8 does not change it, does not claim to, and reports it as
fact 1 of `docs/v1/handoff-f8.md`. Building the queue consumer that makes that
call is the natural next follow-up, and it is the owner's decision.

For completeness, the other confirmation flags in this run (`confirm_poll`,
`confirm_cleanup`) are deliberate dry-run-by-default gates on every Step 2 entry
point, not lifecycle gates. Each was exercised in both modes and the dry mode
wrote nothing.

---

## Phase 2 — every command and its exit code

| # | Command | exit |
| --- | --- | --- |
| — | `gh pr view 1 --repo <throwaway> --json …,merged,…` | **1** — `Unknown JSON field: "merged"`, deliberately run to demonstrate ruling 29 |
| — | `gh pr view 1 --repo <throwaway> --json number,state,isDraft,mergedAt,mergeCommit,mergedBy,…` | 0 |
| — | `gh api repos/<throwaway>/commits/main --jq .sha` | 0 |
| 1 | `python /tmp/e2e-f8-p2-step1.py` (adapter snapshot + dry poll + confirmed poll) | 0 |
| 2 | `python /tmp/e2e-f8-p2-step2.py` (`verify_merge` + §36.1 ancestry check) | 0 |
| 3 | `python /tmp/e2e-f8-p2-step3.py` (dry cleanup + confirmed cleanup) | 0 |
| 4 | `python /tmp/e2e-f8-p2-step4.py` (status vocab + full audit chain) | 0 |
| — | `gh api repos/<throwaway>/branches --jq '.[].name'` (remote branch survives) | 0 |
| — | `find <scratch>/artifacts -type f` (archived evidence) | 0 |

Every command exited 0 apart from the one deliberate `gh` failure that
demonstrates why ruling 29 exists.

---

# VERDICT — the F8 end-to-end gate PASSED

Both phases are complete. Running the same scenario as ruling 43 against the new
path:

- a Ticket whose validators pass reaches `ready_for_integration` **by itself**,
  written by the dispatcher one step after `validating`;
- it enters its repo's integration queue **exactly once**, automatically, keyed
  on the moment it entered that status (§22.1) rather than the enqueue clock;
- **the hand-written compare-and-set that ruling 43's run required is gone**, and
  no status in either phase was written by anything other than a production
  component;
- initial integration, staleness detection, re-integration without a force push,
  merge detection derived per ruling 29, §36 verification against GitHub's merge
  result rather than the original task SHA (which **differed** here), and gated
  cleanup that kept the remote branch (ruling 5) all behaved as specified;
- the Ticket reached persisted `cleaned` = §12 `completed`, closing §43 items
  12 → 33.

The one manual step that remains is the operator's `confirm_integration`
trigger on `integrate_task`, reported honestly and unchanged by F8.

**Throwaway repository, left in place as instructed:**
**https://github.com/anderson930420/agent-taskflow-e2e-f8-20260912-1644**

**PR #203 on `anderson930420/agent-taskflow` is still a DRAFT. The owner merges it.**
