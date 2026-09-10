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

1. **Ruling 4a — Step 1's `tasks` columns move to an explicit migration.** The
   `tasks_ticket_fields` migration — 10 columns, 2 unique indexes — is out of
   `init_db()` and out of the startup path. Only
   **`scripts/migrate_ticket_fields.py`**, run by hand, installs it. Startup
   fails closed: if any of those columns or indexes is missing, the Mission
   Control API refuses to start, and the error names that script. Neither
   `store.init_db()` nor `TicketStore.init_db()` applies it. The pre-existing
   legacy migrations stay inside `store.init_db()`; moving them is a recorded
   follow-up (§10).
2. **Ruling 4b — branch-name collision at Ticket creation.** If the derived
   branch already exists, creation is refused with HTTP 409 naming the
   branch. "Exists" means recorded in `tasks`, or present in the repository as
   a local or remote-tracking branch. The name is never auto-suffixed, and a
   refusal writes no row and no audit event. The repository check reads the
   ref storage directly, so the §43 negative-scope tests did not change — they
   are byte-identical to `dcad084` (verified with a diff).
3. **PR #195 review ruling.** `tasks` is the only canonical Ticket entity. The
   `tickets` / `ticket_events` tables are gone, Step 1's columns live on
   `tasks`, task keys come from one global `AT-0001` counter, the Board's
   Create Ticket link is reverted, and §6 was rewritten.
4. **§12.2 status-vocabulary ruling.** `TASK_STATUSES` stays the persisted
   vocabulary, the §12 names are for display only, and
   `agent_taskflow/status_vocab.py` bridges them. See §4.

---

## 1. Inventory, corrected

**Corrections to earlier versions of this handoff.** Earlier revisions said
`agent_taskflow/models.py` was "unchanged" and that "`models.py` / `store.py` /
`schemas.py` and the legacy `tasks` mirror are untouched". `models.py` is
modified: the §12.2 commit adds five values to `TASK_STATUSES`. The previous
revision also listed `store.py` as modified. After ruling 4a it is **unchanged
again — byte-identical to base `4266c02`** — because the migration it briefly
carried moved to `ticket_fields_schema.py`.

Existing files, and what this branch does to each:

| File | Action | What |
| --- | --- | --- |
| `agent_taskflow/models.py` | **modified, additive** | 5 values added to `TASK_STATUSES` (§12.2 ruling) |
| `agent_taskflow/store.py` | **unchanged** | byte-identical to `4266c02` (ruling 4a) |
| `agent_taskflow/api/main.py` | **extended** | import router, construct `TicketStore`, `include_router`, plus a comment marking the fail-closed lifespan gate |
| `mission-control/components/TaskBoard.tsx` | **reverted** | byte-identical to `4266c02` |
| 12 legacy test files | **fixture change only** | before entering the API lifespan, run the Step 1 migration: `test_api`, `test_api_actions`, `test_api_evidence_readback`, `test_api_scheduler_candidates`, `test_api_scheduler_confirmations`, `test_api_scheduler_proposals`, `test_workflow_policy_read_only_api_contract`, `test_review_evidence`, `test_ui_create_dispatch_dogfood`, `test_run_scheduler_proposal_creation_hardening_smoke`, `test_run_scheduler_confirmation_preparation_hardening_smoke`, `test_api_cors` |
| 9 smoke scripts | **fixture change only** | before `create_app(...)`, init the legacy schema and run the Step 1 migration: `run_mission_control_smoke.py`, `run_pr_handoff_golden_path_smoke.py`, `run_issue_to_prepared_workspace_smoke.py`, `run_draft_pr_fake_gh_golden_path_smoke.py`, `run_scheduler_proposal_creation_hardening_smoke.py`, `run_scheduler_confirmation_preparation_hardening_smoke.py`, `run_pi_executor_golden_path_smoke.py`, `run_runtime_chain_dogfood_smoke.py`, `run_prepared_workspace_golden_path_smoke.py` |
| `projects.py`, `config.py`, `worktree.py`, `artifacts.py`, `tasks.py`, `_helpers.py`, `api/schemas.py`, `mission-control/lib/api.ts` | reused, unchanged | |

