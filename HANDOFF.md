# HANDOFF — V1 Step 2: Integration Controller

Branch: `task/v1-step2`
Draft PR: https://github.com/anderson930420/agent-taskflow/pull/196
Spec: `~/agent-taskflow-ops/v1/SPEC.md` §42 Step 2 (Integration Controller,
Re-integration, PR outcomes, Merge)
Instruction set: `~/agent-taskflow-ops/v1/step2.md`

Status: **implementation-complete, awaiting human review.** Nothing is
approved, merged, or finally complete.

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

These are reported rather than repaired, per the stop-condition rule. None of
them blocked delivery, and each was handled in the way that preserves the most
optionality for you.

**(a) `cancelled` vs `canceled` — spec/code spelling conflict.**
SPEC §12 spells the cancelled state with two `l`s. `models.py` already had a
legacy `canceled` (one `l`) used by the pre-V1 mirror. I added `cancelled` as a
**separate, non-aliased** status alongside it, so a V1 integration cancellation
can never be confused with a legacy one. Two spellings in one enum is a wart.
**Your call:** keep them distinct, or migrate the legacy value once Step 1
lands the V1 status model.

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
| `pytest tests -q` (after Step 2) | `4615 passed, 8 skipped, 0 failed` in 509s |
| `compileall agent_taskflow scripts tests` | exit 0 |
| Step 2 tests only | 225 passed across 14 files |

The delta is exactly +225 passed with the same 8 skips: every new test is a
Step 2 test, and no pre-existing test changed state. The 8 skips are the
pre-existing ones from the baseline, untouched.

`agent_taskflow.cli.local_validation` — all six required checks passed:

| Check | Result |
| --- | --- |
| workflow contract validation | passed |
| workflow policy validation | passed |
| Mission Control golden path smoke | passed |
| PiExecutor golden path smoke (fake Pi) | passed |
| unit tests (`unittest discover -s tests -v`) | passed, 501s |
| compileall | passed |
| openspec validate | skipped — `openspec` is not on PATH (optional check, pre-existing) |

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
