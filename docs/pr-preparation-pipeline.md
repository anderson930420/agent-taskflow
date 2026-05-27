# PR Preparation Pipeline

## Purpose

Level 7C adds explicit PR preparation automation after `waiting_approval`.
It converts one completed runtime result into local PR handoff evidence, a
pushed task branch, and a GitHub draft PR, then stops for human review.

The pipeline prepares reviewable draft PR evidence only. It does not approve,
merge, clean up, or mark the task finally complete.

## What It Exercises

- task status preflight for `waiting_approval`
- runtime evidence preflight, including `runtime_handoff_execution` and
  `runtime_execution_finished`
- PR handoff evidence creation
- branch push through the existing explicit branch-push helper
- draft PR creation through the existing explicit draft-PR helper
- fake mutation smoke coverage for branch push and draft PR creation

## Confirmation Flags

All GitHub mutation confirmations are required for a real branch push and
draft PR:

- `--confirm-prepare-pr`
- `--confirm-github-mutations`
- `--confirm-branch-push`
- `--confirm-draft-pr`

With no confirmation flags, the command runs as a dry-run and writes nothing.
With only some confirmation flags, the command fails before local handoff
evidence or GitHub mutation helpers are run.

## What It Does Not Do

- no GitHub Issue ingest
- no runtime execution
- no approved_task_runner
- no executor
- no validators
- no automatic task discovery
- no automatic task picking
- no scheduler loop
- no background worker
- no approval
- no merge
- no cleanup
- no Mission Control action UI
- no API endpoint

## Commands

Dry-run:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_pr_preparation_pipeline.py \
  --task-key AT-EXAMPLE \
  --db-path /absolute/path/to/state.db \
  --artifact-root /absolute/path/to/artifacts
```

Confirmed with GitHub mutations:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_pr_preparation_pipeline.py \
  --task-key AT-EXAMPLE \
  --db-path /absolute/path/to/state.db \
  --artifact-root /absolute/path/to/artifacts \
  --confirm-prepare-pr \
  --confirm-github-mutations \
  --confirm-branch-push \
  --confirm-draft-pr
```

Smoke:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_pr_preparation_pipeline_smoke.py
```

## Safety Boundary

- one `task_key` per invocation
- explicit operator-triggered
- all GitHub mutation confirmations required
- branch push and draft PR are allowed only with explicit flags
- dry-run writes nothing and performs no GitHub mutation
- draft PR is not approval
- draft PR is not merge
- draft PR is not cleanup
- branch push is not approval
- branch push is not merge
- branch push is not cleanup
- human final review remains required
- no scheduler loop
- no background worker
- no cron, webhook, or polling
- no automatic task picking
