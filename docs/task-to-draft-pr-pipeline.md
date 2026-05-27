# Task to Draft PR Pipeline

## Purpose

Level 7D composes the one-shot task execution pipeline with the PR preparation
pipeline. Given one existing `task_key`, it runs from `task_key` to
`waiting_approval`, prepares PR handoff evidence, pushes the task branch, creates
a draft PR, and stops for human final review.

This is an explicit operator-triggered path. It processes one task per
invocation.

Level 7E adds PR preparation resume composition. The composed pipeline can now
reuse valid PR preparation evidence after a previous successful draft PR run
without rerunning the one-shot runtime, branch push, or draft PR creation.

## What It Exercises

- One-shot task pipeline.
- Optional `resume_existing` reuse of valid one-shot evidence.
- Optional `resume_pr_preparation` reuse of valid PR handoff, branch push, and
  draft PR evidence.
- Runtime execution through `waiting_approval`.
- PR preparation pipeline.
- Branch push.
- Draft PR creation.
- Fake smoke coverage for the approved task runner, branch push, and draft PR
  creation.

## Confirmation Flags

Confirmed mode requires all of these flags:

- `--confirm-run-one-shot-pipeline`
- `--confirm-prepare-pr`
- `--confirm-github-mutations`
- `--confirm-branch-push`
- `--confirm-draft-pr`

Resume flags are intentionally separate:

- `--resume-existing` reuses valid one-shot execution evidence.
- `--resume-pr-preparation` passes resume to PR preparation from the composed
  pipeline.

On rerun after successful draft PR with both flags:

- one-shot runtime is not rerun
- branch push is not repeated
- draft PR is not recreated
- the pipeline returns `draft_pr_already_created`

## What It Does Not Do

- no GitHub Issue ingest
- no automatic task discovery
- no automatic task picking
- no scheduler loop
- no background worker
- no cron/webhook/polling
- no approval
- no merge
- no cleanup
- no task closeout
- no branch deletion
- no worktree deletion
- no Mission Control action UI
- no API endpoint
- no multi-task batch execution

## Commands

Dry-run:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_task_to_draft_pr_pipeline.py \
  --task-key AT-EXAMPLE \
  --db-path /absolute/path/to/state.db \
  --artifact-root /absolute/path/to/artifacts
```

Confirmed:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_task_to_draft_pr_pipeline.py \
  --task-key AT-EXAMPLE \
  --db-path /absolute/path/to/state.db \
  --artifact-root /absolute/path/to/artifacts \
  --resume-existing \
  --resume-pr-preparation \
  --confirm-run-one-shot-pipeline \
  --confirm-prepare-pr \
  --confirm-github-mutations \
  --confirm-branch-push \
  --confirm-draft-pr
```

Smoke:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_task_to_draft_pr_pipeline_smoke.py
```

## Safety Boundary

- one `task_key` per invocation
- explicit operator-triggered
- dry-run writes nothing and performs no mutation
- confirmed mode requires all execution and GitHub mutation confirmations
- `--resume-existing` reuses one-shot evidence only
- `--resume-pr-preparation` passes resume to PR preparation from composed pipeline
- rerun after successful draft PR returns `draft_pr_already_created`
- one-shot runtime is not rerun when valid runtime evidence exists
- branch push is not repeated when valid branch push evidence exists
- draft PR is not recreated when valid draft PR evidence exists
- approved_task_runner may be called only after one-shot gates pass
- branch push and draft PR may happen only after GitHub mutation flags
- draft PR is not approval
- draft PR is not merge
- draft PR is not cleanup
- human final review remains required
- no scheduler loop
- no background worker
- no automatic task picking
- no GitHub Issue ingest
- no automatic task discovery
- no cron/webhook/polling
- no approval / merge / cleanup
- no task closeout
- no branch deletion
- no worktree deletion
- no Mission Control action UI
- no API endpoint
- no multi-task batch execution
