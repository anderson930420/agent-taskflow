# Read-Only Scheduler Candidate Discovery (Phase G)

## 1. Purpose

Phase G implements **Level 1 read-only scheduler candidate discovery**, as
recommended by the Phase F readiness checkpoint
(`docs/semi-automatic-scheduler-readiness-checkpoint.md` §8). It is the
strictly read-only step that makes a future scheduler design discussion
possible without compromising any operator gate.

This phase lists which tasks in the live mirror are scheduler candidates and
what gate the operator would have to walk through to act on each. It is
explicitly **not**:

- a Level 2 proposal generator
- a Level 3 handoff preparer
- a Level 4 runtime executor
- a Level 5 background / daemon scheduler

> Candidate discovery is **not** execution permission.
>
> Being listed by this layer is review material only. Human/operator
> confirmation remains required, `validation_result` remains authoritative,
> and Mission Control remains read-only.

## 2. What This Phase Adds

Phase G adds three artifacts and zero runtime behavior:

- `agent_taskflow/scheduler_candidate_discovery.py` — read-only module that
  reuses `agent_taskflow/task_recommendations.list_task_recommendations` and
  normalizes recommendations into a scheduler-vocabulary candidate list.
- `scripts/discover_scheduler_candidates.py` — read-only CLI that emits the
  candidate list as JSON.
- `tests/test_scheduler_candidate_discovery.py` and
  `tests/test_discover_scheduler_candidates_script.py` — tests that prove
  the read-only contract.

No new DB schema. No new dependency. No new API endpoint. No new Mission
Control affordance.

## 3. What It Does

- Reads task recommendation state via
  `agent_taskflow.task_recommendations.list_task_recommendations`.
- Normalizes each recommendation into a candidate record that speaks the
  scheduler vocabulary: `task_key`, `status`, `current_phase_label`,
  `recommended_command_kind`, `missing_evidence`, `consistency_warnings`,
  `required_next_gate`, `required_operator_action`, and per-candidate
  `safety` flags.
- Returns a top-level `safety` block whose flags are all locked to read-only
  and that explicitly states no proposal / confirmation / handoff / runtime
  evidence was created.

## 4. What It Does Not Do

The discovery layer must not do any of the following, by design and by
test:

- no proposal creation (`scheduler_proposal` artifact / event)
- no confirmation creation (`scheduler_confirmation` artifact / event)
- no verifier report creation
- no `intake_runner_handoff` artifact creation
- no `queued_task_handoff` execution
- no runtime audit events (`runtime_preflight_finished`,
  `runtime_execution_started`, `runtime_execution_finished`,
  `runtime_handoff_execution`)
- no call to `approved_task_runner`
- no executor or validator invocation
- no `gh` / GitHub mutation
- no branch push, PR creation, PR merge, branch deletion, worktree deletion
- no approval, rejection, or cleanup
- no scheduler loop, cron, webhook, or background worker
- no Mission Control action affordance
- no DB mutation in default mode (and there is no non-default mode for this
  layer — it is read-only only)

Structurally, the module does not import
`agent_taskflow.approved_task_runner`, `agent_taskflow.queued_task_handoff`,
`agent_taskflow.intake_runner_handoff`,
`agent_taskflow.scheduler_proposals`,
`agent_taskflow.scheduler_confirmations`,
`agent_taskflow.scheduler_confirmation_verifier`,
`agent_taskflow.executors`, or `agent_taskflow.dispatcher`. Tests assert
this.

## 5. Candidate Readiness

Each candidate carries:

- `candidate_ready: bool` — true when `recommended_command_kind` is in
  `ACTIONABLE_CANDIDATE_KINDS` (see below).
- `required_next_gate: str` — the named gate sequence an operator would
  have to walk through to act on this candidate.
- `required_operator_action: str` — the named next operator action.

A `candidate_ready` candidate is **not** automatically executable; it is
merely a candidate to enter the scheduler proposal flow if the operator so
chooses.

