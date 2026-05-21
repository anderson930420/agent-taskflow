# Scheduler / Background Automation Boundary

This document is documentation-only. It defines the future boundary for
scheduler and background automation in agent-taskflow without implementing any
of it. No runtime code, scripts, DB schema, dependencies, or Mission Control UI
behavior change as a result of this document.

The core architectural principle of agent-taskflow is:

> Manage work, not agents.

AI coding tools (Pi, OpenCode, Codex, Claude Code, future tools) are bounded
implementation workers. They do not own scheduling, task selection, lifecycle
state transitions, validation decisions, approval decisions, merge behavior,
push behavior, or cleanup behavior. This document carries the same principle
forward into any future scheduler discussion: a scheduler is just one more
deterministic component that must respect the same gates, not a new privileged
authority.

## 1. Current Human-Driven Contract

The end-to-end chain implemented today is fully human-driven. Every mutating
transition is operator-confirmed, evidence-backed, and reversible up to the
GitHub merge boundary.

```
GitHub Issue / spec
  → deterministic intake gate                 (github_issue_intake_gate.py,
                                               --confirm-intake)
  → SQLite TaskRecord(status="queued")        (store.py)
  → Task Execution Package                    (artifact + event evidence;
                                               human-authored contract)
  → explicit queued-task handoff              (queued_task_handoff.py;
                                               operator-triggered)
  → workspace preparation                     (workspace_manager.py,
                                               worktree.py;
                                               <repo>/.worktrees/<task-key>)
  → bounded executor                          (executors/: manual, shell,
                                               opencode, pi)
  → deterministic validators                  (validators/: pytest, policy,
                                               changed-files, optional openspec)
  → waiting_approval                          (proof-of-work artifacts
                                               recorded under
                                               .agent-taskflow/artifacts/)
  → PR handoff package                        (pr_handoff_package.py)
  → branch push confirmation                  (branch_push_confirm.py,
                                               --confirm-branch-push;
                                               dry-run default;
                                               protected branches blocked)
  → draft PR confirmation                     (draft_pr_confirm.py,
                                               --confirm-draft-pr;
                                               post-create gh pr view
                                               verification)
  → human review / merge                      (manual, GitHub-side only)
  → post-merge cleanup recommendation         (post_merge_cleanup_recommendation.py;
                                               recommendation only)
  → local cleanup confirmation                (local_cleanup_confirm.py,
                                               --confirm-local-cleanup)
  → remote branch cleanup confirmation        (remote_branch_cleanup_confirm.py,
                                               --confirm-remote-branch-delete)
  → task closeout / archive confirmation      (task_closeout_confirm.py)
  → task recommendations (read-only)          (task_recommendations.py,
                                               scripts/list_task_recommendations.py)
```

Properties of the current contract:

- **Explicit confirmation gates.** Every mutating step requires its own
  `--confirm-*` flag. Dry-run is the default for all mutating commands.
- **Proof-of-work evidence.** Each transition writes reviewable artifacts
  (executor logs, validation reports, changed-files audit, handoff package,
  branch push artifact, draft PR artifact with verification, cleanup
  artifacts, closeout artifact). Lifecycle transitions are gated on this
  evidence, not on AI worker claims.
- **No self-approval.** AI workers cannot mark their own work `approved`.
  Approval is a separate, human-gated decision.
- **No auto-merge.** Nothing in the codebase calls `gh pr merge` or equivalent.
- **No auto-push.** Branch push requires `--confirm-branch-push` and refuses
  protected branches and dirty worktrees.
- **No auto-cleanup.** Local cleanup, remote branch cleanup, and task closeout
  each require their own confirmation; none happen as a side effect of any
  other command.
- **No scheduler yet.** No webhook, no cron, no polling daemon, no background
  worker, no `while True` loop. Every state-changing command is triggered by
  a human typing it (or by a deterministic test driving it).
- **Mission Control is observability.** The FastAPI read-only API and the
  React UI surface state, evidence, and recommendations. They do not execute,
  approve, merge, or clean up.

