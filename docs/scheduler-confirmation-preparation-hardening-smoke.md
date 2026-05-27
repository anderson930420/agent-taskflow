# Scheduler Confirmation Preparation Hardening Smoke

## Purpose

The K5 confirmation preparation hardening smoke proves the Level 3 local
preparation path:

```text
proposal → eligibility → explicit confirmation → readback API
```

It exercises scheduler proposal evidence, K1 confirmation eligibility, K2
explicit confirmation creation, K3 readback, and K4 Mission Control read-only
source checks. It is not verifier report creation, not handoff creation, and
not runtime execution.

## What It Exercises

- An isolated SQLite DB under an isolated workspace.
- An isolated artifact root under that workspace.
- One seeded queued task.
- Level 2 scheduler proposal creation through the existing explicit helper.
- K1 scheduler confirmation eligibility and binding checks.
- K2 explicit `scheduler_confirmation` artifact/event creation.
- K3 helper readback.
- K3 FastAPI readback through:
  - `GET /api/scheduler/confirmations?task_key=...`
  - `GET /api/tasks/{task_key}/scheduler-confirmations`
- K4 Mission Control source checks proving confirmation visibility stays
  read-only.

## What It Does Not Do

- no verifier report
- no handoff
- no runtime execution
- no approved_task_runner
- no executor
- no validators
- no GitHub mutation
- no approval / merge / cleanup
- no scheduler loop
- no background worker
- no automatic task picking

## Command

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_scheduler_confirmation_preparation_hardening_smoke.py
```

Optional keep-workspace command:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_scheduler_confirmation_preparation_hardening_smoke.py --keep-workspace
```

To inspect a specific isolated workspace:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_scheduler_confirmation_preparation_hardening_smoke.py --workspace-root /tmp/agent-taskflow-k5-smoke --keep-workspace
```

## Expected Summary Shape

The script prints compact JSON with this shape:

```json
{
  "ok": true,
  "task_key": "AT-K5-CONFIRMATION-SMOKE",
  "db_path": "...",
  "workspace_root": "...",
  "artifact_root": "...",
  "proposal": {
    "proposal_id": "...",
    "proposal_hash": "...",
    "proposal_item_id": "...",
    "item_hash": "...",
    "recommended_command_kind": "...",
    "artifact_path": "..."
  },
  "eligibility": {
    "eligible": true,
    "reasons": [],
    "warning_count": 0
  },
  "confirmation": {
    "confirmation_id": "...",
    "proposal_hash": "...",
    "proposal_item_id": "...",
    "item_hash": "...",
    "recommended_command_kind": "...",
    "artifact_path": "..."
  },
  "readbacks": {
    "helper_count": 1,
    "api_global_count": 1,
    "api_task_count": 1
  },
  "safety": {
    "proposal_created": true,
    "confirmation_created": true,
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
    "not_execution_permission": true,
    "requires_next_gate": true
  },
  "forbidden_side_effect_counts": {
    "artifacts": 0,
    "events": 0,
    "payload_markers": 0
  }
}
```

## Safety Boundary

This is confirmation preparation hardening smoke only. It creates isolated
local smoke evidence only: one `scheduler_proposal` artifact/event and one
`scheduler_confirmation` artifact/event in the isolated smoke workspace.

scheduler_confirmation is not execution permission.
scheduler_confirmation is not verifier report.
scheduler_confirmation is not handoff.
scheduler_confirmation is not runtime execution.
scheduler_confirmation requires next gate.

Mission Control remains read-only. The smoke checks Mission Control source for
read-only confirmation helpers and safety language, but it does not modify
Mission Control behavior and does not add API, UI, runtime, verifier, handoff,
approved_task_runner, executor, validators, approval, merge, cleanup,
scheduler loop, background worker, automatic task picking, or GitHub mutation
behavior.
