# PR Preparation Pipeline

## Purpose

Level 7C adds explicit PR preparation automation after `waiting_approval`.
It converts one completed runtime result into local PR handoff evidence, a
pushed task branch, and a GitHub draft PR, then stops for human review.

The pipeline prepares reviewable draft PR evidence only. It does not approve,
merge, clean up, or mark the task finally complete.

Level 7E makes the confirmed PR preparation path safe to resume after partial
or complete success. With `--resume-existing`, valid matching PR preparation
evidence is reused instead of repeated.

## What It Exercises

- task status preflight for `waiting_approval`
- runtime evidence preflight, including `runtime_handoff_execution` and
  `runtime_execution_finished`
- PR handoff evidence creation
- branch push through the existing explicit branch-push helper
- draft PR creation through the existing explicit draft-PR helper
- fake mutation smoke coverage for branch push and draft PR creation
- valid PR handoff reuse
- valid branch push evidence reuse
- valid draft PR evidence reuse

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

## Level 7E Resume

`--resume-existing` applies only to PR preparation evidence:

- If valid PR handoff evidence already exists for the `task_key`, branch, base
  branch, and repo, it is reused.
- If valid branch push evidence already exists for the `task_key`, remote,
  branch, and base branch, the branch push is not repeated.
- If valid draft PR evidence already exists for the `task_key`, branch, base
  branch, repo, PR URL, and PR number, the draft PR is not recreated.
- If an existing draft PR is safely identified by the draft-PR helper, the
  pipeline reports `draft_pr_already_created`.
- Invalid, stale, ambiguous, mismatched, malformed, or missing evidence fails
  clearly before a duplicate draft PR can be created.
- existing draft PR is not recreated.
- No approval, merge, cleanup, task closeout, branch deletion, or worktree
  deletion is performed.
- Human final review remains required.

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
  --resume-existing \
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
- `--resume-existing` reuses only valid matching PR preparation evidence
- branch push is not repeated when valid branch push evidence exists
- draft PR creation is not repeated when valid draft PR evidence exists
- invalid/stale/ambiguous evidence fails clearly
- no duplicate draft PR
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