The task recommendation layer (Phase 6E+6 and 6E+6.1) added a *read-only*
decision-support surface on top of this chain. It classifies mirrored evidence
and surfaces the next safe human-driven phase, plus consistency warnings for
DB/disk drift. It is not a scheduler and does not run any workflow action.

## 2. Automation Classes

Future automation work, if any, must be classified into exactly one of these
four classes before it ships. Misclassification — for example, smuggling a
class-C action into class-A "observation" — is a governance-breaking change.

### Class A — Read-only observation

Pure reads of the SQLite mirror, on-disk artifacts, and (optionally) the
filesystem. No writes to the store, the filesystem (outside an explicitly
labeled report path), GitHub, or any external service. No subprocess
invocation of executors or `gh`.

Examples:

- list queued tasks
- compute recommendations (`task_recommendations.py`)
- detect stale worktree rows
- detect missing artifacts
- detect merged PRs from existing draft PR evidence payloads
- detect blocked tasks
- count cleanup mismatches across all tasks
- render an operator dashboard

This class is broadly safe. It can be re-run any number of times without
changing system state.

### Class B — Dry-run preparation

Computes a *proposal* for a future action without performing it. May write to
the filesystem only under an explicitly labeled preview path (for example,
`<artifact_dir>/scheduler_proposals/`) and may write a single `proposal`
artifact to the SQLite store when it adds value, but must not write any event
or artifact that other code interprets as evidence of the underlying action
having occurred.

Examples:

- prepare a candidate recommendation report
- generate a `pr handoff package` preview without producing a final handoff
- generate the exact `gh pr create --draft …` command preview that would be
  executed if confirmation were given
- verify evidence availability for a proposed cleanup (without running it)
- produce a per-operator summary report

Class B may be safe when:

- it writes nothing, or only writes to an explicitly labeled `proposal`
  surface;
- it cannot be confused with class-C evidence by any other reader;
- its output explicitly names the operator command that would carry out the
  action it is proposing.

### Class C — Confirmed deterministic action

The existing mutating operations. Each requires explicit human confirmation
(by `--confirm-*` flag today). A scheduler may stage these actions but may
not execute them without an in-band confirmation captured per action or per
operator-approved batch.

Examples:

- create Task Execution Package
- run queued-task handoff (`queued_task_handoff.py`)
- run approved task (`approved_task_runner.py`)
- branch push (`branch_push_confirm.py --confirm-branch-push`)
- draft PR creation (`draft_pr_confirm.py --confirm-draft-pr`)
- local cleanup (`local_cleanup_confirm.py --confirm-local-cleanup`)
- remote branch cleanup
  (`remote_branch_cleanup_confirm.py --confirm-remote-branch-delete`)
- task closeout (`task_closeout_confirm.py`)

These remain operator-gated. A scheduler must surface them as proposals first
and accept confirmation second, never reverse the order, and never auto-confirm
based on its own analysis.

### Class D — Prohibited automation

Actions that must remain outside any future scheduler regardless of evidence
or policy:

- auto-merge a PR (no `gh pr merge`, no equivalent API call)
- auto-approve a task (no scheduler-issued `status="approved"` transition)
- auto-cleanup without per-action confirmation
- auto-delete branches (local or remote) without confirmation and evidence
- auto-delete worktrees without confirmation and evidence
- auto-push (including any "convenience" follow-on push after another action)
- auto-create a PR from a recommendation alone, without operator confirmation
- auto-run executors from a newly discovered GitHub issue without operator
  approval of the ingestion and the dispatch
- a background process selecting tasks silently (no visible queue,
  no operator-readable proposal)
- workflow mutation without proof-of-work evidence
- "self-approval" by any worker, executor, validator, or scheduler

Class D is non-negotiable for the lifetime of the project as long as the
"manage work, not agents" principle holds.

## 3. Recommendation Layer as Scheduler Input

