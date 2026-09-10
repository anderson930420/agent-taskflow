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

`node_modules` is not committed. If the worktree has none, copy it from the main
checkout — a symlink will not work, Turbopack rejects one that points outside
the project root:

```bash
cp -a /home/ubuntu/agent-taskflow/mission-control/node_modules \
      mission-control/node_modules
cd mission-control && npm run build
```

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
