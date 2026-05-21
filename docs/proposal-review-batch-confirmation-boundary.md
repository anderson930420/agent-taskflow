# Proposal Review / Batch Confirmation Boundary

This document is documentation-only. It defines the boundary between the
existing Phase 6F scheduler proposal contract and any future batch
confirmation mechanism. No runtime code, scripts, DB schema, dependencies,
Mission Control UI, or test behavior changes as a result of this document.

The overarching agent-taskflow principle still holds:

> Manage work, not agents.

A scheduler proposal is decision-support. It is never action evidence, never
self-approving, and never a license to mutate state. A future batch
confirmation mechanism, if and when it is designed, must inherit that
posture exactly. This document specifies the rules a batch confirmation
mechanism must satisfy before any implementation work begins. It does not
implement any of those rules.

## 1. Current Proposal Contract

Phase 6F established a deterministic, read-only-by-default scheduler
proposal contract built on top of the read-only recommendation layer.

- `agent_taskflow/task_recommendations.py` computes per-task
  recommendations. Recommendations are pure reads of the SQLite mirror and
  on-disk artifacts: status, current phase label, evidence summary,
  missing evidence, related artifacts, worktree/branch/PR/cleanup status,
  and consistency warnings. They do not execute anything.
- `agent_taskflow/scheduler_proposals.py` converts those recommendations
  into a scheduler proposal payload. It is gated by an explicit two-part
  confirmation (`dry_run=False` **and** `confirm_create_proposal=True`) to
  even record the proposal artifact; the default mode is dry-run.
- `scripts/create_scheduler_proposal.py` is the operator-typed CLI that
  exposes the proposal computation. There is no daemon, no cron, no loop,
  no webhook, no auto-runner behind it.
- Recorded proposal evidence uses **disjoint** artifact and event types:
  - `artifact_type = "scheduler_proposal"`
  - `event_type   = "scheduler_proposal_created"`
  These are intentionally distinct from any workflow action evidence type
  (`branch_push_completed`, `draft_pr_created`,
  `local_cleanup_completed`, `remote_branch_cleanup_completed`,
  `task_closeout_completed`, etc.).
- A scheduler proposal asserts only:

  > "The system proposes that a human may consider doing X."

  It never asserts that X happened, that X is approved, that X is safe,
  or that the operator has agreed to do X.

The Phase 6F payload bakes the proposal-only stance into its safety flag
surface so that downstream readers cannot accidentally treat proposal
evidence as action evidence. Every proposal carries the following
properties:

- `proposal_only = true`
- `workflow_action_performed = false`
- `action_evidence_created = false`
- `executor_started = false`
- `validators_started = false`
- `branch_pushed = false`
- `pr_created = false`
- `merged = false`
- `approved = false`
- `cleanup_performed = false`
- `background_worker_started = false`
- `will_execute = false`
- `will_push = false`
- `will_create_pr = false`
- `will_merge = false`
- `will_approve = false`
- `will_cleanup = false`
- `will_delete_branch = false`
- `will_delete_worktree = false`
- `will_mutate_github = false`
- `will_start_background_worker = false`

The contract is intentionally narrow: a proposal is just a sorted, filtered
view of the recommendation layer with reviewable provenance. The current
project ships **no** mechanism to "confirm a proposal" and execute it.
That gap is what this document scopes.

## 2. Why Batch Confirmation Is Dangerous

Batch confirmation — letting an operator approve a set of proposed
actions in one operator-typed step — is attractive because it amortizes
review across multiple tasks. It is also the single largest blast-radius
move in the architecture roadmap to date, because it compresses what is
currently N independently-gated decisions into one. The following risks
must be addressed by any future design before a single line of batch
confirmation code is written.

- **Stale proposals.** Time passes between proposal computation and
  confirmation. Task status, evidence, branch SHAs, PR state, worktree
  presence, and consistency warnings can all change. Confirming a stale
  proposal can confirm an action against a different world than the one
  the operator reviewed.
- **Replayed proposal IDs.** Without single-use semantics, the same
  proposal artifact can be confirmed twice (deliberately or by mistake)
  and re-trigger the same operation, causing duplicate PRs, double
  pushes, or double cleanups.
- **Proposal item evidence drift.** Even within the lifetime of a single
  proposal, individual items can drift: a missing artifact appears, a
  worktree gets removed, a PR is merged. Confirmation must be bound to
  *the exact item state* that was reviewed.
