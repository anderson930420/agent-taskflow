# Intake-to-Runner Handoff Contract

This document is documentation-only. It defines the contract between
the read-only scheduler confirmation surface and any future runtime
preflight stage. No runtime code, scripts, models, DB schema,
dependencies, Mission Control UI, or test behavior changes as a result
of this document.

The agent-taskflow principle still holds:

> Manage work, not agents.

The intake-to-runner handoff is the structural bridge from the
read-only scheduler confirmation surface (`scheduler_confirmation`
artifact + read-only verifier) to any future runtime preflight gate.
The handoff is itself not a runtime gate, not action evidence, and not
execution permission.

## 1. Surface map

The chain currently in the codebase, before any runtime execution
exists, is:

1. `agent_taskflow/task_recommendations.py` — per-task recommendation
   listing. Pure read.
2. `agent_taskflow/scheduler_proposals.py` — records the
   `scheduler_proposal` artifact / `scheduler_proposal_created` event.
3. `agent_taskflow/scheduler_proposal_review.py` — read-only review of
   one proposal artifact.
4. `agent_taskflow/scheduler_confirmations.py` — records the
   `scheduler_confirmation` artifact /
   `scheduler_confirmation_created` event when an operator selects one
   or more items.
5. `agent_taskflow/scheduler_confirmation_verifier.py` — dry-run-only,
   read-only verifier. Answers "would this exact confirmation item be
   valid to attempt consumption now?" The verifier itself never writes
   an artifact, never records an event, never mutates DB, never
   contacts GitHub, and never starts a worker.
6. `agent_taskflow/intake_runner_handoff.py` — the subject of this
   document. Calls the verifier, and in confirmed mode persists both
   the verifier report and the handoff artifact. The handoff is
   handoff-only; it is not action evidence and not execution
   permission.

No runtime execution module exists yet. A future runtime preflight
stage is the first piece of code that will *read* a handoff artifact
for any purpose other than human review.

## 2. What confirmed-mode persistence writes

Confirmed mode requires both `dry_run=False` and
`confirm_create_handoff=True`. When both are true and the verifier
returns a `STATUS_VALID` report with `verification_passed=true`,
`eligible_for_command_specific_confirm=true`, and
`execution_allowed`/`execution_performed`/`action_evidence_created` all
exactly `false`, the module writes:

- `<artifact_root>/scheduler_confirmation_verifier_reports/<verifier_run_id>/verifier_report.json`
  — the verifier report artifact. Wraps the entire verifier report
  inside an envelope that carries the artifact schema version
  (`scheduler_confirmation_verifier_report_artifact.v1`),
  `verifier_run_id`, `created_at`, `source = intake_runner_handoff`,
  and a safety block whose flags are `dry_run_report_only=true` plus
  every execution-related flag set to `false`.
- `<artifact_root>/intake_runner_handoffs/<handoff_id>/intake_runner_handoff.json`
  — the handoff artifact. Now carries a top-level `verifier_report`
  block with `verifier_run_id`, `verifier_report_path`, the verifier
  artifact `schema_version`, `persisted=true`, the verifier `status`,
  `verification_passed`, `eligible_for_command_specific_confirm`,
  `execution_allowed=false`, `execution_performed=false`,
  `action_evidence_created=false`, and the verifier `expiration`
  block.
- One `intake_runner_handoff` row in `task_artifacts` (the handoff
  itself; the verifier report is discoverable via the handoff's
  `verifier_report_path` field).
- One `intake_runner_handoff_created` row in `task_events`. The event
  payload includes `verifier_run_id`, `verifier_report_path`,
  `verifier_report_artifact_type`, `verifier_report_schema_version`,
  plus the existing handoff identification fields and the existing
  execution disclaimers (`handoff_only=true`,
  `execution_allowed=false`, `execution_performed=false`,
  `executor_started=false`, `validators_started=false`,
  `action_evidence_created=false`,
  `requires_future_runtime_gate=true`).

Confirmed mode does NOT:

- start an executor;
- start any validator;
- push a branch;
- create a PR;
- merge anything;
- approve, reject, or close any task;
- run cleanup of any kind;
- mutate task lifecycle status;
- contact GitHub;
- start a background worker;
- change the verifier itself, which remains dry-run-only and
  read-only;
- modify `queued_task_handoff.py` confirmed mode;
- call `approved_task_runner`;
- treat the verifier report as execution permission.

## 3. What dry-run does

Dry-run is the default and is also entered when `dry_run=False` is set
without `confirm_create_handoff=True` (which raises
`IntakeRunnerHandoffError`). Dry-run never writes anything. Even when
the verifier returns a valid report:

