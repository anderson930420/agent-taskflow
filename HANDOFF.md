# Handoff — V1 Step 3: Realtime Progress

Branch: `task/v1-step3`
Draft PR: https://github.com/anderson930420/agent-taskflow/pull/197
Base: `4266c02` (`main`), with `task/v1-step1` merged in for the SPEC §12.2
status vocabulary bridge (merge, not rebase — see §4.6)
Spec: `~/agent-taskflow-ops/v1/SPEC.md` §42 Step 3
Instructions: `~/agent-taskflow-ops/v1/step3.md`

Step 3 only. Nothing here transitions lifecycle, schedules, integrates, touches
git, or contacts GitHub.

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
| `scripts/migrate_runtime_progress.py` | create | Operator migration, matching the repo's `migrate_*.py` pattern. |
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

`RuntimeProgressStore` exposes exactly two §14 writes — `record_step` and
`set_current_activity` — plus read helpers. It writes to those two tables and
nothing else. A retry creates a new Attempt and the earlier Attempt's
ObservedStep rows survive untouched.

Reads are batched: `snapshots_for_attempts()` resolves the whole board over a
single connection, because the SSE stream rebuilds the board projection on every
poll and a per-Ticket lookup would otherwise open one SQLite connection per
Ticket per poll.

### Read-only projection — `agent_taskflow/realtime_projection.py`

- `build_board_projection()` — the §16 board: exactly
  `RUNNING / READY / BLOCKED / PAUSED / READY FOR REVIEW`, in spec order, plus
  an `unsectioned` bucket for everything the spec does not place on the board.
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

1. **No runtime call sites were wired.** Step 3 ships the progress *write
   surface* (`RuntimeProgressStore`), which is the allowed layer. It does not
   call it from `dispatcher.py` / `approved_task_runner.py`, because those are
   the control plane that owns lifecycle (§2.1) and "any lifecycle state
   transition" is a forbidden layer for this step. Wiring is further blocked by
   a real design gap: §14.0 requires an Attempt to attach ObservedSteps to, and
   the legacy dispatcher path does not own one — creating an Attempt from Step 3
   would itself be a lifecycle write. **This is the main follow-up** and belongs
   with whoever owns the executor loop. Consequence today: the board and Ticket
   page render every step as `pending` until a runtime starts recording.
2. **No board section for `needs_decision`** — this is the flagged watchlist
   item; see §4.3 below.
3. **No progress percentage, ETA, or completion estimate** (§14.2), **no DAG
   visualization** (§16/§41). Both are forbidden and both are enforced by
   negative-scope tests over the Python modules *and* the frontend templates.
4. **No GitHub call of any kind** — no polling, no PR create/update/comment/
   merge. Enforced by an AST import check plus a token scan.
5. **No validator execution**, no scheduler, no lease/claim/capacity, no
   integration controller or per-repo lock, no cleanup, no Ticket creation or
   metadata derivation, no webhook path.
6. **No auto-migration at API startup.** `create_app` does not run
   `migrate_runtime_progress`; that would write schema to whatever database the
   service points at. Operators run the migration script explicitly, matching
   every other migration in this repo. The read path tolerates the tables being
   absent and renders an all-`pending` board.
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

### 4.3 `needs_decision` has no §16 board section (the flagged watchlist item)

Handled without guessing: a `needs_decision` Ticket is placed in `unsectioned`,
is never `running` and never `eligible_for_execution`, and every board payload
carries a note saying §16 shows five sections and does not include it. Covered
by `test_needs_decision_ticket_is_not_placed_in_the_five_sections`.

**Decision needed from the human:** whether §16 should grow a sixth section.

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

### Not hit

- **No pre-existing test went red.** See §5.
- **No decision the spec assigns to the human was required to proceed** — the
  concurrency gate, merge, and cleanup-of-unmerged-work paths were not touched.

---

## 5. Validation

Baseline was captured first, on a pristine worktree at the same base commit
(`/home/ubuntu/agent-taskflow-wt-base-4266c02`, `4266c02`), so any red test
would be attributable to this branch:

```
baseline: 4390 passed, 8 skipped, 822 subtests passed
```

### Full Python suite — this branch, after the Step 1 merge

```
$ /home/ubuntu/agent-taskflow/.venv/bin/python -m pytest tests -q
4676 passed, 8 skipped, 1527 subtests passed in 539.94s (0:08:59)
```

**No pre-existing test went red, and no test went red at any point.** The
arithmetic accounts for every added test:

| Source | Tests | Measured by |
|---|---|---|
| baseline at `4266c02` | 4390 | pristine worktree |
| Step 3, before the merge | +162 | full run at `e3c7656` gave 4552 |
| Step 1, merged in | +114 | its five test files run alone |
| Step 3 §12.2 bridge tests | +10 | 8 projection + 2 frontend-source |
| **total** | **4676** | matches the run above |

Skips are unchanged at 8 throughout.

### Step 3 tests in isolation

```
$ ... -m pytest -q tests/test_runtime_progress.py \
      tests/test_runtime_progress_store.py tests/test_realtime_projection.py \
      tests/test_api_realtime.py tests/test_realtime_negative_scope.py \
      tests/test_mission_control_realtime_frontend.py \
      tests/test_migrate_runtime_progress_script.py
162 passed, 535 subtests passed in 8.72s
```

### Byte-compile

```
$ ... -m compileall agent_taskflow scripts tests
COMPILEALL OK
```

### Repo validators

```
$ ... scripts/validate_workflow_contract.py
Workflow contract validation / source path: WORKFLOW.md / status: passed

$ ... scripts/validate_workflow_policy.py
Workflow policy validation / source path: examples/workflow-policy.example.json / status: passed
```

### Mission Control build

```
$ cd mission-control && npm run build
✓ Compiled successfully in 3.0s
  Finished TypeScript in 5.5s
Route (app)
┌ ƒ /
├ ○ /_not-found
├ ƒ /live
├ ƒ /tasks/[taskKey]
└ ƒ /tasks/new
```

The new `/live` route is registered and TypeScript passes.

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
  tests/test_migrate_runtime_progress_script.py
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
| Negative: no lifecycle status write | `test_realtime_negative_scope.py::NoLifecycleWriteInStep3CodeTests` |
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

### Manual smoke (optional, read-only)

Against a **scratch** database — never the production one:

```bash
DB=/tmp/step3-smoke/state.db
mkdir -p /tmp/step3-smoke
$PY scripts/migrate_runtime_progress.py --db-path "$DB"
```

Then point the API at it and read:

```bash
curl -s localhost:8100/api/realtime/board | head -c 400
curl -sN localhost:8100/api/realtime/stream | head -c 400
```

The first frame is `event: snapshot`, and no frame ever contains an `id:` line.

---

## 7. Governance

- No commit was pushed to `main`; no merge, no force-push. Only
  `task/v1-step3` was pushed.
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
