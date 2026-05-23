# Runtime Audit Events for Queued Task Handoff (Phase C)

This document describes the runtime audit boundary introduced by
Phase C in `agent_taskflow.queued_task_handoff`. It is
documentation-only: there is no new scheduler loop, no background
worker, no batch consumption, no auto-pick, no GitHub mutation, no
approval, no merge, and no cleanup. The agent-taskflow principle
still holds:

> Manage work, not agents.

Phase C adds a deterministic, append-only audit trail for the
runtime/queued handoff boundary so that future operators can answer
three questions from local DB events and a single on-disk artifact:

1. Why was this runtime/queued handoff allowed to start?
2. When was `approved_task_runner` invoked?
3. What did the runner return (or raise)?

## 1. What is runtime audit evidence (and what it is not)

Runtime audit evidence is a record of the runtime/queued handoff
boundary itself. It is **observation**, not action:

* `runtime_preflight_finished` does **not** mean the executor
  succeeded or that validators passed. It means the queued handoff
  finished re-checking package + intake_runner_handoff binding at
  execution time.
* `runtime_execution_started` does **not** mean the executor or any
  validator succeeded. It means runtime preflight passed and the
  queued handoff is about to call `approved_task_runner`.
* `runtime_execution_finished` does **not** become a second source
  of validator truth. Validator authority remains
  `approved_task_runner` and the existing `validation_result` event
  path. `runtime_execution_finished` summarizes the runner return
  shape so the audit trail closes, but it carries the
  `not_validation_authority=true` flag explicitly so a reader cannot
  treat it as approval.

Phase A's `intake_runner_handoff` artifact remains the structural
bridge between the verifier and the runtime. Phase B's runtime
preflight remains the TOCTOU defense between verifier and runner.
Phase C only adds **DB audit events + a single runtime audit
artifact** around the existing call to `approved_task_runner`.

## 2. New event types

| Event type                       | Source                          | When                                                                                 |
| -------------------------------- | ------------------------------- | ------------------------------------------------------------------------------------ |
| `runtime_preflight_finished`     | `queued_task_handoff_runtime`   | After package verification + handoff verification, in confirmed mode only.           |
| `runtime_execution_started`      | `queued_task_handoff_runtime`   | After preflight passed, immediately before `approved_task_runner` is invoked.        |
| `runtime_execution_finished`     | `queued_task_handoff_runtime`   | When `approved_task_runner` returns or raises `ApprovedTaskRunnerError`.             |

Every event payload includes the bound `runtime_execution_id`, the
verifier binding (`verifier_run_id`, `verifier_report_path`,
`proposal_hash`, `proposal_item_id`, `item_hash`, `confirmation_id`),
the `intake_runner_handoff_artifact_path`, and `not_action_evidence
= true`. `runtime_execution_started` and
`runtime_execution_finished` additionally carry
`background_worker_started=false`, `approved=false`, `merged=false`,
and `cleanup_performed=false` so that no reader can mistake the
runtime audit boundary for an action.

## 3. New artifact type

In confirmed mode, the queued handoff writes a single
`runtime_handoff_execution` artifact under

```
<artifact_dir>/runtime_handoff_executions/<runtime_execution_id>/runtime_handoff_execution.json
```

where `<runtime_execution_id>` has the form
`runtime-execution-<timestamp>-<6-byte hex>`. The artifact is
recorded as a task artifact of type `runtime_handoff_execution` and
schema version `runtime_handoff_execution.v1`. Its payload mirrors
the runtime event chain:

```jsonc
{
  "schema_version": "runtime_handoff_execution.v1",
  "runtime_execution_id": "runtime-execution-...",
  "created_at": "...",
  "source": "queued_task_handoff_runtime",
  "task_key": "...",
  "executor": "...",
  "dry_run": false,
  "intake_runner_handoff_artifact_path": "...",
  "verifier_run_id": "...",
  "verifier_report_path": "...",
  "proposal_hash": "...",
  "proposal_item_id": "...",
  "item_hash": "...",
  "confirmation_id": "...",
  "confirmation_artifact_path": "...",
  "expiration_still_valid": true,
  "preflight": {
    "passed": true,
    "package_verified": true,
    "intake_runner_handoff_verified": true,
    "expiration_still_valid": true,
    "error": null
  },
  "approved_task_runner": {
    "invoked": true,
    "ok": true,
    "status": "waiting_approval",
    "phase": "waiting_approval",
    "executor_started": true,
    "validators_started": true
  },
  "runner_result_summary": {
    "ok": true,
    "status": "waiting_approval",
    "phase": "waiting_approval",
    "error": null,
    "returned": true
  },
  "safety": {
    "runtime_audit_only": true,
    "not_action_evidence": true,
    "not_validation_authority": true,
    "auto_selected_task": false,
    "batch_execution": false,
    "background_worker_started": false,
    "github_mutated_by_runtime": false,
    "approved": false,
    "rejected": false,
    "merged": false,
    "cleanup_performed": false
  }
}
```