- no verifier report artifact is written;
- no handoff artifact is written;
- no `task_artifacts` row is recorded;
- no `task_events` row is recorded;
- the returned payload carries `verifier_report.verifier_run_id =
  null`, `verifier_report.verifier_report_path = null`, and
  `verifier_report.persisted = false` so the preview cannot be
  mistaken for a persisted handoff.

The same is true for `STATUS_BLOCKED` dry-run outcomes (the verifier
did not pass and the caller is in dry-run mode); the returned payload
is blocked-only and writes nothing. Confirmed mode with a non-valid
verifier raises `IntakeRunnerHandoffError` and writes nothing.

## 4. Why the verifier report artifact exists

Before this binding existed, the handoff artifact stored only a
`verifier_report_summary`. A runtime preflight stage reading that
handoff could only see the handoff's own claim that the verifier had
passed; it had no way to re-open the verifier report that produced the
claim. Trusting the handoff's self-report would mean trusting an
artifact's own statement about whether it should be honored — which
this codebase explicitly rejects.

Persisting the verifier report under a stable
`verifier_run_id` / `verifier_report_path` lets a future runtime
preflight stage do three things the prior summary made impossible:

1. Re-open the verifier report by path and confirm it still exists on
   disk.
2. Re-validate the verifier report content (schema, safety block,
   status, expiration) against the runtime preflight's own rules
   without trusting the handoff artifact.
3. Cross-check the handoff's bound `verifier_run_id` against the
   on-disk verifier report's `verifier_run_id` to catch reference
   tampering.

## 5. Runtime preflight overlap is intentional

Runtime preflight intentionally overlaps with verifier checks. The
verifier validates the handoff at verification time; runtime preflight
validates that the same eligibility still holds at execution time.
This defends against time-of-check/time-of-use drift, including task
status changes, artifact replacement, worktree deletion, TTL
expiration, or proposal/item mismatch caused by newer scheduler
activity.

Concretely, between the moment the verifier produced its valid report
and the moment a future runtime preflight stage runs, any of the
following can change in ways that invalidate the original report:

- the task can move into a status that no longer allows the
  recommended command kind;
- new related artifacts can be recorded that change the bound
  recommendation;
- the worktree referenced by the bound recommendation can be deleted
  or marked missing;
- the confirmation can age past its TTL;
- a newer scheduler activity can introduce a different proposal /
  confirmation that selects the same item under different state;
- the bound proposal artifact on disk can be replaced or removed.

Because every one of those is possible, runtime preflight must
re-validate. It must not trust the handoff artifact merely because the
artifact says the verifier passed.

## 6. Runtime preflight contract (informative)

A future runtime preflight stage (not implemented in this phase) is
expected to:

1. Read the handoff artifact and load its `verifier_report` block.
2. Resolve `verifier_report.verifier_report_path` and confirm the file
   exists on disk under
   `scheduler_confirmation_verifier_reports/<verifier_run_id>/`.
3. Parse the verifier report artifact, confirm its
   `schema_version == scheduler_confirmation_verifier_report_artifact.v1`,
   and confirm its `verifier_run_id` matches the handoff's
   `verifier_run_id`.
4. Re-validate the inner verifier report (`report.status == "valid"`,
   `verification_passed = true`, `execution_allowed = false`,
   `execution_performed = false`, `action_evidence_created = false`).
5. Re-run the verifier (calling
   `verify_scheduler_confirmation_item` again) against the current DB
   and current proposal/confirmation artifacts on disk. The
   re-verification must independently pass; a passing handoff
   artifact alone is never sufficient.
6. Require a separate command-specific `--confirm-*` operator gate
   before any executor, validator, push, PR, merge, approval,
   rejection, or cleanup is attempted.

Runtime preflight is NOT implemented in this phase. Nothing in this
phase calls `approved_task_runner`, starts an executor, starts a
validator, or starts a background worker. The verifier itself remains
dry-run-only and read-only.

## 7. Safety summary

| Surface | Writes verifier report artifact | Writes handoff artifact | Writes DB row | Starts executor | Starts validator | Pushes / merges / mutates GitHub |
| --- | --- | --- | --- | --- | --- | --- |
| Dry-run preview (valid verifier) | No | No | No | No | No | No |
| Dry-run blocked | No | No | No | No | No | No |
| Confirmed (valid verifier) | Yes | Yes | Yes (`intake_runner_handoff` + `intake_runner_handoff_created`) | No | No | No |
| Confirmed (invalid verifier) | No (raises) | No (raises) | No (raises) | No | No | No |

The verifier report artifact is itself a persisted dry-run report. It
is not action evidence, not execution permission, and never authorizes
a runtime to start. A future runtime preflight stage must re-validate
it independently.
