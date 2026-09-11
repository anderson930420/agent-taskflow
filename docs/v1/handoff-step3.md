# Handoff — V1 Step 3: Realtime Progress

Branch: `task/v1-step3`
Draft PR: https://github.com/anderson930420/agent-taskflow/pull/197
Base: `4266c02` (`main`), with `task/v1-step1` merged in **three times** — for
the SPEC §12.2 status-vocabulary bridge, for its post-review rework that
collapsed Tickets into `tasks`, and for its third rework (explicit Ticket-column
migration, fail-closed startup, 409 on a branch collision). Merges only; never
rebased (§4.6, §4.10).
Spec: `~/agent-taskflow-ops/v1/SPEC.md` §42 Step 3
Instructions: `~/agent-taskflow-ops/v1/step3.md`

**Scope of the "no lifecycle" claim.** Step 3's *own* code transitions no
lifecycle state, schedules nothing, integrates nothing, touches no git and
contacts no GitHub. The branch as a whole is not read-only, though: it carries
Step 1's merged code, and Step 1's `POST /api/tickets` writes a new `tasks` row
with its initial persisted status (`created`, or `blocked` when `blocked_by` is
given) plus a `created` event. That is Step 1's creation path (§12.1), not a
Step 3 transition, and no Step 3 code calls it.

> *Correction:* this line used to read "Nothing here transitions lifecycle",
> which stopped being true once Step 1 was merged in.

---

## 1. Read-only inventory (what was extended vs. created)

| Module | Extend / Create | Why |
|---|---|---|
| `agent_taskflow/execution_observability.py` | **extend (imported, not modified)** | §14 says Step 3 extends the existing `ExecutionObservedStep` / `UnifiedExecutionSummary`. The new vocabulary module imports and reuses `ExecutionObservedStep` and `to_observability_dict` rather than defining a parallel step shape. The file itself needed no edit. |
| `agent_taskflow/api/main.py` | extend | Added five read-only routes and an injectable `realtime_options`. No existing route changed. |
| `mission-control/lib/types.ts` | extend | Appended realtime payload interfaces. |
| `mission-control/lib/api.ts` | extend | Appended four read-only helpers. |
| `mission-control/app/tasks/[taskKey]/page.tsx` | extend | Mounted `<LiveTicketPanel>`; nothing removed. |
| `mission-control/components/TaskBoard.tsx` | extend | One sidebar link to `/live`. |
| `agent_taskflow/runtime_progress.py` | create | §14.1 vocabulary + §14.2 guard. |
| `agent_taskflow/runtime_progress_schema.py` | create | Additive attempt-scoped tables. |
| `agent_taskflow/runtime_progress_store.py` | create | The runtime progress write surface. |
| `agent_taskflow/realtime_projection.py` | create | Read-only board / Ticket projection. Consumes `status_vocab` (§12.2); holds no copy of the vocabulary bridge. |
| `agent_taskflow/api/realtime.py` | create | §15 SSE transport. |
| `agent_taskflow/status_vocab.py` | **consume (merged in from `task/v1-step1`)** | The single §12.2 bridge between the persisted and display vocabularies. Not modified here. |
| `scripts/migrate_runtime_progress.py` | create | Operator migration, matching the repo's `migrate_*.py` pattern. Fails closed (exit 2) without the lifecycle schema; never installs it. |
| `mission-control/lib/realtime.ts`, `components/ExecutionStepList.tsx`, `components/LiveBoard.tsx`, `components/LiveTicketPanel.tsx`, `app/live/page.tsx` | create | §16 live board and §17 live Ticket page. |

No existing module was rewritten.

---

## 2. What was implemented

### Runtime progress vocabulary — `agent_taskflow/runtime_progress.py`

- §14.1 first-level steps: `Prepare, Scout, Planner, Implementer, Reviewer,
  Validator, Integration`, in spec order.
- §14.1 statuses: `pending / running / passed / failed / blocked`.
- §14.2 glyphs (`○ ● ✓ ✗ ⊘`) — never a number.
- `AttemptProgressSnapshot`: attempt-scoped progress (§14.0), with
  `ordered_steps()` defaulting unrecorded steps to `pending`.
- `find_progress_estimates()` / `assert_no_progress_estimate()`: the §14.2
  guard. It walks any payload and reports the dotted path of any key or string
  that claims a share of work or a countdown. It is enforced **at write time**,
  not only in tests.

### Attempt-scoped progress writes — `runtime_progress_schema.py`, `runtime_progress_store.py`

Two additive tables, both hanging off an Attempt (§14.0):

- `attempt_progress` — one row per Attempt: `current_phase`, `current_activity`.
- `attempt_observed_steps` — one row per (Attempt, step): status, summary,
  metadata.