The `runner_result_summary` block summarizes the runner's return
shape. It is not the validator authority; it never duplicates raw
validator results. If validators ran inside
`approved_task_runner`, their authoritative records remain
`validation_result` events written by the runner.

## 4. Behavior by mode

### 4.1 Dry-run

Dry-run is preview-only. Phase C explicitly writes **no** runtime
events and **no** runtime audit artifact in dry-run, regardless of
whether an `intake_runner_handoff_artifact_path` is supplied. The
preview payload can still report
`intake_runner_handoff_verified` and `expiration_still_valid` so
the operator can preview the result, but `runtime` on the result
is `None`.

### 4.2 Confirmed mode with failing handoff preflight

When confirmed mode reaches `_verify_intake_runner_handoff` and
the helper returns an error (bad schema, mismatched hashes,
expired TTL, missing verifier report, etc.):

* The runner is **not** invoked.
* `runtime_preflight_finished` is recorded with
  `preflight_passed=false`.
* `runtime_execution_started` and `runtime_execution_finished`
  are **not** recorded.
* The runtime audit artifact is still written so the operator
  has a single readable record of why the runner was not
  invoked. The artifact's `approved_task_runner.invoked` is
  `false` and its safety block has `background_worker_started=
  false`, `approved=false`, `merged=false`,
  `cleanup_performed=false`.

### 4.3 Confirmed mode where selection or package verification fails

If the task does not exist, has the wrong status, or the
`task_execution_package` is missing/invalid, the queued handoff
blocks **before** the runtime audit boundary. By design,
Phase C writes **no** runtime audit events or artifact in
these cases because there may be no resolvable `artifact_dir` and
no validated task context — writing partial evidence here would
make the audit trail harder to interpret, not easier. The
authoritative record of these failures remains the existing
blocked result + the event log emitted by `_blocked`.

### 4.4 Confirmed mode with successful runner

* `runtime_preflight_finished` (preflight_passed=true) is
  recorded.
* `runtime_execution_started` (approved_task_runner_invoked=
  true) is recorded immediately before
  `approved_task_runner(...)`.
* `runtime_execution_finished` is recorded after the runner
  returns. `runner_returned=true`, `runner_ok` mirrors the
  runner's `ok` flag, `runner_status` and `runner_phase` mirror
  the runner's structured status fields. `not_validation_authority
  =true` and `not_action_evidence=true` are always set.
* The runtime audit artifact is written with the full preflight
  and approved_task_runner summary blocks.
* `QueuedTaskHandoffResult.runtime` exposes
  `runtime_execution_id`, `runtime_execution_artifact_path`,
  and the three `*_event_recorded` flags so callers can locate
  the audit trail without re-reading the DB.

### 4.5 Confirmed mode where the runner raises

When `approved_task_runner` raises `ApprovedTaskRunnerError`:

* `runtime_preflight_finished` and `runtime_execution_started`
  are recorded as in the success path.
* `runtime_execution_finished` is recorded with
  `runner_returned=false`, `runner_ok=false`, and
  `runner_error` set to the exception message.
* The runtime audit artifact is written with
  `approved_task_runner.ok=false` and
  `runner_result_summary.returned=false`.
* The result is blocked at `phase="runner"` with the runner
  error surfaced.

## 5. Non-goals (still enforced)

Phase C explicitly does **not**:

* Introduce a scheduler loop, periodic process, or background
  worker.
* Auto-pick or batch-consume queued tasks.
* Modify `scheduler_confirmation_verifier.py`.
* Replace `approved_task_runner`'s own preflight checks.
* Add a second source of `validation_result` truth.
* Add any GitHub mutation, approval, rejection, branch deletion,
  worktree deletion, or cleanup.

The verifier (`scheduler_confirmation_verifier.py`) remains
dry-run-only and read-only. The intake-to-runner handoff
(`intake_runner_handoff.py`) remains handoff-only and never starts a
runner. `approved_task_runner` remains the single point where the
executor and validators are invoked, and human approval remains the
final gate.

## 6. Verifier / runtime preflight overlap remains intentional

The verifier validates at the moment the operator was deciding
whether to consume the confirmation; the queued handoff runtime
preflight (Phase B) re-validates at the moment the runner is about
to be invoked. Phase C does not change that. The runtime audit
events recorded by Phase C describe **when** preflight ran at
execution time, not what the verifier decided at verification time.