- **Branch head moving.** Between proposal time and execution time, the
  branch under a task can advance. Executing a "branch_push_review"
  action without re-checking the head SHA risks pushing unreviewed
  commits.
- **Worktree disappearing.** If the worktree path is gone at execution
  time, an action operating on `<repo>/.worktrees/<task-key>` may target
  a path that no longer maps to the recorded branch. This is exactly the
  consistency-warning class that the recommendation layer already
  surfaces, and it must not be ignored at confirmation time.
- **Task status changing.** A task that was `waiting_approval` at
  proposal time may now be `approved`, `rejected`, or `archived`.
  Acting on the older status mis-routes the workflow.
- **Duplicate PRs.** A draft PR may have been created by another operator
  between proposal and confirmation. Auto-creating a second draft PR on
  the same head branch is a destructive duplication risk.
- **Cleanup after wrong merge.** A merged-PR record from one head SHA
  must not authorize cleanup of a branch that has since had additional
  commits added.
- **Mixed-risk action batches.** A batch containing both an
  `inspect_evidence` and a `branch_push_review` invites the operator to
  approve the riskier one with the same eye-load as the inspection.
- **Operator confirming more than they reviewed.** If the proposal item
  set can grow between display and confirmation, an operator may approve
  items they never saw.
- **Scheduler silently expanding a batch.** If a batch is identified by
  a query rather than by an immutable item list, the scheduler can
  re-compute the query at confirmation time and silently include new
  items. This is class D behavior under the
  `docs/scheduler-automation-boundary.md` matrix.
- **Proposal artifact mistaken for action evidence.** A downstream reader
  that mis-classifies `scheduler_proposal` as evidence of an action
  having occurred would inappropriately advance the workflow. The
  artifact/event type namespaces are disjoint **today** and must stay
  disjoint under any future confirmation contract.
- **Hidden self-approval.** A scheduler that treats its own proposal as
  approval, or treats a batch confirmation as approval/rejection of a
  task, has crossed into class D. Approval and rejection are
  human-issued and human-attributed and must never be derived from a
  batch confirmation artifact.

These are the hazards the design below must close.

## 3. Proposal Identity and Item Binding

Confirmation of a proposal must be bound to *the exact proposal* and
*the exact items* that were reviewed. The current proposal payload
already carries identifying fields, but they are not yet cryptographically
bound. A future hash binding is required.

### 3.1 Required proposal fields

Each proposal must carry:

- `proposal_id` — globally unique opaque identifier (already present).
- `schema_version` — e.g. `scheduler_proposal.v1` (already present).
- `created_at` — UTC ISO-8601 (already present).
- `source` — the producing module/policy (already present).
- `policy.name` and `policy.version` — the policy under which the
  proposal was generated. A version field should be added so policy
  drift is itself reviewable. (`policy.name` is present today; an
  explicit version is a future addition.)
- `db_path` (or a DB identity summary suitable for non-local mirrors).
- `filters` — the exact filter object that defined the candidate set.
- `items` — the deterministically-ordered list of selected items.
- `policy.max_items`, `policy.include_command_kinds`,
  `policy.exclude_command_kinds`, `policy.include_completed`,
  `policy.include_no_action`, `policy.include_unknown` — these are
  already present and must remain part of the hashable payload.
- `proposal_hash` — a deterministic hash of the normalized proposal
  payload excluding clearly non-semantic fields (e.g. local
  `artifact_path`, the wall-clock `created_at` if a stable identity is
  preferred, and the proposal_hash field itself). Choice of canonical
  exclusions is part of the future design phase; the rule is that two
  proposals computed from the same mirror state under the same policy
  and filters must hash identically.

### 3.2 Required per-item fields

Each item in `items` must carry:

- `proposal_item_id` — opaque identifier unique within the proposal.
- `task_key` — already present.
- `recommended_command_kind` — already present and constrained to the
  `RECOMMENDED_COMMAND_KINDS` enum.
- `current_phase_label` — already present.
- `expected_status` — the task status the proposal was computed against.
- `expected_evidence_summary` — a normalized digest of the evidence the
  recommendation cited (events, artifacts), sufficient to detect
  semantic drift without inlining the full payload.
- `expected_artifact_ids` / `expected_event_ids` — when the
  recommendation depended on specific artifact/event rows, those IDs
  (or their content hashes) must be captured.
