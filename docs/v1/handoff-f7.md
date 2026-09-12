# HANDOFF — V1 FOLLOWUPS F7: rewrite README.md

**Status: STOPPED on a stop condition. `README.md` was NOT rewritten.**
Branch `task/v1-stepf7`, based on `main` at `5b23895` (F8, PR #203).
This file is the only change on the branch.

The factual audit (steps 1 and 2 of the brief's order of work) is complete and
recorded below, so the rewrite itself is a short turn once the owner decides §1.

---

## 1. Stop condition hit — the rewrite needs a test change

The brief: *"note that no test needed to change. If a test does need to change,
that is a stop condition."* One does.

`tests/test_readme_current_architecture.py` (13 tests, green on `5b23895`) pins
the README to exact phrases from the pre-V1 text. Its docstring says it exists
to fail "when the README drifts from what the system actually does". Several of
those pinned phrases are now false, or are exactly the stale framing F7 exists
to remove. An accurate README cannot contain them without wording contrived
only to satisfy substring checks, and I did not do that.

| Test (line) | Pinned phrase | Can an accurate README keep it? |
| --- | --- | --- |
| `test_mission_control_boundary` (:66-72) | "Mission Control as read-only review and evidence dashboard" | **No — false.** Mission Control creates Tasks and Tickets and starts, approves, rejects and blocks tasks: routes `api/main.py:232, 314, 437, 480, 523`, `api/tickets.py:106`; UI callers `CreateTicketForm.tsx:67`, `CreateTaskForm.tsx:131`, `ActionPanel.tsx:131/183/197/248`, `StartDispatchPanel.tsx:75`. |
| `test_deferred_automation_is_explicit` (:107-115) | "Dispatcher-driven branch push or PR creation", "Automatic cleanup" (in a deferred list) | **No.** This is the list the brief calls stale: "still describes branch push, PR creation and cleanup as deferred". Branch push, PR creation/update and gated cleanup are built (`integration_controller.py`, `integration_cleanup.py`). What is missing is an automated *caller*, not the capability (§4). |
| `test_scheduled_execution_status_is_explicit` (:88-95) | "Publication, merge, and cleanup remain human-gated" | **No, as a boundary.** Merge is human by design (SPEC §34, §44). Publication and cleanup are human-*triggered* today only because nothing calls them; SPEC §1 steps 10-15 make them Taskflow's job. The brief forbids calling the manual trigger "operator-controlled by design". |
| `test_current_loop_is_semi_automatic` (:83-86) | "Semi-Automatic Dogfood Loop", "operator-driven and semi-automatic", "waiting_approval" | Only if relabelled as the **legacy** GitHub-issue path. The test name asserts it is *the current loop*; for Tickets it is not (`ticket_lifecycle.py:44`, F8). |
| `test_review_evidence_and_handoff` (:57-64) | "Branch push is explicit", "Draft PR creation is explicit" | Only in a legacy-scoped section (true there: `push_task_branch.py:73`, `create_draft_pr.py:81`). |
| all others | positioning, SQLite, worktree/executor/validator chain, ExecutionEngine, Hermes note | Yes — still true. |

No other test reads the repo-root README (grep over `tests/`).

**Decision needed from the owner — recommendation first:**

- **A (recommended).** Authorize the F7 builder to update
  `tests/test_readme_current_architecture.py` in the same turn: keep the checks
  that are still true, and replace the stale pins with pins on the V1 facts in
  §3 (e.g. Taskflow cannot merge or force-push; integration has no automated
  caller yet). The test keeps its purpose — pinning the README to reality —
  instead of pinning it to July's reality.
- **B.** Keep the test untouched and write around it. Not viable: the Mission
  Control sentence is false and there is no honest way to include it.
- **C.** Delete the test. Not recommended; it has caught drift before.

Also decide: `README.zh-TW.md` (337 lines) is equally stale and is outside F7's
allowed layers. Either add it to the rewrite scope or record it as a follow-up.

---

## 2. Order of work step 1 — claims in the current README that are false or stale

Line numbers are `README.md` at `5b23895`.

| # | Line(s) | Claim | Why it is false or stale now | Where checked |
| --- | --- | --- | --- | --- |
| 1 | 18-19, 43, 116 | Work enters as a GitHub Issue or spec | V1 work enters as a prompt-first Ticket (repository + prompt + priority); issue intake is the legacy path | `api/tickets.py:38-44,106`; `ticket_creation.py:161-262` |
| 2 | 32, 135, 283 | Mission Control is a read-only review and evidence dashboard | It creates Tickets/Tasks and starts, approves, rejects and blocks tasks; it also has an SSE live board | §1 table; `api/main.py:872-913`; `mission-control/app/live/page.tsx` |
| 3 | 58-70, 94-99 | Pipeline ends Waiting Approval → Human Review → *optional* branch push / draft PR → explicit cleanup | A Ticket ends `ready_for_integration` and is enqueued; integration pushes, opens a draft PR, re-integrates; merge verification then cleanup reach `cleaned` | `ticket_lifecycle.py:44,71-77`; `integration_handoff.py:86-108`; `integration_controller.py`; `integration_cleanup.py` |
| 4 | 107-108 | Safety boundary "No automatic cleanup" | Cleanup after a verified merge is built as a Taskflow step (SPEC §37); it is gated by `confirm_cleanup` and by merge verification, and today only has manual callers | `integration_cleanup.py:73,247-251,267`; §4 |
| 5 | 140-157 | "The current dogfood loop" is the operator-driven, semi-automatic issue loop ending `waiting_approval` | True only of the legacy path; Tickets skip `waiting_approval` since F8 | `ticket_lifecycle.py:71-77`; `approved_task_runner.py:55` |
| 6 | 146, 170-207 | Operator flow: ingest → prepare workspace → `run_dispatcher.py` | For Tickets the scheduler tick creates the worktree and dispatches, up to capacity | `parallel_scheduler.py:249-292`; `ticket_worktree.py`; `scheduler_worker.py:29-31` |
| 7 | 221-234 | `push_task_branch.py`/`create_draft_pr.py --dry-run` are the publication route | Legacy route; it refuses a Ticket since F8. `--dry-run` is also redundant — both scripts force dry-run unless `--confirm-push` / `--confirm-create-pr` is given | `push_task_branch.py:42,73`; `create_draft_pr.py:46,81`; handoff-f8 §1 table |
| 8 | 212-216 | `create_pr_handoff.py` example is complete | Omits `--canonical-attempt-id`, required for Level 2 handoff | `create_pr_handoff.py:43-49` |
| 9 | 122-128 | Validators are pytest, openspec, policy, changed-files, smoke tests | Registry is pytest, openspec, policy, changed-files, typecheck, lint; smoke tests are scripts, not validators | `validators/registry.py:37-60` |
| 10 | 12, 27, 89 | Codex is an executor | No Codex executor adapter; registry is manual, noop, shell, opencode, pi, claude-code (Codex is used via `ops/codex-as-pi.sh` and advisory review) | `executors/registry.py:40-66` |
| 11 | 285 | "Workers cannot self-approve, push, merge, or clean up." | Still true of *workers* (executors), but misleading: Taskflow itself now pushes and cleans up | `integration_git.py:228-250`; `integration_cleanup.py` |
| 12 | 286 | "Validation success does not imply automatic publication, merge, or cleanup." | Stale as a design statement: a Ticket whose validators pass now advances to `ready_for_integration` and the queue automatically. Publication is not automatic *yet* only because the queue has no consumer (F9) | `dispatcher.py:497-551`; `integration_handoff.py` |
| 13 | 292-304 | The scheduled path is the locked one-task GitHub-issue tick, execution-only | Still exists, but V1's scheduler is `run_parallel_scheduler_tick.py` (many Tickets per tick, capacity-bounded). The legacy tick is execution-only by default, but `--publish-after-execution` enables push and draft PR | `parallel_scheduler.py:1-20`; `cli/github_issue_one_task_scheduler_tick.py:77-84`; `github_issue_one_task_scheduler_tick.py:426-427` |
| 14 | 301-304 | Publication, merge and cleanup remain human-gated | Merge: yes, by design. Publication and cleanup: human-triggered today for lack of a caller, not by design | §1; §4 |
| 15 | 321-331 | Deferred: dispatcher-driven workspace creation, branch push or PR creation, automatic cleanup | Worktree-per-Ticket creation is built (Step 5); push/PR creation/update is built (Step 2); gated cleanup is built (Step 2). What is not built is the queue consumer (F9) and any automated watcher/cleanup caller (§4) | `parallel_scheduler.py:265`; `integration_controller.py`; `integration_cleanup.py` |
| 16 | — (absent) | README says nothing of: capacity limit and its evidence gate, `blocked_by` / dependency release, priority, the integration queue, re-integration, merge verification, the live deployment, RULINGS/FOLLOWUPS | All exist | §3 |

---

## 3. Order of work step 2 — replacement claims, each verified

Every row is a statement the rewrite would make. Code citations are at
`5b23895`; ruling citations are `~/agent-taskflow-ops/v1/RULINGS.md` and
`FOLLOWUPS.md`. Nothing below was read from a database; deployed-state rows
come from the deployment record only.

### 3.1 End to end, in order

| # | Claim | Verified at |
| --- | --- | --- |
| E1 | A Ticket is created from repository + prompt + priority via `POST /api/tickets`, from Mission Control's Create Ticket form | `api/tickets.py:38-44,106`; `api/main.py:930`; `mission-control/components/CreateTicketForm.tsx:67`; `lib/tickets.ts:136-139` |
| E2 | Task key (`AT-` prefix), branch, worktree path are generated; title is AI-generated with a deterministic prompt fallback | `ticket_metadata.py:37-39,74-82,97-113`; `ticket_ai_metadata.py:133-197` |
| E3 | Repositories come from the project registry `config/projects.yaml` | `ticket_repositories.py:21,35-45,72-115` |
| E4 | A new Ticket is `ready` (persisted `created`), or `blocked` if created with `blocked_by`; the UI form cannot set `blocked_by` | `ticket_models.py:105-113`; `status_vocab.py:46-47`; `CreateTicketForm.tsx` payload |
| E5 | `scripts/run_parallel_scheduler_tick.py` runs ONE tick and exits (no daemon); it reaps stale leases, updates dependencies, reads capacity, then for each eligible Ticket in priority → `created_at` → `task_key` order creates the Ticket's worktree and starts a worker that dispatches it | `run_parallel_scheduler_tick.py:2-8`; `parallel_scheduler.py:249-292`; `ready_queue.py:36,57-58`; `scheduler_worker.py:29-31` |
| E6 | One Ticket = one worktree; retries reuse it | `parallel_scheduler.py:265` `ensure_ticket_worktree`; ruling 26 |
| E7 | The claim is atomic (`BEGIN IMMEDIATE`) | `runtime_admission.py:363` |
| E8 | Execution records Prepare / Implementer / Validator progress against the Attempt; Mission Control has an SSE live board | ruling 7/F1; `api/main.py:872-913`; `api/realtime.py:35` |
| E9 | Validators pass → Ticket written `ready_for_integration` and enqueued once in its repo's integration queue, FIFO on the moment it entered that status; priority never affects queue order | `ticket_lifecycle.py:44,71-77`; `dispatcher.py:497-551`; `integration_handoff.py:63,86-108,166-183`; `integration_store.py:261-296` |
| E10 | Tickets whose registry entry has no `github_repo` are not enqueued | `integration_handoff.py:151-157` |
| E11 | Validator red → `needs_decision`; infrastructure failure → `failed` (Tickets only) | `ticket_lifecycle.py:80-86`; `dispatcher.py:478-486` |
| E12 | Integration (`integrate_task`) requires `ready_for_integration`, `dry_run=False` AND `confirm_integration=True` (defaults True/False) | `integration_controller.py:98-99,254-266,285-298` |
| E13 | Initial integration: fetch, rebase onto latest `origin/<target>`, validators, push task branch, create PR (draft by default), record `integrated_base_sha`, release lock, `needs_review` (persisted `waiting_for_review`) | `integration_controller.py:447-462,555-574,618-651,661-691`; `status_vocab.py:54` |
| E14 | Integration is serialized per repository by a lock row in `integration_locks`; a held lock returns `lock_unavailable` | `integration_controller.py:300-313`; `integration_store.py:311-340` |
| E15 | Freshness poll: a `needs_review` Ticket whose branch is behind the target goes back to `ready_for_integration` with `reintegration_required` | `integration_watcher.py:121-220` |
| E16 | Re-integration merges (not rebases) the target into the published branch, re-runs validators, pushes normally, updates the SAME PR via `gh api -X PATCH`, `reintegration_count += 1` | `integration_git.py:685-702`; `integration_controller.py:467,615-626,652-672`; `github_pr_adapter.py:413-446` |
| E17 | Integration validator failure, unresolvable conflict, or any exception at the boundary → `needs_decision` with an audited event | `integration_controller.py:370-395,499-538,566-574,738-766` |
| E18 | PR outcome poll (`gh pr view`): changes requested → `needs_decision`; closed unmerged → `canceled`, worktree kept; merged → recorded | `integration_watcher.py:267-368,437-449,519-573` |
| E19 | Merge verification checks GitHub's `merge_commit_sha` is in the latest target history, never the task SHA | `merge_verification.py:84-148`; rulings 46, 61 |
| E20 | Cleanup requires `confirm_cleanup=True` and a passing merge verification; removes the worktree, deletes the local branch, keeps the remote branch, writes `cleaned` (display `completed`) | `integration_cleanup.py:73,247-298,325-457`; ruling 5 |
| E21 | Dependents are released only when the blocker is completed (`cleaned`/`completed`/`done`); a failed/canceled blocker sends dependents to `needs_decision` | `ticket_dependencies.py:43-46,490-517,535-560` |
| E22 | Both phases of this chain have run for real against a throwaway GitHub repo with real `git`/`gh` | rulings 45, 46 (Step 2), 61 (F8) |

### 3.2 What is still manual

| # | Claim | Verified at |
| --- | --- | --- |
| M1 | `integrate_task` has no non-test caller; no script or cron invokes it. FOLLOWUPS F9 closes this | grep over the worktree: only definition + `tests/`; FOLLOWUPS F9 |
| M2 | **Also manual, beyond the brief:** `poll_target_freshness`, `poll_pr_outcomes` and `run_integration_cleanup` have no non-test caller either (`verify_merge` is called only by cleanup). Every freshness poll, PR poll and cleanup to date was a hand call | grep; `docs/v1/handoff-step2-e2e.md:90-91,196,311,500` |
| M3 | Nothing in the repo schedules `run_parallel_scheduler_tick.py`; the checked-in cron/systemd examples run only the legacy one-task tick | `deploy/cron/*.example`; `deploy/systemd/*.example`; `ops/run_tick_manual.sh` |
| M4 | The merge is a human action on GitHub, by design | SPEC §34, §44; `github_pr_adapter.py:164-203` |
| M5 | Pause, resume, cancel, retry, priority change and `blocked_by` edits have no API route or UI; they are CLI-only (`scripts/ticket_dependency.py set/remove/retry`, `scripts/reset_task_status.py`, `scripts/runtime_control.py`) | `api/` has only the POST routes in §1; `scripts/ticket_dependency.py:4-14,73`; `runtime_control.py:52-60` |

### 3.3 Safety boundaries that still hold

| # | Boundary | Enforced at |
| --- | --- | --- |
| S1 | Taskflow cannot merge: `gh pr merge`, `git merge` via the adapter, and REST/GraphQL merge vectors refused | `github_pr_adapter.py:164-203` (`assert_not_a_merge_command`), `:206-292` (`assert_gh_api_allowed`); rulings 35, 42 |
| S2 | Cannot force-push: the only push allowed is `git push [-u] origin <task-branch>`; every force spelling, `+`/`:` refspecs, `--mirror/--all/--tags/--delete` refused | `integration_git.py:228-278` (`assert_push_allowed`), `:281-287`, `:332-340` |
| S3 | Cannot push to a protected branch: main/master/trunk/base/HEAD refused after `refs/heads/`/`heads/` normalization, before any git runs | `integration_git.py:175-225`; rulings 18, 22 |
| S4 | Cannot self-approve: approval needs an operator-attested `decided_by`; worker/system identities refused; integration never writes an approval | `api/main.py:112-128`; `mission_contract.py:219`; `validators/policy.py:43` |
| S5 | Blocked or paused Ticket cannot be claimed | `runtime_admission.py:375-381` |
| S6 | Ticket whose blocker is not completed cannot start | `runtime_admission.py:385` → `:108-124`; ruling 32, 38 |
| S7 | Concurrency is bounded: checked inside the claim transaction against active executor leases; default 1 | `runtime_admission.py:76-90,364`; `runtime_capacity.py:47,118-127` |
| S8 | The limit rises above 1 only with passing rehearsal evidence bound to the current commit (`repo_sha == git HEAD`, disposable DB, production untouched, all checks true); `runtime_control.py set-capacity` exits 2 otherwise | `runtime_capacity.py:262-283`; `concurrency_gate.py:89-111`; `scripts/runtime_control.py:146-155` |
| S9 | Remote branches are never deleted by Taskflow | `integration_cleanup.py:14-17,215-227`; ruling 5 |
| S10 | Closed-unmerged work is not destroyed | `integration_watcher.py` closed branch; SPEC §33.5 |

### 3.4 Deployed state (from the record, not the database)

| # | Fact | Source |
| --- | --- | --- |
| D1 | Live DB: `/home/ubuntu/.agent-taskflow/state/github_issue_scheduler.sqlite3` | FOLLOWUPS F3 step 1; ruling 48 |
| D2 | Three V1 migrations ran on it: `migrate_ticket_fields.py`, `migrate_runtime_progress.py`, `migrate_ticket_worktree_resources.py`; backup taken first | ruling 48 |
| D3 | Global capacity is **2** (`source=configured`, `generation=2`), with rehearsal evidence regenerated at `655901e` | ruling 52 (supersedes ruling 50's 10) |
| D4 | The Discord notifier alerts on `failed`, `needs_decision`, `blocked`, `cleaned`, `ready_for_integration` | rulings 49, 57 |

**Caveat for the rewrite:** D3's evidence is bound to `655901e`. `main` is now
`5b23895` (F8). The record does not say whether F8 was deployed; ruling 61 says
only "merged". Per S8, a capacity change at a new commit needs fresh evidence.
The README should state capacity 2 as of ruling 52 and not claim a deployed
commit the record does not give.

### 3.5 Not built (one line each, per the brief)

| Item | Line | Source |
| --- | --- | --- |
| F5 | Legacy GitHub-issue path still writes `blocked`/`waiting_approval`; one failure vocabulary pending | FOLLOWUPS F5 |
| F6 | §19.3 crash-recovery rehearsal is timing-sensitive on slow runners | FOLLOWUPS F6 |
| F9 | Integration queue has no consumer | FOLLOWUPS F9 |
| Step 6 | Practical UX refinement, only from real friction | SPEC §42; ruling 54 |

---

## 4. Findings beyond the brief — for the owner

1. **The manual gap is wider than F9 as written.** F9 names `integrate_task`.
   The freshness poll, PR-outcome poll and cleanup also have no caller (§3.2 M2),
   and nothing schedules the parallel scheduler tick in the repo (M3). SPEC
   §25.1, §32, §35-37 make these tick-driven. Apply FOLLOWUPS' own question —
   "does the SPEC require this to be automatic?" — to the watcher and cleanup;
   F9's scope or a new item should say who calls them. The brief's item 1
   ("merge verification and gated cleanup (Step 2) → `completed`") would read as
   automatic unless the README says otherwise.
2. **Ruling 58 contradicts what Step 2 built.** Ruling 58 says `integrate_task`
   "stays lock-free, assuming its caller holds it". It does not: it acquires the
   per-repo lock itself (`integration_controller.py:300-313`) and returns
   `lock_unavailable` if held. Ruling 58 makes that a STOP for the F9 builder;
   flagging it now so F9's brief is written knowing it.
3. **Cleanup force-deletes the local branch** (`git branch -D`,
   `integration_cleanup.py:457`), while SPEC §37 says "safe-delete local
   branch". The code comment gives the reason (squash/rebase merges leave the
   task branch unmerged by ancestry) and it runs only after §36 verification. No
   code change proposed; the README should not say "safe-delete".
4. **No AI conflict resolver exists.** Only `NullConflictResolver`
   (`integration_conflict_resolver.py:73-86`), so every integration conflict goes
   to `needs_decision`. The README must not claim AI-assisted resolution.
5. **SPEC §18 manual controls are CLI-only** (§3.2 M5). Step 6 territory.
6. **RULINGS.md has 61 rulings, not 60** (61 is F8's merge). Ruling 54's order is
   amended in FOLLOWUPS F9 to F7 → F9 → F6.
7. **FOLLOWUPS F2** (vocabulary and migration cleanup) is also unbuilt and is not
   in the brief's not-built list.
8. **Other stale docs** outside F7's allowed layers: `README.zh-TW.md`;
   `docs/current-architecture-boundary.md` (its Non-Goals list "automatic push"
   and "automatic PR creation"). `docs/v1-step2-integration-controller.md` is
   current and is a good pointer target, with `WORKFLOW.md`, `docs/script-map.md`
   and `docs/v1/handoff-*.md`.

---

## 5. Validation

No code, test, script, config or workflow file was changed. Only this file was
added. No test needed to change *for this commit*; §1 is about the rewrite.
All run from the worktree root on `5b23895` plus this file:

| Command | Result |
| --- | --- |
| `python -m compileall -q agent_taskflow scripts tests` | exit 0 |
| `PYTHONPATH=. python scripts/validate_workflow_contract.py` | `status: passed`, exit 0 |
| `PYTHONPATH=. python scripts/validate_workflow_policy.py` | `status: passed`, exit 0 |
| `PYTHONPATH=. python -m pytest tests -q -p no:cacheprovider -n 4` | 5389 passed, 8 skipped, 1992 subtests passed (309 s) |
| `pytest tests/test_readme_current_architecture.py` (baseline, before any work) | 13 passed |

(`python` = `/home/ubuntu/agent-taskflow/.venv/bin/python`.) No production
database, `~/.agent-taskflow/state`, scheduler tick or GitHub repository was
touched during the audit.

## 6. Stop conditions

| # | Condition | Hit? |
| --- | --- | --- |
| 1 | A test needs to change | **YES** — §1 |
| 2 | Code must change | No. §4 items are reports, not proposed patches |
| 3 | A claim could not be verified | Deployed commit after `655901e` (§3.4 caveat) — would be left out of the README |
