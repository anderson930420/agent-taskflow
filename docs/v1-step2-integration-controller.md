# V1 Step 2 — Integration Controller Module Map

Implements §42 Step 2 of the V1 Master Spec (Integration Controller,
Re-integration, PR outcomes, Merge). Spec section references below point at
that spec.

This document is the read-only inventory required before implementation plus
the resulting module boundary map.

## Read-only inventory

### Extended (additive only — no existing behavior rewritten)

| Module | Change |
| --- | --- |
| `agent_taskflow/models.py` | Add integration event types to `TASK_EVENT_TYPES` and integration artifact types to `TASK_ARTIFACT_TYPES`. Set members only. No status is added: Step 2 resolves its statuses through `status_vocab` (§12.2 ruling). |
| `agent_taskflow/store.py` | Register two named idempotent migrations in `_MIGRATIONS` / `SCHEMA_MIGRATIONS`: `v1_step2_integration_tables` creates the Step-2 tables; `v1_step2_conflict_verification` adds one column to Step 2's private conflict-evidence table. No existing method changed. |
| `WORKFLOW.md` | Add a descriptive Integration Controller boundary section. Non-Goals list untouched. |

### Created

| Module | Responsibility | Spec |
| --- | --- | --- |
| `agent_taskflow/integration_schema.py` | Authoritative §32.1 Ticket PR field list, enums, defaults, Step-2 lifecycle statuses and allowed transitions | §32.1, §12 |
| `agent_taskflow/integration_store.py` | Persistence for the public PR fields (`task_pr_state`) plus Step-2-private integration state, queue, lock, and evidence tables | §32.1 |
| `agent_taskflow/integration_git.py` | Allowlisted deterministic git operations: fetch, target resolution, `behind_count`, initial rebase, merge-target-into-branch, normal push, ancestry check | §24, §25, §26, §36 |
| `agent_taskflow/github_pr_adapter.py` | `gh` CLI adapter: create PR, update the same PR, poll PR state. Structurally refuses any merge subcommand | §31, §32, §34 |
| `agent_taskflow/integration_validators.py` | Validator runner + validator evidence persistence (name, command, output, branch SHA, target SHA, diff context) | §29 |
| `agent_taskflow/integration_conflict_resolver.py` | Bounded AI conflict resolver interface + resolution evidence; default resolver resolves nothing | §27, §27.2.1 |
| `agent_taskflow/integration_queue.py` | Per-repo FIFO integration queue and per-repo Loose Integration Lock | §22, §22.1, §23.1 |
| `agent_taskflow/reviewer_hints.py` | Reviewer hint generation and the §26.1 re-integration hint block | §26.1, §38 |
| `agent_taskflow/integration_controller.py` | The integration pipeline: initial integration and re-integration under the loose lock | §23, §24, §26 |
| `agent_taskflow/merge_verification.py` | Merge detection identity + verification of `merge_commit_sha` in target history | §35, §36, §36.1 |
| `agent_taskflow/integration_watcher.py` | Target-freshness polling and PR outcome polling (idempotent) | §25, §25.1, §32, §33, §35 |
| `agent_taskflow/integration_cleanup.py` | Cleanup gated on verified merge, plus the explicit-confirmation closed-unmerged path | §37, §37.1 |
| `agent_taskflow/integration_metrics.py` | Optimistic-execution, re-integration, and AI-resolver metrics | §39 |

### Deliberately not touched

`dispatcher.py`, `executors/*`, `scheduler_*`, `draft_pr*.py`, `branch_push*.py`,
`api/*`, `mission-control/*`, the existing `*_cleanup_confirm.py` operator gates,
and `validators/*` (reused through `validators.registry`, never edited).

## Field ownership (§32.1)

`task_pr_state` holds exactly the twelve §32.1 fields plus the `task_key` key.
Step 2 creates the table and is its only writer. Every other component reads it
and must tolerate `NULL`. Integration-internal state that §32.1 does not list
(`previous_integrated_base_sha`, `new_target_sha`, validator evidence, review
evidence, conflict evidence) lives in separate Step-2-private tables.

`tests/test_integration_schema.py` pins the public column set to
`integration_schema.TICKET_PR_FIELD_NAMES` so the set cannot drift.

## Safety boundary

- No code path can force-push: the only push allowed is
  `git push origin <task-branch>` (optional `-u`); every other push form is
  refused by an allowlist (review Ruling 3).
- No code path can merge: the `gh` adapter rejects `gh ... pr merge` by parsed
  argv, whatever the executable path or global flags, and main or the base
  branch can never be the pushed branch.
- No worktree, branch, or artifact is removed unless merge is verified per §36
  or the caller passes the explicit cancelled-cleanup confirmation flag (§37.1).
- Every entry point is dry-run by default and requires an explicit confirmation
  flag before it mutates git, GitHub, or the local filesystem.
- GitHub CI status is persisted but never consulted by any lifecycle transition.
