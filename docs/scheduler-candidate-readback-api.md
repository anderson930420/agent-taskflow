# Scheduler Candidate Readback API (Phase H)

## 1. Purpose

Phase H exposes the Phase G **read-only scheduler candidate discovery** layer
through the Mission Control HTTP API. It is the API readback layer for the
discovery module added in `docs/read-only-scheduler-candidate-discovery.md`
and is strictly **Level 1 — read-only discovery**, not a proposal generator,
not a handoff preparer, not a runtime executor, and not a background
scheduler.

> Candidate readback is **not** execution permission.
>
> Being returned by these endpoints is review material only. Human/operator
> confirmation remains required, `validation_result` remains authoritative,
> and Mission Control remains read-only. Phase H ships **no** new Mission
> Control UI.

## 2. Endpoints

Phase H adds two GET-only API endpoints:

- `GET /api/scheduler/candidates` — list all scheduler candidates from the
  live local mirror.
- `GET /api/tasks/{task_key}/scheduler-candidate` — return the scheduler
  candidate classification for one specific task. Returns `404` when the
  task is unknown, matching existing `/api/tasks/{task_key}/...` behavior.

Both endpoints are read-only. There is no `POST`, `PATCH`, `PUT`, or
`DELETE` route for either path in this phase. There is no scheduler
proposal endpoint, no scheduler confirmation endpoint, no handoff
endpoint, no runtime execution endpoint, and no approval / merge /
cleanup endpoint introduced by this phase.

## 3. Query Parameters

`GET /api/scheduler/candidates` accepts the same filters that the Phase G
`SchedulerCandidateDiscoveryRequest` understands:

| Parameter            | Type      | Default | Description                                                                                    |
|----------------------|-----------|---------|------------------------------------------------------------------------------------------------|
| `task_key`           | string    | `null`  | Filter to a single task_key. The mirror is read; no task is created.                           |
| `project`            | string    | `null`  | Filter to a single project.                                                                    |
| `status`             | string    | `null`  | Filter to a single task status (e.g. `queued`, `blocked`, `waiting_approval`, `completed`).    |
| `include_not_ready`  | bool      | `false` | Include `human_pr_review` / unknown candidates whose required gate is human-only.              |
| `include_no_action`  | bool      | `false` | Include `no_action` candidates (typically completed tasks).                                    |
| `limit`              | int ≥ 0   | `null`  | Cap the number of candidates returned.                                                         |
| `completed_limit`    | int ≥ 0   | `20`    | Underlying recommendations cap for completed tasks. Mirrors discovery default.                 |

`GET /api/tasks/{task_key}/scheduler-candidate` is a fixed-shape endpoint:
it always calls discovery with `include_not_ready=True`,
`include_no_action=True`, and `limit=1` so the operator can see why a
specific task is or is not currently a ready candidate.

### Error responses

- Invalid query (e.g. unknown `status`, empty `project`, negative `limit`)
  returns `422` — same shape as other API validation errors.
- DB read failure raised as `SchedulerCandidateDiscoveryError` returns
  `500` with `detail`. The endpoint **does not** create the DB if it is
  missing.

## 4. Response Shape

Both endpoints return the same envelope as the Phase G discovery payload,
normalized for JSON safety and with the top-level `safety` block forced
to the locked-down defaults:

