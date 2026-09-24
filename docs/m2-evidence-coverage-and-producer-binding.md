# Runner evidence coverage and producer Attempt binding

Level 2 Roadmap §2.2 asks for two things beyond the per-validator summary that
`docs/validation-summary.md` describes: one index of the required evidence per
attempt, and validation evidence that is bound to the Attempt that produced the
work. This document covers both. It describes what the code does today; it does
not claim the milestone's exit gates.

## The evidence coverage index

`agent_taskflow/evidence_coverage.py` collects what a run observed and resolves
it once, when its `ValidationSummaryRecorder` finishes. The result is written
beside the summary, through the same recorder:

```text
<recorded-artifact-root>/validation-runs/<validation-run-uuid>/evidence-coverage.json
```

The summary references it in `evidence_coverage` with `status: published`. The
index has one entry per roadmap evidence kind, always in the roadmap's order:
`validation-summary.json`, `changed-files-audit.json`, `compileall.log`,
`policy-validate.log`, validator-specific logs, `preflight-pr-check.json`,
`executor-launch-spec.json`, `dual-write-consistency.json`.

Each entry carries `applicability` (`applicable`, `not_applicable`, `unknown`),
`status` (`present`, `partial`, `missing`, `not_run`, `not_applicable`,
`unknown`), a reason, the producers it came from, and its references.

### Where a reference comes from

A reference is a path the run itself pointed at. Its `origin` says which:
`validation_summary_recorder` for the recorder's own output,
`reported_by_validator` and `reported_by_executor` for artifacts the run's
components returned, `reported_by_observed_operation` for an operation a seam
recorded, and `discovered_in_artifact_root` for a file found in the artifact
root that this run did not produce. A discovered file is never presented as
this run's work.

Each reference is resolved against the filesystem: existence, regular-file
check, size, SHA-256, and whether it sits inside the declared artifact roots.
The final path component is opened with `O_NOFOLLOW`, so a reference that has
become a symlink is reported unreadable rather than followed somewhere else. An
unreadable reference stays in the index with its error; it is not dropped, and
the entry does not become `present`.

### Readable is not admissible

A reference is **admissible** — evidence this run may claim — only when it is
readable *and* inside `recorder_artifact_root`, the root the recorder itself
enforces. The recorder refuses to snapshot a path outside that root and marks
its own summary incomplete with `evidence_error`; the index records the same
refusal on the reference as `recorder_rejection`, so it cannot present a path
the publication boundary rejected. A seam may declare more than one artifact
root for discovery, but only the recorder's root decides admissibility.

An inadmissible reference keeps its path, origin and digest for diagnosis and
carries `admissible: false` with a `rejection_reason`
(`recorder_rejected:<error>`, `outside_recorder_artifact_root`, or the read
error). Each item reports `admissible_references` and `rejected_references`
counts plus the rejected paths and reasons in its detail, and the index repeats
them all in a top-level `rejected_references` list. Item status follows
admissibility: `present` only when every reference is admissible, `partial` when
some are, `missing` when none are.

### How applicability is decided

Applicability comes from the run's own configuration, never from a fixed
framework assumption:

* `changed-files-audit.json` and `policy-validate.log` are applicable when a
  configured validator reported an artifact with that name.
* `compileall.log` is applicable when a validator reported that log **or** when
  the validator's own resolved command contains a `compileall` token. If the
  command ran and no log was reported, the entry says so rather than passing.
* When no configured validator produces a kind, the entry is `not_applicable`
  and the reason lists the validators this run did configure. That is a
  statement about this run, not a claim that the check was unnecessary.
* `executor-launch-spec.json` is applicable when an executor ran; it is
  `not_applicable` for integration validation, which launches no executor, and
  `not_run` when the executor used a path that publishes no managed launch
  spec.
* `preflight-pr-check.json` is `unknown` unless a seam records an actual
  preflight operation. Nothing in the current pipeline performs one, so nothing
  is written for it. A seam may state applicability explicitly through
  `RunnerEvidenceCollector.note_operation`.
* `dual-write-consistency.json` is required only during a migration window.
  The runner seams observe no migration state, so the entry is `unknown` unless
  an observation exists in the artifact root or a seam supplies one.

`complete` on the index means every **applicable** entry resolved to an
admissible reference; `unresolved` lists the rest. This is separate from the summary's own
`complete` and `passed`, which keep exactly the meaning they had: a coverage
failure is recorded as `evidence_coverage.status: unavailable` with its error
and never converts missing evidence into a pass, but it also does not retrospec-
tively invalidate a validator verdict.