The 21 fixture changes are the direct cost of ruling 4a: each of those tests
and smoke scripts starts the API against a fresh database, and startup now
refuses until the migration has run. Every change inserts the migration call
and nothing else, and no assertion was altered. One side effect:
`test_api.test_app_factory_uses_temp_db_path` now pre-creates its database, so
its `db_path.exists()` assertion no longer proves the lifespan created the
file. The new fail-closed tests cover lifespan behaviour on a fresh database.

No existing column, table, index or row is altered or removed.

New modules: `ticket_models.py`, `ticket_repositories.py`,
`ticket_metadata.py`, `ticket_ai_metadata.py`, `ticket_store.py`,
`ticket_creation.py`, `api/tickets.py`, `status_vocab.py`,
**`ticket_fields_schema.py`** (4a), **`git_ref_storage.py`** (4b), plus the
Mission Control form, the detail page and `lib/tickets.ts`.

New script: **`scripts/migrate_ticket_fields.py`** (4a).

Removed: `agent_taskflow/ticket_schema.py` — the `tickets` table and its
`v1_ticket_creation_v1` migration.

---

## 2. What is implemented

**A Ticket is a `tasks` row.** Prompt-first creation inserts a normal `tasks`
row (`project`, `board`, `title`, `status`, `repo_path`, `artifact_dir`,
timestamps) and fills Step 1's ten columns: `prompt`, `priority`,
`ai_title_status`, `branch_slug_source`, `blocked_by`, `github_repo`,
`base_branch`, `branch`, `worktree_path`, `commit_message_suggestion`. Legacy
rows leave them NULL. The creation audit event is a `created` row in the
existing `task_events` log, with payload `kind: ticket_created`.

**Explicit migration (ruling 4a).** Only `scripts/migrate_ticket_fields.py`
installs the ten columns and the two partial unique indexes (logic in
`agent_taskflow/ticket_fields_schema.py`). The script:

- requires the legacy task-mirror schema (`tasks`, `schema_migrations`) and
  never installs it. If that schema is missing it exits 2 and writes nothing;
  a database file that doesn't exist is never created;
- is idempotent, and prints a JSON report of exactly which columns and
  indexes it added;
- records `tasks_ticket_fields` in `schema_migrations`.

Startup fails closed. The API lifespan calls `TicketStore.init_db()`, which
runs the legacy `store.init_db()` and then `require_ticket_fields()`. That
raises `TicketFieldsMigrationRequired` — naming the script, the database path
and every missing column or index — and never applies anything. On a fresh
database the operator flow is: start the API (the legacy schema is created,
then startup refuses), run the script, start the API again.

**Branch collision (ruling 4b).** Inside the creation `BEGIN IMMEDIATE`
transaction, in this order: allocate the key, derive the metadata, refuse if
the derived branch is already recorded for the same `repo_path` in `tasks`
(the ruling) or in `task_worktrees` (added — also recorded branch data), then
refuse if the repository already holds `refs/heads/<branch>` or
`refs/remotes/<remote>/<branch>`, loose or packed, and only then insert.

- A refusal rolls the transaction back: no row, no audit event, no key
  consumed, no suffixed retry.
- The API returns **409** with `{ok, detail, branch, conflict_source, existing}`.
  `detail` names the branch, and `conflict_source` is `tasks`,
  `task_worktrees` or `repository`.
- The repository check (`agent_taskflow/git_ref_storage.py`) opens and stats
  files only. It follows `.git` directories, `gitdir:` files and `commondir`
  (linked worktrees). It spawns no process and writes nothing — a test
  snapshots every file's mtime and size around the lookup.
- Whatever it cannot read with certainty fails closed: the reftable backend,
  an unreadable file, a malformed `.git` file. Creation is then refused with
  **503** rather than guessing "no collision".
- A `repo_path` that doesn't exist, or isn't a Git working tree, holds no
  branches, so it cannot collide.

**Task keys — one global counter, `AT-0001`.** No sequential generator existed
in the repo: `kanban_create` takes the key from the user, and issue intake
derives `AT-GH-<n>`. The new counter follows the `AT-0001` convention their
help text uses, skips every legacy `AT-<digits>` key, and ignores other shapes.

