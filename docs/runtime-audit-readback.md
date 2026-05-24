# Runtime Audit Readback (Phase D)

## Purpose

Phase D exposes the runtime audit evidence that Phase C started recording
inside `queued_task_handoff` so an operator can read it back through the
API and Mission Control. This phase adds **observability only**. It does
not change runtime behavior, does not add a scheduler loop, does not add
a background worker, does not approve, merge, reject, retry, or clean up
anything.

## What is exposed

`queued_task_handoff` in its confirmed mode records three runtime audit
event kinds and one runtime audit artifact:

- `runtime_preflight_finished`
- `runtime_execution_started`
- `runtime_execution_finished`
- artifact_type `runtime_handoff_execution` (`runtime_handoff_execution.v1`)

Phase D readback surfaces this evidence through:

- `agent_taskflow.store.TaskMirrorStore.list_runtime_audit_events(task_key)`
- `agent_taskflow.store.TaskMirrorStore.list_runtime_execution_artifacts(task_key)`
- `GET /api/tasks/{task_key}/runtime-audits`
- Mission Control task detail page → **Runtime Audit** section

`runtime_handoff_execution` artifact records also remain visible through
the existing `GET /api/tasks/{task_key}/artifacts` endpoint.

## Authority boundary

Runtime audit evidence is **observation only**.

- Runtime audit events are **not action evidence**. The presence of a
  runtime audit event does not mean a push, draft PR, merge, branch
  cleanup, or worktree cleanup occurred.
- Runtime audit events are **not validation authority**.
  `runtime_execution_finished` reports whether `approved_task_runner`
  returned and what its summary was; it does not assert that validators
  passed. `validation_result` events (surfaced through
  `GET /api/tasks/{task_key}/validations` and **Validation Results** in
  Mission Control) remain the authoritative validator record.
- Runtime audit readback never marks a task approved, merged, validated,
  or ready to merge.

The API response and the Mission Control panel both advertise the
boundary explicitly via the `not_action_evidence` and
`not_validation_authority` flags / labels.

## Mission Control remains read-only

Phase D adds no action endpoint, no POST/PATCH/DELETE route, and no UI
button. The runtime audit panel is rendered as a read-only table next to
the existing read-only task evidence surfaces. No scheduler loop, no
background worker, no automatic task picking, no batch execution, no
GitHub mutation, no branch or worktree cleanup is introduced.

## API shape

`GET /api/tasks/{task_key}/runtime-audits`

```jsonc
{
  "items": [
    {
      "id": 123,
      "task_key": "AT-0008",
      "created_at": "2026-05-24T09:00:00+00:00",
      "source": "queued_task_handoff",
      "message": "Runtime preflight passed for AT-0008",
      "kind": "runtime_preflight_finished",
      "runtime_execution_id": "rte-...",
      "executor": "noop",
      "preflight_passed": true,
      "package_verified": true,
      "intake_runner_handoff_verified": true,
      "expiration_still_valid": true,
      "approved_task_runner_invoked": false,
      "runner_returned": null,
      "runner_ok": null,
      "runner_status": null,
      "runner_phase": null,
      "final_status": null,
      "runner_error": null,
      "verifier_run_id": "vr-...",
      "verifier_report_path": "/path/to/verifier_report.json",
      "intake_runner_handoff_artifact_path": "/path/to/handoff.json",
      "proposal_hash": "...",
      "proposal_item_id": "...",
      "item_hash": "...",
      "confirmation_id": "...",
      "runtime_execution_artifact_path": null,
      "not_action_evidence": true,
      "not_validation_authority": true
    }
  ],
  "count": 1
}
```

Missing optional fields degrade to `null` / `false` rather than crashing,
and older databases without any runtime audit events return an empty
list.

## Intended operator use

The Runtime Audit section helps an operator:

- inspect why runtime preflight was allowed or blocked
- inspect which intake-runner handoff and verifier report were rechecked
- inspect whether `approved_task_runner` was invoked and what summary it
  returned
- correlate the runtime audit binding (proposal_hash / item_hash / TTL
  fields) with the upstream scheduler artifacts

It is **not** a substitute for validator review or operator approval.
Human review remains the final gate.