Both carry `CHECK` constraints on the step and status vocabularies and a
`BEFORE INSERT` trigger asserting the Attempt belongs to the Task, mirroring the
existing `lifecycle_events` guard in `attempt_schema.py`.

**The migration never installs lifecycle schema, and fails closed without it.**
Both tables foreign-key into `attempts` and join on `tasks.task_id`, which come
from the Level 2 lifecycle migration `level2_task_attempt_lifecycle_v1`. If
that schema is missing, `migrate_runtime_progress` raises
`RuntimeProgressPreconditionError` before writing anything — the check opens
the database read-only, so it cannot even create the file — and the message
names the command to run by hand:

```
python scripts/migrate_task_attempt_lifecycle.py --db-path <db>
```

The operator script exits 2 with the same instructions. The migration adds no
column to any existing table, `tasks` included;
`tests/test_runtime_progress_schema_diff.py` proves it by diffing the whole
schema of a real fixture database.

> **Correction.** Earlier rounds said this migration "touches nothing that
> already exists" and created no Ticket lifecycle column. That was false.
> `migrate_runtime_progress` used to call `migrate_task_attempt_lifecycle`
> first, which — measured on a base database — adds **six** columns to `tasks`
> (`task_id`, `task_class`, `active_attempt_id`, `final_outcome`, `closed_at`,
> `is_legacy`) and two tables (`attempts`, `lifecycle_events`). The review
> cited three of those columns. The source-scan test missed it because the
> writes lived in another module. Both are fixed: the chained call is gone, and
> a schema-diff test now backs the scan instead of relying on it.

`RuntimeProgressStore` exposes exactly two §14 writes — `record_step` and
`set_current_activity` — plus read helpers. It writes to those two tables and
nothing else. A retry creates a new Attempt and the earlier Attempt's
ObservedStep rows survive untouched.

Reads are batched: `snapshots_for_attempts()` resolves the whole board over a
single connection, because the SSE stream rebuilds the board projection on every
poll and a per-Ticket lookup would otherwise open one SQLite connection per
Ticket per poll.

### Read-only projection — `agent_taskflow/realtime_projection.py`

- `build_board_projection()` — the live board: `NEEDS DECISION` on top (human
  ruling 5, §4.3), then the five §16 sections
  `RUNNING / READY / BLOCKED / PAUSED / READY FOR REVIEW` in spec order, plus an
  `unsectioned` bucket for closed work and anything the projection cannot map.
  Every section serialises `read_only: true, actions: []`.
- `build_ticket_projection()` — the §17 Ticket page: repository, priority,
  status, branch, worktree, execution steps, current activity, artifact links,
  plus the §31 read-only review surface (PR identity, Taskflow validator
  evidence, GitHub CI summary, reviewer hints, `integrated_base_sha`).
- Per-ticket booleans `running` / `eligible_for_execution` / `blocked` /
  `paused` are computed from the section, so a BLOCKED or PAUSED Ticket is
  structurally incapable of rendering as running or as eligible.
- Sectioning runs on the §12 *display* vocabulary: persisted status →
  `status_vocab.to_display_status()` → `DISPLAY_STATUS_SECTIONS`. Per §12.2 the
  projection keeps no copy of the legacy↔display mapping, and a value outside
  both vocabularies renders unsectioned rather than raising.
- BLOCKED renders its blocker as §16 shows it — `Waiting for AT-101`.
- Every value also ships a `display` mapping with `—` substituted for null.

### §32.1 PR fields — read-only and optional

Step 2 has not landed, so none of the §32.1 columns exist yet. (Step 1 *has*
now been merged into this branch — see §4.1 — but it does not touch these
columns.) The projection:

- discovers which of the twelve columns exist via `PRAGMA table_info(tasks)`
  and reads only those;
- renders `—` for a missing column, a missing row, or a null value;
- never creates, migrates, renames, backfills, or writes any of them;
- never polls — it reads persisted state only.

Tests cover all three states: no columns, columns present but null, and columns
present with values. A partial rollout (some columns, not others) also renders.

### SSE — `agent_taskflow/api/realtime.py` (§15, §15.1)

- Every connection sends a full state snapshot first, then streams updates.
- **No `id:` line is ever emitted**, so a client cannot form a resume contract.
  A `Last-Event-ID` request header is ignored. There is no replay and no
  backfill; reconnect correctness comes from the snapshot alone.
- An update frame is emitted only when the projected state actually changed.
- The loop is factored so the semantics are unit-testable without HTTP
  (`RealtimeEventSource`, `iter_realtime_events`) and the route is a thin async
  adapter that also honours client disconnect.

### API routes (all read-only)

