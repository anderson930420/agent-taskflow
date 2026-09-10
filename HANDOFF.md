# Handoff — V1 Step 3: Realtime Progress

Branch: `task/v1-step3`
Base: `4266c02` (`main`)
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
| `agent_taskflow/realtime_projection.py` | create | Read-only board / Ticket projection. |
| `agent_taskflow/api/realtime.py` | create | §15 SSE transport. |
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
- BLOCKED renders its blocker as §16 shows it — `Waiting for AT-101`.
- Every value also ships a `display` mapping with `—` substituted for null.

### §32.1 PR fields — read-only and optional

Step 2 has not landed (the `v1-step1` and `v1-step2` worktrees are still at the
base commit), so none of the §32.1 columns exist yet. The projection:

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

### 4.1 Ticket status vocabulary contradicts existing code (ambiguity / contradiction)

SPEC §12 defines the V1 Ticket statuses as:

```
queued ready blocked paused preparing running validating
ready_for_integration integrating needs_review needs_decision
completed failed cancelled
```

`agent_taskflow/models.py::TASK_STATUSES` today allows:

```
unknown created queued preparing implementing validating waiting_approval
waiting_for_review blocked accepted rejected cleaned completed canceled
archived  (+ external mirror values: backlog todo in_progress review done)
```

So `ready`, `paused`, `needs_review`, `needs_decision`, `ready_for_integration`,
`integrating`, `failed`, and `cancelled` are **not writable statuses today**,
and `cancelled` vs `canceled` differ in spelling.

Step 1 owns Ticket creation and metadata derivation and is a forbidden layer
here; any lifecycle transition is also forbidden. **Nothing was changed.**
Instead `STATUS_SECTIONS` in `realtime_projection.py` maps *both* vocabularies
onto the five §16 sections, and any status in neither table is left unsectioned
rather than guessed into a section. The board therefore works before and after
Step 1 lands.

**Decision needed from the human / Step 1:** whether the repo migrates to the
§12 vocabulary, and if so what happens to `waiting_approval`,
`waiting_for_review`, `accepted`, `cleaned`, and `archived`.

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

### Full Python suite — this branch

```
$ /home/ubuntu/agent-taskflow/.venv/bin/python -m pytest tests -q
4552 passed, 8 skipped, 1357 subtests passed in 444.82s (0:07:24)
```

The delta against the baseline is exactly this branch's additions:
`4552 - 4390 = 162` new tests and `1357 - 822 = 535` new subtests, with the
skip count unchanged at 8. **No pre-existing test went red.**

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
Spec: `~/agent-taskflow-ops/v1/SPEC.md` §42 Step 1
Instructions: `~/agent-taskflow-ops/v1/step1.md`

Status: **implementation-complete, awaiting human review.** Not approved, not
merged. The PR is a draft.

---

## 1. Read-only inventory (done before implementation)

| Module | Action | Why |
| --- | --- | --- |
| `agent_taskflow/projects.py` | **reuse, unchanged** | §11 registry loader already exists |
| `agent_taskflow/config.py` | **reuse, unchanged** | YAML loading |
| `agent_taskflow/worktree.py` | **reuse, unchanged** | `worktree_path_from_base` gives `<worktrees_dir>/<ID>` |
| `agent_taskflow/artifacts.py` | **reuse, unchanged** | `artifact_dir_for` gives `<artifacts_root>/<ID>` |
| `agent_taskflow/tasks.py` | **reuse, unchanged** | `normalize_task_key` validates the ID charset |
| `agent_taskflow/_helpers.py` | **reuse, unchanged** | `require_non_empty` |
| `agent_taskflow/models.py` | **reuse, unchanged** | `require_absolute_path`, `utc_now_iso` |
| `agent_taskflow/store.py` | **reuse, unchanged** | `connect`, `init_db`, `default_db_path` |
| `agent_taskflow/api/main.py` | **extend (3 edits)** | import router, construct `TicketStore`, `include_router` |
| `mission-control/lib/api.ts` | **reuse, unchanged** | `requestJson` / `postJson` helpers |
| `mission-control/components/TaskBoard.tsx` | **extend (1 nav link)** | makes `/tickets/new` reachable |

Nothing existing was rewritten. `models.py` / `store.py` / `schemas.py` and the
legacy `tasks` mirror are untouched.

New modules, following the existing `attempt_schema.py` + `attempt_store.py`
split:

