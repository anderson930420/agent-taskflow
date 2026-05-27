# GitHub Issue One-Task Automation

`scripts/run_github_issue_one_task_automation.py` is a one-shot thin outer
loop. It composes the existing GitHub Issue discovery, issue ingestion, and
confirmed one-task scheduler watcher modules:

```text
discover_github_issues
-> select the first confirmed recommended issue
-> ingest_github_issue
-> run_scheduler_watcher_one_task
-> stop
```

The command processes at most one GitHub Issue and one local task per
invocation. It is intentionally not a daemon, background worker, webhook, cron
job, continuous scheduler, multi-task queue, concurrency framework, claim or
lease mechanism, auto-merge path, auto-approval path, cleanup path, branch
deletion path, or worktree deletion path.

Human review and human merge remain manual final gates. The resulting draft PR
is review evidence, not approval or merge authority.

## Dry Run

Dry-run is the default. In dry-run mode the command calls
`discover_github_issues`, returns the discovered candidates, and reports which
issue would be selected when both `--select-first-issue` and
`--confirm-select-first-issue` are provided.

Dry-run does not call `ingest_github_issue`, does not write SQLite state, does
not call `run_scheduler_watcher_one_task`, does not invoke the approved task
runner, does not push a branch, and does not create a draft PR.

## Confirmed Mode

Confirmed mode starts when any execution confirmation flag is provided. A real
run requires all of these explicit flags:

```bash
--select-first-issue
--confirm-select-first-issue
--confirm-ingest-issue
--confirm-run-watcher-one-task
--confirm-run-one-shot-pipeline
--confirm-prepare-pr
--confirm-github-mutations
--confirm-branch-push
--confirm-draft-pr
```

The first version only supports selecting the first issue from
`recommended_candidates`. If no recommended candidates exist, the command
returns `ok=true` with `status=no_eligible_issues` and performs no ingestion,
watcher run, runner invocation, branch push, or draft PR creation.

After ingestion, the automation calls `run_scheduler_watcher_one_task` with the
ingested `task_key`, `dry_run=False`, `resume_existing=True`,
`resume_pr_preparation=True`, all one-shot and PR confirmation flags enabled,
and `draft=True`. The watcher then stops after that single task.

The request field `recommended_command_kind` defaults to the high-level
`task_to_draft_pr` intent. That value is not passed as a scheduler
recommendation filter because the existing watcher already owns the
task-to-draft-PR pipeline. Specific scheduler recommendation kinds may still be
passed when a narrower downstream filter is needed.

## Reruns

Reruns rely on the duplicate-safe behavior hardened before this step. Once an
issue has been ingested, discovery classifies that issue as `already_ingested`
instead of recommending it again. In select-first mode, a second automation run
with the same issue therefore returns `status=no_eligible_issues` and does not
call ingestion, the watcher, the approved task runner, branch push, or draft PR
creation again.