```
GET /api/realtime/board                     # §16 board snapshot
GET /api/realtime/stream                    # §15 SSE board stream
GET /api/tasks/{task_key}/realtime          # §17 Ticket snapshot (?attempt_id=)
GET /api/tasks/{task_key}/realtime/stream   # §15 SSE Ticket stream
GET /api/tasks/{task_key}/attempts          # §14.0 Attempt list
```

`create_app(..., realtime_options=RealtimeStreamOptions(...))` bounds the stream
for tests; the default is unbounded.

### Mission Control

- `/live` — the §16 live board, server-rendered from a snapshot then subscribed
  to SSE. Five sections, per-Ticket step glyphs, current activity, blocker hint.
- `<LiveTicketPanel>` on the Ticket detail page — §17 fields, the seven-step
  execution list, an Attempt selector (latest by default, earlier Attempts stay
  viewable), the read-only review surface, Taskflow validator evidence rendered
  **separately** from the GitHub CI status, and reviewer hints.
- The panel states in the UI that GitHub CI is not a Taskflow lifecycle
  authority (§30).

---

## 3. What was deliberately skipped, and why

1. **Runtime call-site wiring — reclassified.** Earlier rounds filed this here
   as a deliberate skip. The review is right that it is a **stop condition that
   was hit**, not a skip. It now lives in §4.7.
2. ~~No board section for `needs_decision`~~ — **resolved by human ruling 5**:
   it now has its own read-only section at the top of the board. See §4.3.
3. **No progress percentage, ETA, or completion estimate** (§14.2), **no DAG
   visualization** (§16/§41). Both are forbidden and both are enforced by
   negative-scope tests over the Python modules *and* the frontend templates.
4. **No GitHub call of any kind** — no polling, no PR create/update/comment/
   merge. Enforced by an AST import check plus a token scan.
5. **No validator execution**, no scheduler, no lease/claim/capacity, no
   integration controller or per-repo lock, no cleanup, no Ticket creation or
   metadata derivation, no webhook path.
6. **Step 3 adds no startup migration — but the app does migrate at startup.**
   *Correction:* this item used to read "No auto-migration at API startup",
   which was false for the app as a whole. What is true: `create_app` never runs
   Step 3's `migrate_runtime_progress`, and that migration never chains another
   (pinned by `test_startup_runs_no_step3_migration`). What is also true: the
   lifespan still calls `store.init_db()`, which is pre-existing on `main` and
   runs its legacy `_MIGRATIONS` registry. Since Step 1's third rework it no
   longer applies Step 1's columns, and `ticket_store.init_db()` fails closed
   instead of migrating — see §4.8. The read path
   tolerates Step 3's tables being absent and renders an all-`pending` board.
7. **`WORKFLOW.md` was not edited.** Repo convention gives each component a
   paragraph there, but that is outside the layers step3.md allows and CLAUDE.md
   says not to edit unrelated files. Flagged here as a documentation follow-up
   for the human reviewer.

---

## 4. Stop conditions hit

Per step3.md, these are reported, **not repaired**.

### 4.1 Ticket status vocabulary — RESOLVED by human ruling (SPEC §12.2)

**Originally reported as a contradiction; the human has ruled and this branch
now implements the ruling.**

The conflict was: SPEC §12 names (`ready`, `paused`, `needs_review`,
`needs_decision`, `ready_for_integration`, `integrating`, `failed`,
`cancelled`) were not writable statuses, and §12 spells it `cancelled` where
the repo persists `canceled`.

**Ruling (SPEC §12.2):** no repo-wide migration. `TASK_STATUSES` stays the
canonical *persisted* vocabulary; §12 names are the Mission Control *display*
vocabulary; `agent_taskflow/status_vocab.py` is the single bridge. Persisted
spelling of cancelled stays `canceled`. A repo-wide rename is deferred to its
own ticket after Steps 1–3 merge.

**What changed here:**

1. `task/v1-step1` was **merged** into this branch (not rebased — see §4.6) to
   bring in `status_vocab.py`.
2. The dual-vocabulary `STATUS_SECTIONS` table is **deleted**. The projection
   no longer carries any copy of the legacy↔display mapping.
3. `realtime_projection.py` now converts persisted → display with
   `status_vocab.to_display_status()` and keeps only
   `DISPLAY_STATUS_SECTIONS`, a §16 *board layout* map keyed exclusively by the
   14 §12 display names. That map is Step 3's own concern (which of the five
   sections a display status belongs to), not a status vocabulary.
4. `UNSECTIONED_DISPLAY_STATUSES` is derived, not hand-written, so the board
   note now lists every §12 status with no §16 section
   (`needs_decision, completed, failed, cancelled`) instead of hard-coding one.
5. `BoardTicket` exposes **both** spellings: `status` (persisted — the
   auditable truth, §44) and `display_status` (§12). The Ticket page shows the
   §12 name as *Status* and the persisted value as *Persisted status*
   underneath, so a reviewer can check the render against the database.

