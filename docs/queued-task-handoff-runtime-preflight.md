# Queued Task Handoff Runtime Preflight Contract

This document is documentation-only. It describes how
`agent_taskflow.queued_task_handoff` (the "queued task handoff
runner") now binds confirmed execution to an Phase A
`intake_runner_handoff` artifact and the verifier report it persists.
No additional runtime, scheduler loop, background worker, or
automation is introduced by this contract.

The agent-taskflow principle still holds:

> Manage work, not agents.

The queued task handoff runner is still one explicit operator command
per one explicit queued task. There is still no auto-pick, no batch
consumption, no polling, and no background process. The Phase B
change documented here strictly *tightens* the preconditions on the
existing confirmed path; it never relaxes them.

## 1. What Phase A persists

`agent_taskflow.intake_runner_handoff.create_intake_runner_handoff()`
in confirmed mode writes two on-disk artifacts:

1. The handoff artifact under
   `artifact_root/intake_runner_handoffs/<handoff_id>/intake_runner_handoff.json`.
2. The verifier report artifact under
   `artifact_root/scheduler_confirmation_verifier_reports/<verifier_run_id>/verifier_report.json`.

The handoff artifact's `verifier_report` block stamps the
`verifier_run_id` and `verifier_report_path` so a downstream runtime
preflight stage can re-open the exact verifier report. Both artifacts
carry explicit safety blocks disclaiming execution permission. The
verifier itself remains dry-run-only and read-only.

## 2. What Phase B adds to the queued task handoff

`run_queued_task_handoff` previously verified the
`task_execution_package` JSON + `implementation_prompt.md` and, under
`--confirm-handoff`, called `approved_task_runner` directly. Phase B
introduces a runtime preflight binding:

* `QueuedTaskHandoffRequest` gains
  `intake_runner_handoff_artifact_path: Path | None`.
* In `--dry-run`, the field is **optional**. When omitted, the
  preview payload's handoff block surfaces:
  ```
  intake_runner_handoff_required_for_confirmed_execution = true
  intake_runner_handoff_artifact_path = null
  intake_runner_handoff_verified      = false
  ```
  so the operator immediately sees that confirmed execution would be
  blocked. When provided, the dry-run path still runs the full Phase
  B preflight against the artifact and the verifier report so the
  operator can preview the result without touching the runner.
* In confirmed mode (`--confirm-handoff`), the field is **mandatory**.
  Construction of `QueuedTaskHandoffRequest` raises `ValueError` if
  it is missing, and the CLI also short-circuits to a structured
  `phase="cli"` blocked payload with a clear error message before any
  runner is reached.

The new `_verify_intake_runner_handoff` helper is the gate.
`approved_task_runner` is **only** reachable in confirmed mode when:

1. The task exists, is `queued`, and has a valid
   `task_execution_package` package and prompt on disk.
2. The handoff artifact path exists, parses as a JSON object, has
   `schema_version="intake_runner_handoff.v1"`, `status="created"`,
   `mode="confirmed"`, the same `task_key` as the request, and
   `recommended_command_kind="queued_task_handoff"`.
3. The handoff's `runner_contract` declares
   `runner_may_start=false`, `execution_allowed=false`,
   `execution_performed=false`, `executor_started=false`,
   `validators_started=false`, `action_evidence_created=false`, and
   `requires_future_runtime_gate=true`.
4. The handoff's `safety` block declares `handoff_only=true`,
   `will_execute=false`, `will_start_background_worker=false`, and
   `will_mutate_github=false`.
5. The handoff's `proposal.proposal_hash`,
   `proposal.proposal_item_id`, `proposal.item_hash`,
   `confirmation.confirmation_artifact_path`,
   `verifier_report.verifier_run_id`, and
   `verifier_report.verifier_report_path` are all non-empty strings.
6. The verifier report at `verifier_report.verifier_report_path`
   exists, parses as a JSON object, has
   `schema_version="scheduler_confirmation_verifier_report_artifact.v1"`,
   and its `verifier_run_id` matches the handoff's.
7. The wrapped `report` object inside the verifier report has
   `status="valid"`, `verification_passed=true`,
   `eligible_for_command_specific_confirm=true`,
   `execution_allowed=false`, `execution_performed=false`,
   `action_evidence_created=false`,
   `task_key == request.task_key`, and
   `recommended_command_kind == "queued_task_handoff"`.
8. The verifier report's `proposal_hash`, `proposal_item_id`,
   `item_hash`, `confirmation_artifact_path`, and `confirmation_id`
   each match the handoff artifact byte-for-byte.
9. The verifier report's `expiration.confirmation_created_at` and
   `expiration.effective_max_age_minutes` are present, the
   re-computed age is non-negative, and the age is still less than
   the effective TTL **at execution time** (not at verification
   time).

If any check fails, the helper returns a blocked result with
`phase="handoff_verification"`, the runner is not called, and the
result payload includes the binding fields that were decoded before
the failure so the operator can diagnose which check rejected the
request.

## 3. Expiration is rechecked at execution time

The verifier report contains its own `expiration` block with an
`expired` flag computed at verification time. Phase B intentionally
does **not** trust that flag. `_handoff_expiration_still_valid`
re-parses `confirmation_created_at` and compares it against
`datetime.now(tz=timezone.utc)` and the `effective_max_age_minutes`
in the report. A confirmation that was valid at verification time can
become stale before the operator runs the queued handoff, and Phase
B's job is to reject it at that moment.

## 4. Why the verifier and the runtime preflight overlap

The verifier and the queued handoff runtime preflight check
overlapping conditions on purpose. The verifier validates at the
moment the operator was deciding whether to consume the
confirmation; the queued handoff validates at the moment the runner
is about to be invoked. There is a window between those two moments
during which TTL drift, artifact removal, or handoff/verifier
mismatch could occur. The overlap is what closes that TOCTOU gap.
The verifier is still authoritative for "is the bound proposal item
eligible for command-specific operator confirmation?"; the queued
handoff is authoritative for "is the bound handoff still safe to act
on right now?"

## 5. Non-goals (still enforced)

Phase B explicitly does **not**:

* Introduce a scheduler loop or any periodic process.
* Introduce a background worker.
* Auto-pick or batch-consume queued tasks.
* Modify `scheduler_confirmation_verifier.py` (the verifier remains
  dry-run-only, read-only, and still writes nothing).
* Treat the `intake_runner_handoff` artifact as execution permission.
* Replace `approved_task_runner`'s own preflight checks. The queued
  handoff's preflight is *additive*: the runner still runs its own
  preflight after the handoff binding has been validated.
* Add any GitHub mutation, approval, rejection, branch deletion,
  worktree deletion, or cleanup.

## 6. Result payload extensions

`QueuedTaskHandoffResult.handoff` now surfaces the binding fields on
every result (preview, blocked, or runner-completed):

```
intake_runner_handoff_required_for_confirmed_execution: bool
intake_runner_handoff_artifact_path: str | null
intake_runner_handoff_verified: bool
verifier_run_id: str | null
verifier_report_path: str | null
proposal_hash: str | null
proposal_item_id: str | null
item_hash: str | null
confirmation_id: str | null
confirmation_artifact_path: str | null
expiration_still_valid: bool | null
```

The existing safety flags
(`approved_task_runner_invoked`, `executor_started`,
`validators_started`, `background_worker_started=false`) are
preserved unchanged.

## 7. CLI surface change

`scripts/run_queued_task_handoff.py` exposes a new
`--intake-runner-handoff-artifact-path` argument:

* In dry-run, omitting it produces a preview that explicitly states
  the handoff path will be required for confirmed execution.
* In confirmed mode, omitting it causes the CLI to short-circuit to a
  structured blocked payload with `phase="cli"` and a non-zero exit
  code; the underlying runner is not reached.
* In confirmed mode with a valid handoff path, the existing runner
  invocation path runs after both package verification and handoff
  verification succeed.