**Status.** §12.1's display status is persisted through `status_vocab`:
`ready` becomes `created`, and `blocked` stays `blocked`. `queued` is never
written.

**`ai_title_status`** is `generated`, `fallback` or `not_attempted`.

**Visibility.** A Ticket appears in `/api/tasks` and on `/tasks/<key>`.
`/api/tickets/<key>` serves prompt-first rows only.

**Legacy upsert safety.** `upsert_task` names its columns explicitly, so a
mirror re-sync cannot clobber ticket-only columns. A test pins this.

**Structural invariants.** `ux_tasks_worktree_path` and
`ux_tasks_repo_branch` are partial unique indexes, installed by the script, and
they keep `One Ticket = One Worktree` enforced at the storage layer.

Step 1 checklist (§42): repo dropdown, prompt-first Ticket, priority, auto task
key, AI title with deterministic fallback, auto branch (collision-refusing),
auto worktree path (string only), and a basic detail page
(`/tickets/<task_key>`).

---

## 3. Acceptance gate

177 tests across 8 Ticket-related files.

| Gate item | Test |
| --- | --- |
| §43.1 create from repo/prompt/priority | `test_ticket_creation.MinimalCreationInputTests` |
| §43.2 metadata generated automatically | `DerivedMetadataTests` |
| §43.3 AI title failure cannot block creation | `AiTitleFallbackTests` |
| §43.4 unique worktree/branch derivation | `OneTicketOneWorktreeTests`, `test_ticket_store.AllocationTests` |
| §44 One Ticket = One Worktree | storage-level uniqueness in `AllocationTests` |
| §44 all lifecycle mutations auditable | `AuditabilityTests`, `test_ticket_store.AuditTests` |
| §12.1 / §12.2 initial status | `InitialStatusTests` |
| negative scope: no Git command, no directory | `NegativeScopeTests` — **byte-identical to `dcad084`** |
| PR #195: `tasks` is the only entity | `CanonicalEntityTests`, `TicketsAreTasksTests` |
| PR #195: global `AT-0001` counter | `TaskKeyTests`, `AllocationTests` |
| 4a: fresh DB → startup refuses, names the script | `test_ticket_fields_schema.StartupGateTests` |
| 4a: after the script → startup succeeds | `StartupGateTests.test_after_the_script_runs_startup_succeeds` (runs the real script) |
| 4a: neither `init_db()` applies it | `NoStartupApplyTests` |
| 4a: the script is idempotent | `MigrationTests.test_migration_is_idempotent`, `test_migrate_ticket_fields_script` |
| 4a: schema diff is exactly Step 1's columns and indexes | `SchemaDiffTests` |
| 4b: branch in `tasks` → 409 naming it | `test_api_tickets.BranchCollisionRouteTests`, `test_ticket_creation.BranchCollisionTests` |
| 4b: repository branch → 409 naming it | the same, plus `test_git_ref_storage` (local, remote-tracking, packed, linked worktree) |
| 4b: no row, no event, no directory, no subprocess | asserted in every collision test |
| 4b: never auto-suffixed, no key consumed | `BranchCollisionTests.test_refusal_never_auto_suffixes_and_consumes_no_key` |

Per file: `test_ticket_metadata` 22, `test_ticket_store` 28, `test_ticket_creation` 44, `test_api_tickets` 21, `test_status_vocab` 29, `test_ticket_fields_schema` 12, `test_migrate_ticket_fields_script` 5, `test_git_ref_storage` 16.

**Fixture convention (4a).** Fixtures call `migrate_ticket_fields()`, the
function the script wraps. The script itself runs end-to-end as a subprocess
in `test_migrate_ticket_fields_script` and in the startup-succeeds test. This
follows Step 3's precedent: its fixtures call `migrate_runtime_progress()`, and
`test_migrate_runtime_progress_script.py` runs the script. If "runs the script
explicitly" meant a subprocess in every fixture, say so — it is mechanical,
but it slows the suite noticeably.

One guarantee was lost with the table. The old `ticket_events` table had
append-only triggers; `task_events` is a legacy table without them. See §10.

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
stop conditions fired, and I did not stop on either one when it did:

1. **The §12 vs `TASK_STATUSES` vocabulary conflict** — a spec requirement
   contradicting existing code. Instead of stopping, I sidestepped it with a
   separate `tickets` table, which is the root cause the PR #195 review found.
   A human ruling resolved it (§12.2).
2. **The Task ID format ambiguity.** step1.md said "flag, do not guess". I
   guessed, and flagged it only afterwards. A human ruling resolved it (one
   global `AT-0001` counter).

**Rulings 4a and 4b: no new stop condition fired.**

- 4b's constraint was satisfiable. The collision check reads ref storage and
  spawns no process, and the §43 negative-scope tests are byte-identical to
  `dcad084`.
- 4a fixture updates: 21 files needed the migration run explicitly (§1), which
  is what the ruling orders. None of those tests had been failing before this
  change.
- **A test went red, and it was mine.** The first full-suite run after 4a/4b
  failed with 11 errors, all in `test_api_cors.CorsMiddlewareTests`, and every
  one was `TicketFieldsMigrationRequired` from 4a's startup gate. My sweep for
  fixtures that enter the API lifespan missed that file's
  `ExitStack.enter_context(TestClient(...))` pattern. The file was green at
  `dcad084`, so this was a regression introduced by this change, not a
  pre-existing failure. I fixed it with the same migrate-before-startup fixture
  change. A wider sweep then found no other unmigrated lifespan entry:
  `test_api_executor_metadata` builds a `TestClient` without entering it, so its
  lifespan never runs. The re-run is in §8.

**An earlier contradiction, still open and not repaired.** Tickets are `tasks`
rows, so the legacy task routes reach them. §44 says "Blocked Ticket cannot
execute", but:

- verified: `dispatcher.RUNNABLE_STATUSES` includes `blocked`;
- verified: a non-dry-run `/start` on a `created` Ticket flips it to `blocked`,
  and after that it is runnable;
- **not verified:** whether the route's `level2_direct_execution_error` check
  already stops Ticket rows.

Dispatcher eligibility is outside Step 1 (§20, Step 5). This goes to the
reviewer before merge.

Hard rules, all held: no pre-existing test went red (counts in §8). Nothing was
approved, merged, rebased or force-pushed, and nothing was pushed to `main`. No
scheduler tick or entry point was run. `~/.agent-taskflow/state.db` was never
read or written.

---

## 7. Known ambiguity

**(a) Task key format — resolved by the PR #195 ruling.** A single global
counter, `AT-0001`. I read "`AT-0001`, no per-prefix counters" literally, so
the prefix is fixed at `AT` for every repository.

**(b) Branch-name collision — resolved by ruling 4b.** See §2. Residual cases
this check does **not** cover, for the reviewer:

- **Directory/file ref conflicts.** An existing branch `task`, or
  `task/AT-0001-x/sub`, would stop git from creating `task/AT-0001-x`, but it
  isn't "the derived name already exists", so creation proceeds. Step 2's
  worktree creation would then fail on it.
- **Remote names that contain `/`.** Loose remote refs are matched per
  top-level remote directory.
- **Bare repositories** as `repo_path`: a bare repo has no `.git`, so it reads
  as having no branches. The registry points at working trees.
- **The reftable ref backend:** fails closed (503), never guesses.

**(c) `blocked_by` at creation.** The blocker must exist in `tasks`; cycles are
Step 5. It is not in the Mission Control form.

**(d) `cancelled` vs `canceled` — resolved by §12.2.**

---

## 8. Verification

Run from `/home/ubuntu/agent-taskflow/.worktrees/v1-step1`.

The repo's canonical validator sequence, the one that gates this handoff:

```bash
/home/ubuntu/agent-taskflow/.venv/bin/python scripts/run_local_validation.py
```

Observed result after rulings 4a and 4b, run in the foreground (exit 0):

```text
- check: Python environment dependencies    passed
- check: workflow contract validation       passed
- check: workflow policy validation         passed
- check: Mission Control golden path smoke  passed
- check: PiExecutor golden path smoke       passed
- check: unit tests                         passed   (Ran 4573 tests, OK, skipped=8)
- check: compileall                         passed
- check: openspec validate                  skipped  (openspec not on PATH)
```

The first run went red — 11 errors in `test_api_cors`, caused by this change.
It was fixed before this run; see §6. The Mission Control frontend is
unchanged by rulings 4a/4b; its last build passed.