Behaviour this corrected, which the old dual table got wrong:

- `created` now displays as `ready` and lands in **READY**; it was previously
  unsectioned.
- `accepted` now displays as `needs_review` and stays in **READY FOR REVIEW**,
  matching §33.1 (approved but not yet merged is still awaiting review). It was
  previously unsectioned.
- The external mirror spellings `backlog` / `todo` / `done` are now placed
  rather than dropped off the board.

A test asserts the projection source contains no legacy-only status literal, so
the mapping cannot drift back in. Another walks every value in
`PERSISTED_TO_DISPLAY` through the board and asserts each one is placed.

A value outside **both** vocabularies (which `status_vocab` raises on) is caught
and rendered unsectioned with `display_status = —`, never as an error: the hard
rule is that nothing blocks a render.

**No decision outstanding.**

### 4.2 §14.1 and §14.2 disagree on step order (ambiguity)

§14.1 lists the first-level steps as `... Reviewer, Validator, Integration`.
The §14.2 UI sample renders `... Implement, Validate, Integrate, Review` —
Review last. Step 3 uses the **§14.1 order** for storage (`step_order`) and for
rendering, everywhere. Flagged rather than guessed at a second ordering.

### 4.3 `needs_decision` board section — RESOLVED by human ruling 5

**Originally reported as the flagged watchlist item:** §16 shows five sections
and none of them holds `needs_decision`, so those Tickets were listed as
unsectioned and the question went to the human.

**Ruling 5:** add a `needs_decision` section at the **top** of the board, above
RUNNING, resolved through `status_vocab`, read-only, offering no actions.

**What changed:**

- `realtime_projection.py` — `BOARD_SECTION_NEEDS_DECISION = "NEEDS DECISION"`
  is first in `BOARD_SECTIONS`. `DISPLAY_STATUS_SECTIONS` gains
  `"needs_decision"`, keyed by the §12 display name like every other entry. The
  projection names no persisted value: whatever `status_vocab` resolves to
  `needs_decision` — today the canonical `needs_decision` plus the legacy
  aliases `rejected` and `unknown` — lands there. Tickets carry a new
  `awaiting_decision` flag, and are never `running`, `eligible_for_execution`,
  `blocked`, `paused` or `awaiting_review`.
- Every `BoardSection` now serialises `read_only: true` and `actions: []`, so
  "offers no actions" is a checked API contract rather than an assumption.
- `UNSECTIONED_DISPLAY_STATUSES` is still derived. It drops `needs_decision`
  and now reads `completed, failed, cancelled`; the board note says so.