- `agent_taskflow/ticket_models.py` — Ticket record, status enum (§12),
  priority enum (§7), `initial_ticket_status()` (§12.1).
- `agent_taskflow/ticket_repositories.py` — read-only §11 registry view.
- `agent_taskflow/ticket_metadata.py` — deterministic derivation (§10.1).
- `agent_taskflow/ticket_ai_metadata.py` — AI adapter + fallback wrapper.
- `agent_taskflow/ticket_schema.py` — additive SQLite migration.
- `agent_taskflow/ticket_store.py` — Ticket persistence + Task ID allocation.
- `agent_taskflow/ticket_creation.py` — creation service.
- `agent_taskflow/api/tickets.py` — HTTP entry point.
- `mission-control/lib/tickets.ts`, `components/CreateTicketForm.tsx`,
  `app/tickets/new/page.tsx`, `app/tickets/[ticketId]/page.tsx`.

---

## 2. What was implemented

Step 1 checklist from §42:

- [x] Repo dropdown — `GET /api/repositories` reads `config/projects.yaml`
      read-only and feeds the form's `<select>`.
- [x] Prompt-first Ticket — request body is exactly `repository`, `prompt`,
      `priority` (plus optional `blocked_by`, see §5 below).
- [x] Priority — Critical / High / Normal / Low, default Normal.
- [x] Auto Task ID — `<task_key_prefix>-<NNN>`, allocated per prefix inside the
      same `BEGIN IMMEDIATE` transaction as the insert.
- [x] AI title + deterministic fallback — injectable adapter; every failure mode
      falls back to the §10.1 rule.
- [x] Auto branch — `<branch_prefix><TICKET_ID>-<slug>`.
- [x] Auto worktree path — `<worktrees_dir>/<TICKET_ID>` (string only).
- [x] Basic detail page — statically rendered `/tickets/<id>`.

Also derived by Python and never asked of the user: `repo_path`,
`github_repo`, `base_branch`, `artifact_dir`.

Initial status follows §12.1 exactly: `ready`, or `blocked` when `blocked_by`
is present. `queued` is a valid enum member that creation never writes.

Creation writes one append-only `ticket_created` audit event. `ticket_events`
carries `no_update` / `no_delete` triggers.

### Structural invariants

- `ux_tickets_worktree_path` — unique index. `One Ticket = One Worktree` is
  enforced by storage, not only by convention.
- `ux_tickets_repo_branch` — unique index on `(repo_path, branch)`.
- The Task ID is embedded in both the branch name and the worktree path, so two
  Tickets with an identical prompt *and* an identical AI-generated title still
  derive distinct branches and worktrees.
- Task ID allocation additionally skips past legacy `tasks.task_key` values of
  the same `<PREFIX>-<n>` shape, so a derived worktree path cannot collide with
  a path an existing mirror task already owns.

---

## 3. Acceptance gate

Written before implementation. 89 new tests across four files.

| Gate item | Test |
| --- | --- |
| §43.1 create from repo/prompt/priority | `MinimalCreationInputTests` |
| §43.2 metadata generated automatically | `DerivedMetadataTests` |
| §43.3 AI title failure cannot block creation | `AiTitleFallbackTests` (raise, hang past deadline, `TimeoutError`, empty, whitespace, `None`) |
| §43.4 one worktree per Ticket (derivation only) | `OneTicketOneWorktreeTests` |
| §44 `One Ticket = One Worktree` | `OneTicketOneWorktreeTests`, `AllocationTests.test_worktree_paths_are_unique_at_the_storage_layer` |
| §44 `All lifecycle mutations are auditable` | `AuditabilityTests`, `AuditTests` |
| §12.1 initial status, never `queued` | `InitialStatusTests` |
| Negative scope: no Git, no directories | `NegativeScopeTests` |

`NegativeScopeTests.test_creation_runs_no_subprocess` patches
`subprocess.Popen`, `subprocess.run` and `os.system` to raise.
`test_creation_creates_no_directory` snapshots the sandbox tree before and
after and asserts it is unchanged.

---

## 3b. Status vocabulary bridge (SPEC §12.2 ruling)

Added after the human ruling on the §12-vs-`TASK_STATUSES` conflict that all
three Step builders reported.

**Ruling as implemented.** No repo-wide migration. `TASK_STATUSES` stays the
canonical *persisted* vocabulary. The §12 names are the Mission Control
*display* vocabulary. `agent_taskflow/status_vocab.py` is the single bridge.

