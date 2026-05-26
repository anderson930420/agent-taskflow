# Scheduler Confirmation Verifier Report Hardening Smoke

## Purpose

The Level 4A minimal verifier report path proves:

```text
scheduler_confirmation -> verifier binding check -> explicit verifier report
```

It exercises the minimum local path from existing confirmation evidence to a
`scheduler_confirmation_verifier_report` artifact/event. It is not handoff
creation and not runtime execution.

## What It Exercises

- An isolated SQLite DB under an isolated workspace.
- An isolated artifact root under that workspace.
- One seeded queued task.
- Scheduler proposal creation.
- Scheduler confirmation creation.
- Read-only verifier binding check.
- Explicit verifier report creation.
- Forbidden side-effect checks.

## What It Does Not Do

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
PYTHONPATH=. .venv/bin/python3 scripts/run_scheduler_confirmation_verifier_report_hardening_smoke.py
```

Optional keep-workspace command:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_scheduler_confirmation_verifier_report_hardening_smoke.py --keep-workspace
```

To inspect a specific isolated workspace:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_scheduler_confirmation_verifier_report_hardening_smoke.py --workspace-root /tmp/agent-taskflow-l4a-smoke --keep-workspace
```

## Expected Summary Shape

The script prints JSON with this shape:

```json
{
  "ok": true,
  "task_key": "AT-L4A-VERIFIER-REPORT-SMOKE",
  "proposal": {
    "proposal_id": "...",
    "proposal_hash": "...",
    "proposal_item_id": "...",
    "item_hash": "...",
    "recommended_command_kind": "...",
    "artifact_path": "..."
  },
  "confirmation": {
    "confirmation_id": "...",
    "proposal_hash": "...",
    "proposal_item_id": "...",
    "item_hash": "...",
    "recommended_command_kind": "...",
    "artifact_path": "..."
  },
  "verifier_report": {
    "verifier_report_id": "...",
    "confirmation_id": "...",
    "proposal_hash": "...",
    "proposal_item_id": "...",
    "item_hash": "...",
    "recommended_command_kind": "...",
    "artifact_path": "..."
  },
  "binding": {
    "verification_passed": true,
    "reasons": [],
    "warning_count": 0
  },
  "safety": {
    "proposal_created": true,
    "confirmation_created": true,
    "verifier_report_created": true,
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

This is minimal verifier report path only. It creates isolated local smoke
evidence only: one `scheduler_proposal` artifact/event, one
`scheduler_confirmation` artifact/event, and one
`scheduler_confirmation_verifier_report` artifact/event in the isolated smoke
workspace.

scheduler_confirmation_verifier_report is not execution permission.
scheduler_confirmation_verifier_report is not handoff.
scheduler_confirmation_verifier_report is not runtime execution.
scheduler_confirmation_verifier_report requires next gate.

The verifier report CLI creates `scheduler_confirmation_verifier_report`
artifact/event evidence only when explicitly confirmed with
`--confirm-create-verifier-report`. Level 4A does not add API, Mission Control
UI, runtime, handoff, approved_task_runner, executor, validators, approval,
merge, cleanup, scheduler loop, background worker, automatic task picking, or
GitHub mutation behavior.
