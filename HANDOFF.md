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
  `Ran 4396 tests ... OK (skipped=8)`. After:
  `Ran 4485 tests ... OK (skipped=8)`.
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
- check: unit tests                         passed   (Ran 4485 tests, OK, skipped=8)
- check: compileall                         passed
- check: openspec validate                  skipped  (openspec not on PATH)
```

Step 1 tests only:

```bash
/home/ubuntu/agent-taskflow/.venv/bin/python -m unittest \
  tests.test_ticket_metadata tests.test_ticket_store \
  tests.test_ticket_creation tests.test_api_tickets -v
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