- `expected_branch_name`, `expected_branch_head_sha`,
  `expected_base_sha` — when applicable to the command kind
  (push, draft PR, cleanup).
- `expected_pr_number`, `expected_pr_state` — when applicable.
- `consistency_warnings` — exactly as observed at proposal time
  (already present).
- `item_hash` — deterministic hash of the normalized item payload.

### 3.3 Deterministic ordering

Items must be sorted by a fully deterministic key — today this is
`(priority_rank, severity_rank, task_key)`. Any future change must
remain a total order so that two equivalent proposal computations
produce byte-identical `items` lists, which is necessary for
`proposal_hash` to be stable.

### 3.4 Hash binding rule

A future confirmation request must include:

- `proposal_id`
- `proposal_hash`
- one or more `proposal_item_id` values
- each selected item's `item_hash`

A confirmation that names items by query, filter, command kind, or
"all current recommendations" is invalid by construction. The rule
is:

> A confirmation cannot say "confirm all current recommendations." It
> must say "confirm these exact proposal items from this exact
> proposal, with these exact item hashes."

## 4. Replay Prevention

The following are hard rules. None of them are implemented in this
phase; this section is the contract any future implementation must
satisfy.

- **Single-use confirmations.** A confirmation that names a specific
  `(proposal_id, proposal_item_id)` pair may be processed at most once.
  A repeat confirmation of the same pair is a hard failure regardless
  of operator identity.
- **No item re-confirmation.** Once a proposal item has been confirmed,
  it cannot be confirmed again. If the operator needs to redo it, the
  scheduler must re-compute a fresh proposal that produces a new
  proposal_id and a new proposal_item_id.
- **Expiration.** Every proposal must carry an expiration policy
  (see §5). A confirmation must reject any item whose proposal is
  expired.
- **Revalidation.** Every confirmed item must be revalidated against
  the current mirror state immediately before the future
  command-specific helper performs any action (see §6). Unexpired ≠
  fresh.
- **Confirmation artifact contents.** The confirmation artifact must
  record, at minimum:
  - `confirmation_id` (opaque identifier)
  - `confirmed_by` (operator identity if available; otherwise an
    explicit "operator-typed CLI" attribution)
  - `confirmed_at` (UTC ISO-8601)
  - `proposal_id`
  - `proposal_hash` (as known at confirmation time)
  - `selected_proposal_item_ids`
  - `selected_item_hashes`
  - `expiration_check_result` (`fresh` / `expired`)
  - `revalidation_result` per item (`match` / `drift_detected` /
    `not_revalidated`)
  - `actions_allowed_for_later_execution` (the command kinds the
    confirmation is willing to authorize, after revalidation)
- **Hash invalidation.** If the proposal_hash has changed since the
  proposal was reviewed (e.g. policy version or item set drifted), the
  confirmation is invalid. If an individual item_hash has changed, that
  item is invalid even if the rest of the proposal hash still matches.
- **Status invalidation.** If the task's current status differs from the
  proposal's `expected_status`, the confirmation for that item is
  invalid.
- **Evidence invalidation.** If the evidence summary the proposal
  depended on has changed in a way that would alter the recommendation
  (new validator failure, new event indicating later state, new
  artifact replacing an expected one), the confirmation for that item
  is invalid.

Again: this section is **documented requirements**, not implemented
behavior.

## 5. Proposal Expiration Policy

A proposal must define how long it is *eligible* for confirmation.
"Eligible" is necessary but not sufficient: even an unexpired proposal
must still pass §6 revalidation before any execution.

### 5.1 Default expirations by command kind

The following defaults are recommendations for any future design. They
reflect the blast-radius of the action and the rate at which the
underlying evidence drifts.