The Ticket work from Step 1 is unaffected: `tickets.status` was already a
separate column with its own §12 enum, and the legacy `tasks` mirror keeps its
own spelling. The bridge is what lets a future Mission Control surface show
both under one vocabulary.

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

`tests/test_status_vocab.py`, 25 tests. Every §12 name round-trips exactly;
every one of the 25 `TASK_STATUSES` values maps to a valid display name and
canonicalizes into a real persisted value; canonicalization is idempotent; the
alias table is asserted as an exact dict; aliases and canonicals partition the
persisted vocabulary with no overlap and no gap; `unmapped_persisted_statuses()`
is empty, so a future addition to `TASK_STATUSES` that forgets this module
fails the suite instead of raising a `KeyError` in Mission Control.

---

## 4. Deliberately skipped, and why

Each is a **forbidden layer** in `step1.md`:

- Scheduler, eligibility, atomic claim, lease, Attempt, capacity (§19, §20).
- Executor runtime and any AI implementation agent.
- **All Git mutation.** No worktree, no branch, no fetch/rebase/merge/push.
  Step 1 records strings.
- Integration controller, per-repo queue, per-repo lock (§22, §23).
- Validators (§29).
- GitHub adapter — the §32.1 PR fields are **not** in the `tickets` table.
  §32.1 assigns them to the Step 2 watcher, so Step 2 should add them.
- Cleanup (§37), SSE / live progress (§15), metrics (§39).
- `blocked_by` **mutation** endpoints and cycle validation (Step 5).

Consequences visible in the UI: the detail page shows derived metadata and the
audit trail, and states plainly that no execution has run. It renders no
execution step list and no progress figure — §14.2 forbids inventing one.

Not touched, worth a follow-up: `WORKFLOW.md` still documents only the legacy
`queued → running → validating → waiting_approval` task lifecycle. The V1
Ticket status model (§12) now exists alongside it. Reconciling that document is
outside Step 1's allowed layers and should be an explicit task.

---

## 5. Known ambiguity — flagged, not silently decided

`step1.md` lists these as "flag, do not guess". Ticket creation cannot proceed
without *some* answer, so each is implemented behind a single named constant
and needs a human decision before this becomes real workload.

**(a) Task ID format and counter scope.** The spec shows only `AT-101`,
`AT-098`, `AT-123`.

- Implemented: `<task_key_prefix>-<NNN>`, zero-padded to 3
  (`ticket_metadata.TICKET_SEQUENCE_PAD`), growing naturally past 999 to
  `AT-1000`. The prefix is the registry's existing `task_key_prefix`.
- Counter is **per prefix**, starting at 1 (`FIRST_TICKET_SEQUENCE`), so the
  first agent-taskflow Ticket would be `AT-001` in an empty DB. Per-prefix
  rather than per-repository so that two repositories sharing a prefix cannot
  produce colliding IDs.
- Open questions for the human: is the counter meant to be per-repository or
  global? Should it start at 1 or at 101, as the examples suggest? Should
  Tickets share an ID space with legacy mirror task keys at all? Changing any
  of these later is a data migration, so decide before real Tickets exist.

**(b) Branch slug collision policy.**

- Collision *between Tickets* cannot happen: the unique Task ID is embedded in
  the branch name, so an AI slug that duplicates another Ticket's slug — or
  duplicates the deterministic fallback slug — still yields a distinct branch.
  Same for the worktree path.
- Collision with an **existing Git branch is not checked**, and cannot be:
  reading refs means running Git, and the required negative-scope test forbids
  any Git command during creation. Whoever owns Step 2 must handle
  "derived branch name already exists in the repository" at worktree-creation
  time. Flagging rather than guessing a rename/suffix policy.

**(c) `blocked_by` at creation.** §12.1 makes the initial status depend on it,
so the field is accepted, stored, and drives `ready` vs `blocked`. Validation
is limited to §5.2 rule 1 (the blocker must exist) plus a self-reference guard
— both cheap row-level checks, neither of them cycle validation. Full
dependency semantics stay in Step 5. A freshly created Ticket cannot be part of
a cycle, since nothing references it yet. The field is intentionally **not** in
the Mission Control form (§10 lists three fields).

