# Scheduler ExecutionEngine Fallback Hardening (P5-e)

P5-e is the **legacy-vs-engine fallback hardening** stage of the staged
scheduler-to-ExecutionEngine migration plan defined by the P5-a boundary
document (`docs/scheduler-execution-engine-migration-boundary.md`). It hardens
the P5-d opt-in engine path so fallback semantics are explicit,
machine-readable, and impossible to confuse with approval or execution
authority.

> The **legacy scheduler path remains the effective authority**. The engine
> path remains **opt-in and off by default** (`--use-execution-engine`). The
> **active cron is unchanged**, and P5-e makes
> **no cron / deploy / systemd example change** — the active crontab, the cron
> example command lines, the deploy cron examples, and the systemd examples
> are all untouched.

See also:

- `docs/scheduler-execution-engine-migration-boundary.md` — the P5-a migration
  boundary.
- `docs/scheduler-execution-engine-request-builder.md` — the P5-b request
  builder.
- `docs/scheduler-execution-engine-shadow-compare.md` — the P5-c shadow /
  compare layer whose `matched` result this stage classifies.
- `docs/scheduler-execution-engine-opt-in-path.md` — the P5-d opt-in path whose
  `execution_engine` evidence block this stage hardens.

## Relationship to P5-a / P5-b / P5-c / P5-d

- **P5-a** defined the scheduler-to-ExecutionEngine migration boundary.
- **P5-b** added a pure scheduler ExecutionEngine request builder.
- **P5-c** added a pure legacy-vs-engine shadow / compare layer.
- **P5-d** added the explicit `--use-execution-engine` opt-in path, off by
  default, confirmed-mode only, execution-only, runtime evidence only.
- **P5-e** (this stage) adds a pure fallback / readiness classification layer
  around the P5-d `execution_engine` evidence block. It changes no behavior:
  it classifies evidence.

## Purpose

P5-d produces engine evidence; P5-e answers, deterministically and
machine-readably, "may the legacy path rely on this engine candidate, or is
fallback to the legacy scheduler required — and why?" The answer never changes
what actually happens in P5-e: the scheduler tick payload `ok` and `status`
continue to come from the legacy scheduler path, and engine output never
changes the legacy tick decision. The classification exists so a future
migration stage can be driven by explicit, audited readiness criteria instead
of ad-hoc judgment.

## Module and integration

Module: `agent_taskflow/scheduler_execution_engine_fallback.py` — a pure helper
with no filesystem, DB, GitHub, cron, or runtime access:

- `SchedulerExecutionEngineFallbackAssessmentInput` — the legacy tick payload
  paired with the (possibly absent) `execution_engine` evidence block; both
  mappings are copied defensively.
- `assess_scheduler_execution_engine_fallback(input)` — the pure classification
  function.
- `SchedulerExecutionEngineFallbackAssessment` — the frozen result value.
- `scheduler_execution_engine_fallback_assessment_to_json_dict(result)` — the
  JSON-compatible serialization helper.
- Schema: `scheduler_execution_engine_fallback.v1`.

Integration: when (and only when) the P5-d opt-in is enabled, the opt-in helper
(`agent_taskflow/scheduler_execution_engine_opt_in.py`) attaches to every
`execution_engine` evidence block:

- `fallback_assessment` — the JSON form of the assessment;
- `effective_authority: "legacy_scheduler"`;
- `engine_authority: false`;
- `engine_result_accepted_as_authority: false`.

When the opt-in flag is not provided, nothing runs: the default legacy path is
unchanged and no fallback assessment is produced.

## Fallback assessment fields

Every assessment pins the authority semantics by construction:

- `effective_authority` — always `legacy_scheduler`. The actual tick `ok` /
  `status` come from the legacy scheduler path.
- `engine_authority` — always false (`engine_authority=False`).
- `engine_result_accepted_as_authority` — always false
  (`engine_result_accepted_as_authority=False`). The engine result is **never
  approval authority**.