```json
{
  "ok": true,
  "mode": "read_only",
  "schema_version": "scheduler_candidate_discovery.v1",
  "discovery_note": "Candidate discovery is read-only and is NOT execution permission. Human/operator confirmation remains required; validation_result remains authoritative; Mission Control remains read-only.",
  "db_path": "/home/.../state.db",
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
      "task_key": "AT-CAND-001",
      "project": "agent-taskflow",
      "title": "Discovery task",
      "status": "queued",
      "current_phase_label": "queued_needs_package",
      "recommended_command_kind": "create_task_execution_package",
      "recommended_next_action": "create_task_execution_package",
      "candidate_ready": true,
      "required_next_gate": "scheduler_proposal",
      "required_operator_action": "create_scheduler_proposal",
      "missing_evidence": [],
      "consistency_warnings": [],
      "related_artifacts": [],
      "severity": "info",
      "confidence": "high",
      "reason": "Task is queued and ready for execution package creation.",
      "blocked_reason": null,
      "discovery_note": "Candidate discovery is read-only and is NOT execution permission. ...",
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
    "recommended_command_kind_counts": {"create_task_execution_package": 1},
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

The serializer deliberately:

- never emits an `execution_allowed` top-level field — `candidate_ready` is
  about discovery eligibility, not execution permission;
- always overwrites `safety` with the locked-down defaults from the
  discovery module, regardless of payload contents;
- always emits a `discovery_note` that says candidate readback is **NOT**
  execution permission.

## 5. Safety Boundary

The Phase H API surface is bound to the same Level 1 read-only contract as
the Phase G discovery module:

- candidate readback is **not** execution permission
- no scheduler proposal is created
- no scheduler confirmation is created
- no `intake_runner_handoff` artifact is created
- no verifier report is created
- no runtime execution is started (no `queued_task_handoff`)
- no `approved_task_runner` call is made
- no executor or validator is invoked
- no `gh` / GitHub mutation (no push, PR create, PR merge, branch / worktree
  deletion)
- no approval, rejection, or cleanup
- no scheduler loop, cron, webhook, or background worker is started
- no DB mutation: no rows added to `tasks`, `task_events`, `task_artifacts`,
  `task_worktrees`, `validation_results`, `executor_runs`, or approval
  decision tables
- no artifact files are written to the artifact directory
- no Mission Control action affordance is exposed in this phase
- Mission Control remains read-only and is **not** modified by this phase

The route definitions are also GET-only:

- `POST /api/scheduler/candidates` → `405 Method Not Allowed`
- `PATCH /api/scheduler/candidates` → `405 Method Not Allowed`
- `PUT /api/scheduler/candidates` → `405 Method Not Allowed`
- `DELETE /api/scheduler/candidates` → `405 Method Not Allowed`
- `POST /api/tasks/{task_key}/scheduler-candidate` → `405 Method Not Allowed`
- `PATCH /api/tasks/{task_key}/scheduler-candidate` → `405 Method Not Allowed`
- `DELETE /api/tasks/{task_key}/scheduler-candidate` → `405 Method Not Allowed`

These guarantees are pinned by tests in `tests/test_api_scheduler_candidates.py`
and `tests/test_api_actions.py`.

## 6. Relationship to Phase G

Phase H is a thin readback wrapper. It:

- imports `SchedulerCandidateDiscoveryRequest` and
  `discover_scheduler_candidates` from
  `agent_taskflow/scheduler_candidate_discovery.py`;
- adds no new candidate classification logic;
- adds no new gate/operator-action mapping;
- adds no DB schema changes;
- adds no new module dependency;
- normalizes the discovery payload via
  `scheduler_candidate_to_dict` /
  `scheduler_candidate_discovery_to_dict` in
  `agent_taskflow/api/schemas.py` to keep the safety block, discovery note,
  and per-candidate safety flags pinned to read-only.

The Phase G CLI (`scripts/discover_scheduler_candidates.py`) remains the
operator-side read-only entry point and is unchanged by Phase H.

## 7. Relationship to Future Mission Control

Phase H does **not** add any Mission Control UI, page, button, type, or
component. A future phase (Phase I) may render the scheduler candidate
readback in Mission Control for review purposes only. Any such
rendering must:

- consume only these GET endpoints;
- continue to advertise the `discovery_note` and `safety` block exactly as
  the API returns them;
- continue to refuse to display `candidate_ready` as "execution allowed";
- preserve Mission Control's read-only contract.

No action affordance — proposal create, confirmation create, handoff
create, runtime start, approve, reject, merge, cleanup — may be added on
top of this readback layer without an explicit follow-up phase that
re-opens governance review.
