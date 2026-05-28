# GitHub Issue One-Task Scheduler Tick

`scripts/run_github_issue_one_task_scheduler_tick.py` is a scheduled,
locked, one-task tick wrapper around the existing one-shot GitHub Issue
automation.

It is intended to be called by cron or a systemd timer. It is not a daemon,
not a scheduler loop, not a webhook, not a background worker, and not a
multi-task queue. Each invocation starts one tick, attempts to acquire the
non-overlap lock, calls the existing one-shot automation at most once, and
stops.

## Execution Shape

```text
acquire non-overlap lock
-> run github_issue_one_task_automation once
-> process at most one GitHub Issue / one task
-> (confirmed default) execute one-shot pipeline to waiting_approval and stop
-> release lock
-> stop
```

The lock is shared with `scripts/run_github_issue_one_task_automation.py`, so
manual and scheduled write-capable runs cannot overlap. If another run already
holds the lock, the wrapper returns a safe no-op payload with `status=locked`
and `ok=true`. This avoids treating normal timer overlap as a failed service
run. In that locked result it does not call discovery, does not ingest an
issue, does not call the watcher, does not run a task, does not push a branch,
and does not create a draft PR.

The default lock path is:

```text
~/.agent-taskflow/github_issue_one_task.lock
```

Use `--lock-path` to point systemd or cron at an operator-managed runtime
location.

## Dry Run

Dry-run is the default. Without `--confirmed`, the tick calls the existing
one-shot automation in dry-run mode and stops.

Dry-run does not ingest a GitHub Issue, does not write SQLite state, does not
call the scheduler watcher, does not invoke the approved task runner, does not
push a branch, and does not create a draft PR. It is safe to run repeatedly.

Example:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_github_issue_one_task_scheduler_tick.py \
  --repo OWNER/REPO \
  --db-path /absolute/path/to/state.db \
  --local-repo-path /absolute/path/to/repo \
  --artifact-root /absolute/path/to/artifacts \
  --json
```

## Confirmed Mode

Confirmed mode is controlled by a single scheduler-level flag:

```text
--confirmed
```

The scheduler tick then passes the lower-level confirmation preset internally
to `run_github_issue_one_task_automation`:

- `dry_run=False`
- `select_first_issue=True`
- `confirm_select_first_issue=True`
- `confirm_ingest_issue=True`
- `confirm_run_watcher_one_task=True`
- `confirm_run_one_shot_pipeline=True`
- `confirm_prepare_pr=True`
- `confirm_github_mutations=True`
- `confirm_branch_push=True`
- `confirm_draft_pr=True`
- `draft=True`

The service or cron entry does not need to list every lower-level confirmation
flag. The scheduler-level `--confirmed` flag is the controlled preset.

Confirmed mode is execution-only by default. It processes at most one eligible
GitHub Issue, ingests it, and runs at most one local task through the one-shot
task pipeline (proposal -> confirmation -> verifier report -> handoff ->
approved runner -> validators) until the task reaches `waiting_approval`, then
stops. By default it does not call the scheduler watcher, does not prepare a
PR, does not push a branch, and does not create a draft PR. PR publication
readiness is a separate concern, so a publication-gate failure can no longer
turn a successful execution into an overall `ok=false` tick.

A successful execution-only tick returns `status=execution_completed` with the
one-shot execution evidence under `automation.execution` and a
`publication.skipped=true` marker. Publication remains the explicit, separate
`scripts/run_task_to_draft_pr_pipeline.py` workflow.

If the first GitHub Issue repeatedly fails ingestion before the local task
mirror is written, the underlying automation records the failure in SQLite and
discovery skips active backoff or quarantined records. A later eligible issue
can still be selected on a later tick. The JSON output includes
`failed_ingestions` in the nested discovery block and
`summary.failed_ingestion_count` at the scheduler, nested automation, and
discovery summary levels.

Example:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_github_issue_one_task_scheduler_tick.py \
  --repo OWNER/REPO \
  --db-path /absolute/path/to/state.db \
  --local-repo-path /absolute/path/to/repo \
  --artifact-root /absolute/path/to/artifacts \
  --lock-path /absolute/path/to/github-issue-one-task.lock \
  --confirmed \
  --json
```

## Executor Profile

By default the scheduled tick records no executor selection on the ingested
task, which preserves the existing noop/default execution behavior. To make a
confirmed tick capable of driving a real executor profile, optional executor
profile metadata can be passed and is threaded down to the mirrored
`TaskRecord` through ingestion:

```text
--model      executor model recorded on the ingested task
--provider   executor provider recorded on the ingested task
--tools      executor tool recorded on the ingested task (repeatable)
--pi-bin     Pi executor binary recorded on the ingested task profile
```

These flags only record profile metadata. The scheduler tick does not add an
executor selection flag, does not run a Claude Code executor, does not add an
AI validator, and does not change any safety gate. The recorded profile is
consumed later by the deterministic approved task runner when a real executor
(such as `opencode` or `pi`) is invoked; `opencode` requires a model, so a
missing model is reported explicitly at runtime.

Example:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_github_issue_one_task_scheduler_tick.py \
  --repo OWNER/REPO \
  --db-path /absolute/path/to/state.db \
  --local-repo-path /absolute/path/to/repo \
  --artifact-root /absolute/path/to/artifacts \
  --lock-path /absolute/path/to/github-issue-one-task.lock \
  --model claude-sonnet-4-6 \
  --provider anthropic \
  --tools read --tools write \
  --confirmed \
  --json
```

## Publication Opt-In

Publication is opt-in and off by default for the scheduler confirmed tick:

```text
--publish-after-execution
```

When this flag is present, the confirmed tick forwards
`publish_after_execution=True` to the automation, which restores the existing
watcher + task-to-draft-PR publication path (prepare PR, push branch, create
draft PR). When the flag is omitted, the tick stays execution-only.

The result includes a `publication_config` block reporting:

- `publish_after_execution` (the requested mode)
- `mode` (`execution_only` or `publication`)
- `next_operator_action` (publication guidance when execution-only)

No merge, approval, or cleanup flag is exposed. Branch push and draft PR
creation only happen through this explicit opt-in or through the separate
`scripts/run_task_to_draft_pr_pipeline.py` workflow.

## Safety Boundary

Human Review and Human Merge remain final gates. The scheduler tick does not
approve work, merge PRs, run cleanup, delete branches, delete worktrees, close
issues, or run multi-task batches.

The result safety block reports:

- `scheduled_tick=True`
- `one_tick_only=True`
- `one_issue_only=True`
- `one_task_only=True`
- lock acquisition and contention state
- `publish_after_execution` (execution-only when `False`)
- nested automation safety flags for discovery, ingestion, watcher, runner,
  branch push, and draft PR creation
- `approved=False`
- `merged=False`
- `cleanup_performed=False`
- `branch_deleted=False`
- `worktree_deleted=False`
- `scheduler_loop_started=False`
- `background_worker_started=False`
- `multi_task_batch_started=False`
- `human_review_required=True`

## Deployment Examples

Example-only files are provided under:

- `deploy/systemd/agent-taskflow-github-issue-one-task.service.example`
- `deploy/systemd/agent-taskflow-github-issue-one-task.timer.example`
- `deploy/cron/github-issue-one-task.cron.example`

They are not active installations. Operators must provide paths, user/group,
logging, environment, and GitHub authentication through their own deployment
mechanism. Do not embed secrets in service or cron files.