| Command kind                       | Default expiration | Notes |
| ---                                | ---                | ---   |
| `branch_push_review`               | 15 minutes         | Branch HEAD moves; pushing stale HEAD is a regression. |
| `draft_pr_review`                  | 15 minutes         | Other operators may create a draft PR on the same head; collisions must be re-checked. |
| `cleanup_continue`                 | 15 minutes         | Cleanup chain state can advance; stale cleanup confirmations are dangerous. |
| `post_merge_cleanup_review`        | 15 minutes         | Depends on merged-PR state; downstream cleanup events may have already run. |
| `pr_handoff_package`               | 30 minutes         | Less destructive than push/PR creation, but still operates on current branch state. |
| `queued_task_handoff`              | 15 minutes         | Executor-start adjacent; revalidation required regardless. |
| `create_task_execution_package`    | 30 minutes         | Mostly authoring; still must revalidate task status. |
| `human_pr_review`                  | 24 hours           | Non-mutating: just points the operator at GitHub. |
| `inspect_blocker`                  | 24 hours           | Read-only. |
| `inspect_evidence`                 | 24 hours           | Read-only. |
| `no_action`                        | n/a                | Not confirmable; no execution to gate. |
| `unknown`                          | n/a                | Not confirmable until reclassified. |

### 5.2 Expiration policy rule

- Mutating command kinds default to **15 minutes** unless explicitly
  argued otherwise in a future design phase.
- Non-mutating inspection kinds may extend to **24 hours**.
- Expiration must be carried in the proposal payload, not inferred at
  confirmation time. Two readers must compute the same eligibility
  answer.
- Expiration is a **floor**, not a ceiling. The §6 revalidation step
  may still reject a fresh proposal.

> Even an unexpired proposal must be revalidated before execution.

## 6. Revalidation Before Execution

For every command kind a future batch confirmation might authorize, the
table below specifies what must be re-checked between the moment
confirmation is recorded and the moment a command-specific helper acts
on it.

Read columns as:

- "Task status must still match `expected_status`?"
- "Required artifacts/events must still exist (and match `expected_*` IDs/hashes)?"
- "Worktree path must still exist on disk under `<repo>/.worktrees/<task-key>`?"
- "Branch SHA must still match `expected_branch_head_sha`?"
- "PR state must still match `expected_pr_state`?"
- "Consistency warnings allowed at execution?"
- "Execution allowed from batch confirmation alone?"
- "Human confirmation (`--confirm-*`) still required?"

| Command kind                       | Status match | Evidence match | Worktree match | Branch SHA match | PR state match | Warnings allowed | Batch-confirm execute | Human confirm still required |
| ---                                | ---          | ---            | ---            | ---              | ---            | ---              | ---                   | ---                          |
| `create_task_execution_package`    | Yes          | Yes            | n/a            | n/a              | n/a            | No               | No (Design A) / Maybe (Design B, single-action only) | Yes |
| `queued_task_handoff`              | Yes          | Yes            | n/a            | n/a              | n/a            | No               | No                    | Yes                          |
| `pr_handoff_package`               | Yes          | Yes            | Yes            | n/a              | n/a            | No               | No                    | Yes                          |
| `branch_push_review`               | Yes          | Yes            | Yes            | Yes              | n/a            | No               | No                    | Yes (`--confirm-branch-push`) |
| `draft_pr_review`                  | Yes          | Yes            | Yes            | Yes              | Yes (no existing open PR on head) | No | No                    | Yes (`--confirm-draft-pr`)   |
| `human_pr_review`                  | n/a (info)   | Yes            | n/a            | n/a              | Yes (links must still resolve) | Yes (warned, not actioned) | No (system cannot execute this) | n/a (human-side only) |
| `post_merge_cleanup_review`        | Yes          | Yes (merged-PR evidence) | Yes | Yes              | Yes (`MERGED`) | No               | No                    | Yes                          |
| `cleanup_continue`                 | Yes          | Yes            | Yes (or evidence of prior local cleanup) | Yes | Yes (`MERGED`) | No | No                    | Yes (`--confirm-local-cleanup` / `--confirm-remote-branch-delete` / closeout, as applicable) |
| `inspect_blocker`                  | n/a          | n/a            | n/a            | n/a              | n/a            | Yes (this is the point) | No (no mutation to execute) | n/a |
| `inspect_evidence`                 | n/a          | n/a            | n/a            | n/a              | n/a            | Yes              | No                    | n/a                          |
| `no_action`                        | n/a          | n/a            | n/a            | n/a              | n/a            | n/a              | **Never**             | n/a                          |
| `unknown`                          | n/a          | n/a            | n/a            | n/a              | n/a            | n/a              | **Never**             | n/a                          |

Policy implications:

- `inspect_*` items are proposable, reviewable, and can be confirmed in
  a batch as *acknowledged observations*. They never mutate state.
- `no_action` and `unknown` are never executable from a batch
  confirmation. They are surface signals only.
