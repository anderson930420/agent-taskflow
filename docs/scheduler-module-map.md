# Scheduler Module Map

This architecture map describes the scheduler-adjacent read, proposal,
confirmation, and verification surfaces. It documents existing boundaries only.
It does not implement scheduler confirmation consumption, auto-merge,
self-approval, branch push automation, PR creation automation, cleanup
automation, or background scheduler behavior.

## Module Boundaries

| Module | Responsibility | Writes | Boundary |
| --- | --- | --- | --- |
| `agent_taskflow/task_recommendations.py` | Read-only analysis of mirrored task evidence and next operator recommendation kinds. | None. | Recommendations are read-only analysis; they never execute, push, create PRs, merge, approve, reject, clean up, or start background workers. |
| `agent_taskflow/scheduler_candidate_discovery.py` | Phase G Level 1 read-only scheduler candidate listing layered atop `task_recommendations.py`. | None. | Candidate discovery is not execution permission; it never writes proposals, confirmations, handoffs, runtime audits, or invokes `approved_task_runner`. |
| `agent_taskflow/scheduler_proposals.py` | Build proposal payloads from recommendations and optionally record proposal artifacts/metadata. | Only proposal artifacts/DB metadata when explicitly confirmed by caller. | A proposal is not action evidence. It is review material only and must not be interpreted as proof that an action ran. |
| `agent_taskflow/scheduler_proposal_review.py` | Load and verify recorded scheduler proposal artifacts for human review. | None. | Review output is read-only and is not confirmation, execution permission, or action evidence. |
| `agent_taskflow/scheduler_confirmations.py` | Record operator-attested selection of hash-bound proposal items. | Only confirmation artifacts/DB metadata when explicitly confirmed by caller. | A confirmation is not runtime consumption. It is audit/pre-approval only and does not execute or permit execution by itself. |
| `agent_taskflow/scheduler_confirmation_verifier.py` | Dry-run binding, revalidation, and expiration checks for one confirmation item. | None. | The verifier is not a consumer. It emits no consumption event or artifact and is not action evidence. |

## Related Scripts

| Script | Responsibility | Confirmation | Boundary |
| --- | --- | --- | --- |
| `scripts/recommend_next_tasks.py` | Rank queued tasks for operator review. | No `--confirm-*`; read-only analysis. | Recommendation output is not a scheduler loop and not action evidence. |
| `scripts/list_task_recommendations.py` | List per-task recommendations across task states. | No `--confirm-*`; read-only analysis. | Recommendation output is not a scheduler loop and not action evidence. |
| `scripts/discover_scheduler_candidates.py` | List read-only scheduler candidates with required next gate and operator action (Phase G — Level 1). | No `--confirm-*`; discovery only. | Candidate listing is not execution permission and does not write proposals, confirmations, handoffs, or runtime evidence. |
| `scripts/create_scheduler_proposal.py` | Create or dry-run a proposal artifact from recommendations. | `--confirm-create-proposal` is required to record artifacts/metadata. | A proposal is not action evidence and does not execute selected actions. |
| `scripts/review_scheduler_proposal.py` | Review recorded proposal artifacts. | No `--confirm-*`; read-only review. | Review does not confirm or execute anything. |
| `scripts/create_scheduler_confirmation.py` | Create or dry-run a confirmation artifact for selected proposal items. | `--confirm-create-confirmation` is required to record artifacts/metadata. | A confirmation is not runtime consumption and does not bypass command-specific `--confirm-*` helpers. |
| `scripts/verify_scheduler_confirmation.py` | Dry-run verification of one confirmation item. | No `--confirm-*`; dry-run verifier only. | The verifier is not a consumer and writes no consumption evidence. |

## Flow

```text
task_recommendations.py
  -> scheduler_proposals.py
  -> scheduler_proposal_review.py
  -> scheduler_confirmations.py
  -> scheduler_confirmation_verifier.py
```

Each arrow is an operator-review boundary, not a runtime action boundary. The
pipeline can produce analysis, proposals, reviews, confirmations, and dry-run
verification reports. It must not be treated as a daemon, cron job, webhook, or
background scheduler.

## Safety Invariants

- Recommendations are read-only analysis.
- A proposal is not action evidence.
- A confirmation is not runtime consumption.
- The verifier is a dry-run binding/revalidation/expiration check, not a
  consumer.
- No module in this group starts background workers.
- No module in this group performs push, PR creation, merge, approval, cleanup,
  branch deletion, worktree deletion, auto-merge, or self-approval by itself.
- Future runtime consumption, if ever designed, must be a separate explicit
  workflow with fresh validation and command-specific confirmation gates.
