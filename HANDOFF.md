> This file covers **two** V1 steps. `task/v1-step1` was merged into
> `task/v1-step2` to bring in `agent_taskflow/status_vocab.py` per the SPEC
> §12.2 ruling, so both handoffs travel together on this branch. Step 2 is
> the subject of draft PR #196; the Step 1 handoff is preserved verbatim
> below, unedited.

---

# HANDOFF — V1 Step 2: Integration Controller

Branch: `task/v1-step2`
Draft PR: https://github.com/anderson930420/agent-taskflow/pull/196
Spec: `~/agent-taskflow-ops/v1/SPEC.md` §42 Step 2 (Integration Controller,
Re-integration, PR outcomes, Merge)
Instruction set: `~/agent-taskflow-ops/v1/step2.md`

Status: **implementation-complete, awaiting human review.** Nothing is
approved, merged to `main`, or finally complete.

`task/v1-step1` has been merged into this branch (merge commit, not a rebase)
to bring in `agent_taskflow/status_vocab.py` for the SPEC §12.2 ruling — see
§3(a). This branch therefore contains Step 1 *and* Step 2; PR #196 reviews
Step 2.

---

## 1. What was implemented

### Extended (additive only — no existing module rewritten)

| Module | Change |
| --- | --- |
| `agent_taskflow/models.py` | Added the Step-2 lifecycle statuses, integration event types, and integration artifact types as new set members. No existing value changed or removed. |
| `agent_taskflow/store.py` | Added one named idempotent migration, `v1_step2_integration_tables`, registered in the existing `_MIGRATIONS` tuple. No existing method touched. |
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
`reset`, `checkout`, `clean`, `update-ref`, …), a force-push denylist covering
the flag and `+refspec` forms, and a protected/target-branch push check. Every
gh argv goes through `GitHubPrAdapter.run`, which rejects merge argv. A test
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
them blocked delivery. **(a) has since been ruled on and implemented**; (b) and
(c) are still open.

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

**One consequence worth your attention.** Step 2's watcher selects Tickets by
the persisted value `waiting_for_review`. The legacy `waiting_approval`, which
the existing dispatcher writes and which `status_vocab` also maps to the
`needs_review` display name, is therefore **not** picked up by the Step 2
watcher. That is deliberate — Step 2 should only manage Tickets it integrated
itself, not adopt every legacy task sitting at the old approval gate — but it
does mean the display name `needs_review` covers a strictly larger set in
Mission Control than the set Step 2 acts on. If you want the watcher to adopt
legacy `waiting_approval` tasks too, that is a one-line change and a decision
for you, not me.

**(b) `WORKFLOW.md` Non-Goals vs Step 2's mandate.**
The repo-owned contract lists "automatic push" and "automatic cleanup/delete"
as things agent-taskflow should not provide. V1 Step 2 requires exactly those.
I did not edit the Non-Goals list. Instead every Step 2 entry point is
**dry-run by default and requires an explicit confirmation flag**, and nothing
runs itself — which keeps WORKFLOW.md's actual guarantee (no background
mutation) intact while implementing the spec's machinery. **Your call:** V1
supersedes that Non-Goals line, or Step 2 stays operator-triggered forever.

**(c) §42 checklist vs step2.md Allowed layers on "Retry → new Attempt".**
Described in §2.1 above. I followed step2.md, which overrides.

### "You need a decision the spec assigns to the human" — 2 parameterised, not guessed

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
| `compileall agent_taskflow scripts tests` | exit 0 |
| Step 2 tests only | 227 passed across 14 files |
| Step 1 tests merged in | 114 passed across 5 files |

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

The skip count is 8 throughout — the same 8 pre-existing skips. No test was
lost, silently skipped, or newly red at any point.

A second full `pytest tests -q` run after the merge, in the foreground, gave
the identical result: `4731 passed, 8 skipped, 0 failed` in 523s.

`agent_taskflow.cli.local_validation`, run after the merge (exit 0) — every
required check passed:

| Check | Result |
| --- | --- |
| Python environment dependencies | passed |
| workflow contract validation | passed |
| workflow policy validation | passed |
| Mission Control golden path smoke | passed |
| PiExecutor golden path smoke (fake Pi) | passed |
| unit tests (`unittest discover -s tests -v`) | passed — `Ran 4737 tests`, `OK (skipped=8)`, 513s |
| compileall | passed |
| openspec validate | skipped — `openspec` is not on PATH (optional check, pre-existing) |

`unittest` reports 4737 and `pytest` reports 4731 + 8 skipped because the two
runners collect tests differently; both report **zero failures**, and both
report the same 8 skips as the pre-edit baseline.

**No test went red at any point** — not after Step 2, not after the Step 1
merge, and not after the §12.2 rework. There was no pre-existing failure to
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

# Appendix — V1 Step 1 handoff (merged in, preserved verbatim)

The section below was written by the Step 1 builder on `task/v1-step1` and is
reproduced unchanged. It is included here because that branch was merged into
`task/v1-step2`; it is not part of the Step 2 review.

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