### Actionable kinds

```text
ACTIONABLE_CANDIDATE_KINDS = {
    "create_task_execution_package",
    "queued_task_handoff",
    "branch_push_review",
    "draft_pr_review",
    "pr_handoff_package",
    "cleanup_continue",
    "post_merge_cleanup_review",
    "inspect_blocker",
    "inspect_evidence",
}
```

### Required next gate mapping

| `recommended_command_kind` | `required_next_gate` | `required_operator_action` |
| --- | --- | --- |
| `create_task_execution_package` | `scheduler_proposal` | `create_scheduler_proposal` |
| `queued_task_handoff` | `scheduler_proposal_then_confirmation_then_verifier_then_handoff` | `create_scheduler_proposal` |
| `branch_push_review` | `scheduler_proposal_then_confirmation_then_command_specific_confirm` | `create_scheduler_proposal` |
| `draft_pr_review` | `scheduler_proposal_then_confirmation_then_command_specific_confirm` | `create_scheduler_proposal` |
| `pr_handoff_package` | `scheduler_proposal_then_confirmation_then_command_specific_confirm` | `create_scheduler_proposal` |
| `cleanup_continue` | `scheduler_proposal_then_confirmation_then_command_specific_confirm` | `create_scheduler_proposal` |
| `post_merge_cleanup_review` | `scheduler_proposal_then_confirmation_then_command_specific_confirm` | `create_scheduler_proposal` |
| `inspect_blocker` | `human_inspection` | `inspect_manually` |
| `inspect_evidence` | `human_inspection` | `inspect_manually` |
| `no_action` | `none` | `none` |
| `unknown` | `manual_triage` | `inspect_manually` |
| `human_pr_review` | `human_github_review` | `none` |

### Not-ready kinds

```text
NOT_READY_KINDS = {"unknown", "human_pr_review"}
NO_ACTION_KINDS = {"no_action"}
```

By default, candidates whose kind is in `NOT_READY_KINDS` or
`NO_ACTION_KINDS` are excluded from the listing. They are surfaced only
when the operator explicitly opts in:

- `--include-not-ready` re-includes `unknown` and `human_pr_review` (and,
  as a superset, also `no_action`).
- `--include-no-action` re-includes `no_action` only.

When included, their `candidate_ready` remains `false` and their
`required_next_gate` reflects that they are not scheduler-actionable.

## 6. Example Command

```bash
PYTHONPATH=. .venv/bin/python3 scripts/discover_scheduler_candidates.py --pretty
```

Useful filters:

```bash
# Only one task
PYTHONPATH=. .venv/bin/python3 scripts/discover_scheduler_candidates.py \
  --pretty --task-key AT-GH-101

# Only a single project
PYTHONPATH=. .venv/bin/python3 scripts/discover_scheduler_candidates.py \
  --pretty --project agent-taskflow

# Include otherwise-excluded categories
PYTHONPATH=. .venv/bin/python3 scripts/discover_scheduler_candidates.py \
  --pretty --include-not-ready
```

Default output is compact JSON. `--pretty` emits indented JSON for
operators. Both forms include the `safety` block.

## 7. Example Output