The recommendation layer (`agent_taskflow/task_recommendations.py`,
`scripts/list_task_recommendations.py`) is **not** a scheduler. It is a
read-only decision-support layer. It produces a deterministic per-task
record:

- `task_key`
- `project`
- `status`
- `current_phase_label`
- `recommended_next_action` (operator-facing string)
- `recommended_command_kind` (machine-facing enum, in
  `RECOMMENDED_COMMAND_KINDS`)
- `confidence`, `severity`, `reason`, `required_human_confirmation`
- `safety_flags` (all mutation flags `false`; `read_only=true`)
- `evidence_summary`, `missing_evidence`, `related_artifacts`
- `worktree_status`, `branch_status`, `pr_status`, `cleanup_status`
- `consistency_warnings` (stale worktree row, missing physical path, cleanup
  evidence vs row mismatch)

A future scheduler may consume this output, but must:

- treat `recommended_command_kind` as a *suggestion*, not a license to act;
- never execute any class-C action solely because a recommendation pointed
  to it;
- always pair execution with explicit human confirmation captured per action
  (or per an operator-approved batch, see §4);
- record proposal evidence separately from action evidence, so the audit
  trail distinguishes "we proposed X" from "X was confirmed and executed";
- refuse to act on items whose `consistency_warnings` is non-empty without
  surfacing the warnings to the operator first.

The recommendation layer is, in effect, the *input contract* for any future
scheduler. The scheduler does not get to invent its own decision model.

## 4. Future Minimal Scheduler Boundary

A future scheduler, if implemented at all, must start at the strictest end of
the design space and earn each relaxation through explicit operator
authorization. The minimum viable scheduler is:

- **Read-only scan.** Reads the SQLite mirror via the same read-only URI
  pattern used today (`file:…?mode=ro`, `PRAGMA query_only`). No writes to
  the store during scan.
