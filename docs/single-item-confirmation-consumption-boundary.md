# Single-item Confirmation Consumption Boundary

This document is documentation-only. It defines the boundary between the
existing Phase 6F+4 `scheduler_confirmation` artifact contract and any
future command-specific helper that might consume one such confirmation
item. No runtime code, scripts, models, DB schema, dependencies, Mission
Control UI, or test behavior changes as a result of this document.

The overarching agent-taskflow principle still holds:

> Manage work, not agents.

A scheduler confirmation is an operator-typed pre-approval record. It is
never action evidence. It is never execution permission by itself. It can
never replace the existing command-specific `--confirm-*` helpers. This
document specifies the rules a future single-item consumer must satisfy
*before* any implementation work begins. It does not implement any of
those rules.

This document narrows the scope from
`docs/proposal-review-batch-confirmation-boundary.md` §10 (Design A vs
Design B) to the smallest plausible first step: **one** confirmation
item, **one** command kind, **one** command-specific helper, **once**.
Anything broader is explicitly out of scope.

## 1. Current State

The recommendation → proposal → review → confirmation chain that exists
today is read-only by default and disjoint from any action evidence type.
The Phase 6F family established the following pieces, in order:

- **`agent_taskflow/task_recommendations.py`** — read-only per-task
  next-action classification. Computes the recommendation kind, current
  phase label, evidence summary, missing evidence, related artifacts,
  worktree/branch/PR/cleanup status, and consistency warnings. Pure read
  of the SQLite mirror plus on-disk artifacts. Never executes.
- **`agent_taskflow/scheduler_proposals.py`** — converts the
  recommendation listing into a deterministic proposal payload. Records
  a `scheduler_proposal` artifact and a `scheduler_proposal_created`
  event when (and only when) `dry_run=False` *and*
  `confirm_create_proposal=True`. Carries proposal-only safety flags
  (`workflow_action_performed=false`, `action_evidence_created=false`,
  `will_execute=false`, etc.). No action.
- **Hash binding (Phase 6F+2).** Each proposal carries
  `proposal_hash`; each `items` entry carries `proposal_item_id` and
  `item_hash`, plus `expected_status`, `expected_phase_label`,
  `expected_evidence_summary`, and `expected_refs` describing the
  recommendation the item was bound to.
- **`agent_taskflow/scheduler_proposal_review.py`** (Phase 6F+3) — a
  read-only proposal review. Loads a recorded `scheduler_proposal`,
  recomputes the canonical proposal hash and item hashes, and reports
  match/mismatch. **No DB mutation. No action evidence. No artifact
  recorded by the review itself.**
- **`agent_taskflow/scheduler_confirmations.py`** (Phase 6F+4) —
  records a `scheduler_confirmation` artifact and a
  `scheduler_confirmation_created` event, binding
  `proposal_id` + `proposal_hash` + `proposal_item_id` + `item_hash`
  for one or more selected items. Gated by `dry_run=False` *and*
  `confirm_create_confirmation=True`. The artifact is audit /
  pre-approval only:
  - `safety.execution_allowed = false`
  - `safety.workflow_action_performed = false`
  - `safety.action_evidence_created = false`
  - plus every `will_*` flag set to `false`
- **`scripts/create_scheduler_confirmation.py`** — operator-typed CLI
  that produces the confirmation artifact. There is no daemon, no
  loop, no auto-runner, no webhook behind it.

No consumer exists. **Nothing in the current codebase reads a
`scheduler_confirmation` artifact and uses it to perform any workflow
action.** The existing `--confirm-*` helpers do not look at confirmation
artifacts at all; they remain the sole mutation gates.

State clearly:

> A `scheduler_confirmation` is not action evidence.
> It is not execution permission by itself.
> It cannot bypass command-specific `--confirm-*` helpers.

A future consumer module would be the first piece of code that *reads*
one of these artifacts for a purpose other than human review. This
document scopes what that consumer is allowed to do.

## 2. Consumption Definition

"Consumption" in this document means exactly the following:

> A future command-specific helper reads one selected confirmation
> item and decides whether that item may be used as an additional
> precondition for a specific command attempt.

That is the entire surface area of "consumption". A consumer answers a
single yes/no question:

> "Is this exact confirmation item still valid enough to allow the
> operator to proceed to the normal command-specific confirmation
> path?"

