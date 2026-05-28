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
-> release lock
-> stop
```

If another tick already holds the lock, the wrapper returns a safe no-op
payload with `status=locked` and `ok=true`. This avoids treating normal timer
overlap as a failed service run. In that locked result it does not call
discovery, does not ingest an issue, does not call the watcher, does not run a
task, does not push a branch, and does not create a draft PR.

The default lock path is:

```text
~/.agent-taskflow/github_issue_one_task_scheduler_tick.lock
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

Confirmed mode processes at most one eligible GitHub Issue, runs at most one
local task through the one-task watcher, creates at most one draft PR through
the existing confirmed path, and stops.

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
