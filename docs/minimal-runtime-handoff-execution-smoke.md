# Minimal Runtime Handoff Execution Smoke (Level 6A)

## Purpose

Level 6A is the minimal runtime preflight + `approved_task_runner` path.
It proves that an existing `intake_runner_handoff` artifact/event can be
run through runtime preflight and, only under explicit operator
confirmation, used to invoke `approved_task_runner` exactly once, with
the resulting `runtime_handoff_execution` artifact and runtime audit
events recorded for review.

This path is explicit operator-gated only. There is no scheduler loop,
no background worker, no automatic task picking, no approval, no merge,
no cleanup, and no GitHub mutation outside of what
`approved_task_runner` is itself designed to do.

## What it exercises

The smoke creates an isolated workspace, an isolated SQLite DB, and an
isolated artifact root, then:

- seeds a queued `TaskRecord`
- creates a `scheduler_proposal` artifact/event
- creates a `scheduler_confirmation` artifact/event
- creates a `scheduler_confirmation_verifier_report` artifact/event
- creates an `intake_runner_handoff` artifact/event
- runs `check_runtime_handoff_preflight` against the handoff
- invokes a fake `approved_task_runner` exactly once under explicit
  operator confirmation
- writes a `runtime_handoff_execution` artifact and the three runtime
  audit events: `runtime_preflight_finished`,
  `runtime_execution_started`, `runtime_execution_finished`
- reads runtime audit events back via
  `TaskMirrorStore.list_runtime_audit_events`
- asserts that no forbidden artifacts, events, or payload markers exist

## What it does not do

- no scheduler loop
- no background worker
- no automatic task picking
- no approval, merge, or cleanup
- no GitHub mutation
- no real executor or validator invocation (the fake
  `approved_task_runner` returns a stable payload)

## Command

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_minimal_runtime_handoff_execution_smoke.py
```

## Expected summary shape

```json
{
  "ok": true,
  "task_key": "AT-L6A-RUNTIME-SMOKE",
  "proposal": {"...": "..."},
  "confirmation": {"...": "..."},
  "verifier_report": {"...": "..."},
  "handoff": {"...": "..."},
  "runtime_execution": {
    "runtime_execution_id": "runtime-execution-...",
    "approved_task_runner_called": true,
    "runner_ok": true,
    "artifact_path": "..."
  },
  "preflight": {"preflight_passed": true, "reasons": [], "warning_count": 0},
  "readbacks": {
    "runtime_audit_event_count": 3,
    "runtime_execution_artifact_count": 1
  },
  "safety": {
    "runtime_started": true,
    "approved_task_runner_called": true,
    "executor_started": false,
    "validators_started": false,
    "github_mutated": false,
    "approved": false,
    "merged": false,
    "cleanup_performed": false,
    "background_worker_started": false,
    "scheduler_loop_started": false,
    "automatic_task_picking_started": false,
    "requires_human_review_after_runtime": true
  },
  "forbidden_side_effect_counts": {
    "artifacts": 0,
    "events": 0,
    "payload_markers": 0
  }
}
```

## Safety boundary

- Runtime invocation is explicit operator-gated. The CLI defaults to
  dry-run and never calls `approved_task_runner` without
  `--confirm-run-approved-task-runner`.
- Dry-run does not call `approved_task_runner` and writes no
  `runtime_handoff_execution` artifact or runtime audit events.
- Confirmed mode calls `approved_task_runner` only after
  `check_runtime_handoff_preflight` returns `preflight_passed: true`.
- The `runtime_handoff_execution` artifact and the
  `runtime_preflight_finished`, `runtime_execution_started`, and
  `runtime_execution_finished` events are runtime audit evidence only.
- Runtime audit evidence is not approval, not merge, and not cleanup.
- Human review remains required after runtime. The smoke exists to
  exercise the runtime audit boundary, not to advance a task through
  workflow approval.