Consumption is **not**:

- running the underlying command automatically
- executing a batch of items
- mutating GitHub by itself
- mutating task status by itself
- approving or rejecting a task
- merging a PR
- creating a real (non-draft) PR
- cleanup of any kind
- writing action evidence (`branch_push_completed`, `draft_pr_created`,
  `local_cleanup_completed`, `remote_branch_cleanup_completed`,
  `task_closeout_completed`, etc.)
- replacing the `--confirm-*` flag family
- mutating the `scheduler_confirmation` artifact or the task mirror
  beyond, at most, writing a separate `scheduler_confirmation_consumption`
  artifact (see §8) once that artifact is itself specified and
  implemented in a later phase

A successful "consumption" answer is "yes, this confirmation looks valid
*right now*, and you may now proceed to type the helper's normal
`--confirm-*` flag." The consumer does not perform the action; it only
unlocks the door behind which the existing single-action helper still
stands.

Even within this narrow surface, the consumer is a **read** operation
plus, at most, a single audit artifact write (see §8). It must remain
disjoint from every action evidence type.

## 3. Single-item Only Rule

The first consumption scope is single-item, single-command, single-task,
single-helper, and non-transitive. Explicitly:

- exactly one `confirmation_id`
- exactly one `proposal_item_id`
- exactly one `recommended_command_kind` (the command kind)
- exactly one `task_key`
- exactly one command-specific helper
- no batch expansion ("apply all matching items" is forbidden)
- no "all items in this confirmation" expansion
- no transitive execution after success

This rule exists because the entire reason the `--confirm-*` family is
shaped as it is — one operator-typed flag per mutating step — is that
each gate is a separate decision. A first consumption surface that
quietly authorizes more than one step or more than one item rebuilds the
exact failure mode the existing helpers were designed to prevent.

Example — what is allowed (eventually, after a future scoped
implementation phase):

> A future branch-push helper may consume one `scheduler_confirmation`
> item whose `recommended_command_kind` is `branch_push_review` for one
> exact `task_key` with one exact `proposal_item_id` and one exact
> `item_hash`. The helper still requires `--confirm-branch-push`. The
> helper writes the normal `branch_push_completed` evidence on success.

Example — what is forbidden:

- push the branch and then create the draft PR in the same invocation
- consume two `branch_push_review` items in one invocation (even for
  the same task key)
- "upgrade" a `branch_push_review` confirmation into a
  `draft_pr_review` confirmation
- continue from a successful push into cleanup without re-confirming
  cleanup against fresh proposal evidence
- consume one `cleanup_continue` item and then continue into
  `task_closeout_completed` without a separate consumption against a
  separate confirmation item