- `human_pr_review` cannot be executed by the system — the system does
  not call `gh pr merge` or any merge equivalent. A batch may
  "acknowledge" such an item, but the system never acts.
- `branch_push_review`, `draft_pr_review`, `post_merge_cleanup_review`,
  `cleanup_continue`, `pr_handoff_package`, `queued_task_handoff`, and
  `create_task_execution_package` must revalidate the exact branch / PR
  / evidence / worktree state listed above. Drift at revalidation time
  must invalidate the confirmed item.
- A merge action is **never** executable. There is no
  `gh pr merge` call in the codebase; there must not be one introduced
  by a scheduler.
- Approval and rejection are **never** scheduler-executable. They are
  human decisions and a batch confirmation must not write an approval
  record.
- Cleanup is **never automatic**. Even if a future batch confirmation
  authorizes a `cleanup_continue` item, the action must still name an
  exact worktree path, branch name, and merged-PR target, and must
  still pass `local_cleanup_confirm` / `remote_branch_cleanup_confirm`
  preconditions (merged-PR evidence, protected-branch list,
  post-delete `git ls-remote` re-check, etc.).

## 7. Batch Composition Rules

Even with hash binding, expiration, and revalidation, the *shape* of a
batch matters. A batch that mixes risk classes leads to operators
approving high-risk items with the eye-load of low-risk ones.

### 7.1 Default composition policy

- **No mixed mutating command kinds in a single confirmed batch.** A
  batch is allowed to contain multiple items, but they must share a
  single command kind unless the batch consists entirely of
  inspection-only kinds.
- **Inspection-only items may be grouped.** `inspect_blocker` and
  `inspect_evidence` items may be batched together because they cannot
  mutate state.
- **Mutating proposal items group by command kind only.** For example,
  all items confirmed in a batch must be `branch_push_review`, or all
  `draft_pr_review`, or all `cleanup_continue`. They must not mix.
- **Never combine in a single batch:**
  - `branch_push_review` + `draft_pr_review`
  - `draft_pr_review` + `cleanup_continue`
  - `queued_task_handoff` + `draft_pr_review`
  - `cleanup_continue` + closeout-adjacent kinds
- **Never include in any batch:** merge, approve, reject, force push,
  protected-branch deletion, worktree deletion outside
  `<repo>/.worktrees/`. These are class D under
  `docs/scheduler-automation-boundary.md`.

### 7.2 No transitive execution

Confirming "branch_push_review" must not authorize a subsequent
"draft_pr_review" automatically, even if the push succeeded. Each
mutating step must be its own confirmation against its own proposal
item, against fresh evidence. The chain may be re-proposed and
re-confirmed, but it is not implicit.

This is a hard rule because the entire reason for the existing
`--confirm-*` family is that *each gate* is a separate decision. Batch
confirmation must not erode that.

## 8. Never Batch-Confirm List

The following actions must never be executable from any batch
confirmation, today or in the future, regardless of evidence quality:

- merge a PR (no `gh pr merge`, no equivalent API call)
- approve a task (no `status="approved"` transition driven by
  confirmation)
- reject a task (no `status="rejected"` transition driven by
  confirmation)
- force push (no `--force` from a scheduler or batch confirmation)
- delete a protected branch (`main`, `master`, `trunk`)
- delete a worktree outside `<repo>/.worktrees/<task-key>`
- delete `main` / `master` / `trunk`
- auto-run an executor for a newly discovered issue without an
  operator-selected task key
- auto-create a real (non-draft) PR from a recommendation alone
- auto-cleanup without an exact target worktree path **and** branch
  name **and** merged-PR evidence
- any action whose proposal item carries non-empty
  `consistency_warnings` unless the operator has explicitly
  acknowledged each warning by name (per-warning, not a blanket
  acknowledgement)