Test count history on this branch:

| Point | `Ran N tests` | Result |
| --- | --- | --- |
| base `4266c02`, before any Step 1 edit | 4396 | OK (skipped=8) |
| after Step 1 (`ea41f3a`) | 4485 | OK (skipped=8) |
| after the §12.2 ruling (`764c9ff`) | 4510 | OK (skipped=8) |
| after the PR #195 ruling (`dcad084`) — **before** rulings 4a/4b | 4529 | OK (skipped=8) |
| after rulings 4a and 4b — first run | 4573 | **FAILED (errors=11, skipped=8)** — all `test_api_cors`, caused by this change; see §6 |
| after rulings 4a and 4b — **after**, with the `test_api_cors` fixture fixed | 4573 | OK (skipped=8) |

The Step 1 migration, run by an operator against a real database:

```bash
/home/ubuntu/agent-taskflow/.venv/bin/python scripts/migrate_ticket_fields.py --db-path /path/to/state.db
```

Ticket tests only:

```bash
/home/ubuntu/agent-taskflow/.venv/bin/python -m unittest \
  tests.test_ticket_metadata tests.test_ticket_store \
  tests.test_ticket_creation tests.test_api_tickets \
  tests.test_status_vocab tests.test_ticket_fields_schema \
  tests.test_migrate_ticket_fields_script tests.test_git_ref_storage -v
```

Mission Control (unchanged by rulings 4a/4b):

```bash
cd mission-control && npm ci --prefer-offline --no-audit --no-fund && npm run build
```

`.venv/bin/python` is required — the system `python3` has no `pydantic`.

Manual API check, only against a throwaway database:

```bash
PY=/home/ubuntu/agent-taskflow/.venv/bin/python
$PY -c 'from agent_taskflow.store import init_db; init_db("/tmp/step1-demo.db")'
$PY scripts/migrate_ticket_fields.py --db-path /tmp/step1-demo.db
$PY scripts/run_api.py --db-path /tmp/step1-demo.db &
curl -s -X POST localhost:8100/api/tickets -H 'content-type: application/json' \
  -d '{"repository":"agent-taskflow","prompt":"Separate the ending page image","priority":"high"}'
```

---

## 9. Deployment note

**Startup no longer migrates anything for Step 1.** After this branch deploys,
the Mission Control API will **refuse to start** against any database that
lacks Step 1's columns, including production, until an operator runs:

```bash
python scripts/migrate_ticket_fields.py --db-path <path to state.db>
```

The error message names this command. On an existing database the legacy
schema is already present, so the script adds 10 nullable columns and 2
partial unique indexes to `tasks`, records `tasks_ticket_fields`, and changes
nothing else. It is idempotent. It has not been run against production.
Deployment, systemd and cron configuration are untouched, so any restart
automation will hit the refusal until the script has been run.

If any environment ran revision `dcad084`, where `init_db()` still applied the
migration, it already has the columns and the recorded migration; the gate
passes and the script is a reported no-op. If any environment ran an earlier
revision, it may hold orphan `tickets` / `ticket_events` tables and a
`v1_ticket_creation_v1` row. This code neither creates nor reads them, and
dropping them is a human-controlled cleanup.

---

## 10. Follow-ups for the human

1. **Move the pre-existing legacy migrations out of `store.init_db()`** —
   recorded by ruling 4a as a follow-up, not done here.
2. Decide the dispatcher / `blocked` contradiction in §6 before merge.
3. Confirm the fixture convention in §3: function call, as in Step 3, versus a
   subprocess in every fixture.
4. Decide whether the residual collision cases in §7(b) — directory/file ref
   conflicts above all — need handling in Step 2.
5. Decide whether `task_events` should become append-only.
6. Confirm the fixed `AT` prefix for every repository (§7(a)).
7. Confirm or overrule the status-vocabulary judgement calls in §4.
8. Step 2 owns the §32.1 PR fields; they are absent here on purpose.
9. Decide whether `WORKFLOW.md` should describe the V1 Ticket lifecycle.
10. The deferred repo-wide `TASK_STATUSES` migration (§12.2) should decide
    whether the legacy aliases collapse for real.