```json
{
  "ok": true,
  "schema_version": "scheduler_candidate_discovery.v1",
  "mode": "read_only",
  "discovery_note": "Candidate discovery is read-only and is NOT execution permission. Human/operator confirmation remains required; validation_result remains authoritative; Mission Control remains read-only.",
  "db_path": "/home/operator/.agent-taskflow/state.db",
  "filters": {
    "status": null,
    "project": null,
    "task_key": null,
    "include_not_ready": false,
    "include_no_action": false,
    "limit": null
  },
  "candidate_count": 1,
  "candidates": [
    {
      "task_key": "AT-GH-101",
      "project": "agent-taskflow",
      "title": "Document Phase G discovery layer",
      "status": "queued",
      "current_phase_label": "queued_handoff_ready",
      "recommended_command_kind": "queued_task_handoff",
      "recommended_next_action": "Run queued-task handoff.",
      "candidate_ready": true,
      "required_next_gate": "scheduler_proposal_then_confirmation_then_verifier_then_handoff",
      "required_operator_action": "create_scheduler_proposal",
      "missing_evidence": [
        "executor_finished_ok",
        "validators_all_passed",
        "pr_handoff_package",
        "branch_push_completed",
        "draft_pr_verified",
        "pr_merged",
        "local_cleanup_completed",
        "remote_branch_cleanup_completed",
        "task_closeout_completed"
      ],
      "consistency_warnings": [],
      "related_artifacts": [
        {
          "artifact_type": "task_execution_package",
          "path": "/home/operator/agent-taskflow/artifacts/AT-GH-101/task_execution_package.json",
          "created_at": "2026-05-25T00:00:00Z"
        }
      ],
      "severity": "medium",
      "confidence": "high",
      "reason": "Task is queued and a Task Execution Package is present.",
      "safety": {
        "read_only": true,
        "proposal_created": false,
        "confirmation_created": false,
        "handoff_created": false,
        "runtime_started": false,
        "approved_task_runner_called": false,
        "github_mutated": false,
        "approved": false,
        "merged": false,
        "cleanup_performed": false,
        "background_worker_started": false
      }
    }
  ],
  "summary": {
    "candidate_count": 1,
    "candidate_ready_count": 1,
    "warning_count": 0,
    "recommended_command_kind_counts": {"queued_task_handoff": 1},
    "execution_allowed": false,
    "requires_human_review": true
  },
  "safety": {
    "read_only": true,
    "db_written": false,
    "artifact_written": false,
    "proposal_created": false,
    "confirmation_created": false,
    "handoff_created": false,
    "verifier_report_created": false,
    "runtime_started": false,
    "approved_task_runner_called": false,
    "github_mutated": false,
    "approved": false,
    "merged": false,
    "cleanup_performed": false,
    "background_worker_started": false,
    "task_status_changed": false,
    "scheduler_loop_started": false
  }
}
```

## 8. Relationship to Phase F

The Phase F readiness checkpoint
(`docs/semi-automatic-scheduler-readiness-checkpoint.md`) concluded that the
repo is ready for Level 1–4 explicit-command semi-automation and **not**
ready for Level 5 background / daemon scheduling. Its
"Recommended Next Implementation Phase" (§8) was:

> **Phase G — Read-only scheduler candidate discovery.**

Phase G implements that recommendation exactly, and only that. It does not
take any step toward Levels 2–5. Subsequent phases — if they are scoped at
all — would re-cross the proposal/confirmation/handoff/runtime gates that
Phases A–E already proved, and each would inherit those gates explicitly.

## 9. Safety Boundary

These invariants apply to this discovery layer and are asserted by tests:

- candidate discovery is **not** execution permission
- discovery is read-only by default and has no non-read-only mode
- no DB rows are written (no `tasks`, `task_events`, `task_artifacts`, or
  `task_worktrees` mutation)
- no artifact files are written to disk
- no scheduler proposal, confirmation, verifier report, or
  `intake_runner_handoff` artifact is ever produced by this layer
- no runtime audit events are emitted
- `approved_task_runner` is never invoked from this layer
- no GitHub mutation: no `gh` calls that push, comment, create PRs,
  merge, or delete branches
- no scheduler loop, cron job, webhook, or background worker
- no Mission Control action affordance
- human review remains final; `validation_result` remains authoritative

## 10. Out of Scope for Phase G

Phase G explicitly does not include:

- API endpoint (any future read-only endpoint would be its own phase)
- Mission Control UI surface (Mission Control remains read-only with no
  action affordances)
- automatic task picking, batching, or queueing for execution
- any background scheduler behavior
- any approval, merge, or cleanup automation

Those are deliberately deferred. Their gates are spelled out in
`docs/semi-automatic-scheduler-readiness-checkpoint.md` §6–§7.