These reaffirm `docs/scheduler-automation-boundary.md` §2 class D and
extend it with the batch-confirmation-specific cases (the
"acknowledge-each-warning" rule and the "no real PR from recommendation
alone" rule).

## 9. Future Confirmation Artifact Contract

Phase 6F established the proposal artifact and event. Any future
confirmation phase would need a parallel artifact / event pair,
**again disjoint** from action evidence types.

### 9.1 Types

- `artifact_type = "scheduler_confirmation"`
- `event_type   = "scheduler_confirmation_created"`

These must never be interpreted by downstream readers as evidence that
the underlying workflow action ran. They record operator intent only.

### 9.2 Possible schema (illustrative; not implemented)

```json
{
  "schema_version": "scheduler_confirmation.v1",
  "confirmation_id": "confirmation-…",
  "proposal_id": "proposal-…",
  "proposal_hash": "sha256:…",
  "confirmed_at": "2026-…Z",
  "confirmed_by": "operator-typed-cli",
  "selected_items": [
    {
      "proposal_item_id": "item-…",
      "item_hash": "sha256:…",
      "task_key": "agent-taskflow/repo#123",
      "recommended_command_kind": "branch_push_review",
      "operator_acknowledged_warnings": true,
      "revalidation_required": true
    }
  ],
  "execution_allowed": false,
  "reason": "Confirmation artifact only; execution still requires the command-specific confirm helper (--confirm-*) to revalidate and act."
}
```

### 9.3 Important properties

- `execution_allowed = false` by default. A confirmation artifact is
  not an executable contract by itself; it is a record of operator
  intent that *some other command-specific helper* may consume.
- `revalidation_required = true` per selected item. Confirmation is not
  the last gate; the command-specific helper must re-check task
  status, evidence, branch SHA, PR state, worktree path, and warnings
  before acting.
- `operator_acknowledged_warnings` must be true on a per-item basis if
  any consistency warning was present at proposal time. The blanket
  "acknowledge all" form is not allowed.
- The confirmation must reference its proposal by `proposal_id` *and*
  `proposal_hash`, and must reference each item by `proposal_item_id`
  *and* `item_hash`.

> The confirmation artifact is **not** action evidence. It says only:
> "Operator approved this proposal item for a later
> command-specific execution attempt."

## 10. Relationship to Existing Confirm Helpers

The current architecture has a family of single-action confirm helpers,
each of which is the actual gate for one mutating step:

- `--confirm-handoff` (`queued_task_handoff.py`)
- `--confirm-branch-push` (`branch_push_confirm.py`)
- `--confirm-draft-pr` (`draft_pr_confirm.py`)
- `--confirm-local-cleanup` (`local_cleanup_confirm.py`)
- `--confirm-remote-branch-delete` (`remote_branch_cleanup_confirm.py`)
- `--confirm-task-closeout` (`task_closeout_confirm.py`)

A future batch confirmation must **not** bypass these. There are two
plausible relationships between batch confirmation and the existing
confirm helpers.

### 10.1 Design A — confirmation artifact as pre-approval (recommended)

- Batch confirmation records operator intent in a
  `scheduler_confirmation` artifact.
- The artifact is **not** consumed by command-specific helpers.
- Each command-specific helper still requires its own `--confirm-*`
  flag, typed by the operator, and still performs its own
  revalidation.
- The confirmation artifact is purely an audit / queue artifact: it
  documents that the operator looked at proposal X and intends to
  carry out items Y and Z, but the actual mutation still happens
  through the normal single-action confirmation path.

Properties:

- Safest. No automation surface introduced.
- The audit trail is improved (intent → action linkage), but the
  decision boundary is unchanged.
- The "confirmation" word does not imply "execution" anywhere in this
  design. That separation must be preserved in documentation and code
  names.

### 10.2 Design B — batch confirmation token consumed by command helper

- Batch confirmation produces a token (the confirmation artifact ID).
- Each command-specific helper accepts that token in place of the
  per-action `--confirm-*` flag.
- Before acting, the helper verifies:
  - `proposal_hash` matches the proposal it points at
  - the named `proposal_item_id` is in the confirmation
  - the named `item_hash` still matches the current proposal item
  - the confirmation has not expired (per §5)
  - revalidation passes (per §6)
  - only one command-specific action is attempted per consumption
- The helper performs only the single action for which the token was
  meant. The token is consumed (marked used) after a successful
  action, and cannot be reused.

Properties:

- More automated; less typing per batch.
- More risky. The "operator approved by typing" property weakens to
  "operator approved by token". Token handling must be designed with
  the same care as a credential.
- Any future implementation must address: token storage, token
  exposure in logs, token replay, partial-batch failure, and
  rollback.

### 10.3 Recommendation

Design A is the recommended starting point. It captures operator intent
and gives the audit trail the linkage it currently lacks, without
weakening any existing gate. Design B should be considered only after
Design A has been in operator use long enough to surface the friction
it does not solve, and only after the hash binding, expiration, and
revalidation rules above are implemented and proven.

## 11. Proposal Review UI Boundary

If Mission Control later surfaces scheduler proposals to operators, the
following constraints apply.

- **Initial mode is read-only display.** The proposal review surface
  must show: proposal id, created_at, policy name and version,
  filters, item list, per-item recommended_command_kind, severity,
  reason, evidence summary, missing evidence, consistency warnings,
  and the exact `--confirm-*` command the operator would type to act
  on the item.
- **No execute / merge / approve / cleanup buttons.** The first
  iteration must not surface action affordances at all. The
  read-only stance from `docs/workflow-policy-mission-control-display-design.md`
  applies.
- **At most "copy command" or "mark reviewed" in a later phase.**
  These are operator ergonomics, not execution. "Copy command" must
  include the full operator-typed CLI invocation with the relevant
  `--confirm-*` flag, so the operator can paste-and-edit. "Mark
  reviewed" must be confined to the operator's own review state and
  must not write workflow evidence.
- **No silent refresh-and-execute.** A proposal that drifts (new
  items appearing, items disappearing) must not be confirmable as if
  it were the proposal the operator originally reviewed. The UI must
  surface drift loudly and force the operator to re-review.
- **No "approve all".** Even Design A does not allow blanket
  approval of all current recommendations. The UI must require
  per-item selection.

## 12. Future Phase Order

The following ordering is proposed. None of these are committed by
this document; each requires its own scoping, design, and operator
approval before implementation.

1. **Add `proposal_hash` and `item_hash` to `scheduler_proposals.py`.**
   This is the smallest, lowest-risk forward step and is required by
   §3 and §4. Pure addition to the payload; existing readers ignore
   unknown fields.
2. **Add `expected_status`, `expected_evidence_summary`, and (where
   applicable) `expected_branch_head_sha`, `expected_base_sha`,
   `expected_pr_number`, `expected_pr_state` to proposal items.**
   These are also pure additions to the payload.
3. **Add design-doc tests if the repo convention supports them**
   (existing governance docs do not appear to use doc-tests; if a
   future convention emerges, this entry expands).
4. **Add a read-only proposal review API** (FastAPI), strictly
   read-only, returning proposal payloads as already recorded under
   `.agent-taskflow/artifacts/scheduler_proposals/`.
5. **Add a Mission Control proposal display** that respects §11.
6. **Specify the `scheduler_confirmation` artifact contract.**
   Documentation only, no code, no implementation.
7. **Implement a confirmation dry-run smoke test.** Verifies that the
   confirmation artifact format is stable, deterministic, and disjoint
   from action evidence over a synthetic mirror.
8. **Implement single-item confirmation consumption (Design A only).**
   The confirmation artifact is recorded; command-specific helpers
   still gate via their own `--confirm-*` flag.
9. **Only then discuss batch confirmation runtime,** including
   policy on which command kinds may be batched and how revalidation
   integrates with the command-specific helpers.
10. **Still no background loop** until the proposal + confirmation +
    revalidation chain is proven over multiple real operator-driven
    cycles.

The ordering is conservative on purpose. Each step is a strict subset
of the safety surface of the next.

## 13. Non-Goals

The following are explicitly out of scope for this phase, and any phase
following directly from it without separate authorization:

- No batch confirmation implementation in this phase.
- No scheduler execution.
- No background loop.
- No polling.
- No webhook.
- No cron.
- No auto-runner.
- No DB schema migration.
- No runtime code changes.
- No GitHub mutation.
- No executor run.
- No validator run.
- No branch push.
- No PR creation.
- No merge.
- No approval transition.
- No rejection transition.
- No cleanup of any kind.
- No new dependency.
- No Mission Control UI mutation affordance.
- No changes to the existing `--confirm-*` helpers.
- No new artifact types, event types, or store APIs.
- No new tests beyond what a governance doc-only phase already
  requires (this repository's governance docs do not currently carry
  doc-tests, so none are added here).

The "manage work, not agents" principle is preserved by this document.
A future batch confirmation mechanism, when and if it exists, will be
one more deterministic operator-driven step that records intent and
gates execution behind revalidation. It will not be a worker, will not
be a scheduler, and will not approve, merge, or clean up on its own.