**(d) `cancelled` vs `canceled`.** The Ticket enum uses the spec's spelling
`cancelled`. The legacy mirror's `TASK_STATUSES` uses `canceled`. They are
separate enums on separate tables; no conversion exists yet.

---

## 6. Stop conditions

None fired.

- No pre-existing test went red. Baseline before any edit:
  `Ran 4396 tests ... OK (skipped=8)`. After Step 1:
  `Ran 4485 tests ... OK (skipped=8)`. After the §12.2 status-vocabulary
  ruling: `Ran 4510 tests ... OK (skipped=8)`.
- Adding five values to `TASK_STATUSES` broke nothing. `TASK_STATUSES` is read
  only by `validate_task_status`, and no test asserted its exact contents;
  `tests/test_status_vocab.py` now does, so a future edit to it is deliberate.
- No spec requirement contradicted existing code. The V1 Ticket model is
  additive: the legacy `tasks` mirror, its API and its UI are unchanged, and a
  test asserts Tickets are not mirrored into `/api/tasks`.
- No human-owned decision was taken. Nothing was approved, merged, pushed to
  `main`, or cleaned up. No scheduler tick was run. `~/.agent-taskflow/state.db`
  was never read or written.

---

## 7. Verification commands

Run from `/home/ubuntu/agent-taskflow/.worktrees/v1-step1`.

The repo's canonical validator sequence — this is the one that was run last,
after every edit, and it is what gates this handoff:

```bash
/home/ubuntu/agent-taskflow/.venv/bin/python scripts/run_local_validation.py
```

Observed result (exit 0):

```text
- check: Python environment dependencies    passed
- check: workflow contract validation       passed
- check: workflow policy validation         passed
- check: Mission Control golden path smoke  passed
- check: PiExecutor golden path smoke       passed
- check: unit tests                         passed   (Ran 4510 tests, OK, skipped=8)
- check: compileall                         passed
- check: openspec validate                  skipped  (openspec not on PATH)
```

Step 1 tests only:

```bash
/home/ubuntu/agent-taskflow/.venv/bin/python -m unittest \
  tests.test_ticket_metadata tests.test_ticket_store \
  tests.test_ticket_creation tests.test_api_tickets \
  tests.test_status_vocab -v
```

Full Python suite and compile check:

```bash
/home/ubuntu/agent-taskflow/.venv/bin/python -m unittest discover -s tests
/home/ubuntu/agent-taskflow/.venv/bin/python -m compileall -q agent_taskflow scripts tests
```

Mission Control:

```bash
cd mission-control && npm ci && npm run build
```

`.venv/bin/python` is required — the system `python3` has no `pydantic`.

Manual API check against a throwaway database (never the default path):

```bash
/home/ubuntu/agent-taskflow/.venv/bin/python scripts/run_api.py --db-path /tmp/step1-demo.db &
curl -s localhost:8100/api/repositories | python3 -m json.tool
curl -s -X POST localhost:8100/api/tickets -H 'content-type: application/json' \
  -d '{"repository":"agent-taskflow","prompt":"Separate the ending page image","priority":"high"}'
```

That writes to `/tmp/step1-demo.db` only. Do not point it at
`~/.agent-taskflow/state.db`.

---

## 8. Deployment note

`TicketStore.init_db()` runs from the FastAPI lifespan. On the next API restart
against an existing database it will additively create `tickets` and
`ticket_events` and record the `v1_ticket_creation_v1` migration. No existing
table, column, index or row is altered. This has not been run against the
production database.

---

## 9. Follow-ups for the human

1. Decide (a) Task ID format and counter scope before real Tickets exist.
2. Decide the Step 2 policy for a derived branch name that already exists.
3. Decide whether `WORKFLOW.md` should describe the V1 Ticket lifecycle.
4. Step 2 owns the §32.1 PR fields; they are absent here on purpose.
5. Confirm or overrule the two status-vocabulary judgement calls in §3b —
   `waiting_approval → needs_review` and `accepted → needs_review` — and the
   `rejected` / `unknown → needs_decision` aliases.
6. Nothing yet *uses* `status_vocab` at a boundary. It is the bridge the
   ruling asked for, with the mapping pinned by tests; wiring it into the API
   serializers and Mission Control is a separate change, and Steps 2 and 3
   rebase onto it.
7. The deferred repo-wide `TASK_STATUSES` migration (§12.2, after Steps 1-3
   merge) should decide whether the legacy aliases collapse for real.