- **Deterministic candidate selection.** Given the recommendation output,
  selects candidates by a published, reviewable policy (e.g. "all
  `pr_handoff_package` recommendations with no consistency warnings"). No
  hidden heuristics, no LLM-based selection.
- **Produces a queue of proposed actions.** The output is a class-B proposal
  artifact: a list of operator commands the scheduler thinks should be run
  next, in order, with the exact evidence that would justify each.
- **No execution by default.** Default mode is "report only". Executing any
  class-C action requires an explicit operator step distinct from the
  scan.
- **No hidden loops.** No `while True`, no cron job, no webhook, no daemon.
  Initial invocation is by an operator typing a CLI command.
- **All proposed actions visible to the operator.** Each proposal must
  include task_key, recommended command, the exact `--confirm-*` flag the
  operator would type, the evidence the scheduler relied on, and any
  consistency warnings.
- **Explicit confirmation per action or per batch.** Per-action confirmation
  is the default. Batch confirmation, if added later, must be a single
  signed/operator-typed approval that names every command in the batch and
  cannot be silently expanded once issued.
- **Proposal evidence stored separately from action evidence.** A
  `scheduler_proposal` artifact type and `scheduler_proposal_created` event
  type can be introduced; they must never be interpreted as evidence that
  the proposed action ran. Action evidence remains the existing
  `branch_push_completed`, `draft_pr_created`, `local_cleanup_completed`,
  `remote_branch_cleanup_completed`, `task_closeout_completed` events with
  their existing semantics.

In short: the minimum scheduler is **a recommendation report generator with a
queue view**, not an autonomous worker loop. Any step beyond that is a
separate design discussion, not a delivery.

## 5. Allowed Future Automation Matrix

The matrix below specifies the intended policy per workflow phase. "Allowed?"
means "permitted under the agent-taskflow governance contract", not "shipped".
Nothing in this matrix is implemented as automation yet; the "Required
evidence" column simply lists what already exists or is expected as
proof-of-work for that step.

| Phase | Read-only detect allowed? | Dry-run allowed? | Auto-confirm allowed? | Human confirmation required? | Never automate? | Required evidence |
| --- | --- | --- | --- | --- | --- | --- |
| issue discovery | Yes | Yes | No | Yes (intake gate) | — | source issue/spec snapshot |
| task ingestion | Yes | Yes (default) | No | Yes (`--confirm-intake`) | — | `github_issue_ingested` event; `TaskRecord(status="queued")` |
| Task Execution Package | Yes | Yes | No | Yes | — | `task_execution_package` artifact + `task_execution_package_created` event; mission contract; implementation prompt |
| queued-task handoff | Yes | Yes | No | Yes | — | handoff record; `status` transition out of `queued`; executor run start event |
| executor run | Yes | Yes | No | Yes (operator-triggered) | Self-approval by executor | `executor_run_started` and `executor_run_finished` events; executor log; mission plan |
| validators | Yes | Yes | No | Yes (deterministic gate) | Model-based validation | `validation_result` events (pytest, policy, changed-files, optional openspec); review logs |
| waiting_approval | Yes | Yes | No | Yes | Self-approval by worker | waiting-approval summary; changed-files audit; PR-handoff readiness evidence |
| PR handoff package | Yes | Yes | No | Yes | — | `pr_handoff_package` artifact + `pr_handoff_package_created` event |
| branch push | Yes | Yes (default) | No | Yes (`--confirm-branch-push`) | Force push; push to protected branch | `branch_push` artifact + `branch_push_completed` event; `push_ok=true`; recorded head SHA |
| draft PR | Yes | Yes (default) | No | Yes (`--confirm-draft-pr`) | Auto-create real PR from recommendation alone | `draft_pr` artifact + `draft_pr_created` event; post-create `gh pr view` verification |
| human merge | Yes (read merged state) | n/a | No | Yes (GitHub-side, human) | Auto-merge | merged-PR verification payload (`current_state=MERGED` or `recorded_post_merge=true`) |
| post-merge cleanup recommendation | Yes | Yes | No | Yes (operator reads it) | — | cleanup recommendation snapshot from merged-PR evidence |
| local cleanup | Yes | Yes (default) | No | Yes (`--confirm-local-cleanup`) | Delete unmerged branch; delete worktree without merged-PR evidence | `local_cleanup` artifact + `local_cleanup_completed` event |
| remote branch cleanup | Yes | Yes (default) | No | Yes (`--confirm-remote-branch-delete`) | Delete protected branch; delete branch without merged-PR + local-cleanup evidence | `remote_branch_cleanup` artifact + `remote_branch_cleanup_completed` event; post-delete `git ls-remote` re-check |
| task closeout / archive | Yes | Yes | No | Yes | Mark complete without verified merge + local + remote cleanup | `task_closeout` artifact + `task_closeout_completed` event |

Read across each row:

- Read-only detection is broadly allowed for every phase.
- Dry-run preparation is allowed for every mutating phase.
- Auto-confirm is **No** for every phase.
- Human confirmation is required for every state-changing transition.
- Specific destructive actions (force push, protected-branch delete,
  unmerged-branch delete, model-based validation, self-approval, auto-merge)
  are explicitly in the "never automate" column.
- Required evidence is what exists today (artifact types and event types
  produced by the current modules) — not new evidence categories invented by
  this document.

## 6. Human Gates

The following gates are hard and survive any future scheduler design. Each is
the contract between the deterministic orchestration code and the human
operator.

- **Executor start.** A scheduler may select a queued task, but starting an
  executor run remains operator-confirmed until a separate, explicit
  scheduler policy is designed *and approved*. A scheduler that quietly
  launches executors is class D.
- **Branch push.** Always requires confirmation (today: `--confirm-branch-push`).
  No silent push. Protected branches and dirty worktrees stay rejected.
- **Draft PR creation.** Always requires confirmation
  (`--confirm-draft-pr`), with post-create `gh pr view` verification of the
  base, head, draft state, title, files, and commits. No PR creation from a
  recommendation alone.
- **Merge.** Human / GitHub-side only. agent-taskflow does not, and will
  not, call `gh pr merge` or equivalent.
- **Local cleanup.** Requires `--confirm-local-cleanup`; protected branches,
  unmerged work, and missing merged-PR evidence are rejected paths.
- **Remote branch cleanup.** Requires `--confirm-remote-branch-delete`,
  merged-PR evidence, local cleanup evidence, and a post-delete
  `git ls-remote` re-check.
- **Task closeout.** Requires explicit closeout confirmation after verified
  merge, local cleanup, and remote branch cleanup. A scheduler may not mark
  a task complete on its own.
- **Approval / rejection.** Cannot be self-issued by an executor, a
  validator, or a scheduler. Approval is a human decision recorded as an
  approval record. A scheduler that produces an approval record is class D.

## 7. Evidence Requirements

Each transition requires the corresponding evidence to already exist in the
mirror (events + artifacts) before any automation is allowed to consider it
"reachable":

| Transition | Required existing evidence |
| --- | --- |
| issue → queued | source issue/spec snapshot; `github_issue_ingested` event; `TaskRecord(status="queued")` |
| queued → execution package | Task Execution Package artifact + `task_execution_package_created` event |
| queued → handoff | execution package above; explicit queued-task handoff record |
| handoff → executor run | `executor_run_started` event |
| executor → validators | `executor_run_finished` event with `status="completed"` and `exit_code in {0, None}` |
| validators → waiting_approval | one `validation_result` event per required validator, all `passed`; changed-files audit artifact |
| waiting_approval → PR handoff | `pr_handoff_package` artifact + `pr_handoff_package_created` event |
| PR handoff → branch push | `branch_push` artifact + `branch_push_completed` event with `push_ok=true` |
| branch push → draft PR | `draft_pr` artifact + `draft_pr_created` event with successful `gh pr view` verification |
| draft PR → merge (human) | merged-PR verification payload (`current_state=MERGED` or `recorded_post_merge=true`) |
| merge → cleanup recommendation | cleanup recommendation snapshot derived from the merged-PR evidence |
| cleanup recommendation → local cleanup | `local_cleanup` artifact + `local_cleanup_completed` event |
| local cleanup → remote branch cleanup | `remote_branch_cleanup` artifact + `remote_branch_cleanup_completed` event |
| remote cleanup → closeout | `task_closeout` artifact + `task_closeout_completed` event |

Any future scheduler is permitted to *read* this evidence to compute its
proposals. It is not permitted to *write* downstream evidence on the
assumption that an upstream step "must have happened". Missing evidence is
the answer; it is not an obstacle to be worked around.

## 8. Scheduler Failure Modes

This section lists the realistic failure modes a future scheduler must defend
against, with mitigations the current architecture already supplies.

| Failure mode | Risk | Mitigation |
| --- | --- | --- |
| Acting on stale DB rows | A scheduler executes based on a `TaskRecord` whose `status`/evidence has since been overwritten | Re-read state immediately before every proposal; only confirm an action whose proposal evidence still matches the current mirror state. |
| Acting on stale worktree paths | The mirror says a worktree is active but the path is gone (the GH-9605 case) | `task_recommendations.consistency_warnings` already surfaces this; a scheduler must refuse to confirm class-C actions for tasks with non-empty consistency warnings until the operator resolves them. |
| Double-running an executor | Two scheduler ticks both launch the same task | Per-task idempotency keyed off `executor_run_started` events; refuse to start if an in-flight run exists. Today this is enforced by explicit operator-triggered runs; a scheduler must preserve it. |
| Pushing the wrong branch | Scheduler attempts push for a branch that is not the recorded task branch | Branch push reads `TaskWorktreeRecord.branch` and the recorded `base_sha`; protected-branch list and dirty-worktree check stay in place; no `--force`. |
| Creating duplicate PRs | Scheduler creates a draft PR for a head branch that already has one open | Draft PR creation already checks for an existing open PR on the head branch before creation. Scheduler must surface that check result in its proposal. |
| Deleting unmerged branches | Scheduler triggers branch cleanup before merge | Local and remote cleanup confirmations both require merged-PR evidence; remote cleanup additionally requires local cleanup evidence. |
| Deleting the wrong worktree | Scheduler removes a worktree belonging to another task | Cleanup confirmations are scoped by `task_key` and verify worktree path, branch name, and base SHA before acting; protected branch list rejects `main`, `master`, `trunk`. |
| Treating model output as validation | A scheduler decides a task "looks done" without running validators | Validators remain deterministic Python code; their `validation_result` events are the only accepted signal. AI worker output is never a validator. |
| Hidden background mutation | A scheduler quietly writes events/artifacts that downstream code reads as confirmation | Proposal evidence must use a distinct artifact/event namespace (e.g. `scheduler_proposal`). Downstream readers (cleanup, closeout, recommendations) must not treat proposal evidence as action evidence. |
| Accidental self-approval | A scheduler issues an approval record from its own decision | Approval records are human-issued and human-attributed; a scheduler that writes one is non-conformant. |
| Confirmation replay / stale batches | An operator-approved batch is reused after state has changed | Batches must name a specific evidence snapshot (e.g. SHA of the mirror state and proposal artifact). Re-running after state change must require re-confirmation. |
| Webhook / polling race | A future webhook re-triggers the scheduler while a previous proposal is mid-confirmation | No webhook/polling in this phase. If ever added, must be guarded by the same idempotency + evidence-matching checks. |

## 9. Proposed Phase Order After This Document

Each entry is a possible future phase. None of them are committed by this
document; each requires its own scoping, design, and explicit operator
approval before any implementation work begins.

1. **Scheduler policy document / config schema.** A human-readable + later
   machine-readable schema describing which `recommended_command_kind` values
   the scheduler may propose, which it must skip, and which it must surface
   only with a warning.
2. **Read-only scheduler proposal generator.** A CLI that consumes
   `list_task_recommendations` output and produces a proposal report:
   per-task proposed command, exact `--confirm-*` flag the operator would
   type, supporting evidence, and any consistency warnings. Class A/B only.
3. **Proposal artifact format.** A `scheduler_proposal` artifact type and
   `scheduler_proposal_created` event type, defined in `store.py` and the
   workflow schema, kept disjoint from action evidence types.
4. **Batch confirmation design.** A design doc (not code) for how an
   operator could approve a named batch of proposals in one step without
   weakening per-action accountability.
5. **Scheduler dry-run smoke.** A golden-path smoke test that proves the
   proposal generator produces stable, evidence-backed proposals over a
   synthetic mirror without ever invoking a class-C action.
6. **Optional operator UI for proposals.** A read-only Mission Control view
   that surfaces current proposals with their evidence and `--confirm-*`
   commands, with no execute affordance.
7. **Only then consider timed/background mode.** Any decision to put the
   proposal generator behind a timed trigger requires its own dedicated
   phase, its own governance review, and an explicit revisit of every entry
   in §8.

The implicit ordering rule: each step makes the *next* step strictly less
risky than it would otherwise be. Skipping steps re-introduces the failure
modes in §8.

## 10. Non-Goals

The following are explicitly out of scope for this phase and any phase
following directly from it without separate authorization:

- No scheduler implementation in this phase.
- No daemon.
- No cron.
- No webhook.
- No GitHub polling loop.
- No auto-runner.
- No model-based validator.
- No AI reviewer.
- No auto-merge.
- No auto-cleanup.
- No automatic task selection.
- No automatic confirmation of any `--confirm-*` flag.
- No new DB schema migration.
- No new Mission Control mutation affordance.
- No new dependency.
- No changes to executor or validator behavior.
- No background process of any kind shipped under another label
  ("watcher", "observer", "syncer", "tick", "reconciler") that performs
  mutation outside the human gates listed in §6.

The "manage work, not agents" principle is preserved by this document. A
scheduler, when and if it exists, will be one more deterministic component
that surfaces work for human operators. It will not be a worker.