### Validator identity

`validator_config_identity()` records the validator object the run resolved,
immediately before invoking it: the configured name, where that name came from
in the run's configuration, how it was resolved, the implementation chain
(runtime-path proxies first, the real validator last), and the argv when the
resolved object exposes one. In-process validators are recorded as
`in_process_validator_exposes_no_command`; a `command` that raises is recorded
as `unreadable` with the error. Because the identity is captured before the
call, a row that ends in a tool error still names what was about to run.
`run_integration_validators` records the same shape from
`IntegrationValidatorSpec`, including the working directory and timeout, so a
validator that never returns still has its exact command in evidence.

## Producer Attempt binding

Integration runs long after the execution Attempt that produced the tree has
released its runtime claim, and the task's active Attempt pointer may by then
belong to a different run. The producing Attempt is therefore **handed over**,
not looked up.

1. The dispatcher passes the Attempt it claimed at dispatch to
   `handoff_completed_implementation`.
2. The handoff records it on the `integration_queued` event together with the
   queue entry it created: `queue_sequence` (the entry's own autoincrement id),
   `enqueued_at` and `repo`.
3. `resolve_producer_attempt_binding` reads it back for the entry the
   integration run is about to consume, and `integrate_task` passes the result
   to `run_integration_validators`, records it on the `integration_started` and
   `integration_completed` events, and returns it on `IntegrationResult`.

The summary then carries `attempt_id` with `attempt_binding:
producer_handoff` — never `runtime_claim`, because a completed producer is not
a live claim — plus `attempt_binding_provenance` with the full record.

### When there is no producer

The binding is `None` with a `reason_code` whenever the producer cannot be
established. It never falls back to the active pointer, the newest Attempt, or
another entry's producer:

| `reason_code` | when |
| --- | --- |
| `no_queue_entry_for_repo` | manual, watcher or legacy run; nothing handed it off |
| `no_recorded_execution_handoff` | the entry was enqueued by something other than the handoff |
| `queue_event_carries_no_producer_binding` | the watcher's re-integration re-queue, or a record written before this binding existed |
| `queue_entry_not_recorded_by_latest_handoff` | a stale record, or a later queue generation |
| `producer_superseded_by_later_attempt` | a second run finished the Ticket while its entry stayed queued, whether or not that run held an Attempt |
| `handoff_recorded_no_producer_attempt` | the handing-off caller held no Attempt |
| `producer_attempt_task_mismatch` | the recorded Attempt belongs to another task |

The supersession case keeps the queue entry exactly where it is — FIFO order and
the original timestamp are unchanged (§22.1) — and audits an
`integration_producer_superseded` event once per new finisher. The entry then
resolves to no producer, because neither run can honestly be named as the one
that produced what this run integrates.

A later finisher that holds **no** Attempt supersedes in the same way. It cannot
name itself, so the event records `observed_producer_attempt_id: null` with
`observed_producer_binding: none`, and the binding resolves unbound rather than
leaving the earlier Attempt named for work it may not have done. The mirror
case is unaffected: when the first handoff recorded no producer, there is
nothing to supersede and a later bound finisher still resolves to
`handoff_recorded_no_producer_attempt`.

`producer_attempt_task_mismatch` is checked against the Attempt store: the
recorded Attempt's `task_id` must be the Ticket's own. A Ticket with no Attempt
identity cannot own an Attempt, so that is a mismatch too. Only a missing
Attempt row — a database without the Attempt tables, or a producer older than
them — leaves the check `unavailable`, and the recorded handoff then stands on
its own with that fact written down.

## What this does not change

Lifecycle, validator verdicts, queue FIFO and idempotence, the per-repo
integration lock, dry-run defaults and the human review gate are untouched. An
unresolvable producer costs the evidence its Attempt reference and nothing else:
the validators still gate, the Ticket still reaches `needs_review`, and merge
remains a human action on GitHub. Nothing here produces
`preflight-pr-check.json`, finishes the outcome ledger, or completes the M2 exit
gates.

## Review and rollback

Coverage lives in `tests/test_evidence_coverage.py` and
`tests/test_producer_attempt_binding.py`, both driving the real Dispatcher, the
real approved-task runner, the real handoff and real `integrate_task` runs, plus
the existing dispatcher, approved runner, integration validator, queue, handoff
and controller suites. Rollback is reverting the additive module, the recorder's
additive parameters and the caller wiring; existing evidence directories and
recorded events are kept.