- `fallback_required` — true when the engine candidate cannot be relied on.
- `fallback_reason` — the primary machine-readable reason (or null).
- `fallback_reasons` — every machine-readable reason, in rule order.
- `engine_candidate_usable_for_future_migration` — true only for a clean
  candidate; **usable for future migration is not approval authority** and is
  not execution authority in P5-e.
- `legacy_ok_preserved` / `legacy_status_preserved` — the legacy decision
  fields are present and recorded as the effective authority.
- `publication_boundary_preserved` — the execution-only publication boundary
  held.
- `safety_boundary_preserved` — no unsafe safety marker appeared.
- `summary` — records at minimum the legacy `ok` / `status`, the engine `ok` /
  `status`, and the effective authority.

## Failure classifications

If engine evidence is missing, failed, unsafe, mismatched, or
non-JSON-compatible, fallback is required. The machine-readable reasons:

- `engine_evidence_absent` — no `execution_engine` evidence block exists.
- `engine_not_enabled` — the evidence does not show `enabled=true`.
- `engine_not_executed` — the evidence does not show `executed=true`.
- `engine_not_ok` — the engine result is not ok.
- `engine_failure_status:<status>` — the engine status indicates engine error /
  not executed / blocked / failed (for example
  `engine_failure_status:engine_error`,
  `engine_failure_status:validator_failed`).
- `shadow_compare_missing` / `shadow_compare_mismatch` — see below.
- `engine_safety_block_missing` / `unsafe_engine_safety_marker` — see below.
- `publication_boundary_violation` — see below.
- `legacy_ok_missing` / `legacy_status_missing` — the legacy tick payload does
  not carry the decision fields the legacy authority is recorded from.

## Shadow compare mismatch handling

If the P5-c **shadow compare** result is absent or its `matched` flag is not
true, fallback is required (`shadow_compare_mismatch`). The mismatch count and
the mismatch list are recorded in the assessment summary. A shadow compare
mismatch never changes the legacy tick decision — the legacy payload `ok` /
`status` are preserved unchanged; the mismatch only disqualifies the engine
candidate.

## Unsafe safety marker handling

If the engine evidence `safety` block carries any **unsafe safety marker** set
true, fallback is required (`unsafe_engine_safety_marker`) and
`safety_boundary_preserved` is false. The unsafe markers are:
`approval_authority`, `approved`, `merged`, `github_mutated`, `branch_pushed`,
`draft_pr_created`, `cleanup_performed`, `archived`, `closed_out`,
`branch_deleted`, `worktree_deleted`, `daemon_started`, `webhook_started`,
`background_worker_started`, `scheduler_loop_started`,
`multi_task_batch_started`. A missing safety block is itself a fallback reason
(`engine_safety_block_missing`).

## Publication boundary handling

The opt-in engine path is execution-only by construction. The assessment
verifies the **publication boundary** on the engine request evidence
(`request_summary`, falling back to the request `metadata`):

- `publish_after_execution=False`
- `mode=execution_only`
- `execution_only=True`

Any violation — including an executed engine run whose publication markers
cannot be verified — requires fallback (`publication_boundary_violation`).

## What P5-e will not do

P5-e adds classification only. It adds:

- **no publish / PR publication / branch push / draft PR** behavior;
- **no approval / merge / cleanup / archive / closeout** behavior;
- **no branch deletion / worktree deletion**;
- **no daemon / webhook / background worker / scheduler loop / multi-task
  behavior**.

P5-e does not make the engine path default, does not change active cron, and
does not make the ExecutionEngine the authoritative scheduler executor. A clean
`engine_candidate_usable_for_future_migration=true` candidate is readiness
evidence for a future migration decision — it is **not approval authority** and
not a substitute for review: **deterministic validators and human review gates
remain the validation and approval authority**, exactly as the P5-a boundary
requires.

## Rollback

Rollback remains **removing the opt-in flag**: drop `--use-execution-engine`
and the tick returns to the unchanged legacy path, with no fallback assessment
produced and no other change required.

## Next stage

A future **P5-f** stage will add an **operator rollout runbook**: how a human
operator uses the accumulated fallback assessments to decide whether, when, and
how to widen the engine path. That runbook is future work and is not
implemented by P5-e.
