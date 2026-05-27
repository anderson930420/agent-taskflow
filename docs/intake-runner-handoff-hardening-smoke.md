# Intake Runner Handoff Hardening Smoke

## Purpose

This smoke covers the Level 5A minimal handoff path:

```text
scheduler_confirmation_verifier_report
-> handoff binding check
-> explicit intake_runner_handoff
```

It proves that an existing verifier report can be re-opened, checked
against its confirmation and proposal bindings, and converted into local
`intake_runner_handoff` evidence only after explicit confirmation.

This is not runtime execution. The approved_task_runner is not called.

## What It Exercises

- isolated DB
- seeded queued task
- proposal creation
- K1 eligibility
- confirmation creation
- verifier report creation
- handoff binding check
- explicit handoff creation
- forbidden side-effect checks

## What It Does Not Do

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
PYTHONPATH=. .venv/bin/python3 scripts/run_intake_runner_handoff_hardening_smoke.py
```

## Expected Summary Shape

The command prints JSON with this shape:

```json
{
  "ok": true,
  "task_key": "AT-L5A-HANDOFF-SMOKE",
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
  "handoff": {
    "handoff_id": "...",
    "verifier_report_id": "...",
    "confirmation_id": "...",
    "proposal_hash": "...",
    "proposal_item_id": "...",
    "item_hash": "...",
    "recommended_command_kind": "...",
    "artifact_path": "..."
  },
  "binding": {
    "handoff_allowed": true,
    "reasons": [],
    "warning_count": 0
  },
  "safety": {
    "proposal_created": true,
    "confirmation_created": true,
    "verifier_report_created": true,
    "handoff_created": true,
    "runtime_started": false,
    "approved_task_runner_called": false,
    "executor_started": false,
    "validators_started": false,
    "github_mutated": false,
    "approved": false,
    "merged": false,
    "cleanup_performed": false,
    "not_execution_permission": true,
    "requires_runtime_preflight": true,
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

intake_runner_handoff is not execution permission.

intake_runner_handoff is not runtime execution.

intake_runner_handoff does not call approved_task_runner.

intake_runner_handoff requires runtime preflight.

intake_runner_handoff requires next gate.
