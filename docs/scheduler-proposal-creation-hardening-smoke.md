# Scheduler Proposal Creation Hardening Smoke

## Purpose

The J4 hardening smoke proves the operator-gated Level 2 scheduler proposal
creation path without crossing into confirmation or runtime execution.

It creates local proof that a queued task can produce a live scheduler
candidate, that an explicit operator-gated call can record scheduler proposal
evidence, and that the existing read-only readback surfaces can see that
evidence.

## What It Exercises

- Seeds one local queued task in an isolated SQLite store.
- Uses the live scheduler candidate path for that task.
- Creates a proposal through the existing J1
  `create_scheduler_proposal_from_candidate` helper with
  `confirm_create_proposal=True`.
- Verifies the `scheduler_proposal` artifact row and
  `scheduler_proposal_created` event.
- Reads the proposal back through the existing J2 helper.
- Reads the proposal back through the existing J2 API endpoints.
- Checks Mission Control source for read-only proposal visibility helpers and
  safety text.

## What It Does Not Do

- No proposal creation API.
- No Mission Control proposal creation UI.
- No confirmation.
- No verifier report.
- No handoff.
- No runtime execution.
- No `approved_task_runner`.
- No executor.
- No validators.
- No GitHub mutation.
- No approval, merge, or cleanup.
- No scheduler loop.
- No background worker.
- No automatic task picking.

## Command

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_scheduler_proposal_creation_hardening_smoke.py
```

Optional:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_scheduler_proposal_creation_hardening_smoke.py --keep-workspace
```

To keep a specific workspace:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_scheduler_proposal_creation_hardening_smoke.py --workspace-root /tmp/agent-taskflow-j4-smoke
```

## Expected Summary Shape

The script prints JSON with these top-level fields:

```json
{
  "ok": true,
  "task_key": "AT-J4-PROPOSAL-SMOKE",
  "db_path": "...",
  "workspace_root": "...",
  "artifact_root": "...",
  "proposal": {
    "proposal_id": "...",
    "proposal_hash": "...",
    "proposal_item_id": "...",
    "item_hash": "...",
    "recommended_command_kind": "create_task_execution_package",
    "artifact_path": "..."
  },
  "readbacks": {
    "helper_count": 1,
    "api_global_count": 1,
    "api_task_count": 1
  },
  "safety": {
    "proposal_created": true,
    "confirmation_created": false,
    "verifier_report_created": false,
    "handoff_created": false,
    "runtime_started": false,
    "approved_task_runner_called": false,
    "executor_started": false,
    "validators_started": false,
    "github_mutated": false,
    "approved": false,
    "merged": false,
    "cleanup_performed": false,
    "not_execution_permission": true
  },
  "forbidden_side_effect_counts": {
    "artifacts": 0,
    "events": 0,
    "payload_markers": 0
  }
}
```

## Safety Boundary

This is an operator-gated proposal creation hardening smoke only. A scheduler
proposal is not confirmation, and a scheduler proposal is not execution
permission. Human/operator confirmation remains required before any downstream
action.

Mission Control remains read-only for scheduler proposals. The smoke uses the
existing J1 helper, the existing J2 readback API, and the existing J3 read-only
Mission Control source. It does not add a proposal creation API, proposal
creation UI, scheduler loop, background worker, automatic task picking,
confirmation, verifier report, handoff, runtime execution, executor run,
validator run, GitHub mutation, approval, merge, or cleanup.