- `LiveBoard.tsx` renders the section first, with a read-only note ("Mission
  Control offers no decision actions here") and an "awaiting a human decision"
  badge on each card. The card stays a link to the read-only Ticket page —
  navigation, not an action. The file has no button, form, or click handler.

**Tests rewritten because the ruling reverses what they pinned** (these are
this branch's own tests, not pre-existing failures):
`test_needs_decision_is_unsectioned_and_flagged_as_ambiguous` and
`test_needs_decision_ticket_is_not_placed_in_the_five_sections` are replaced by
`NeedsDecisionSectionTests`, and the five-section order assertions in the
projection, API and frontend-source tests now expect six sections with NEEDS
DECISION first.

**New coverage:** the Ticket appears in that section and in no other; every
persisted alias `status_vocab` maps to `needs_decision` lands there (the test
asks `status_vocab` which values those are rather than listing them); the
section is first; it and every other section is read-only with no actions; and
the same holds end to end through `GET /api/realtime/board`.

**No decision outstanding.**

### 4.4 `ready_for_integration` / `integrating` are not named by §16 (ambiguity)

§16 shows five sections but §12 has states between "running" and "ready for
review". These two are placed in **RUNNING** on the reading that they are
Taskflow-owned and in flight, not human-reviewable. This is a judgment call, not
a spec statement — flagged for confirmation.

### 4.5 §32.1 columns do not exist yet (expected, handled)

Step 2 has not landed. This is anticipated by the step3.md hard rules and is
handled as specified: read defensively, render `—`, never create. Recorded here
because it is the reason several PR fields render as `—` in every screenshot
today.

### 4.6 Merge, not rebase, onto a published PR branch

The §12.2 ruling required picking up `task/v1-step1`. This branch is published
as draft PR #197, and SPEC §26 keeps the no-force-push invariant for a published
PR branch in V1, so a **merge** was used and the merge commit is the accepted
cost. The branch was never rebased and never force-pushed.

Three files conflicted, all additive on both sides and resolved as unions:

- `agent_taskflow/api/main.py` — both branches extended the `create_app`
  signature and appended routes. Kept both; verified both route sets coexist.
- `mission-control/components/TaskBoard.tsx` — auto-merged.
- `HANDOFF.md` — add/add. **Both documents were kept**; Step 1's is reproduced
  verbatim in the appendix rather than dropped.

One post-merge rename for clarity: the attempts route handler was
`list_ticket_attempts`, which now reads confusingly next to Step 1's separate
Ticket entity. It is `list_task_attempts`; the route path is unchanged.

**Second merge (post-review).** `task/v1-step1` was reworked after its own
review (`dcad084`: Tickets collapsed into `tasks`, one global `AT-0001` counter)
and merged again as `1141a25`, again without rebasing. Step 1's history was
linear on top of the first merge, so only `dcad084` came in. It merged with **no
conflicts**: `TaskBoard.tsx` kept `/live` while Step 1 reverted its own
"Create Ticket" link, and `HANDOFF.md` auto-merged because this side only
prepends the Step 3 document — the appendix now carries Step 1's rewritten
handoff. Suite before/after the merge: 4682 → 4701 (§5).

### 4.7 Runtime ObservedStep writes have no producer — STOP CONDITION HIT

Reclassified from §3.1 at the review's request. §42 lists "Runtime ObservedStep
writes" as a Step 3 deliverable. Step 3 ships the write surface
(`RuntimeProgressStore.record_step`, `.set_current_activity`), but nothing in
the runtime calls it, so every board step renders `pending` today.

Producing those writes needs a forbidden layer, which is exactly the case
step3.md covers ("If the step seems to require a forbidden layer, stop and
write it in the handoff note instead of proceeding"):

- The producer is the execution runtime — `dispatcher.py` /
  `approved_task_runner.py` — the control plane that owns lifecycle (§2.1).
- §14.0 attaches ObservedSteps to an **Attempt**. The legacy dispatcher path
  creates none, and neither does `POST /api/tickets`: on a freshly started app
  there is no `attempts` table at all (§4.9). Creating an Attempt from Step 3
  would itself be a lifecycle write.

**Decision needed from the human:** who wires `RuntimeProgressStore` into the
executor loop, and where a Ticket's Attempt gets created so there is something
to attach steps to. Not repaired here.

### 4.8 "Nothing may run migrations at process startup" — PARTLY RESOLVED; still contradicts `main`

For Step 3's own code the rule holds and is tested: the app never applies
`v1_step3_runtime_progress_v1`, and that migration chains nothing
(`test_startup_runs_no_step3_migration`,
`tests/test_runtime_progress_schema_diff.py`).

**Step 1's half is resolved by Step 1's third rework** (`dabc709`, merged here,
§4.10). `tasks_ticket_fields` is gone from `store.py`'s `_MIGRATIONS` registry
and runs only from the operator script `scripts/migrate_ticket_fields.py`.
`ticket_store.init_db()` no longer migrates: it ensures the base schema, then
fails closed with `TicketFieldsMigrationRequired` when Step 1's columns are
missing. Both behaviours were observed for this handoff (§6).

**What still contradicts the rule is pre-existing on `main`.** Startup still
calls `store.init_db()`, which creates the base tables and runs the three
legacy migrations left in its `_MIGRATIONS` registry (`tasks_blocked_reason`,
`tasks_executor_selection`, `task_worktrees_base_sha`), and
`ticket_store.init_db()` calls the same `init_task_db` again before its gate.
That is neither Step 3's code nor Step 1's rework, so it is reported, not
repaired.

**Decision needed from the human:** whether the rule is repo-wide, in which
case `store.init_db()`'s startup migrations need their own ticket.

### 4.9 Review item 3c — the reviewer's reproduction, run for real

The board and detail endpoints read the canonical `tasks` table. Run through the
real app (`create_app`, real lifespan) against a scratch database, with nothing
seeded by hand:

```
POST /api/tickets -> 200
  task_key=AT-0001 status=created display_status=ready
GET /api/realtime/board -> 200
  AT-0001 found in section 'READY'; status=created display_status=ready priority=high eligible_for_execution=True
GET /api/tasks/AT-0001/realtime -> 200
  priority='high' repository='forms' branch='task/AT-0001-separate-the-ending-page-image-from-the'
  worktree_path='/tmp/tmphtb2krbk/sandbox/forms/.worktrees/AT-0001'
  steps=[('Prepare', 'pending'), ('Scout', 'pending')] ... (all pending: True)
Step 3 tables created by startup: none
```

One gap found while preparing it: the projection read branch and worktree only
from `task_worktrees`, but Step 1 stores a Ticket's derived branch and worktree
path on `tasks` and writes no `task_worktrees` row — so for a Step 1 Ticket
they would have rendered `—`. It now prefers a `task_worktrees` record (a
worktree that was actually prepared) and falls back to the `tasks` columns.
Pinned by `TicketReproductionTests` in `tests/test_api_realtime.py` — five
tests, including a `blocked_by` Ticket rendering `Waiting for <key>` from
`tasks.blocked_by`. Since the third Step 1 merge its fixture runs Step 1's
explicit migration first, as an operator must; the five tests still pass
(§4.10).

### 4.10 Third Step 1 merge (`dabc709`) and the fixture fallout

Step 1 was reworked a third time: its `tasks` columns now come only from the
operator-run `scripts/migrate_ticket_fields.py`, API startup fails closed when
they are absent, and Ticket creation refuses a branch-name collision with 409.
It was merged here as `533e9ab` — a merge, not a rebase, with **no
conflicts**. Step 1's history was linear on `dcad084`, so only `dabc709` came
in. `main.py` took Step 1's fail-closed comment beside the realtime routes, and
`HANDOFF.md`'s appendix now carries Step 1's rewritten handoff.

**Fallout, measured before touching anything.** On the merged tree, before any
fixture change, the eight Step 3 test files ran **27 failed / 176 passed**. All
27 were in `tests/test_api_realtime.py`, and every one entered the API lifespan
on a database without Step 1's columns:

| Fixture | Lifespan entry | Failed |
|---|---|---|
| `RealtimeApiTestCase.setUp` (board, ticket and SSE endpoint classes) | `TestClient(...).__enter__()` | 21 |
| `TicketReproductionTests.setUp` (fresh database) | `TestClient(...).__enter__()` | 5 |
| `DefaultAppWiringTests.test_startup_runs_no_step3_migration` (fresh database) | `with TestClient(...)` | 1 |

**Fix: fixtures only.** Each now runs Step 1's migration before entering the
lifespan: `TaskMirrorStore(db).init_db()` where the database is fresh (the
migration needs the base schema and fails closed without it), then
`migrate_ticket_fields(db)`. Test intent is unchanged — the startup test still
asserts that startup installs none of Step 3's schema. After the fix, the same
eight files plus Step 1's ticket suites ran **264 passed, 0 failed**.

Unaffected, checked across all three lifespan entry patterns: the second app in
the SSE update test reuses the already-migrated database, and
`test_default_app_exposes_realtime_routes` never enters its client. The
projection, store, schema-diff and migration-script tests never start the app.

**Convention.** Fixtures call `migrate_ticket_fields()`, the function
`scripts/migrate_ticket_fields.py` wraps. That is the convention Step 1's
handoff sets ("Fixture convention (4a)"), where the script itself runs end to
end as a subprocess in Step 1's own script test. The same open question
applies here: if "run the script explicitly" means a subprocess in every
fixture, switching is mechanical, at a noticeable cost to suite time.

**Optional hardening, not fallout.** `tests/test_runtime_progress_schema_diff.py`
did not fail, but its fixture comment claimed API startup applies Step 1's
columns, which `dabc709` made false. The fixture now also applies
`migrate_ticket_fields`, so it mirrors an operator-prepared database and the
whole-schema diff proves Step 3's migration leaves Step 1's columns and unique
indexes untouched.

**Not changed, and not needed:** Step 1's code, the fail-closed startup gate,
and `init_db()`. No Step 3 production file was touched in this round — only
tests and this handoff.

### Not hit

- No pre-existing test went red, in any round (§5).
- No push was rejected; the branch never diverged from `origin`.

---

## 5. Validation

This round's counts use `python -m unittest discover -s tests` — the repo's
documented runner, and the one the reviewer used. Earlier rounds quoted pytest
counts, which are not comparable to unittest's `Ran N tests`; they are kept
below for history only.

### Third Step 1 merge round (`dabc709`)

| Point | Tree | Result |
|---|---|---|
| BEFORE the merge | `8f26d8f` | Ran 4732 tests, OK (skipped=8) |
| AFTER the merge and the fixture fixes | `533e9ab` + the fixture fixes in the commit carrying this handoff | Ran 4776 tests, OK (skipped=8) |

- Delta **+44** is entirely Step 1's: `dabc709` reports its own suite going 4529 → 4573 (+44). The fixture fixes here add no test methods; they only change `setUp` bodies and imports.
- **No test went red** in either full run. The only reds this round were the 27 expected fixture failures, measured on the merged tree before the fix and listed in §4.10. Skips unchanged at 8.

| Command | Result |
|---|---|
| `python -m compileall agent_taskflow scripts tests` | OK |
| `scripts/validate_workflow_contract.py` | passed |
| `scripts/validate_workflow_policy.py` | passed |
| `cd mission-control && npm run build` | compiled, TypeScript clean — rerun although no `mission-control/` file changed in this round |

### Ruling 5 round (NEEDS DECISION section)

| Point | Tree | Result |
|---|---|---|
| BEFORE | `e0c2583` | Ran 4722 tests, OK (skipped=8) |
| AFTER | `e0c2583` + the ruling 5 change in the commit carrying this handoff | Ran 4732 tests, OK (skipped=8) |

- Delta **+10** is exactly the test change: `test_realtime_projection.py` replaces two tests with the seven in `NeedsDecisionSectionTests` (+5), `test_api_realtime.py` gains one (+1), and `test_mission_control_realtime_frontend.py` gains four (+4).
- **No test went red.** Skips unchanged at 8.

| Command | Result |
|---|---|
| `python -m compileall agent_taskflow scripts tests` | OK |
| `scripts/validate_workflow_contract.py` | passed |
| `scripts/validate_workflow_policy.py` | passed |
| `cd mission-control && npm run build` | compiled, TypeScript clean; `/live` still listed |

No `task/v1-step1` merge in this round, by instruction.

### Post-review round

| Point | Tree | Result |
|---|---|---|
| BEFORE the second merge | `760d015` | Ran 4682 tests, OK (skipped=8) |
| AFTER the merge | `1141a25` | Ran 4701 tests, OK (skipped=8) |
| AFTER the Step 3 fixes | `1141a25` + the fixes in the commit carrying this handoff | Ran 4722 tests, OK (skipped=8) |

- Merge delta **+19** equals `dcad084`'s own reported delta (4510 → 4529).
- Fix delta **+21** is exactly the new tests: schema-diff file 10, fail-closed
  script tests +4, reviewer reproduction 5, startup-runs-no-Step-3-migration 1,
  cross-module migration import check 1.
- **No test went red at any point.** Skips unchanged at 8.

| Command | Result |
|---|---|
| `python -m compileall agent_taskflow scripts tests` | OK |
| `scripts/validate_workflow_contract.py` | passed |
| `scripts/validate_workflow_policy.py` | passed |
| `cd mission-control && npm run build` | completed; route table lists `/live`, `/tickets/[taskKey]`, `/tickets/new` alongside the existing routes |

### Earlier rounds (pytest, history only)

- baseline `4266c02`: 4390 passed, 8 skipped
- `e3c7656`, Step 3 alone: 4552 passed, 8 skipped
- `760d015`, after the first Step 1 merge and §12.2: 4676 passed, 8 skipped

---

## 6. Exact commands to verify

Run from the worktree root, `/home/ubuntu/agent-taskflow/.worktrees/v1-step3`.

Use the repo virtualenv — the system `python3` lacks `pydantic`/`fastapi`:

```bash
PY=/home/ubuntu/agent-taskflow/.venv/bin/python
```

### Full Python suite

```bash
$PY -m pytest tests -q
```

### Step 3 tests only (the acceptance gate)

```bash
$PY -m pytest -q \
  tests/test_runtime_progress.py \
  tests/test_runtime_progress_store.py \
  tests/test_realtime_projection.py \
  tests/test_api_realtime.py \
  tests/test_realtime_negative_scope.py \
  tests/test_mission_control_realtime_frontend.py \
  tests/test_migrate_runtime_progress_script.py \
  tests/test_runtime_progress_schema_diff.py
```

Mapping to the acceptance gate:

| Gate item | Test |
|---|---|
| §43.11 runtime state in Mission Control | `test_realtime_projection.py::RuntimeStateOnTheBoardTests`, `test_api_realtime.py::RealtimeBoardEndpointTests`, `RealtimeTicketEndpointTests` |
| §43.11 SSE delivers updates as state changes | `test_api_realtime.py::RealtimeEventSourceTests`, `SseStreamEndpointTests::test_stream_delivers_an_update_when_state_changes` |
| §43.5 display half — blocked / paused | `test_realtime_projection.py::BlockedAndPausedDisplayTests` |
| §43.15 / §43.16 display half — validators and PR | `test_realtime_projection.py::PrFieldsAreReadOnlyAndOptionalTests`, `TicketDetailProjectionTests` |
| §43.27 red CI does not mutate lifecycle | `test_realtime_projection.py::GithubCiIsNotALifecycleAuthorityTests` |
| §44 paused cannot acquire work | `BlockedAndPausedDisplayTests::test_paused_ticket_is_never_rendered_as_eligible` |
| §44 blocked renders its blocker | `BlockedAndPausedDisplayTests::test_blocked_ticket_renders_its_blocker` |
| §44 SSE is a projection, never its own truth | `test_realtime_projection.py::ProjectionIsReadOnlyTests`, `test_runtime_progress_store.py::NoLifecycleWriteTests` |
| §15.1 no replay / no `Last-Event-ID` | `test_api_realtime.py::SseFormattingTests`, `SseStreamEndpointTests::test_stream_ignores_a_last_event_id_header` |
| §14.0 latest Attempt by default, earlier viewable | `test_realtime_projection.py::AttemptScopedDetailTests`, `test_runtime_progress_store.py::RetryKeepsEarlierAttemptTests` |
| Negative: no percentage / ETA / estimate | `test_realtime_negative_scope.py::NoProgressEstimateAnywhereTests` |
| Negative: no lifecycle status write | `test_runtime_progress_schema_diff.py` (authoritative: whole-schema diff), `test_realtime_negative_scope.py::NoLifecycleWriteInStep3CodeTests` (source scan, not sufficient alone) |
| Review 3c: board and detail read `tasks` | `test_api_realtime.py::TicketReproductionTests` |
| Ruling 5: NEEDS DECISION section on top, read-only | `test_realtime_projection.py::NeedsDecisionSectionTests`, `test_api_realtime.py::RealtimeBoardEndpointTests::test_needs_decision_ticket_is_on_top_read_only`, `test_mission_control_realtime_frontend.py::LiveBoardTests` |
| Review 3a: fail closed, never chain a lifecycle migration | `test_runtime_progress_schema_diff.py::FailClosedPreconditionTests`, `test_migrate_runtime_progress_script.py::FailClosedTests` |
| Negative: no Git / GitHub call | `test_realtime_negative_scope.py::NoGitOrGithubCallInStep3CodeTests` |

### Byte-compile

```bash
$PY -m compileall agent_taskflow scripts tests
```

### Repo validators

```bash
$PY scripts/validate_workflow_contract.py
$PY scripts/validate_workflow_policy.py
```

### Mission Control build

`node_modules` is not committed. A fresh worktree has none; restore it offline
(~8s), then build:

```bash
cd mission-control
npm ci --prefer-offline
npm run build
```

Do **not** symlink `node_modules` to another checkout — Turbopack rejects a
symlink that points outside the project root and the build fails with
`Symlink [project]/node_modules is invalid`.

### Manual smoke (optional)

Against a **scratch** database — never the production one. This exact sequence
was run for this handoff, after the third Step 1 merge:

```bash
DB=/tmp/step3-smoke/state.db
mkdir -p /tmp/step3-smoke

# 1. Step 3's migration alone refuses: exit 2, and the DB file is not created.
$PY scripts/migrate_runtime_progress.py --db-path "$DB"

# 2. Install the lifecycle schema on purpose, as the refusal instructs.
$PY scripts/migrate_task_attempt_lifecycle.py --db-path "$DB"

# 3. Step 3's migration now installs its two tables and adds no tasks column.
$PY scripts/migrate_runtime_progress.py --db-path "$DB"

# 4. Step 1's explicit Ticket-column migration; the API will not start without it.
$PY scripts/migrate_ticket_fields.py --db-path "$DB"
```

Observed: step 1 exit 2, no file created; step 2 exit 0; step 3 exit 0 with
`task_columns_added_by_this_migration: []`; step 4 exit 0 with
`still_missing: []`. Entering the API lifespan against that database then
succeeded, and `GET /api/realtime/board` returned 200 with the six sections
`NEEDS DECISION, RUNNING, READY, BLOCKED, PAUSED, READY FOR REVIEW`. As a
control, entering the lifespan on a fresh database without step 4 was refused
with `TicketFieldsMigrationRequired`.

Caveat: `migrate_task_attempt_lifecycle.py` has no source-package bootstrap, so
it imports `agent_taskflow` from the venv's editable install — which points at
the **main** checkout, not this worktree. That is harmless here because neither
branch changes the lifecycle migration. `migrate_runtime_progress.py` and
`migrate_ticket_fields.py` both bootstrap this worktree's source.

Then point the API at it and read:

```bash
curl -s localhost:8100/api/realtime/board | head -c 400
curl -sN localhost:8100/api/realtime/stream | head -c 400
```

The first frame is `event: snapshot`, and no frame ever contains an `id:` line.

---

## 7. Governance

- No commit was pushed to `main`; nothing was merged to `main`; no force-push;
  the branch was never rebased. Only `task/v1-step3` was pushed, with normal
  pushes.
- The PR is a **draft**.
- No task was approved, closed, or marked complete.
- No deployment, systemd, nginx, or cron configuration was touched.
- The production database was not read or written. Every test uses a
  `TemporaryDirectory`.
- Human review remains the final gate.

---
---

# Appendix — Step 1 handoff, carried in by the merge

`task/v1-step1` was merged into this branch to pick up
`agent_taskflow/status_vocab.py` (the SPEC §12.2 ruling). Both branches wrote a
`HANDOFF.md` at the repo root, so the add/add conflict was resolved by keeping
**both** documents rather than dropping either. Step 1's handoff is reproduced
verbatim below; it describes PR #195, not this one.

---

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
