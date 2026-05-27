# Level 7A / 7B: One-Shot Task Pipeline

## Purpose

The Level 7A one-shot task pipeline is the explicit operator-triggered
bridge that walks one already-known `task_key` through the existing
gated chain in a single command:

```
task_key
    -> scheduler_proposal
    -> scheduler_confirmation
    -> scheduler_confirmation_verifier_report
    -> intake_runner_handoff
    -> runtime preflight
    -> approved_task_runner invocation
    -> runtime_handoff_execution audit evidence
```

It is operator-triggered. It is not a background scheduler, not a
worker loop, and not automatic task picking. It processes exactly one
`task_key` per invocation.

Level 7B adds idempotent resume for that same one-shot command. Resume
is still per `task_key`, still operator-triggered, and still one task
per invocation.

## What it exercises

Level 7A composes the existing Level 2 / Level 3 / Level 4A / Level 5A
/ Level 6A helpers without bypassing any of their dry-run defaults,
confirm flags, hash/binding checks, or duplicate detection. A single
confirmed invocation produces the following audit evidence:

- `scheduler_proposal` artifact + `scheduler_proposal_created` event
- `scheduler_confirmation` artifact + `scheduler_confirmation_created`
  event
- `scheduler_confirmation_verifier_report` artifact +
  `scheduler_confirmation_verifier_report_created` event
- `intake_runner_handoff` artifact + `intake_runner_handoff_created`
  event
- runtime preflight (`runtime_preflight_finished` event)
- one `approved_task_runner` invocation through the existing Level 6A
  helper (only with `--confirm-run-one-shot-pipeline`)
- `runtime_handoff_execution` artifact +
  `runtime_execution_started` and `runtime_execution_finished` events

When the injected runner succeeds, the final task status is
`waiting_approval`.

## What it does not do

Level 7A explicitly does not:

- ingest GitHub Issues
- discover or pick tasks automatically
- run a scheduler loop
- run a background worker
- run via cron, webhook, or polling
- push branches
- create draft PRs
- approve, merge, or clean up
- expose Mission Control action UI
- expose any API endpoint
- accept POST / PATCH / DELETE routes
- run multi-task batch execution

## Commands

### Dry-run (default)

```
PYTHONPATH=. .venv/bin/python3 scripts/run_one_shot_task_pipeline.py \
  --task-key AT-EXAMPLE \
  --db-path /absolute/path/to/state.db \
  --artifact-root /absolute/path/to/artifacts
```

Dry-run writes nothing, does not call `approved_task_runner`, and
returns a stable preview JSON describing what each stage would do.

### Confirmed

```
PYTHONPATH=. .venv/bin/python3 scripts/run_one_shot_task_pipeline.py \
  --task-key AT-EXAMPLE \
  --db-path /absolute/path/to/state.db \
  --artifact-root /absolute/path/to/artifacts \
  --confirm-run-one-shot-pipeline
```

Confirmed mode runs the full one-shot chain for exactly one
`task_key`. It may call `approved_task_runner` only after the
proposal, confirmation, verifier report, handoff, and runtime
preflight stages all pass and no duplicate runtime evidence is
detected.

### Confirmed resume

```
PYTHONPATH=. .venv/bin/python3 scripts/run_one_shot_task_pipeline.py \
  --task-key AT-EXAMPLE \
  --db-path /absolute/path/to/state.db \
  --artifact-root /absolute/path/to/artifacts \
  --confirm-run-one-shot-pipeline \
  --resume-existing
```

With `--resume-existing`, Level 7B reuses only valid matching evidence
for the same `task_key`:

- `scheduler_proposal`
- `scheduler_confirmation`
- `scheduler_confirmation_verifier_report`
- `intake_runner_handoff`

If `runtime_handoff_execution` already exists for the matching handoff,
the pipeline returns `already_executed`. It does not call
`approved_task_runner` again, does not create new runtime audit
evidence, and still requires human review.

Invalid, stale, mismatched, ambiguous, or missing evidence fails
clearly at the affected stage. Resume is not task discovery, not
automatic task selection, and not multi-task automation.

### Smoke

```
PYTHONPATH=. .venv/bin/python3 scripts/run_one_shot_task_pipeline_smoke.py
```

The smoke runs the full chain against an isolated temp workspace with a
fake `approved_task_runner` injection, asserts evidence and safety
markers, then runs a second `--resume-existing` scenario and verifies
that the second status is `already_executed`, the runner call count
does not increase, and evidence counts remain unchanged.

## Safety boundary

- One `task_key` per invocation.
- Explicit operator-triggered; no scheduler loop, background worker,
  cron, webhook, or polling.
- Dry-run is the default. Dry-run writes nothing and does not call
  `approved_task_runner`.
- Confirmed mode may call `approved_task_runner` only after every
  prior gate (proposal, confirmation, verifier report, handoff,
  runtime preflight) passes.
- `--resume-existing` reuses only valid matching evidence for the same
  `task_key`.
- Existing `runtime_handoff_execution` is not rerun.
- `approved_task_runner` is not called again for an `already_executed`
  runtime.
- Runtime audit evidence is not approval.
- Runtime audit evidence is not merge.
- Runtime audit evidence is not cleanup.
- Human review remains required after runtime.
- No GitHub Issue ingest.
- No automatic task discovery.
- No automatic task picking.
- No branch push.
- No draft PR creation.
- No approval, merge, or cleanup.
- No Mission Control action UI.
- No API endpoint.