If a chain is wanted, the chain must be re-proposed and re-confirmed
between each mutating step. Each step is its own decision against fresh
evidence. This is non-negotiable and inherits directly from
`docs/proposal-review-batch-confirmation-boundary.md` §7.2 ("No
transitive execution").

## 4. Required Binding Checks

A future consumer must verify *all* of the following before treating a
confirmation item as a usable precondition. Failure of any single check
is a hard block (see §10). These checks are *binding* checks — they
verify the confirmation artifact itself is internally consistent and
points where it says it points. They do **not** by themselves prove the
confirmation is still safe to act on; that is §5's job.

- the confirmation artifact exists at its recorded path and parses as
  JSON
- `confirmation.schema_version` equals a value the consumer
  supports — at time of writing this is `scheduler_confirmation.v1`;
  any unknown schema version is a block
- `confirmation.safety.execution_allowed == false`
- `confirmation.safety.workflow_action_performed == false`
- `confirmation.safety.action_evidence_created == false`
- every `confirmation.safety.will_*` flag is `false`
- `confirmation.proposal_id` matches the proposal the consumer was
  pointed at (either via `--proposal-id` or by reading the
  confirmation's recorded reference, never by recomputing from the
  current mirror)
- `confirmation.proposal_hash` equals the canonical
  `proposal_hash` of the proposal payload as currently recorded on
  disk; this is the same canonical hash computed by
  `agent_taskflow/scheduler_proposal_review.py`
- the selected `proposal_item_id` exists in
  `confirmation.selected_items`
- the selected item's `item_hash` (recorded in the confirmation)
  equals the canonical `item_hash` of the corresponding item in the
  proposal payload
- `confirmation.selected_items[i].task_key` equals the helper's
  current `task_key`
- `confirmation.selected_items[i].recommended_command_kind` equals
  the command kind the helper implements (e.g. `branch_push_review`
  for a future branch-push consumer)
- the confirmation is not already consumed, if single-use is
  implemented (see §8)
- if the proposal item recorded a non-empty
  `consistency_warnings` list, the confirmation must record
  `operator_acknowledged_warnings = true` for that item *by name*; a
  blanket acknowledgement is not acceptable
- the command-specific `--confirm-*` flag for the helper is still
  present on the helper's invocation

A consumer must **reject** a confirmation that appears to claim execution
already happened. Specifically:

- if any `safety.*_performed` field is `true`
- if any `safety.action_evidence_created` field is `true`
- if `safety.execution_allowed` is `true`
- if any `will_*` field is `true`
- if any item carries an `execution_result` field, or any field
  asserting an action was carried out

Such a confirmation is malformed by contract and must be blocked, not
re-interpreted.

## 5. Revalidation Before Consumption

Hash and binding matching prove the confirmation refers to the proposal
the operator reviewed. They do **not** prove the world still matches what
the operator reviewed. Time passes between confirmation and consumption.
Task status, evidence, branch SHAs, PR state, worktree presence, and
consistency warnings can all change. The consumer must therefore
**revalidate** the current state of the system against the recorded
expected state, before treating the confirmation as usable.

For every command kind that may eventually be consumable, the consumer
must verify, immediately before acting:

- the task still exists in the SQLite mirror
- the task's current status still matches
  `expected_status`, unless the command kind explicitly allows a status
  transition between proposal time and consumption time (none currently
  do)
- the `current_phase_label` returned by the recommendation layer
  still matches `expected_phase_label`
- the recommendation layer's current `recommended_command_kind` for
  this task still matches the confirmation's
  `recommended_command_kind`
- the `item_hash` recomputed from the *current* recommendation
  (under the proposal policy's normalization) still matches the
  confirmed `item_hash` — *or* a command-specific equivalent evidence
  check passes (see §7)
- `consistency_warnings` are empty, *or* every warning previously
  recorded is still acknowledged and no new warning has appeared
- required artifacts and events referenced by the item still exist by
  ID / path / content hash
- no newer event or artifact contradicts the recommendation
  (e.g. a `merged` event after a `draft_pr_review` recommendation, a
  `local_cleanup_completed` after a `cleanup_continue` recommendation
  targeting the same worktree, etc.)
- the confirmation has not expired (see §6)

The consumer must read **current** DB state and **current** on-disk
artifact state to perform the above. It must not trust the confirmation
payload alone, and it must not trust a recently-cached recommendation
listing.

> Hash matching alone is not enough. Revalidation must read current
> DB/artifact/worktree state immediately before action.

Revalidation is the moral equivalent of the existing single-action
helpers' precondition checks (e.g. `branch_push_confirm.py` checks the
local branch head, the remote branch state, the protected-branch list,
and `git ls-remote` before pushing). The consumer does not replace
those; it precedes them. The single-action helper still does its own
checks after the consumer hands off.

## 6. Expiration Rules

A confirmation must define how long it is *eligible* for consumption.
The expiration defaults below are recommendations for any future
consumer; they reflect the blast-radius of the action and the rate at
which the underlying evidence drifts. They are consistent with the
defaults in
`docs/proposal-review-batch-confirmation-boundary.md` §5.

| Command kind                       | Default expiration | Notes |
| ---                                | ---                | ---   |
| `branch_push_review`               | 15 minutes         | Branch HEAD moves; consuming a stale confirmation risks pushing unreviewed commits. |
| `draft_pr_review`                  | 15 minutes         | Other operators may create a draft PR on the same head between confirmation and consumption. |
| `queued_task_handoff`              | 15 minutes         | Executor-start adjacent; package/worktree state can change quickly. |
| `cleanup_continue`                 | 15 minutes         | Cleanup chain state can advance; stale cleanup is dangerous. |
| `post_merge_cleanup_review`        | 15 minutes         | Depends on merged-PR state; downstream cleanup events may already have run. |
| `pr_handoff_package`               | 30 minutes         | Less destructive than push/PR creation, but still operates on current branch state. |
| `create_task_execution_package`    | 30 minutes         | Mostly authoring; still must revalidate task status and that no package already exists. |
| `inspect_blocker`                  | 24 hours           | Read-only. |
| `inspect_evidence`                 | 24 hours           | Read-only. |
| `no_action`                        | n/a (not consumable) | No execution to gate. |
| `unknown`                          | n/a (not consumable) | Not consumable until reclassified. |
| `human_pr_review`                  | n/a (not consumable) | Human GitHub-side action; not consumable by any helper. |

Expiration failure must hard-block consumption. An expired confirmation
cannot be "renewed" by the consumer; the operator must re-propose,
re-review, and re-confirm to obtain a fresh `confirmation_id` over a
fresh `proposal_id`.

> Expiration is necessary but not sufficient. An unexpired confirmation
> still requires §5 revalidation. Two readers must agree on
> eligibility; eligibility does not imply safety.

Expiration must be carried in the confirmation payload (or derived
deterministically from the proposal payload's `created_at` plus a
command-kind-keyed TTL), not invented by the consumer at read time. Two
consumers must compute the same eligibility answer from the same
artifact.

## 7. Command-kind Mapping

This is the canonical mapping from `recommended_command_kind` to the
future helper that *could* consume one such confirmation item, and the
existing `--confirm-*` flag the helper still requires. None of these
helpers consume confirmations today; this section documents the only
shape such consumption is allowed to take if and when it is built.

| Recommended command kind          | Eventual consumer (future)                          | Still requires                                      | Notes |
| ---                               | ---                                                 | ---                                                 | ---   |
| `create_task_execution_package`   | future `create_task_execution_package` helper       | `--confirm-create-package`                          | Must verify task is still queued and no execution package already exists for this task_key. Lowest blast radius; preferred first runtime target (§12). |
| `queued_task_handoff`             | `scripts/run_queued_task_handoff.py`                | `--confirm-handoff`                                 | Must verify execution package exists, task is queued, worktree constraints are met, executor identity matches. |
| `pr_handoff_package`              | `scripts/create_pr_handoff_package.py`              | command-specific confirm flag if one is added       | Must verify task is `waiting_approval`, validators have passed, branch SHA matches `expected_branch_head_sha`. |
| `branch_push_review`              | `scripts/branch_push_confirm.py`                    | `--confirm-branch-push`                             | Must verify worktree path exists, branch name matches `expected_branch_name`, local head matches `expected_branch_head_sha`, branch is not protected, `git ls-remote` is consistent. |
| `draft_pr_review`                 | `scripts/draft_pr_confirm.py`                       | `--confirm-draft-pr`                                | Must verify a successful branch push evidence exists for `expected_branch_head_sha`, no existing open PR conflicts on the head branch, base SHA still matches `expected_base_sha`. |
| `post_merge_cleanup_review`       | `scripts/local_cleanup_confirm.py` / `scripts/remote_branch_cleanup_confirm.py` / `scripts/task_closeout_confirm.py` | respective `--confirm-*` flags | Must verify exact worktree path under `<repo>/.worktrees/<task-key>`, exact branch name, merged-PR evidence with correct head SHA. |
| `cleanup_continue`                | `scripts/local_cleanup_confirm.py` / `scripts/remote_branch_cleanup_confirm.py` / `scripts/task_closeout_confirm.py` | respective `--confirm-*` flags | Same constraints as above; chain step must be named exactly (no implicit "next step"). |
| `inspect_blocker`                 | none, or future review-only marker                  | n/a                                                 | Read-only. May eventually be "marked reviewed" without mutation, but never consumed to act. |
| `inspect_evidence`                | none, or future review-only marker                  | n/a                                                 | Read-only. Same as above. |
| `human_pr_review`                 | none                                                | n/a                                                 | Human GitHub-side only; never system-consumable. |
| `no_action`                       | none                                                | n/a                                                 | Never consumable. |
| `unknown`                         | none                                                | n/a                                                 | Never consumable. |

The leftmost three columns are contract; the rightmost is informative.
Any addition of a new consumer requires updating this table, the
expiration table in §6, and the binding/revalidation checks in §4 and
§5.

## 8. Single-use / Consumption Evidence

If and when a consumption surface is implemented, it should record its
own audit artifact and event, disjoint from action evidence and disjoint
from confirmation evidence. This section sketches that artifact and the
event; **it does not implement them**.

Proposed types (not implemented):

- `artifact_type = "scheduler_confirmation_consumption"`
- `event_type   = "scheduler_confirmation_consumed"`

Suggested payload (illustrative; not implemented):

```json
{
  "schema_version": "scheduler_confirmation_consumption.v1",
  "consumption_id": "consumption-...",
  "confirmation_id": "confirmation-...",
  "proposal_id": "proposal-...",
  "proposal_item_id": "item-...",
  "item_hash": "sha256:...",
  "task_key": "agent-taskflow/repo#123",
  "recommended_command_kind": "branch_push_review",
  "consumer": "confirm_branch_push",
  "consumed_at": "2026-...Z",
  "revalidation": {
    "passed": true,
    "checks": [
      "task_status_matches_expected",
      "current_recommendation_kind_matches",
      "item_hash_recomputed_matches",
      "expected_branch_head_sha_matches",
      "no_protected_branch",
      "git_ls_remote_consistent",
      "no_unacknowledged_warnings"
    ]
  },
  "execution_result": "not_recorded_here",
  "action_evidence_created_by": "command_specific_helper_only"
}
```

Important properties of this design:

- **Consumption evidence is not action evidence.** The actual action
  helper still writes the normal `branch_push_completed`,
  `draft_pr_created`, `local_cleanup_completed`,
  `remote_branch_cleanup_completed`, `task_closeout_completed`, etc.
  evidence as it does today. The consumption artifact must not carry
  any `*_performed` or `action_evidence_created=true` field. Its sole
  purpose is to record "the operator opened the door".
- **Single-use semantics.** A given
  `(confirmation_id, proposal_item_id)` pair may be consumed at most
  once. A repeat consumption against the same pair is a hard failure
  regardless of operator identity, even if the action that followed
  the first consumption did not itself succeed. To redo the work, the
  operator must re-propose and re-confirm.
- **`execution_result` is a stub.** The consumption artifact must
  never claim the action ran. The helper that records action evidence
  is the only authoritative source of execution outcome.
- **Disjoint type namespace.** `scheduler_confirmation_consumption`
  must not be mistaken for any action evidence type by downstream
  readers (`branch_push_completed`, etc.).

The single-use property closes the replay-against-the-same-token risk
that
`docs/proposal-review-batch-confirmation-boundary.md` §10.2 (Design B)
warns about.

## 9. Relationship to Existing `--confirm-*` Helpers

State this as a hard rule:

> A scheduler confirmation may be an *additional precondition* for a
> command-specific helper. It may **never** be a replacement for the
> `--confirm-*` flag that helper requires today.

The existing single-action confirm flags are unchanged by this design:

- `--confirm-create-package` (future
  `create_task_execution_package` helper)
- `--confirm-handoff` (`scripts/run_queued_task_handoff.py`)
- `--confirm-branch-push` (`scripts/branch_push_confirm.py`)
- `--confirm-draft-pr` (`scripts/draft_pr_confirm.py`)
- `--confirm-local-cleanup` (`scripts/local_cleanup_confirm.py`)
- `--confirm-remote-branch-delete`
  (`scripts/remote_branch_cleanup_confirm.py`)
- `--confirm-task-closeout` (`scripts/task_closeout_confirm.py`)

An allowed future helper invocation would look like (illustrative; not
implemented):

```
.../branch_push_confirm.py \
  --task-key agent-taskflow/repo#123 \
  --confirmation-id confirmation-... \
  --proposal-item-id item-... \
  --confirm-branch-push
```

The helper flow would then be:

1. load the named confirmation artifact from disk
2. verify all §4 binding checks pass
3. verify all §5 revalidation checks pass against current state
4. verify §6 expiration has not been reached
5. verify the command-specific `--confirm-*` flag is still present on
   the invocation
6. (if §8 is also implemented) write a
   `scheduler_confirmation_consumption` artifact and a
   `scheduler_confirmation_consumed` event
7. execute the existing single-action helper logic
8. on success, write the helper's normal action evidence
   (`branch_push_completed`, etc.) as it does today

Not allowed under any future design starting from this boundary:

- `--confirmation-id` alone performing the action (i.e. without
  `--confirm-*`)
- a confirmation auto-supplying or implying `--confirm-*`
- a helper consuming multiple items in one invocation (this is a
  *batch* consumer, which is explicitly out of scope until the §12
  ordering reaches that step, and only after Design A is proven)
- a helper chaining into the next phase after success without a
  separate confirmation against a separate proposal item
- a helper trying to consume an `inspect_*`, `human_pr_review`,
  `no_action`, or `unknown` confirmation

This document does not authorize any change to the existing
`--confirm-*` helpers. They are referenced only because the future
consumer must call into them, not replace them.

## 10. Failure Modes and Blocks

Every one of the conditions below is a hard block. The consumer must
report and stop; it must never partially proceed, never silently
"degrade" to a weaker check, and never auto-retry.

- missing confirmation artifact at the recorded path
- confirmation artifact does not parse as JSON
- unsupported `confirmation.schema_version`
- unsafe confirmation payload (`safety.execution_allowed=true`,
  any `*_performed=true`, any `will_*=true`, or any
  `action_evidence_created=true` field)
- `proposal_hash` mismatch between confirmation and current
  proposal on disk
- `item_hash` mismatch between confirmation and current proposal
  item
- selected `proposal_item_id` not present in
  `confirmation.selected_items`
- selected item's `recommended_command_kind` does not match the
  helper invoking the consumer
- `task_key` mismatch between confirmation item and helper
  invocation
- confirmation expired per §6
- confirmation already consumed (when §8 is implemented)
- task status drift (current task status no longer matches
  `expected_status`, unless the command kind explicitly allows the
  observed transition — none currently do)
- `current_phase_label` drift
- branch SHA drift (current head no longer matches
  `expected_branch_head_sha`)
- PR state drift (current PR state no longer matches
  `expected_pr_state`)
- base SHA drift (current base no longer matches `expected_base_sha`)
- worktree missing under `<repo>/.worktrees/<task-key>`
- worktree path does not match the recorded item, or points to a
  different repository
- consistency warnings present at consume time were not acknowledged
  by the operator at confirm time
- new consistency warnings have appeared since confirmation that were
  not acknowledged
- required artifacts/events referenced by the item are missing or
  have changed content hash
- newer contradictory evidence exists (merged event after
  `draft_pr_review`, cleanup-completed event after
  `cleanup_continue` for the same target, etc.)
- command-specific `--confirm-*` flag missing from the helper
  invocation

For each block, the consumer must:

1. emit a human-readable failure indicating which check failed
2. exit non-zero
3. not write any consumption artifact (failed consumption is not
   itself an artifact; only successful consumption is, per §8)
4. not mutate task status
5. not mutate GitHub
6. not write any action evidence

Partial execution is forbidden. Reporting a failure is not optional; a
silently-blocked consumption is indistinguishable from no consumption
at all and would defeat the audit purpose.

## 11. Non-consumable Actions

The following are explicitly never consumable by any future helper
contemplated by this document, regardless of confirmation contents,
regardless of evidence quality, regardless of operator identity. They
inherit from
`docs/scheduler-automation-boundary.md` §2 class D and
`docs/proposal-review-batch-confirmation-boundary.md` §8.

- merge a PR (no `gh pr merge`, no equivalent API call, no merge
  driven from any consumed confirmation)
- approve a task (no `status="approved"` transition driven by any
  consumed confirmation)
- reject a task (no `status="rejected"` transition driven by any
  consumed confirmation)
- force push (no `--force` from any consumer)
- delete a protected branch (`main`, `master`, `trunk`)
- delete a worktree outside `<repo>/.worktrees/<task-key>`
- delete `main` / `master` / `trunk`
- auto-run an executor for a newly discovered issue without an
  operator-selected task key
- create a real (non-draft) PR from a recommendation alone
- auto-cleanup without an exact target worktree path **and** branch
  name **and** merged-PR evidence
- act on any confirmation item whose
  `consistency_warnings` were not acknowledged by name (per-warning,
  not a blanket acknowledgement)
- act on any confirmation item whose `recommended_command_kind` is
  `no_action`, `unknown`, or `human_pr_review`

These remain hard never. A future consumer that finds itself reasoning
about how to bypass any of these is, by definition, out of scope.

## 12. Future Phase Order

The following ordering is proposed. None of these are committed by this
document; each requires its own scoping, design, and operator approval
before implementation. The ordering deliberately starts non-mutating and
ends with the smallest plausible mutation surface.

1. **Add a confirmation consumption verifier module in dry-run only.**
   Pure read: given a `confirmation_id` and a `proposal_item_id`,
   the verifier performs all §4 binding checks and all §5 revalidation
   checks, and returns a structured pass/fail. It does **not** call
   into any `--confirm-*` helper. It does not write any artifact. It
   does not mutate state.
2. **Add tests against synthetic confirmation/proposal artifacts.**
   Unit tests covering all §4 and §5 failure modes, plus passing
   cases, against fixture confirmations and fixture proposals. No
   real GitHub, no real executor, no real DB beyond the in-process
   SQLite mirror conventions already used in the existing test suite.
3. **Add a CLI to verify a confirmation item without executing.** An
   operator-typed `scripts/verify_scheduler_confirmation_item.py` (or
   equivalent name) that wraps the verifier from step 1. Output is a
   structured report. No mutation; no action evidence; no integration
   with any `--confirm-*` helper.
4. **Add an optional read-only API endpoint** for the verifier.
   FastAPI exposes a strictly read-only "verify this confirmation
   item" endpoint that returns the structured report. No POST that
   triggers action.
5. **Add single command helper integration for the lowest-risk
   command kind.** This is the **first** time a `--confirm-*` helper
   accepts `--confirmation-id` and `--proposal-item-id`. It must
   start with a non-GitHub-mutating consumer; see "Recommended first
   runtime target" below.
6. **Prove no command-specific `--confirm-*` bypass.** Operator-run
   smoke tests covering: omitting `--confirm-*` while providing
   `--confirmation-id` must fail; providing a stale confirmation
   must fail; providing a confirmation for a different task must
   fail; providing a confirmation for a different command kind must
   fail; providing a confirmation that already has a consumption
   artifact must fail.
7. **Only then discuss multi-item / batch consumption.** Batch
   consumption is the surface scoped by
   `docs/proposal-review-batch-confirmation-boundary.md` §7; it is
   not authorized by this document and is a strict superset of the
   single-item case.
8. **Still no background loop.** No daemon, no cron, no auto-runner,
   no webhook. The chain remains operator-driven for the foreseeable
   future, per `docs/scheduler-automation-boundary.md`.

### Recommended first runtime target

The first runtime consumer should not be `branch_push_review`. Branch
push is GitHub-mutating, and a regression there would be visible to
collaborators on the remote. A safer first target is one whose failure
is local and easy to audit:

- **`create_task_execution_package`** — a local helper that authors an
  execution package on disk for a queued task. Failure is contained to
  the orchestrator workspace. Or
- **`pr_handoff_package`** — generates a local handoff package
  describing the PR-side state. Read-heavy, write-light, and the
  write is purely a local artifact.

Either choice keeps the first consumer entirely off GitHub-side
mutation. Branch push, draft PR creation, and cleanup all introduce
remote or destructive state changes and should follow only after the
above non-GitHub-mutating consumer is proven across multiple operator
cycles.

## 13. Non-goals

The following are explicitly out of scope for this phase, and any phase
following directly from it without separate authorization:

- No runtime consumption in this phase.
- No scheduler loop.
- No background worker.
- No batch confirmation in this phase.
- No multi-item consumption in this phase.
- No command helper changes in this phase.
- No new artifact types in this phase (the
  `scheduler_confirmation_consumption` artifact in §8 is sketched as a
  *future* contract; it is not authorized here).
- No new event types in this phase.
- No DB schema migration.
- No new store APIs.
- No new model classes.
- No GitHub mutation.
- No task status mutation.
- No executor run.
- No validator run.
- No branch push.
- No PR creation.
- No merge.
- No approval transition.
- No rejection transition.
- No cleanup of any kind.
- No Mission Control UI change.
- No mutation affordances introduced anywhere.
- No new dependencies.
- No new tests (existing governance docs in this repository do not
  carry doc-tests; this document follows the same convention).

The "manage work, not agents" principle is preserved by this document.
A future single-item confirmation consumer, when and if it exists, will
be one more deterministic operator-driven precondition layered *in
front of* the existing `--confirm-*` helpers — never replacing them,
never expanding past one item, and never executing on its own.
